"""Official GitHub Remote MCP Connector for SAMANVAYA · SŪTRA.

Connects to the official GitHub Remote MCP Server (default: https://api.githubcopilot.com/mcp/)
using Streamable HTTP transport, user-owned Personal Access Token (PAT) authentication,
operating system keyring integration, and centralized PolicyEngine security.

Key Architecture:
- Provider: 'github'
- Transport: Streamable HTTP over HTTPS
- Default endpoint: https://api.githubcopilot.com/mcp/
- Credential management: GitHubCredentialProvider (Memory > SUTRA_GITHUB_TOKEN > GITHUB_PERSONAL_ACCESS_TOKEN > Keyring)
- Zero secret leakage in logs, descriptors, SQLite, ActivityStore, or exceptions.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sutra.activity_store import ActivityStore
from sutra.credentials import CredentialProvider, GitHubCredentialProvider
from sutra.redaction import redact_secrets
from ..errors import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorTimeoutError,
)
from ..models import (
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorHealth,
    ConnectorStatus,
    ConnectorType,
    sanitize_metadata,
)
from .connector import MCPConnector
from .http import HttpMCPTransport, MCPHttpConfig
from .models import (
    MCPClientCapabilities,
    MCPClientInfo,
    MCPProtocolVersion,
)

log = logging.getLogger(__name__)

# Official GitHub Remote MCP Server endpoint
DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
DEFAULT_CONNECTOR_ID = "github-mcp"


class GitHubMCPConnector(MCPConnector):
    """SŪTRA Connector for the official GitHub Remote MCP Server."""

    def __init__(
        self,
        connector_id: str = DEFAULT_CONNECTOR_ID,
        *,
        endpoint: str = DEFAULT_GITHUB_MCP_URL,
        token: Optional[str] = None,
        credential_provider: Optional[CredentialProvider] = None,
        activity_store: Optional[ActivityStore] = None,
        policy_engine: Optional[Any] = None,
        http_config: Optional[MCPHttpConfig] = None,
        allow_http: bool = False,
        request_timeout: float = 30.0,
        connect_timeout: float = 10.0,
        name: Optional[str] = None,
        description: str = "Official GitHub Remote MCP Server integration",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._connector_id = connector_id
        self._endpoint = endpoint.strip()
        self._name = name or "GitHub Remote MCP Server"
        self._description = description

        # 1. Setup Credential Provider
        if credential_provider is not None:
            self._credential_provider = credential_provider
            if token and hasattr(self._credential_provider, "set_memory_token"):
                self._credential_provider.set_memory_token(token)
        else:
            self._credential_provider = GitHubCredentialProvider(
                connector_id=connector_id,
                memory_token=token,
            )

        # 2. Setup HTTP Transport Configuration
        if http_config is not None:
            self._http_config = http_config
        else:
            # Check if endpoint is local development endpoint
            is_local = "127.0.0.1" in self._endpoint or "localhost" in self._endpoint
            self._http_config = MCPHttpConfig(
                url=self._endpoint,
                auth_provider=self._get_auth_headers,
                allow_http=allow_http or is_local,
                request_timeout=request_timeout,
                connect_timeout=connect_timeout,
                max_response_bytes=10 * 1024 * 1024,  # 10MB
                max_event_bytes=1 * 1024 * 1024,      # 1MB
            )

        transport = HttpMCPTransport(self._http_config)

        # 3. Initialize Base MCPConnector
        custom_meta = {
            "endpoint": self._endpoint,
            "transport_hint": "streamable_http",
            **(metadata or {}),
        }

        super().__init__(
            connector_id=self._connector_id,
            transport=transport,
            name=self._name,
            description=self._description,
            provider="github",
            activity_store=activity_store,
            policy_engine=policy_engine,
            metadata=custom_meta,
        )

    @property
    def endpoint(self) -> str:
        """Configured remote MCP endpoint URL."""
        return self._endpoint

    @property
    def credential_provider(self) -> CredentialProvider:
        """Active credential provider for this connector instance."""
        return self._credential_provider

    async def _get_auth_headers(self) -> dict[str, str]:
        """Dynamic auth headers provider injected at request dispatch time."""
        return self._credential_provider.get_auth_headers()

    async def connect(self) -> None:
        """Establish MCP session handshake after verifying local credential presence."""
        if not self._credential_provider.has_credential():
            raise ConnectorAuthError(
                connector_id=self._connector_id,
                message=(
                    f"No GitHub Personal Access Token configured for '{self._connector_id}'. "
                    "Set SUTRA_GITHUB_TOKEN or run 'sutra connectors auth github-mcp'."
                ),
            )

        try:
            await super().connect()
        except Exception as exc:
            # Unwind causes to uncover underlying auth/permission/connection root causes
            curr: Any = exc
            while curr is not None:
                if isinstance(curr, (ConnectorAuthError, ConnectorPermissionError, ConnectorConnectionError, ConnectorTimeoutError)):
                    raise curr
                curr = getattr(curr, "cause", None) or getattr(curr, "__cause__", None)
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorConnectionError(
                connector_id=self._connector_id,
                message=f"Failed to connect to GitHub Remote MCP Server at {self._endpoint}: {exc}",
                cause=exc,
            ) from exc

    def identity(self) -> ConnectorDescriptor:
        """Return immutable descriptor detailing identity, capabilities, and status."""
        desc = super().identity()
        has_token = self._credential_provider.has_credential()
        source = (
            self._credential_provider.get_source().value
            if hasattr(self._credential_provider, "get_source")
            else "unknown"
        )

        meta = {
            **desc.metadata,
            "endpoint": self._endpoint,
            "provider": "github",
            "transport": "streamable_http",
            "has_credentials": has_token,
            "credential_source": source,
        }

        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=ConnectorType.MCP,
            provider="github",
            version="1.0.0",
            description=self._description,
            capabilities=desc.capabilities,
            status=desc.status,
            metadata=meta,
            enabled=desc.enabled,
        )

    def analysis_descriptor(self) -> ConnectorAnalysisDescriptor:
        """Standardized metadata descriptor for Generic Environment & Connector Analyzer."""
        desc = self.identity()
        has_token = self._credential_provider.has_credential()
        source = (
            self._credential_provider.get_source().value
            if hasattr(self._credential_provider, "get_source")
            else "unknown"
        )

        # If credential is already present via Memory, Keyring, or Env, require no missing vars
        declared_env = () if has_token else ("SUTRA_GITHUB_TOKEN",)

        return ConnectorAnalysisDescriptor(
            connector_id=desc.connector_id,
            name=desc.name,
            type=desc.type,
            provider="github",
            transport="streamable_http",
            enabled=desc.enabled,
            status=desc.status,
            capabilities=desc.capabilities,
            version=desc.version,
            description=desc.description,
            declared_env_vars=declared_env,
            declared_binary_names=(),
            metadata=dict(desc.metadata),
        )
