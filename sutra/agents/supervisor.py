"""Agent supervision, health tracking, and controlled recovery."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .adapter import AgentAdapter
from .errors import (
    AgentNotFoundError,
    AgentStartFailedError,
    CapabilityNotSupportedError,
    SessionNotFoundError,
)
from .manager import AgentManager, STOP_TIMEOUT_SECONDS
from .models import (
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionStatus,
    EventSeverity,
)
from .registry import AgentRegistry
from ..loop_detector import AgentLoopDetector, LoopEvidence

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health model
# ---------------------------------------------------------------------------


class AgentHealthState(str, Enum):
    """Operational health states observable by the supervisor."""
    HEALTHY = "HEALTHY"
    NO_RECENT_ACTIVITY = "NO_RECENT_ACTIVITY"
    POSSIBLE_STALL = "POSSIBLE_STALL"
    SUSPECTED_LOOP = "SUSPECTED_LOOP"
    UNRESPONSIVE = "UNRESPONSIVE"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


@dataclass
class AgentHealthSnapshot:
    """Point-in-time health snapshot for one session."""
    agent_id: str = ""
    session_id: str = ""
    project_id: str = ""
    generation: int = 1
    session_status: str = ""
    health: str = AgentHealthState.UNKNOWN.value
    process_alive: bool | None = None
    last_event_at: float | None = None
    last_output_at: float | None = None
    last_activity_at: float | None = None
    last_checked_at: float = field(default_factory=time.time)
    current_task: str | None = None
    last_event_type: str | None = None
    failure_info: str | None = None
    recovery_active: bool = False
    recovery_attempt: int = 0
    recovery_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "generation": self.generation,
            "session_status": self.session_status,
            "health": self.health,
            "process_alive": self.process_alive,
            "last_event_at": self.last_event_at,
            "last_output_at": self.last_output_at,
            "last_activity_at": self.last_activity_at,
            "last_checked_at": self.last_checked_at,
            "current_task": self.current_task,
            "last_event_type": self.last_event_type,
            "failure_info": self.failure_info,
            "recovery": {
                "active": self.recovery_active,
                "attempt": self.recovery_attempt,
                "reason": self.recovery_reason,
            },
        }


@dataclass
class SupervisionPolicy:
    """Configurable policy for the supervisor."""
    watchdog_interval_seconds: float = 30.0
    no_activity_threshold_seconds: float = 120.0
    stall_threshold_seconds: float = 300.0
    unresponsive_retries: int = 3
    unresponsive_retry_delay_seconds: float = 10.0
    max_recovery_attempts: int = 3
    recovery_cooldown_seconds: float = 60.0
    health_event_maxsize: int = 500


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class AgentSupervisor:
    """Lightweight per-session supervisor observing AgentManager sessions.

    The supervisor is read-only with respect to sessions: it never replaces
    AgentManager, never executes vendor logic, and never bypasses adapters.
    It observes, detects, and (optionally) initiates bounded recovery.
    """

    def __init__(
        self,
        manager: AgentManager | None = None,
        registry: AgentRegistry | None = None,
        policy: SupervisionPolicy | None = None,
    ) -> None:
        self.manager = manager if manager is not None else AgentManager()
        self.registry = registry if registry is not None else AgentRegistry()
        self.policy = policy if policy is not None else SupervisionPolicy()

        # Per-session supervision state
        self._health: dict[str, AgentHealthSnapshot] = {}
        self._supervision_tasks: dict[str, asyncio.Task[Any]] = {}
        self._recovery_attempts: dict[str, int] = {}
        self._recovery_last_attempt: dict[str, float] = {}
        self._alerted_states: dict[str, str] = {}
        # Sessions explicitly released from supervision — watchdog won't re-supervise them
        self._released_sessions: set[str] = set()
        self._state_lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._shutdown = False
        self._loop_detectors: dict[str, AgentLoopDetector] = {}

    # ------------------------------------------------------------------
    # Supervision lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the global watchdog loop."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._shutdown = False
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        log.info("AgentSupervisor watchdog started")

    async def stop(self) -> None:
        """Stop the watchdog and all per-session supervision."""
        self._shutdown = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        session_ids = list(self._supervision_tasks.keys())
        for sid in session_ids:
            await self.stop_session_supervision(sid)
        log.info("AgentSupervisor stopped")

    async def supervise_session(self, session_id: str) -> None:
        """Start supervising a session."""
        async with self._state_lock:
            if session_id in self._supervision_tasks:
                task = self._supervision_tasks[session_id]
                if not task.done():
                    return
            self._ensure_health_snapshot(session_id)
            # Initialize activity to now so a brand-new session reports HEALTHY
            snap = self._health.get(session_id)
            if snap is not None:
                snap.last_activity_at = time.time()
                snap.last_event_at = time.time()
            task = asyncio.create_task(self._session_supervision_loop(session_id))
            self._supervision_tasks[session_id] = task
            log.debug("Supervision started for session %s", session_id)

    async def stop_session_supervision(self, session_id: str) -> None:
        """Stop supervision for a session and release resources."""
        self._released_sessions.add(session_id)
        task = self._supervision_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._health.pop(session_id, None)
        self._recovery_attempts.pop(session_id, None)
        self._recovery_last_attempt.pop(session_id, None)
        self._alerted_states.pop(session_id, None)
        log.debug("Supervision stopped for session %s", session_id)

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Periodically scan managed sessions and supervise new ones."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self.policy.watchdog_interval_seconds)
                if self._shutdown:
                    break
                active = list(self.manager.list_sessions())
                for session in active:
                    sid = session.session_id
                    if sid in self._supervision_tasks:
                        continue
                    if sid in self._released_sessions:
                        continue
                    # Only supervise live sessions (RUNNING, WAITING, BLOCKED, STARTING, FAILED)
                    if session.status not in (
                        AgentSessionStatus.RUNNING,
                        AgentSessionStatus.WAITING,
                        AgentSessionStatus.BLOCKED,
                        AgentSessionStatus.STARTING,
                        AgentSessionStatus.FAILED,
                    ):
                        continue
                    await self.supervise_session(sid)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.debug("Watchdog loop error: %s", exc)

    async def _session_supervision_loop(self, session_id: str) -> None:
        """Per-session background supervision."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self.policy.watchdog_interval_seconds)
                if self._shutdown:
                    break
                await self._check_session(session_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.debug("Supervision loop error for session %s: %s", session_id, exc)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def _check_session(self, session_id: str) -> None:
        """Run one health check cycle for a session."""
        session = self._get_session(session_id)
        if session is None:
            await self.stop_session_supervision(session_id)
            return

        snapshot = self._health.get(session_id)
        if snapshot is None:
            self._ensure_health_snapshot(session_id)
            snapshot = self._health[session_id]

        now = time.time()
        last_event = session.last_event_at
        last_output = getattr(session, "last_output_at", None)

        # Update snapshot fields — keep existing snapshot values when session has none
        if last_event is not None:
            snapshot.last_event_at = last_event
        if last_output is not None:
            snapshot.last_output_at = last_output

        # Determine activity timestamp: prefer most recent signal available
        last_activity = max(filter(None, [
            snapshot.last_activity_at,
            snapshot.last_output_at,
            snapshot.last_event_at,
            session.last_activity_at,
            session.last_output_at,
            session.last_event_at,
        ]), default=None)
        snapshot.last_activity_at = last_activity

        # Update snapshot fields
        snapshot.agent_id = session.agent_id
        snapshot.project_id = session.project_id
        snapshot.generation = session.generation
        snapshot.session_status = session.status.value
        snapshot.last_event_at = last_event
        snapshot.last_output_at = last_output
        snapshot.last_activity_at = last_activity
        snapshot.current_task = session.current_task
        snapshot.last_checked_at = now

        # Determine process liveness
        process_alive = await self._check_process_alive(session_id)
        snapshot.process_alive = process_alive

        # Crash detection: only when process_alive is explicitly False
        if process_alive is False and session.status in (
            AgentSessionStatus.RUNNING, AgentSessionStatus.WAITING, AgentSessionStatus.BLOCKED
        ):
            failure_info = f"Process exited unexpectedly (session {session_id})"
            snapshot.failure_info = failure_info
            snapshot.health = AgentHealthState.FAILED.value
            snapshot.recovery_active = False
            await self._on_health_change(session_id, snapshot)
            return

        if session.status == AgentSessionStatus.FAILED:
            snapshot.health = AgentHealthState.FAILED.value
            snapshot.failure_info = snapshot.failure_info or "Session marked as FAILED"
            await self._maybe_recover(session_id, session, snapshot)
            return

        if session.status in (AgentSessionStatus.STOPPED, AgentSessionStatus.COMPLETED):
            snapshot.health = AgentHealthState.STOPPED.value
            await self._on_health_change(session_id, snapshot)
            return

        # Activity-based detection
        if last_activity is not None:
            idle_seconds = now - last_activity
            if idle_seconds > self.policy.stall_threshold_seconds:
                new_health = AgentHealthState.POSSIBLE_STALL.value
            elif idle_seconds > self.policy.no_activity_threshold_seconds:
                new_health = AgentHealthState.NO_RECENT_ACTIVITY.value
            else:
                new_health = AgentHealthState.HEALTHY.value
        else:
            new_health = AgentHealthState.UNKNOWN.value

        snapshot.health = new_health
        await self._on_health_change(session_id, snapshot)

    async def _check_process_alive(self, session_id: str) -> bool | None:
        """Return True if process is alive, False if dead, None if unknown.

        Uses the adapter's get_status capability if available, without blocking
        the event loop. Adapters may return a string, an enum, or a truthy value.
        """
        try:
            session = self.manager.get_session(session_id)
            adapter = self.registry.get(session.agent_id)
        except (SessionNotFoundError, AgentNotFoundError):
            return None

        if AgentCapability.GET_STATUS not in adapter.capabilities():
            return None

        try:
            raw = await adapter.get_status(session_id)
        except Exception:
            return None

        # Normalise adapter return value to AgentSessionStatus
        if isinstance(raw, AgentSessionStatus):
            status = raw
        elif isinstance(raw, str):
            try:
                status = AgentSessionStatus(raw.upper())
            except ValueError:
                # Treat any truthy non-idle string as alive
                if raw and raw.upper() not in ("IDLE", "UNKNOWN"):
                    return True
                return None
        else:
            return None

        if status == AgentSessionStatus.RUNNING:
            return True
        if status in (
            AgentSessionStatus.STOPPED,
            AgentSessionStatus.COMPLETED,
            AgentSessionStatus.FAILED,
            AgentSessionStatus.STOPPING,
        ):
            return False
        return None

    # ------------------------------------------------------------------
    # Event integration
    # ------------------------------------------------------------------

    def on_adapter_event(self, event: AgentEvent) -> None:
        """Hook for AgentManager to notify supervisor of adapter events."""
        session_id = event.agent_session_id
        snapshot = self._health.get(session_id)
        if snapshot is None:
            return

        now = event.timestamp or time.time()
        snapshot.last_event_at = now
        snapshot.last_event_type = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        snapshot.last_checked_at = now

        # Check for loops and repeated failures (Issue 12)
        detector = self._loop_detectors.setdefault(session_id, AgentLoopDetector())
        evidence: LoopEvidence | None = None
        if event.event_type == AgentEventType.AGENT_OUTPUT:
            stream = event.payload.get("stream", "stdout")
            text = str(event.payload.get("text", ""))
            if stream == "stderr" or event.severity in (EventSeverity.WARNING, EventSeverity.ERROR):
                evidence = detector.record_error(text)
        elif event.event_type == AgentEventType.TOOL_REQUEST or "command" in event.payload or "tool" in event.payload:
            cmd = event.payload.get("command") or event.payload.get("tool") or ""
            evidence = detector.record_command(str(cmd))

        if evidence is not None:
            snapshot.health = AgentHealthState.SUSPECTED_LOOP.value
            snapshot.failure_info = evidence.details
            log.warning("SUSPECTED_LOOP detected for session %s: %s", session_id, evidence.details)
            if self.manager is not None:
                self.manager.publish_event(AgentEvent(
                    event_type=AgentEventType.AGENT_BLOCKED,
                    agent_id=event.agent_id,
                    agent_session_id=session_id,
                    project_id=event.project_id,
                    severity=EventSeverity.WARNING,
                    payload={
                        "reason": "SUSPECTED_LOOP",
                        "loop_type": evidence.loop_type,
                        "details": evidence.details,
                        "sample": evidence.sample,
                    },
                ))
            return

        activity_event_types = {
            AgentEventType.AGENT_PROGRESS,
            AgentEventType.AGENT_OUTPUT,
            AgentEventType.AGENT_READY,
            AgentEventType.AGENT_STATUS_CHANGED,
            AgentEventType.FILE_CHANGED,
            AgentEventType.TEST_STARTED,
            AgentEventType.TEST_FAILED,
            AgentEventType.TEST_PASSED,
            AgentEventType.PHASE_STARTED,
            AgentEventType.PHASE_COMPLETED,
        }
        if event.event_type in activity_event_types:
            snapshot.last_activity_at = now
            snapshot.last_output_at = now
            snapshot.current_task = event.payload.get("message") or event.payload.get("action") or snapshot.current_task
            # Any meaningful activity resets health to HEALTHY if not in suspected loop
            if snapshot.health != AgentHealthState.SUSPECTED_LOOP.value:
                if snapshot.health != AgentHealthState.HEALTHY.value:
                    snapshot.health = AgentHealthState.HEALTHY.value
                    self._alerted_states.pop(session_id, None)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def _maybe_recover(self, session_id: str, session: AgentSession, snapshot: AgentHealthSnapshot) -> None:
        """Attempt bounded recovery if policy allows."""
        if snapshot.recovery_active:
            return

        # Do not recover user-stopped sessions
        if session.stopped_at is not None:
            return

        # Stale-generation protection
        if snapshot.generation != session.generation:
            return

        adapter = self.registry.get(session.agent_id)
        if AgentCapability.START not in adapter.capabilities():
            return

        # Check cooldown
        last_attempt = self._recovery_last_attempt.get(session_id, 0.0)
        if time.time() - last_attempt < self.policy.recovery_cooldown_seconds:
            return

        attempts = self._recovery_attempts.get(session_id, 0)
        if attempts >= self.policy.max_recovery_attempts:
            return

        snapshot.recovery_active = True
        snapshot.recovery_attempt = attempts + 1
        snapshot.recovery_reason = snapshot.failure_info
        snapshot.health = AgentHealthState.RECOVERING.value

        self._recovery_attempts[session_id] = attempts + 1
        self._recovery_last_attempt[session_id] = time.time()

        log.info("Recovering session %s (attempt %d)", session_id, attempts + 1)
        await self._on_health_change(session_id, snapshot)

        try:
            await self.manager.start_session(session_id)
            snapshot.health = AgentHealthState.HEALTHY.value
            snapshot.recovery_active = False
            snapshot.recovery_reason = None
            snapshot.failure_info = None
            self._alerted_states.pop(session_id, None)
            await self._on_health_change(session_id, snapshot)
        except AgentStartFailedError as exc:
            snapshot.recovery_active = False
            snapshot.failure_info = f"Recovery attempt {attempts + 1} failed: {exc.message}"
            await self._on_health_change(session_id, snapshot)
        except Exception as exc:
            snapshot.recovery_active = False
            snapshot.failure_info = f"Recovery error: {exc}"
            await self._on_health_change(session_id, snapshot)

    # ------------------------------------------------------------------
    # Health snapshots
    # ------------------------------------------------------------------

    def get_health(self, session_id: str) -> AgentHealthSnapshot | None:
        """Return the latest health snapshot for a session, or None if not supervised."""
        return self._health.get(session_id)

    def list_health(self) -> list[AgentHealthSnapshot]:
        """Return health snapshots for all supervised sessions."""
        result = list(self._health.values())
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> AgentSession | None:
        try:
            return self.manager.get_session(session_id)
        except SessionNotFoundError:
            return None

    def _ensure_health_snapshot(self, session_id: str) -> None:
        if session_id not in self._health:
            session = self._get_session(session_id)
            snap = AgentHealthSnapshot(
                agent_id=session.agent_id if session else "",
                session_id=session_id,
                project_id=session.project_id if session else "",
                generation=session.generation if session else 0,
                session_status=session.status.value if session else "",
            )
            self._health[session_id] = snap

    async def _on_health_change(self, session_id: str, snapshot: AgentHealthSnapshot) -> None:
        """Called when health state changes. Emits events and deduplicates alerts."""
        # Emit a health event for meaningful transitions
        event_types = {
            AgentHealthState.FAILED.value: AgentEventType.AGENT_FAILED,
            AgentHealthState.RECOVERING.value: AgentEventType.AGENT_STATUS_CHANGED,
            AgentHealthState.STOPPED.value: AgentEventType.AGENT_STOPPED,
        }
        event_type = event_types.get(snapshot.health)
        if event_type is not None:
            self.manager.publish_event(AgentEvent(
                event_type=event_type,
                agent_id=snapshot.agent_id,
                agent_session_id=session_id,
                project_id=snapshot.project_id,
                severity=EventSeverity.ERROR if snapshot.health == AgentHealthState.FAILED.value else EventSeverity.INFO,
                payload={"health": snapshot.health, "session_status": snapshot.session_status},
            ))

        # Alert deduplication: only alert on meaningful state changes
        previous = self._alerted_states.get(session_id)
        if previous != snapshot.health:
            self._alerted_states[session_id] = snapshot.health
            if snapshot.health in (
                AgentHealthState.FAILED.value,
                AgentHealthState.POSSIBLE_STALL.value,
                AgentHealthState.UNRESPONSIVE.value,
            ):
                log.warning(
                    "Health alert: session=%s agent=%s health=%s task=%s",
                    session_id, snapshot.agent_id, snapshot.health, snapshot.current_task,
                )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """Return lightweight supervision metrics."""
        healths = list(self._health.values())
        by_health: dict[str, int] = {}
        for h in healths:
            by_health[h.health] = by_health.get(h.health, 0) + 1
        return {
            "supervised_sessions": len(healths),
            "healthy": by_health.get(AgentHealthState.HEALTHY.value, 0),
            "no_recent_activity": by_health.get(AgentHealthState.NO_RECENT_ACTIVITY.value, 0),
            "possible_stall": by_health.get(AgentHealthState.POSSIBLE_STALL.value, 0),
            "unresponsive": by_health.get(AgentHealthState.UNRESPONSIVE.value, 0),
            "failed": by_health.get(AgentHealthState.FAILED.value, 0),
            "recovering": by_health.get(AgentHealthState.RECOVERING.value, 0),
            "stopped": by_health.get(AgentHealthState.STOPPED.value, 0),
            "unknown": by_health.get(AgentHealthState.UNKNOWN.value, 0),
            "recovery_attempts_total": sum(self._recovery_attempts.values()),
            "watchdog_running": self._watchdog_task is not None and not self._watchdog_task.done(),
        }
