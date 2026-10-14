"""Graceful degradation policy for SŪTRA.

Defines which operations are critical vs non-critical, and how the system
should behave when non-critical dependencies become unavailable.

Design principles:
- Never fabricate success for side-effecting operations.
- Never bypass Circuit Breakers.
- Stale data is only used for read-only operations where it cannot cause harm.
- Degradation is observable via metrics and structured logs.
"""
from __future__ import annotations

import logging
from enum import Enum

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Criticality classification
# ---------------------------------------------------------------------------

#: Commands that have irreversible side effects or must never return
#: fabricated/uncertain results.
CRITICAL_COMMANDS: frozenset[str] = frozenset({
    "workspace.add",
    "access_root.add",
    "session.stop",
    "agent.start",
    "agent.stop",
    "agent.prompt",
})

#: Commands that are read-only and safe to degrade with partial or stale data.
READ_ONLY_COMMANDS: frozenset[str] = frozenset({
    "workspace.list",
    "project.list",
    "project.refresh",
    "file.list",
    "file.preview",
    "file.download",
    "agent.list",
    "agent.status",
    "runtime.status",
    "runtime.health",
})

#: Commands that are safe to serve from cache when the live source fails.
CACHE_FALLBACK_COMMANDS: frozenset[str] = frozenset({
    "project.list",
    "agent.list",
    "agent.status",
})

#: Commands where an empty result is acceptable when the source is degraded.
EMPTY_FALLBACK_COMMANDS: frozenset[str] = frozenset({
    "project.list",
    "project.refresh",
    "agent.list",
    "agent.status",
    "workspace.list",
})


# ---------------------------------------------------------------------------
# Dependency criticality
# ---------------------------------------------------------------------------

class DependencyCriticality(Enum):
    """How essential a dependency is for normal operation."""
    CRITICAL = "critical"
    OPTIONAL = "optional"
    DEGRADABLE = "degradable"


#: Dependency -> (criticality, whether cache fallback is available)
DEPENDENCY_POLICY: dict[str, tuple[DependencyCriticality, bool]] = {
    "project_index": (DependencyCriticality.DEGRADABLE, True),
    "cache": (DependencyCriticality.DEGRADABLE, False),
    "wss": (DependencyCriticality.CRITICAL, False),
    "control_plane_store": (DependencyCriticality.CRITICAL, False),
    "state_store": (DependencyCriticality.CRITICAL, False),
    "pairing_broker": (DependencyCriticality.CRITICAL, False),
    "rate_limiter": (DependencyCriticality.CRITICAL, False),
    "single_flight": (DependencyCriticality.DEGRADABLE, False),
    "telegram_api": (DependencyCriticality.OPTIONAL, False),
    "control_plane_http": (DependencyCriticality.OPTIONAL, False),
}


# ---------------------------------------------------------------------------
# Degradation decision helpers
# ---------------------------------------------------------------------------

def can_degrade(command: str) -> bool:
    """Return True if the command is non-critical and may receive a degraded response."""
    return command not in CRITICAL_COMMANDS


def can_use_cache_fallback(command: str) -> bool:
    """Return True if the command may serve stale cached data when the live source fails."""
    return command in CACHE_FALLBACK_COMMANDS


def can_return_empty(command: str) -> bool:
    """Return True if returning an empty result is acceptable for this command when degraded."""
    return command in EMPTY_FALLBACK_COMMANDS


def is_read_only(command: str) -> bool:
    """Return True if the command does not modify state."""
    return command in READ_ONLY_COMMANDS


def dependency_policy(dependency: str) -> tuple[DependencyCriticality, bool]:
    """Return (criticality, cache_fallback_available) for a dependency name."""
    return DEPENDENCY_POLICY.get(dependency, (DependencyCriticality.CRITICAL, False))


def should_degrade_on_dependency_failure(
    command: str,
    dependency: str,
) -> bool:
    """Return True if a failure of *dependency* during *command* should trigger
    graceful degradation rather than immediate failure."""
    if not can_degrade(command):
        return False
    criticality, _ = dependency_policy(dependency)
    return criticality in (DependencyCriticality.OPTIONAL, DependencyCriticality.DEGRADABLE)


# ---------------------------------------------------------------------------
# Degraded response builders
# ---------------------------------------------------------------------------

def degraded_response(
    command: str,
    dependency: str,
    *,
    reason: str = "",
    cached: bool = False,
) -> dict[str, object]:
    """Build a degraded response for a non-critical command.

    The response clearly communicates that the result is degraded.
    """
    data: dict[str, object] = {
        "status": "degraded",
        "degraded": True,
        "dependency": dependency,
        "command": command,
    }
    if reason:
        data["reason"] = reason
    if cached:
        data["source"] = "cache"
        data["stale"] = True
    if can_return_empty(command):
        if command in ("project.list", "project.refresh"):
            data["items"] = []
            data["total_count"] = 0
            data["refreshing"] = False
        elif command == "workspace.list":
            data["items"] = []
        elif command == "agent.list":
            data["items"] = []
            data["total_count"] = 0
        elif command == "agent.status":
            data["agents"] = []
            data["active_sessions"] = []
    return data


def feature_unavailable_response(
    command: str,
    dependency: str,
    *,
    message: str = "",
) -> dict[str, object]:
    """Build a controlled 'feature unavailable' response for a critical or
    non-degradable command."""
    msg = message or f"{command} is temporarily unavailable"
    return {
        "status": "error",
        "error": msg,
        "error_type": "feature_unavailable",
        "dependency": dependency,
        "command": command,
    }


# ---------------------------------------------------------------------------
# Failure classification helpers
# ---------------------------------------------------------------------------

def is_non_critical_failure(exc: BaseException) -> bool:
    """Return True if *exc* represents a non-critical failure where
    graceful degradation may be appropriate.

    Never degrade on:
    * Side-effecting operations (agent.start, workspace.add, etc.)
    * Authentication / authorization errors
    * Validation errors
    * Circuit breaker OPEN (should be handled upstream)
    """
    import asyncio
    if isinstance(exc, (PermissionError, ValueError, TypeError)):
        return False
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return False  # timeout on side-effecting ops must not silently degrade
    if isinstance(exc, OSError):
        return True
    return False
