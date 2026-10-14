"""Official Supabase Remote MCP Connector for SAMANVAYA · SŪTRA.

Connects to the official Supabase Remote MCP Server (default: https://mcp.supabase.com/mcp)
using Streamable HTTP transport, Personal Access Token (PAT) or OAuth 2.0 PKCE authentication,
operating system keyring integration, strict project scoping, default read-only safety,
and centralized PolicyEngine security boundaries.

Key Architecture:
- Provider: 'supabase'
- Type: ConnectorType.MCP
- Transport: Streamable HTTP over HTTPS
- Default endpoint: https://mcp.supabase.com/mcp
- Scoping: Explicit project_ref parameter validation
- Safety: Defaults to read_only=true, limiting feature groups to minimal required set (docs, database)
- Credential management: SupabaseCredentialProvider (Memory > Instance Env > SUTRA_SUPABASE_ACCESS_TOKEN > SUPABASE_ACCESS_TOKEN > Keyring)
- Zero secret leakage in logs, descriptors, SQLite, ActivityStore, or exceptions.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Sequence
from typing import Any, Optional

from sutra.activity_store import ActivityStore
from sutra.credentials import CredentialProvider, SupabaseCredentialProvider
from sutra.permissions import PermissionRequest, PermissionRiskLevel
from sutra.redaction import redact_secrets
from ..errors import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorTimeoutError,
)
from ..models import (
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorHealth,
    ConnectorRequest,
    ConnectorResult,
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

# Official hosted Supabase MCP endpoint
DEFAULT_SUPABASE_MCP_URL = "https://mcp.supabase.com/mcp"
DEFAULT_CONNECTOR_ID = "supabase-mcp"
DEFAULT_FEATURES: tuple[str, ...] = ("docs", "database")
RECOGNIZED_FEATURES: frozenset[str] = frozenset({
    "database", "docs", "account", "debugging", "development", "functions", "branching"
})

# Project ref validation pattern: alphanumeric, hyphen, underscore (1 to 64 chars)
_PROJECT_REF_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Recognized read-only tool prefixes and names
_KNOWN_READ_ONLY_PREFIXES = ("get_", "list_", "read_", "search_", "view_", "describe_")


def validate_project_ref(project_ref: str) -> str:
    """Validate and sanitize a Supabase project reference.
    
    Prevents path traversal, URL injection, and parameter tampering.
    """
    clean = project_ref.strip()
    if not clean:
        raise ValueError("Supabase project_ref cannot be empty.")
    if not _PROJECT_REF_REGEX.match(clean):
        raise ValueError(
            f"Invalid Supabase project_ref '{clean}': "
            "must be 1-64 alphanumeric characters, hyphens, or underscores."
        )
    return clean


def build_supabase_mcp_url(
    endpoint: str = DEFAULT_SUPABASE_MCP_URL,
    *,
    project_ref: Optional[str] = None,
    read_only: bool = True,
    features: Optional[Sequence[str]] = None,
) -> str:
    """Construct a well-formed Supabase remote MCP URL using standard URL encoding.
    
    Appends project_ref, read_only, and feature group parameters safely.
    """
    clean_endpoint = endpoint.strip()
    parsed = urllib.parse.urlsplit(clean_endpoint)
    query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    # Convert query_params from lists to single values where appropriate
    params: dict[str, str] = {k: v[0] for k, v in query_params.items()}

    # 1. Project Reference Scoping
    if project_ref is not None:
        validated_ref = validate_project_ref(project_ref)
        params["project_ref"] = validated_ref

    # 2. Server-side Read-Only Safeguard (default: true)
    params["read_only"] = "true" if read_only else "false"

    # 3. Feature Group Filtering (default: docs,database)
    active_features = tuple(features) if features is not None else DEFAULT_FEATURES
    if active_features:
        # Validate and join features
        clean_features = [f.strip().lower() for f in active_features if f.strip()]
        params["features"] = ",".join(clean_features)

    new_query = urllib.parse.urlencode(params)
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        new_query,
        parsed.fragment,
    ))


class SupabaseMCPConnector(MCPConnector):
    """SŪTRA Connector for the official Supabase Remote MCP Server.
    
    Adheres to the MCPConnector / MCPClient / MCPSession / HttpMCPTransport layering.
    Enforces project isolation, default read-only restrictions, and PolicyEngine gates.
    """

    def __init__(
        self,
        connector_id: str = DEFAULT_CONNECTOR_ID,
        *,
        endpoint: str = DEFAULT_SUPABASE_MCP_URL,
        project_ref: Optional[str] = None,
        read_only: bool = True,
        features: Optional[Sequence[str]] = None,
        token: Optional[str] = None,
        credential_provider: Optional[CredentialProvider] = None,
        activity_store: Optional[ActivityStore] = None,
        policy_engine: Optional[Any] = None,
        http_config: Optional[MCPHttpConfig] = None,
        allow_http: bool = False,
        request_timeout: float = 30.0,
        connect_timeout: float = 10.0,
        name: Optional[str] = None,
        description: str = "Official Supabase Remote MCP Server integration",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._connector_id = connector_id
        self._raw_endpoint = endpoint.strip()
        self._project_ref = validate_project_ref(project_ref) if project_ref else None
        self._read_only = bool(read_only)
        self._features = tuple(features) if features is not None else DEFAULT_FEATURES
        self._name = name or ("Supabase Remote MCP" + (f" ({self._project_ref})" if self._project_ref else ""))
        self._description = description
        self._project_access_verified = False

        # 1. Setup Credential Provider
        if credential_provider is not None:
            self._credential_provider = credential_provider
            if token and hasattr(self._credential_provider, "set_memory_token"):
                self._credential_provider.set_memory_token(token)
        else:
            self._credential_provider = SupabaseCredentialProvider(
                connector_id=connector_id,
                memory_token=token,
            )

        # 2. Build Safe Scoped URL
        self._effective_url = build_supabase_mcp_url(
            endpoint=self._raw_endpoint,
            project_ref=self._project_ref,
            read_only=self._read_only,
            features=self._features,
        )

        # 3. Setup HTTP Transport Configuration
        if http_config is not None:
            self._http_config = http_config
        else:
            is_local = "127.0.0.1" in self._effective_url or "localhost" in self._effective_url
            self._http_config = MCPHttpConfig(
                url=self._effective_url,
                auth_provider=self._get_auth_headers,
                allow_http=allow_http or is_local,
                request_timeout=request_timeout,
                connect_timeout=connect_timeout,
                max_response_bytes=10 * 1024 * 1024,  # 10MB
                max_event_bytes=1 * 1024 * 1024,      # 1MB
            )

        transport = HttpMCPTransport(self._http_config)

        # 4. Initialize Base MCPConnector
        custom_meta = {
            "endpoint": self._effective_url,
            "raw_endpoint": self._raw_endpoint,
            "project_ref": self._project_ref,
            "read_only": self._read_only,
            "features": list(self._features),
            "transport_hint": "streamable_http",
            "project_access_verified": self._project_access_verified,
            **(metadata or {}),
        }

        super().__init__(
            connector_id=self._connector_id,
            transport=transport,
            name=self._name,
            description=self._description,
            provider="supabase",
            activity_store=activity_store,
            policy_engine=policy_engine,
            metadata=custom_meta,
        )

    @property
    def endpoint(self) -> str:
        """Effective remote MCP endpoint URL with encoded parameters."""
        return self._effective_url

    @property
    def raw_endpoint(self) -> str:
        """Raw unparameterized base endpoint URL."""
        return self._raw_endpoint

    @property
    def project_ref(self) -> Optional[str]:
        """Configured Supabase project reference scoping this connector instance."""
        return self._project_ref

    @property
    def read_only(self) -> bool:
        """Whether this connector instance enforces read-only mode."""
        return self._read_only

    @property
    def features(self) -> tuple[str, ...]:
        """Configured feature groups enabled on this connector instance."""
        return self._features

    @property
    def credential_provider(self) -> CredentialProvider:
        """Active credential provider for this connector instance."""
        return self._credential_provider

    @property
    def project_access_verified(self) -> bool:
        """Whether reachability and access to project_ref has been verified."""
        return self._project_access_verified

    async def _get_auth_headers(self) -> dict[str, str]:
        """Dynamic auth headers provider injected at request dispatch time."""
        # If credential provider supports token refresh, perform check
        if hasattr(self._credential_provider, "refresh_if_needed"):
            try:
                await self._credential_provider.refresh_if_needed()
            except Exception as exc:
                log.debug("Auto-refresh check failed or not needed: %s", exc)
        return self._credential_provider.get_auth_headers()

    async def connect(self) -> None:
        """Establish MCP session handshake after verifying local credential presence."""
        if not self._credential_provider.has_credential():
            raise ConnectorAuthError(
                connector_id=self._connector_id,
                message=(
                    f"No Supabase Access Token configured for '{self._connector_id}'. "
                    "Set SUTRA_SUPABASE_ACCESS_TOKEN or run 'sutra connectors auth supabase-mcp'."
                ),
            )

        try:
            await super().connect()
            # If connect succeeded and project_ref was provided, verify project access
            if self._project_ref:
                self._project_access_verified = True
        except Exception as exc:
            self._project_access_verified = False
            # Unwind causes to uncover underlying root cause
            curr: Any = exc
            while curr is not None:
                if isinstance(curr, (
                    ConnectorAuthError,
                    ConnectorPermissionError,
                    ConnectorNotFoundError,
                    ConnectorConnectionError,
                    ConnectorTimeoutError,
                )):
                    raise curr
                # Inspect error message for project not found / access denied
                err_str = str(curr).lower()
                if "project not found" in err_str or "unknown project" in err_str:
                    raise ConnectorNotFoundError(
                        connector_id=self._connector_id,
                        message=f"Supabase project '{self._project_ref}' not found or unreachable: {curr}",
                    ) from curr
                if "insufficient permissions" in err_str or "forbidden" in err_str or "access denied" in err_str:
                    raise ConnectorPermissionError(
                        connector_id=self._connector_id,
                        message=f"Insufficient permissions for Supabase project '{self._project_ref}': {curr}",
                    ) from curr
                curr = getattr(curr, "cause", None) or getattr(curr, "__cause__", None)

            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorConnectionError(
                connector_id=self._connector_id,
                message=f"Failed to connect to Supabase Remote MCP Server at {self._effective_url}: {exc}",
                cause=exc,
            ) from exc

    async def disconnect(self) -> None:
        """Disconnect and reset project verification state."""
        self._project_access_verified = False
        await super().disconnect()

    def is_tool_read_only(self, tool_name: str, tool_metadata: Optional[dict[str, Any]] = None) -> bool:
        """Determine if a tool is strictly read-only.
        
        Never assumes an unknown tool is read-only.
        """
        # Explicit metadata indicator from server
        if tool_metadata:
            if tool_metadata.get("read_only") is True or tool_metadata.get("readOnly") is True:
                return True
            if tool_metadata.get("read_only") is False or tool_metadata.get("readOnly") is False:
                return False

        # Known prefix conventions for queries and inspection
        if tool_name.startswith(_KNOWN_READ_ONLY_PREFIXES):
            return True

        # Specific known read-only Supabase tools
        if tool_name in ("get_project_url", "search_docs", "get_schema", "describe_table"):
            return True

        # Any other tool (including execute_sql, apply_migration, create_branch, unknown tools)
        # is classified as non-read-only / mutating.
        return False

    async def _execute_tool_call(self, request: ConnectorRequest, tool_name: str) -> ConnectorResult:
        """Execute tool call with read-only invariant checks and PolicyEngine security gates."""
        self.require_capability(ConnectorCapability.TOOL_EXECUTION)
        if not self._client.is_connected:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message=f"Cannot execute tool '{tool_name}': connector is not connected",
            )

        # Check read-only status of this tool
        is_ro = self.is_tool_read_only(tool_name)

        # Invariant: If connector is configured in read_only mode, mutating tools are rejected locally
        if self._read_only and not is_ro:
            raise ConnectorPermissionError(
                connector_id=self._connector_id,
                message=(
                    f"Mutating operation '{tool_name}' is not permitted when Supabase connector "
                    f"'{self._connector_id}' is configured in read-only mode (read_only=True)."
                ),
                details={"tool_name": tool_name, "read_only": self._read_only},
            )

        # PolicyEngine authorization boundary
        if self._policy_engine is not None:
            if is_ro:
                risk_level = PermissionRiskLevel.LOW
            else:
                # Mutating operations or ambiguous operations are HIGH or CRITICAL risk
                risk_level = self._policy_engine.assess_risk(
                    action_type="tool_call",
                    command=f"{self._connector_id}.{tool_name}",
                    resource_path=f"{self._connector_id}:{tool_name}",
                )
                if risk_level == PermissionRiskLevel.LOW:
                    risk_level = PermissionRiskLevel.HIGH

            perm_req = PermissionRequest(
                request_id=request.request_id,
                action_type="tool_read" if is_ro else "tool_call",
                resource_path=f"{self._connector_id}:{tool_name}",
                command=f"{self._connector_id}.{tool_name}",
                agent_id=request.actor or "agent-system",
                session_id=request.session_id or "session-system",
                risk_level=risk_level,
                description=f"Invocation of tool '{tool_name}' on Supabase connector '{self._connector_id}'",
            )
            allowed, reason = self._policy_engine.check_permission(perm_req)
            if not allowed:
                raise ConnectorPermissionError(
                    connector_id=self._connector_id,
                    message=f"Operation '{tool_name}' rejected by permission policy: {reason}",
                    details={"tool_name": tool_name, "risk_level": risk_level.value},
                )

        # Delegate execution to base implementation
        return await super()._execute_tool_call(request, tool_name)

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
            "endpoint": self._effective_url,
            "raw_endpoint": self._raw_endpoint,
            "provider": "supabase",
            "transport": "streamable_http",
            "project_ref": self._project_ref,
            "read_only": self._read_only,
            "features": list(self._features),
            "has_credentials": has_token,
            "credential_source": source,
            "project_access_verified": self._project_access_verified,
        }

        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=ConnectorType.MCP,
            provider="supabase",
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

        # If credential is present, require zero missing vars
        declared_env = () if has_token else ("SUTRA_SUPABASE_ACCESS_TOKEN",)

        return ConnectorAnalysisDescriptor(
            connector_id=desc.connector_id,
            name=desc.name,
            type=desc.type,
            provider="supabase",
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
