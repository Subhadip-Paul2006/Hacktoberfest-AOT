"""Portable, user-owned credential management for SŪTRA.

Provides a pluggable credential-provider interface, operating-system keyring
storage (Windows Credential Manager, macOS Keychain, Linux Secret Service),
environment-variable resolution with deterministic precedence, and strict
redaction of all secrets in logs, descriptors, and string representations.

Security Invariants:
1. Tokens and secrets are NEVER logged, printed, or persisted in SQLite.
2. Tokens are never silently stored in plaintext files when keyring is unavailable.
3. String and repr representations always mask or redact credential values.
4. Independent connector instances use isolated credential keys to prevent cross-account leakage.
"""
from __future__ import annotations

import abc
import enum
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

from sutra.redaction import redact_secrets

log = logging.getLogger(__name__)

# Keyring service namespace
KEYRING_SERVICE_NAME = "sutra"


class CredentialSource(str, enum.Enum):
    """Deterministic source from which a credential was resolved."""
    MEMORY = "memory"              # Explicit runtime/session parameter
    ENV_PRIMARY = "env_primary"    # SUTRA_GITHUB_TOKEN
    ENV_FALLBACK = "env_fallback"  # GITHUB_PERSONAL_ACCESS_TOKEN
    KEYRING = "keyring"            # Operating system secure keyring
    NONE = "none"                  # No credential present


class CredentialError(Exception):
    """Base exception for credential management operations."""


class KeyringUnavailableError(CredentialError):
    """Raised when an interactive operation requires an OS keyring, but none is available."""


class CredentialStorageError(CredentialError):
    """Raised when an underlying OS keyring read/write/delete operation fails."""


class KeyringBackend:
    """Safe abstraction over system keyring backends.
    
    Provides platform-independent access to Windows Credential Manager,
    macOS Keychain, and Linux Secret Service/Freedesktop secret storage.
    """

    def __init__(self, service_name: str = KEYRING_SERVICE_NAME) -> None:
        self.service_name = service_name

    def is_available(self) -> bool:
        """Check if a usable, non-failing OS keyring backend is active."""
        try:
            import keyring
            backend = keyring.get_keyring()
            if backend is None:
                return False
            # Check for dummy/failing keyring backends
            backend_cls = backend.__class__.__name__.lower()
            if "fail" in backend_cls or "null" in backend_cls or "chainer" in backend_cls:
                # If it's a fail backend with priority 0, it's not usable
                priority = getattr(backend, "priority", 0)
                if priority <= 0:
                    return False
            return True
        except Exception as exc:
            log.debug("Keyring availability check failed: %s", exc)
            return False

    def get_password(self, account_name: str) -> Optional[str]:
        """Retrieve a secret from the OS keyring."""
        try:
            import keyring
            return keyring.get_password(self.service_name, account_name)
        except Exception as exc:
            log.debug("Keyring get_password failed for account '%s': %s", account_name, exc)
            return None

    def set_password(self, account_name: str, password: str) -> None:
        """Store a secret in the OS keyring.
        
        Raises KeyringUnavailableError if no usable keyring backend exists.
        """
        if not self.is_available():
            raise KeyringUnavailableError(
                "Secure OS keyring is not available on this platform. "
                "Configure credentials via SUTRA_GITHUB_TOKEN or use an in-memory session."
            )
        try:
            import keyring
            keyring.set_password(self.service_name, account_name, password)
        except Exception as exc:
            raise CredentialStorageError(
                f"Failed to store credential in OS keyring: {exc}"
            ) from exc

    def delete_password(self, account_name: str) -> bool:
        """Remove a secret from the OS keyring. Returns True if deleted or already absent."""
        if not self.is_available():
            return False
        try:
            import keyring
            try:
                keyring.delete_password(self.service_name, account_name)
                return True
            except keyring.errors.PasswordDeleteError:
                # Password was not present
                return False
        except Exception as exc:
            log.debug("Keyring delete_password failed for account '%s': %s", account_name, exc)
            return False


@dataclass(frozen=True)
class CredentialInfo:
    """Safe, non-sensitive credential summary for diagnostics and CLI status."""
    connector_id: str
    source: CredentialSource
    has_credential: bool
    masked_preview: Optional[str] = None
    service_name: str = KEYRING_SERVICE_NAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "source": self.source.value,
            "has_credential": self.has_credential,
            "masked_preview": self.masked_preview,
            "service_name": self.service_name,
        }


class CredentialProvider(abc.ABC):
    """Abstract base class for pluggable credential providers."""

    @abc.abstractmethod
    def resolve_token(self) -> Optional[str]:
        """Resolve and return raw token, or None if no credential exists."""

    @abc.abstractmethod
    def get_source(self) -> CredentialSource:
        """Return the source from which the active credential was resolved."""

    def has_credential(self) -> bool:
        """Check if a valid credential can be resolved."""
        tok = self.resolve_token()
        return bool(tok and tok.strip())

    def get_auth_headers(self) -> dict[str, str]:
        """Return HTTP headers for authentication (memory-only at dispatch time)."""
        tok = self.resolve_token()
        if tok and tok.strip():
            return {"Authorization": f"Bearer {tok.strip()}"}
        return {}


class GitHubCredentialProvider(CredentialProvider):
    """Portable PAT credential provider for GitHub MCP connectors.
    
    Resolution Precedence:
    1. Explicit in-memory token override (memory session)
    2. Primary environment variable: `SUTRA_GITHUB_TOKEN` (or instance-specific `SUTRA_GITHUB_TOKEN_<ID>`)
    3. Fallback environment variable: `GITHUB_PERSONAL_ACCESS_TOKEN`
    4. Operating System Keyring (`service="sutra"`, `account="github-mcp:<connector_id>"`)
    """

    def __init__(
        self,
        connector_id: str = "github-mcp",
        *,
        memory_token: Optional[str] = None,
        keyring_backend: Optional[KeyringBackend] = None,
        custom_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.connector_id = connector_id
        self._memory_token: Optional[str] = memory_token.strip() if memory_token else None
        self._keyring = keyring_backend or KeyringBackend()
        self._custom_env = custom_env

    @property
    def keyring_account_name(self) -> str:
        """Unique account key in OS keyring preventing cross-account leakage."""
        return f"github-mcp:{self.connector_id}"

    def set_memory_token(self, token: Optional[str]) -> None:
        """Set or clear the active in-memory token override."""
        self._memory_token = token.strip() if token else None

    def store_token(self, token: str) -> None:
        """Store the token in OS keyring for interactive persistence."""
        clean = token.strip()
        if not clean:
            raise ValueError("Token cannot be empty")
        self._keyring.set_password(self.keyring_account_name, clean)
        # Also cache in memory for current process lifecycle
        self._memory_token = clean

    def remove_token(self) -> bool:
        """Remove token from both OS keyring and in-memory cache."""
        self._memory_token = None
        return self._keyring.delete_password(self.keyring_account_name)

    def _get_env_val(self, key: str) -> Optional[str]:
        if self._custom_env is not None:
            return self._custom_env.get(key)
        return os.environ.get(key)

    def resolve_token(self) -> Optional[str]:
        """Resolve token following strict deterministic precedence."""
        # 1. In-memory override
        if self._memory_token:
            return self._memory_token

        # 2. Instance-specific environment variable e.g. SUTRA_GITHUB_TOKEN_GITHUB_MCP_WORK
        inst_env_key = f"SUTRA_GITHUB_TOKEN_{self.connector_id.upper().replace('-', '_')}"
        inst_token = self._get_env_val(inst_env_key)
        if inst_token and inst_token.strip():
            return inst_token.strip()

        # 3. Primary environment variable
        primary_token = self._get_env_val("SUTRA_GITHUB_TOKEN")
        if primary_token and primary_token.strip():
            return primary_token.strip()

        # 4. Fallback environment variable
        fallback_token = self._get_env_val("GITHUB_PERSONAL_ACCESS_TOKEN")
        if fallback_token and fallback_token.strip():
            return fallback_token.strip()

        # 5. OS Keyring
        keyring_token = self._keyring.get_password(self.keyring_account_name)
        if keyring_token and keyring_token.strip():
            return keyring_token.strip()

        return None

    def get_source(self) -> CredentialSource:
        """Determine which source provides the active credential."""
        if self._memory_token:
            return CredentialSource.MEMORY

        inst_env_key = f"SUTRA_GITHUB_TOKEN_{self.connector_id.upper().replace('-', '_')}"
        if self._get_env_val(inst_env_key):
            return CredentialSource.ENV_PRIMARY

        if self._get_env_val("SUTRA_GITHUB_TOKEN"):
            return CredentialSource.ENV_PRIMARY

        if self._get_env_val("GITHUB_PERSONAL_ACCESS_TOKEN"):
            return CredentialSource.ENV_FALLBACK

        if self._keyring.get_password(self.keyring_account_name):
            return CredentialSource.KEYRING

        return CredentialSource.NONE

    def get_info(self) -> CredentialInfo:
        """Return safe diagnostic summary with masked preview."""
        tok = self.resolve_token()
        has_cred = bool(tok and tok.strip())
        preview: Optional[str] = None
        if has_cred and tok:
            if len(tok) >= 8:
                preview = f"{tok[:4]}...{tok[-3:]}"
            else:
                preview = "***"
        return CredentialInfo(
            connector_id=self.connector_id,
            source=self.get_source(),
            has_credential=has_cred,
            masked_preview=preview,
            service_name=self._keyring.service_name,
        )

    def __repr__(self) -> str:
        """Never expose secrets in repr."""
        return f"<GitHubCredentialProvider connector_id='{self.connector_id}' source='{self.get_source().value}'>"

    def __str__(self) -> str:
        return self.__repr__()


class SupabaseCredentialProvider(CredentialProvider):
    """Portable PAT & OAuth credential provider for Supabase MCP connectors.

    Resolution Precedence:
    1. Explicit in-memory token override (memory PAT or memory OAuthToken)
    2. Instance-specific environment variable: `SUTRA_SUPABASE_ACCESS_TOKEN_<ID>`
    3. Primary environment variable: `SUTRA_SUPABASE_ACCESS_TOKEN`
    4. Fallback environment variable: `SUPABASE_ACCESS_TOKEN`
    5. Operating System Keyring (`service="sutra"`, `account="supabase-mcp:<connector_id>"`)
    """

    def __init__(
        self,
        connector_id: str = "supabase-mcp",
        *,
        memory_token: Optional[Union[str, Any]] = None,
        keyring_backend: Optional[KeyringBackend] = None,
        custom_env: Optional[Mapping[str, str]] = None,
        oauth_client: Optional[Any] = None,
    ) -> None:
        self.connector_id = connector_id
        self._memory_token: Optional[Union[str, Any]] = memory_token
        self._keyring = keyring_backend or KeyringBackend()
        self._custom_env = custom_env
        self.oauth_client = oauth_client

    @property
    def keyring_account_name(self) -> str:
        """Unique account key in OS keyring preventing cross-instance/project leakage."""
        return f"supabase-mcp:{self.connector_id}"

    def set_memory_token(self, token: Optional[Union[str, Any]]) -> None:
        """Set or clear the active in-memory token override."""
        self._memory_token = token

    def store_token(self, token: str) -> None:
        """Store a Personal Access Token (PAT) in OS keyring."""
        clean = token.strip()
        if not clean:
            raise ValueError("Token cannot be empty")
        self._keyring.set_password(self.keyring_account_name, clean)
        self._memory_token = clean

    def store_oauth_token(self, oauth_token: Any) -> None:
        """Store an OAuthToken in OS keyring serialized as JSON."""
        if hasattr(oauth_token, "to_json"):
            serialized = oauth_token.to_json()
        elif isinstance(oauth_token, dict):
            serialized = json.dumps(oauth_token)
        else:
            raise ValueError("Invalid OAuth token payload")
        self._keyring.set_password(self.keyring_account_name, serialized)
        self._memory_token = oauth_token

    def remove_token(self) -> bool:
        """Remove credential from both OS keyring and in-memory cache."""
        self._memory_token = None
        return self._keyring.delete_password(self.keyring_account_name)

    def _get_env_val(self, key: str) -> Optional[str]:
        if self._custom_env is not None:
            return self._custom_env.get(key)
        return os.environ.get(key)

    def _extract_access_token(self, val: Any) -> Optional[str]:
        """Safely extract access_token from string, JSON, or OAuthToken object."""
        if val is None:
            return None
        if hasattr(val, "access_token"):
            return str(val.access_token).strip()
        if isinstance(val, dict):
            tok = val.get("access_token")
            return str(tok).strip() if tok else None
        if isinstance(val, str):
            clean = val.strip()
            if clean.startswith("{") and "access_token" in clean:
                try:
                    parsed = json.loads(clean)
                    tok = parsed.get("access_token")
                    return str(tok).strip() if tok else None
                except Exception:
                    pass
            return clean
        return None

    def get_oauth_token(self) -> Optional[Any]:
        """Retrieve the OAuthToken if currently available in memory or keyring."""
        if self._memory_token and hasattr(self._memory_token, "access_token"):
            return self._memory_token

        keyring_val = self._keyring.get_password(self.keyring_account_name)
        if keyring_val and keyring_val.strip().startswith("{"):
            try:
                from sutra.connectors.mcp.oauth import OAuthToken
                return OAuthToken.from_json(keyring_val)
            except Exception:
                pass
        return None

    async def refresh_if_needed(self) -> Optional[str]:
        """Refresh expired OAuth token if an OAuthClient is configured."""
        oauth_token = self.get_oauth_token()
        if oauth_token and hasattr(oauth_token, "is_expired") and oauth_token.is_expired():
            if self.oauth_client and oauth_token.refresh_token:
                try:
                    new_token = await self.oauth_client.refresh_token(oauth_token.refresh_token)
                    self.store_oauth_token(new_token)
                    return new_token.access_token
                except Exception as exc:
                    log.error("Failed to refresh OAuth token for '%s': %s", self.connector_id, exc)
                    raise
        return self.resolve_token()

    def resolve_token(self) -> Optional[str]:
        """Resolve token following strict deterministic precedence."""
        # 1. In-memory override
        if self._memory_token:
            extracted = self._extract_access_token(self._memory_token)
            if extracted:
                return extracted

        # 2. Instance-specific environment variable e.g. SUTRA_SUPABASE_ACCESS_TOKEN_SUPABASE_MCP_STAGING
        inst_env_key = f"SUTRA_SUPABASE_ACCESS_TOKEN_{self.connector_id.upper().replace('-', '_')}"
        inst_token = self._get_env_val(inst_env_key)
        if inst_token and inst_token.strip():
            return inst_token.strip()

        # 3. Primary environment variable
        primary_token = self._get_env_val("SUTRA_SUPABASE_ACCESS_TOKEN")
        if primary_token and primary_token.strip():
            return primary_token.strip()

        # 4. Fallback environment variable
        fallback_token = self._get_env_val("SUPABASE_ACCESS_TOKEN")
        if fallback_token and fallback_token.strip():
            return fallback_token.strip()

        # 5. OS Keyring
        keyring_raw = self._keyring.get_password(self.keyring_account_name)
        if keyring_raw and keyring_raw.strip():
            extracted = self._extract_access_token(keyring_raw)
            if extracted:
                return extracted

        return None

    def get_source(self) -> CredentialSource:
        """Determine which source provides the active credential."""
        if self._memory_token:
            return CredentialSource.MEMORY

        inst_env_key = f"SUTRA_SUPABASE_ACCESS_TOKEN_{self.connector_id.upper().replace('-', '_')}"
        if self._get_env_val(inst_env_key):
            return CredentialSource.ENV_PRIMARY

        if self._get_env_val("SUTRA_SUPABASE_ACCESS_TOKEN"):
            return CredentialSource.ENV_PRIMARY

        if self._get_env_val("SUPABASE_ACCESS_TOKEN"):
            return CredentialSource.ENV_FALLBACK

        if self._keyring.get_password(self.keyring_account_name):
            return CredentialSource.KEYRING

        return CredentialSource.NONE

    def get_info(self) -> CredentialInfo:
        """Return safe diagnostic summary with masked preview."""
        tok = self.resolve_token()
        has_cred = bool(tok and tok.strip())
        preview: Optional[str] = None
        if has_cred and tok:
            if len(tok) >= 8:
                preview = f"{tok[:4]}...{tok[-3:]}"
            else:
                preview = "***"
        return CredentialInfo(
            connector_id=self.connector_id,
            source=self.get_source(),
            has_credential=has_cred,
            masked_preview=preview,
            service_name=self._keyring.service_name,
        )

    def __repr__(self) -> str:
        """Never expose secrets in repr."""
        return f"<SupabaseCredentialProvider connector_id='{self.connector_id}' source='{self.get_source().value}'>"

    def __str__(self) -> str:
        return self.__repr__()

