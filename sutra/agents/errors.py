"""Structured error types for the SŪTRA agent subsystem."""
from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class for all agent-related exceptions."""

    def __init__(self, message: str, code: str = "AGENT_ERROR", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class AgentNotFoundError(AgentError):
    """Raised when an requested agent adapter is not registered."""

    def __init__(self, agent_id: str, details: dict[str, Any] | None = None) -> None:
        payload = {"agent_id": agent_id, **(details or {})}
        super().__init__(f"Agent '{agent_id}' not found", code="AGENT_NOT_FOUND", details=payload)


class AgentUnavailableError(AgentError):
    """Raised when an agent is registered but not installed or available on the host."""

    def __init__(self, agent_id: str, reason: str = "Agent is not installed or available", details: dict[str, Any] | None = None) -> None:
        payload = {"agent_id": agent_id, "reason": reason, **(details or {})}
        super().__init__(f"Agent '{agent_id}' unavailable: {reason}", code="AGENT_UNAVAILABLE", details=payload)


class CapabilityNotSupportedError(AgentError):
    """Raised when an operation is attempted that the agent does not support."""

    def __init__(self, agent_id: str, capability: str, details: dict[str, Any] | None = None) -> None:
        payload = {"agent_id": agent_id, "capability": capability, **(details or {})}
        super().__init__(
            f"Agent '{agent_id}' does not support capability '{capability}'",
            code="CAPABILITY_NOT_SUPPORTED",
            details=payload,
        )


class SessionNotFoundError(AgentError):
    """Raised when an agent session does not exist."""

    def __init__(self, session_id: str, details: dict[str, Any] | None = None) -> None:
        payload = {"session_id": session_id, **(details or {})}
        super().__init__(f"Agent session '{session_id}' not found", code="SESSION_NOT_FOUND", details=payload)


class InvalidProjectScopeError(AgentError):
    """Raised when an agent operation targets a path outside authorized project scope."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        payload = {"reason": reason, **(details or {})}
        super().__init__(f"Invalid project scope: {reason}", code="INVALID_PROJECT_SCOPE", details=payload)


class AgentStartFailedError(AgentError):
    """Raised when starting an agent session fails."""

    def __init__(self, session_id: str, reason: str, details: dict[str, Any] | None = None) -> None:
        payload = {"session_id": session_id, "reason": reason, **(details or {})}
        super().__init__(
            f"Failed to start agent session '{session_id}': {reason}",
            code="AGENT_START_FAILED",
            details=payload,
        )


class AgentStopFailedError(AgentError):
    """Raised when stopping an agent session fails."""

    def __init__(self, session_id: str, reason: str, details: dict[str, Any] | None = None) -> None:
        payload = {"session_id": session_id, "reason": reason, **(details or {})}
        super().__init__(
            f"Failed to stop agent session '{session_id}': {reason}",
            code="AGENT_STOP_FAILED",
            details=payload,
        )


class AgentProcessFailedError(AgentError):
    """Raised when an agent subprocess execution fails or exits non-zero unexpectedly."""

    def __init__(self, agent_id: str, exit_code: int | None, stderr: str = "", details: dict[str, Any] | None = None) -> None:
        payload = {"agent_id": agent_id, "exit_code": exit_code, "stderr": stderr, **(details or {})}
        super().__init__(
            f"Agent process '{agent_id}' failed with exit code {exit_code}",
            code="AGENT_PROCESS_FAILED",
            details=payload,
        )


class AgentTimeoutError(AgentError):
    """Raised when an agent operation times out."""

    def __init__(self, operation: str, timeout_seconds: float, details: dict[str, Any] | None = None) -> None:
        payload = {"operation": operation, "timeout_seconds": timeout_seconds, **(details or {})}
        super().__init__(
            f"Agent operation '{operation}' timed out after {timeout_seconds}s",
            code="AGENT_TIMEOUT",
            details=payload,
        )


class InvalidStateTransitionError(AgentError):
    """Raised when an agent session attempts an invalid state transition."""

    def __init__(self, session_id: str, from_state: str, to_state: str, details: dict[str, Any] | None = None) -> None:
        payload = {
            "session_id": session_id,
            "from_state": from_state,
            "to_state": to_state,
            **(details or {}),
        }
        super().__init__(
            f"Invalid state transition for session '{session_id}': {from_state} -> {to_state}",
            code="INVALID_STATE_TRANSITION",
            details=payload,
        )


class DuplicateAgentError(AgentError):
    """Raised when attempting to register an agent ID that already exists."""

    def __init__(self, agent_id: str, details: dict[str, Any] | None = None) -> None:
        payload = {"agent_id": agent_id, **(details or {})}
        super().__init__(f"Agent '{agent_id}' is already registered", code="DUPLICATE_AGENT", details=payload)
