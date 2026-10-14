"""Canonical domain models for connectors, capabilities, execution, and normalized results.

These models establish the contract for future MCP, REST, and CLI connector
implementations, completely decoupled from Telegram, specific transport protocols,
or proprietary provider SDKs.
"""
from __future__ import annotations

import enum
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sutra.redaction import redact_secrets


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ConnectorType(str, enum.Enum):
    """Transport and architectural style of the connector."""
    MCP = "MCP"        # Model Context Protocol (stdio / streamable HTTP)
    REST = "REST"      # HTTP REST API
    CLI = "CLI"        # Command line subprocess interface
    CUSTOM = "CUSTOM"  # Custom in-process or specialized adapter


class ConnectorCapability(str, enum.Enum):
    """Declared capabilities supported by a connector."""
    DISCOVERY = "discovery"             # Can list available tools/resources
    TOOL_EXECUTION = "tool_execution"   # Can invoke tools with parameters
    HEALTH_CHECK = "health_check"       # Supports active ping/health diagnostics
    RESOURCES = "resources"             # Can read external structured resources/URIs
    PROMPTS = "prompts"                 # Exposes parameterized prompt templates
    READ = "read"                       # Read-only operations allowed
    WRITE = "write"                     # Mutating/write operations allowed


class ConnectorStatus(str, enum.Enum):
    """Deterministic lifecycle state of a connector."""
    REGISTERED = "REGISTERED"      # Declared and registered in system, not yet connected
    CONNECTING = "CONNECTING"      # Handshake or network initialization in progress
    READY = "READY"                # Operational and ready to execute requests
    DEGRADED = "DEGRADED"          # Partially operational (rate limited or fallback mode)
    DISCONNECTED = "DISCONNECTED"  # Explicitly stopped or closed
    ERROR = "ERROR"                # In an unrecoverable failure state


# ---------------------------------------------------------------------------
# Security Helpers for Metadata
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_SUBSTRINGS = (
    "token", "secret", "password", "key", "auth", "credential", "bearer", "private"
)


def sanitize_metadata(data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Ensure no raw credentials, tokens, or private keys exist in metadata dicts."""
    if not data:
        return {}
    clean: dict[str, Any] = {}
    for k, v in data.items():
        k_lower = str(k).lower()
        if any(sub in k_lower for sub in _SENSITIVE_KEY_SUBSTRINGS):
            clean[k] = "[REDACTED]"
        elif isinstance(v, str):
            clean[k] = redact_secrets(v)
        elif isinstance(v, dict):
            clean[k] = sanitize_metadata(v)
        else:
            clean[k] = v
    return clean


# ---------------------------------------------------------------------------
# Connector Identity & Descriptors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectorDescriptor:
    """Immutable identity and metadata specification for a connector."""
    connector_id: str
    name: str
    type: ConnectorType
    version: str = "1.0.0"
    description: str = ""
    provider: str = ""
    enabled: bool = True
    capabilities: frozenset[ConnectorCapability] = field(default_factory=frozenset)
    status: ConnectorStatus = ConnectorStatus.REGISTERED
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.connector_id or not self.connector_id.strip():
            raise ValueError("connector_id cannot be empty")
        if not self.name or not self.name.strip():
            raise ValueError("name cannot be empty")
        # Ensure metadata is secret-safe
        clean_meta = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean_meta)

    def has_capability(self, capability: ConnectorCapability) -> bool:
        """Check if the descriptor declares a specific capability."""
        return capability in self.capabilities

    def with_status(self, new_status: ConnectorStatus) -> "ConnectorDescriptor":
        """Return a new descriptor instance with updated status."""
        return ConnectorDescriptor(
            connector_id=self.connector_id,
            name=self.name,
            type=self.type,
            version=self.version,
            description=self.description,
            provider=self.provider,
            enabled=self.enabled,
            capabilities=self.capabilities,
            status=new_status,
            metadata=dict(self.metadata),
        )

    def with_enabled(self, enabled: bool) -> "ConnectorDescriptor":
        """Return a new descriptor instance with updated enabled state."""
        return ConnectorDescriptor(
            connector_id=self.connector_id,
            name=self.name,
            type=self.type,
            version=self.version,
            description=self.description,
            provider=self.provider,
            enabled=enabled,
            capabilities=self.capabilities,
            status=self.status,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class ConnectorAnalysisDescriptor:
    """Standardized metadata structure for the Phase 7.4 Generic Environment & Connector Analyzer.
    
    Exposes configuration, transport, and client expectations in a provider-neutral
    format without requiring provider-specific conditionals in verification pipelines.
    """
    connector_id: str
    name: str
    type: ConnectorType
    provider: str = ""
    transport: str = "custom"
    enabled: bool = True
    status: ConnectorStatus = ConnectorStatus.REGISTERED
    capabilities: frozenset[ConnectorCapability] = field(default_factory=frozenset)
    version: str = "1.0.0"
    description: str = ""
    declared_env_vars: tuple[str, ...] = ()
    declared_binary_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    health: Optional["ConnectorHealth"] = None

    def __post_init__(self) -> None:
        clean_meta = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean_meta)



@dataclass(frozen=True)
class ConnectorHealth:
    """Point-in-time health diagnostic for a connector."""
    status: ConnectorStatus
    healthy: bool
    latency_ms: Optional[float] = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        clean_meta = sanitize_metadata(self.details)
        object.__setattr__(self, "details", clean_meta)
        if self.message:
            object.__setattr__(self, "message", redact_secrets(self.message))


# ---------------------------------------------------------------------------
# Tool & Resource Discovery Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectorToolDefinition:
    """Normalized metadata for a tool exposed by a connector."""
    name: str
    description: str = ""
    parameters_schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema
    read_only: bool = False
    risk_level: str = "LOW"  # Hint for PermissionEngine: LOW, MEDIUM, HIGH, CRITICAL

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Tool name cannot be empty")
        if self.description:
            object.__setattr__(self, "description", redact_secrets(self.description))


@dataclass(frozen=True)
class ConnectorResource:
    """Normalized metadata for a resource exposed by a connector."""
    uri: str
    name: str
    description: str = ""
    mime_type: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri or not self.uri.strip():
            raise ValueError("Resource URI cannot be empty")
        clean_meta = sanitize_metadata(self.metadata)
        object.__setattr__(self, "metadata", clean_meta)


# ---------------------------------------------------------------------------
# Execution Request & Result Models
# ---------------------------------------------------------------------------

@dataclass
class ConnectorRequest:
    """Normalized request initiating an operation on a connector.
    
    Correlated universally via request_id to align with ActivityStore
    and cross-system tracing.
    """
    connector_id: str
    operation: str
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    parameters: dict[str, Any] = field(default_factory=dict)
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    actor: Optional[str] = "system"
    source: Optional[str] = "agent"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.connector_id or not self.connector_id.strip():
            raise ValueError("connector_id cannot be empty")
        if not self.operation or not self.operation.strip():
            raise ValueError("operation cannot be empty")
        self.metadata = sanitize_metadata(self.metadata)

    def sanitized_parameters(self) -> dict[str, Any]:
        """Return parameters dictionary with secrets scrubbed."""
        return sanitize_metadata(self.parameters)


@dataclass
class ConnectorResult:
    """Normalized, provider-neutral execution result from a connector.
    
    Hides transport-specific constructs (HTTP status codes, subprocess exit codes,
    or MCP protocol objects) behind a unified domain representation.
    """
    success: bool
    content: Optional[str] = None
    structured_data: Optional[Any] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[str] = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.content:
            self.content = redact_secrets(self.content)
        if self.error:
            self.error = redact_secrets(self.error)
        self.metadata = sanitize_metadata(self.metadata)

    @classmethod
    def ok(
        cls,
        content: Optional[str] = None,
        structured_data: Optional[Any] = None,
        metadata: Optional[dict[str, Any]] = None,
        truncated: bool = False,
    ) -> "ConnectorResult":
        """Factory for successful connector execution results."""
        return cls(
            success=True,
            content=content,
            structured_data=structured_data,
            metadata=metadata or {},
            error=None,
            error_code=None,
            truncated=truncated,
        )

    @classmethod
    def fail(
        cls,
        error: str,
        error_code: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "ConnectorResult":
        """Factory for failed connector execution results."""
        return cls(
            success=False,
            content=None,
            structured_data=None,
            metadata=metadata or {},
            error=error,
            error_code=error_code,
            truncated=False,
        )
