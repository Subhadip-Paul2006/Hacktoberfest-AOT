"""SŪTRA canonical agent models, adapter interface, registry, and manager."""
from __future__ import annotations

from .adapter import AgentAdapter, PlaceholderAdapter
from .supervisor import AgentHealthSnapshot, AgentHealthState, AgentSupervisor, SupervisionPolicy
from .errors import (
    AgentError,
    AgentNotFoundError,
    AgentProcessFailedError,
    AgentStartFailedError,
    AgentStopFailedError,
    AgentTimeoutError,
    AgentUnavailableError,
    CapabilityNotSupportedError,
    DuplicateAgentError,
    InvalidProjectScopeError,
    InvalidStateTransitionError,
    SessionNotFoundError,
)
from .manager import AgentManager
from .models import (
    AgentAvailability,
    AgentCapability,
    AgentDescriptor,
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionStatus,
    EventSeverity,
)
from .antigravity import AntigravityAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .generic_cli import GenericCLIAdapter
from .jules import JulesAdapter
from .registry import AgentRegistry, get_default_adapters, get_default_descriptors
from .security import SafeProcessConfig, redact_secrets, validate_project_scope

__all__ = [
    "AgentAdapter",
    "AgentAvailability",
    "AgentCapability",
    "AgentDescriptor",
    "AgentError",
    "AgentEvent",
    "AgentEventType",
    "AgentHealthSnapshot",
    "AgentHealthState",
    "AgentManager",
    "AgentNotFoundError",
    "AgentProcessFailedError",
    "AgentRegistry",
    "AgentSession",
    "AgentSessionStatus",
    "AgentStartFailedError",
    "AgentStopFailedError",
    "AgentSupervisor",
    "AgentTimeoutError",
    "AgentUnavailableError",
    "AntigravityAdapter",
    "CapabilityNotSupportedError",
    "CodexAdapter",
    "CursorAdapter",
    "DuplicateAgentError",
    "EventSeverity",
    "GenericCLIAdapter",
    "InvalidProjectScopeError",
    "InvalidStateTransitionError",
    "JulesAdapter",
    "PlaceholderAdapter",
    "SafeProcessConfig",
    "SessionNotFoundError",
    "SupervisionPolicy",
    "get_default_adapters",
    "get_default_descriptors",
    "redact_secrets",
    "validate_project_scope",
]
