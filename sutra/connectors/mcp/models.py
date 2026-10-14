"""MCP (Model Context Protocol) client domain models for SAMANVAYA · SŪTRA.

Defines protocol versioning, session states, capabilities, tools, resources,
prompts, and pagination in a transport-agnostic, provider-neutral representation.

Invariants:
1. Completely decoupled from transport implementations (stdio, Streamable HTTP, SSE).
2. Server/client identity metadata is strictly informational (never an authorization boundary).
3. Secret-safe: all descriptions, arguments, and metadata are sanitized.
4. Normalizes cleanly to and from SŪTRA Connector models.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Generic, Optional, TypeVar

from sutra.redaction import redact_secrets
from ..models import (
    ConnectorCapability,
    ConnectorResource,
    ConnectorToolDefinition,
    sanitize_metadata,
)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Protocol Versions & Session State
# ---------------------------------------------------------------------------

class MCPProtocolVersion(str, enum.Enum):
    """Supported Model Context Protocol specification versions."""
    V2026_07_28 = "2026-07-28"  # Current stable specification revision
    V2025_03_26 = "2025-03-26"  # Intermediary release
    V2024_11_05 = "2024-11-05"  # Initial public specification release

    @classmethod
    def latest(cls) -> "MCPProtocolVersion":
        """Return the latest supported protocol version."""
        return cls.V2026_07_28

    @classmethod
    def supported_versions(cls) -> list[str]:
        """Return list of supported version strings in descending preference."""
        return [cls.V2026_07_28.value, cls.V2025_03_26.value, cls.V2024_11_05.value]

    @classmethod
    def is_supported(cls, version_str: str) -> bool:
        """Check if a protocol version string is supported."""
        return version_str in cls.supported_versions()

    @classmethod
    def negotiate(cls, requested: str, supported_by_server: Optional[list[str]] = None) -> "MCPProtocolVersion":
        """Negotiate protocol version.
        
        If server reports supported versions, picks highest mutually supported.
        Otherwise validates requested version.
        """
        if supported_by_server:
            for pref in cls.supported_versions():
                if pref in supported_by_server:
                    return cls(pref)
            raise ValueError(
                f"No compatible MCP protocol version. Server offers {supported_by_server}, "
                f"client supports {cls.supported_versions()}"
            )
        
        if cls.is_supported(requested):
            return cls(requested)
        raise ValueError(
            f"Unsupported MCP protocol version '{requested}'. "
            f"Supported versions: {cls.supported_versions()}"
        )


class MCPSessionState(str, enum.Enum):
    """Protocol-level lifecycle states for an MCP client session."""
    DISCONNECTED = "DISCONNECTED"  # Session created or transport closed
    CONNECTING = "CONNECTING"      # Transport opening and protocol initialization in progress
    READY = "READY"                # Protocol handshake complete, ready for requests
    DEGRADED = "DEGRADED"          # Operational with degraded capabilities or high latency
    ERROR = "ERROR"                # Protocol or transport error occurred
    CLOSED = "CLOSED"              # Session explicitly terminated


# ---------------------------------------------------------------------------
# Identity & Capability Declarations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MCPClientInfo:
    """Informational metadata identifying the MCP client."""
    name: str = "sutra-mcp-client"
    version: str = "1.0.0"
    title: Optional[str] = "SAMANVAYA · SŪTRA MCP Client"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title:
            d["title"] = self.title
        return d


@dataclass(frozen=True)
class MCPServerInfo:
    """Informational metadata identifying the remote MCP server.
    
    SECURITY INVARIANT: Server identity is informational only and MUST NOT
    be used as an authorization boundary or trust decision.
    """
    name: str
    version: str = "unknown"
    title: Optional[str] = None
    description: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean)


@dataclass(frozen=True)
class MCPClientCapabilities:
    """Capabilities declared by SŪTRA to the MCP server during initialization."""
    roots: bool = False
    sampling: bool = False
    experimental: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        caps: dict[str, Any] = {}
        if self.roots:
            caps["roots"] = {"listChanged": True}
        if self.sampling:
            caps["sampling"] = {}
        if self.experimental:
            caps["experimental"] = dict(self.experimental)
        return caps


@dataclass(frozen=True)
class MCPServerCapabilities:
    """Capabilities declared by the MCP server during initialization."""
    tools: bool = False
    tools_list_changed: bool = False
    resources: bool = False
    resources_subscribe: bool = False
    resources_list_changed: bool = False
    prompts: bool = False
    prompts_list_changed: bool = False
    logging: bool = False
    experimental: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "MCPServerCapabilities":
        if not data:
            return cls()
        
        tools_cfg = data.get("tools")
        has_tools = tools_cfg is not None
        tools_changed = bool(isinstance(tools_cfg, dict) and tools_cfg.get("listChanged"))

        res_cfg = data.get("resources")
        has_res = res_cfg is not None
        res_sub = bool(isinstance(res_cfg, dict) and res_cfg.get("subscribe"))
        res_changed = bool(isinstance(res_cfg, dict) and res_cfg.get("listChanged"))

        prompts_cfg = data.get("prompts")
        has_prompts = prompts_cfg is not None
        prompts_changed = bool(isinstance(prompts_cfg, dict) and prompts_cfg.get("listChanged"))

        has_logging = data.get("logging") is not None
        exp = data.get("experimental") or {}

        return cls(
            tools=has_tools,
            tools_list_changed=tools_changed,
            resources=has_res,
            resources_subscribe=res_sub,
            resources_list_changed=res_changed,
            prompts=has_prompts,
            prompts_list_changed=prompts_changed,
            logging=has_logging,
            experimental=exp if isinstance(exp, dict) else {},
        )

    def to_connector_capabilities(self) -> frozenset[ConnectorCapability]:
        """Map MCP server capabilities into SŪTRA ConnectorCapability set."""
        caps: set[ConnectorCapability] = {
            ConnectorCapability.HEALTH_CHECK,
            ConnectorCapability.READ,
        }
        if self.tools or self.resources or self.prompts:
            caps.add(ConnectorCapability.DISCOVERY)
        if self.tools:
            caps.add(ConnectorCapability.TOOL_EXECUTION)
            caps.add(ConnectorCapability.WRITE)
        if self.resources:
            caps.add(ConnectorCapability.RESOURCES)
        if self.prompts:
            caps.add(ConnectorCapability.PROMPTS)
        return frozenset(caps)


# ---------------------------------------------------------------------------
# Tools Domain Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MCPTool:
    """Normalized MCP tool definition exposed by an MCP server."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("MCP tool name cannot be empty")
        if self.description:
            object.__setattr__(self, "description", redact_secrets(self.description))
        clean_meta = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean_meta)

    def to_connector_tool_definition(self, *, read_only: bool = False, risk_level: str = "MEDIUM") -> ConnectorToolDefinition:
        """Convert MCPTool into SŪTRA ConnectorToolDefinition."""
        return ConnectorToolDefinition(
            name=self.name,
            description=self.description,
            parameters_schema=self.input_schema,
            read_only=read_only,
            risk_level=risk_level,
        )


@dataclass
class MCPToolCallResult:
    """Normalized result of an MCP tool invocation."""
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False
    structured_data: Optional[Any] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = sanitize_metadata(self.metadata)

    def get_text_content(self) -> str:
        """Extract concatenated text content from result blocks."""
        texts: list[str] = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts)


# ---------------------------------------------------------------------------
# Resources Domain Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MCPResource:
    """Normalized MCP resource definition."""
    uri: str
    name: str
    description: str = ""
    mime_type: Optional[str] = None
    size: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri or not self.uri.strip():
            raise ValueError("MCP resource URI cannot be empty")
        if not self.name or not self.name.strip():
            raise ValueError("MCP resource name cannot be empty")
        if self.description:
            object.__setattr__(self, "description", redact_secrets(self.description))
        clean_meta = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean_meta)

    def to_connector_resource(self) -> ConnectorResource:
        """Convert MCPResource into SŪTRA ConnectorResource."""
        return ConnectorResource(
            uri=self.uri,
            name=self.name,
            description=self.description,
            mime_type=self.mime_type,
            metadata={**self.metadata, "size": self.size} if self.size is not None else self.metadata,
        )


@dataclass(frozen=True)
class MCPResourceTemplate:
    """Parameterized URI template for dynamic resources."""
    uri_template: str
    name: str
    description: str = ""
    mime_type: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.uri_template or not self.uri_template.strip():
            raise ValueError("URI template cannot be empty")
        if self.description:
            object.__setattr__(self, "description", redact_secrets(self.description))


@dataclass(frozen=True)
class MCPResourceContent:
    """Content of a retrieved MCP resource."""
    uri: str
    mime_type: Optional[str] = None
    text: Optional[str] = None
    blob: Optional[str] = None  # Base64-encoded binary content


# ---------------------------------------------------------------------------
# Prompts Domain Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MCPPromptArgument:
    """Argument specification for an MCP prompt template."""
    name: str
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class MCPPrompt:
    """Normalized MCP prompt definition."""
    name: str
    description: str = ""
    arguments: list[MCPPromptArgument] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("MCP prompt name cannot be empty")
        if self.description:
            object.__setattr__(self, "description", redact_secrets(self.description))


@dataclass(frozen=True)
class MCPPromptMessage:
    """Rendered message inside an MCP prompt response."""
    role: str  # 'user' or 'assistant'
    content: dict[str, Any]  # Text or embedded resource content block


@dataclass(frozen=True)
class MCPPromptResult:
    """Rendered result of get_prompt()."""
    description: str = ""
    messages: list[MCPPromptMessage] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bounded Pagination Wrapper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MCPPage(Generic[T]):
    """Page wrapper for paginated MCP listing results."""
    items: list[T]
    next_cursor: Optional[str] = None
    total: Optional[int] = None

    @property
    def has_more(self) -> bool:
        """Check if additional pages exist."""
        return bool(self.next_cursor and self.next_cursor.strip())


@dataclass(frozen=True)
class MCPPaginationParams:
    """Pagination query parameters for bounded listings."""
    cursor: Optional[str] = None
    limit: Optional[int] = None
