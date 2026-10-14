"""Canonical internal domain models for agents, capabilities, sessions, and events."""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class AgentCapability(str, enum.Enum):
    """Capability-based feature flags for agents."""
    START = "START"
    STOP = "STOP"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    SEND_PROMPT = "SEND_PROMPT"
    STREAM_OUTPUT = "STREAM_OUTPUT"
    GET_STATUS = "GET_STATUS"
    FILE_OPERATIONS = "FILE_OPERATIONS"
    COMMAND_EXECUTION = "COMMAND_EXECUTION"
    PERMISSION_REQUESTS = "PERMISSION_REQUESTS"
    TOOL_CALLS = "TOOL_CALLS"
    PLAN_EVENTS = "PLAN_EVENTS"
    GIT_AWARE = "GIT_AWARE"
    HEADLESS = "HEADLESS"
    ACP = "ACP"
    API = "API"
    CLI = "CLI"


class AgentAvailability(str, enum.Enum):
    """Availability status of an agent on the host system."""
    INSTALLED = "INSTALLED"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class AgentSessionStatus(str, enum.Enum):
    """Deterministic lifecycle states of an agent session."""
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class AgentEventType(str, enum.Enum):
    """Canonical event types emitted by agents and adapters."""
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_READY = "AGENT_READY"
    AGENT_PROGRESS = "AGENT_PROGRESS"
    AGENT_TURN_COMPLETED = "AGENT_TURN_COMPLETED"
    AGENT_OUTPUT = "AGENT_OUTPUT"
    AGENT_STATUS_CHANGED = "AGENT_STATUS_CHANGED"
    TOOL_REQUEST = "TOOL_REQUEST"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    FILE_CHANGED = "FILE_CHANGED"
    TEST_STARTED = "TEST_STARTED"
    TEST_FAILED = "TEST_FAILED"
    TEST_PASSED = "TEST_PASSED"
    PHASE_STARTED = "PHASE_STARTED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    AGENT_BLOCKED = "AGENT_BLOCKED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_STOPPED = "AGENT_STOPPED"


class EventSeverity(str, enum.Enum):
    """Severity levels for canonical agent events."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class AgentDescriptor:
    """Represents an installed or configured agent and its capabilities."""
    agent_id: str
    provider: str
    display_name: str
    version: str | None = None
    executable: str | None = None
    availability: AgentAvailability = AgentAvailability.UNAVAILABLE
    capabilities: tuple[str, ...] = ()
    installation_notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    status: str = "Not Connected"
    implementation: str = "Not Implemented"

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, tuple):
            self.capabilities = tuple(sorted(
                c.value if hasattr(c, "value") else str(c) for c in self.capabilities
            ))
        if not self.name:
            self.name = self.display_name
        if not self.status:
            self.status = "Not Connected"
        if not self.implementation:
            self.implementation = "Not Implemented"

    def has_capability(self, capability: AgentCapability | str) -> bool:
        cap_val = capability.value if hasattr(capability, "value") else str(capability)
        return cap_val in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "provider": self.provider,
            "display_name": self.display_name,
            "name": self.name,
            "version": self.version,
            "executable": self.executable,
            "availability": str(self.availability.value if hasattr(self.availability, "value") else self.availability),
            "status": self.status,
            "implementation": self.implementation,
            "capabilities": list(self.capabilities),
            "installation_notes": self.installation_notes,
            "metadata": dict(self.metadata),
        }


@dataclass
class AgentSession:
    """Represents one active or historical agent session scoped to a project."""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    project_id: str = ""
    project_root: str = ""
    status: AgentSessionStatus = AgentSessionStatus.IDLE
    started_at: float | None = None
    stopped_at: float | None = None
    current_task: str | None = None
    last_event_at: float | None = None
    last_output_at: float | None = None
    last_activity_at: float | None = None
    generation: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "project_root": self.project_root,
            "status": str(self.status.value if hasattr(self.status, "value") else self.status),
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "current_task": self.current_task,
            "last_event_at": self.last_event_at,
            "last_output_at": self.last_output_at,
            "last_activity_at": self.last_activity_at,
            "generation": self.generation,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AgentEvent:
    """Canonical event envelope emitted during agent execution."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: AgentEventType = AgentEventType.AGENT_PROGRESS
    agent_id: str = ""
    agent_session_id: str = ""
    project_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0
    severity: EventSeverity = EventSeverity.INFO
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": str(self.event_type.value if hasattr(self.event_type, "value") else self.event_type),
            "agent_id": self.agent_id,
            "agent_session_id": self.agent_session_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "severity": str(self.severity.value if hasattr(self.severity, "value") else self.severity),
            "payload": dict(self.payload),
            "trace_id": self.trace_id,
        }
