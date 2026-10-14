"""Central manager governing agent sessions, lifecycle transitions, and event routing."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .adapter import AgentAdapter
from .errors import (
    AgentNotFoundError,
    AgentStartFailedError,
    AgentStopFailedError,
    AgentUnavailableError,
    CapabilityNotSupportedError,
    InvalidStateTransitionError,
    SessionNotFoundError,
)
from .models import (
    AgentCapability,
    AgentDescriptor,
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionStatus,
    EventSeverity,
)
from .registry import AgentRegistry
from .security import validate_project_scope
from ..workspace_lock import WorkspaceLockManager

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_EVENT_PAYLOAD_SIZE = 8192  # bytes — prevents oversized WSS frames
MAX_AGENT_OUTPUT_CHUNKS = 50   # max coalesced output chunks per flush
OUTPUT_FLUSH_INTERVAL = 0.25   # seconds between automatic output flushes
EVENT_QUEUE_MAXSIZE = 200      # per-session subscriber queue depth
STOP_TIMEOUT_SECONDS = 15.0    # timeout for adapter.stop() calls

# ---------------------------------------------------------------------------
# Valid state transitions
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[AgentSessionStatus, set[AgentSessionStatus]] = {
    AgentSessionStatus.IDLE:     {AgentSessionStatus.STARTING},
    AgentSessionStatus.STARTING: {AgentSessionStatus.RUNNING, AgentSessionStatus.FAILED},
    AgentSessionStatus.RUNNING:  {
        AgentSessionStatus.WAITING,
        AgentSessionStatus.BLOCKED,
        AgentSessionStatus.STOPPING,
        AgentSessionStatus.COMPLETED,
        AgentSessionStatus.FAILED,
    },
    AgentSessionStatus.WAITING:  {AgentSessionStatus.RUNNING},
    AgentSessionStatus.BLOCKED:  {AgentSessionStatus.RUNNING},
    AgentSessionStatus.STOPPING: {AgentSessionStatus.STOPPED, AgentSessionStatus.FAILED},
    AgentSessionStatus.STOPPED:  set(),
    AgentSessionStatus.FAILED:   {AgentSessionStatus.STARTING},  # can restart
    AgentSessionStatus.COMPLETED: set(),
}


class AgentManager:
    """Orchestrates agent sessions, lifecycle transitions, and event routing."""

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self.registry = registry if registry is not None else AgentRegistry()
        self._sessions: dict[str, AgentSession] = {}
        self._event_queues: dict[str, list[asyncio.Queue[AgentEvent]]] = {}
        self._event_sequences: dict[str, int] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_generations: dict[str, int] = {}
        self._session_background_tasks: dict[str, list[asyncio.Task[Any]]] = {}
        self._state_lock = asyncio.Lock()
        self._supervisor_callback: Callable[[AgentEvent], None] | None = None
        self.workspace_locks = WorkspaceLockManager()
        self._coalesce_buffer: dict[str, list[AgentEvent]] = {}
        self._coalesce_timers: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    @staticmethod
    def _validate_transition(current: AgentSessionStatus, desired: AgentSessionStatus) -> None:
        """Raise InvalidStateTransitionError if the transition is not allowed."""
        allowed = _VALID_TRANSITIONS.get(current, set())
        if desired not in allowed:
            raise InvalidStateTransitionError(
                session_id="",
                from_state=current.value,
                to_state=desired.value,
            )

    def _increment_generation(self, session_id: str) -> int:
        """Bump and return the new generation for a session restart."""
        current = self._session_generations.get(session_id, 0)
        new_gen = current + 1
        self._session_generations[session_id] = new_gen
        return new_gen

    def _check_generation(self, session_id: str, expected_generation: int | None) -> bool:
        """Return True if expected_generation matches current (or None means skip)."""
        if expected_generation is None:
            return True
        actual = self._session_generations.get(session_id, 0)
        return actual == expected_generation

    def _sanitize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Truncate oversized payload values to prevent memory / WSS issues."""
        if not isinstance(payload, dict):
            return {}
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str) and len(value.encode("utf-8", errors="replace")) > MAX_EVENT_PAYLOAD_SIZE:
                sanitized[key] = value[:MAX_EVENT_PAYLOAD_SIZE] + "... [TRUNCATED]"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_payload(value)
            else:
                sanitized[key] = value
        return sanitized

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def create_session(
        self,
        agent_id: str,
        project_root: str | Path,
        authorized_roots: Sequence[str | Path],
        *,
        project_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AgentSession:
        """Create a new AgentSession verified within authorized project scope."""
        adapter = self.registry.get(agent_id)
        desc = adapter.descriptor()

        # Validate project containment and canonical path
        canonical_project_path = validate_project_scope(project_root, authorized_roots)

        session = AgentSession(
            agent_id=desc.agent_id,
            project_id=project_id or canonical_project_path.name,
            project_root=str(canonical_project_path),
            status=AgentSessionStatus.IDLE,
            generation=1,
            metadata=dict(metadata or {}),
        )
        self._sessions[session.session_id] = session
        self._event_sequences[session.session_id] = 0
        self._session_generations[session.session_id] = 1
        self._session_background_tasks[session.session_id] = []
        log.info(
            "Created agent session %s for agent '%s' at project '%s'",
            session.session_id,
            session.agent_id,
            session.project_root,
        )
        return session

    def get_session(self, session_id: str) -> AgentSession:
        """Retrieve a session by its unique ID, raising SessionNotFoundError if absent."""
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    def list_sessions(
        self,
        agent_id: str | None = None,
        project_root: str | None = None,
    ) -> list[AgentSession]:
        """List active sessions, optionally filtered by agent_id or project path."""
        results = list(self._sessions.values())
        if agent_id:
            canon_agent = agent_id.casefold()
            results = [s for s in results if s.agent_id.casefold() == canon_agent]
        if project_root:
            canon_path = str(Path(project_root).expanduser().resolve(strict=False))
            results = [s for s in results if s.project_root == canon_path]
        return results

    # ------------------------------------------------------------------
    # Lifecycle: start / stop / prompt / status
    # ------------------------------------------------------------------

    async def start_session(self, session_id: str, **kwargs: Any) -> AgentSession:
        """Transition IDLE -> STARTING -> RUNNING (or FAILED). Generates lifecycle events."""
        session_lock = self._get_session_lock(session_id)
        async with session_lock:
            session = self.get_session(session_id)
            adapter = self.registry.get(session.agent_id)
            adapter.require_capability(AgentCapability.START)

            # Validate state transition
            if session.status == AgentSessionStatus.RUNNING:
                return session
            self._validate_transition(session.status, AgentSessionStatus.STARTING)

            # Bump generation if restarting a FAILED session
            if session.status == AgentSessionStatus.FAILED:
                new_gen = self._increment_generation(session_id)
                session.generation = new_gen

            session.status = AgentSessionStatus.STARTING
            self.publish_event(AgentEvent(
                event_type=AgentEventType.AGENT_STARTED,
                agent_id=session.agent_id,
                agent_session_id=session.session_id,
                project_id=session.project_id,
                severity=EventSeverity.INFO,
                payload={"transition": "STARTING", "generation": session.generation},
            ))

            try:
                kwargs_copy = dict(kwargs)
                kwargs_copy.setdefault("on_event", self._adapter_event_callback)
                started_session = await adapter.start(session, **kwargs_copy)
                if started_session is not None and isinstance(started_session, AgentSession):
                    session = started_session
                    self._sessions[session.session_id] = session

                self._validate_transition(AgentSessionStatus.STARTING, AgentSessionStatus.RUNNING)
                session.status = AgentSessionStatus.RUNNING
                session.started_at = time.time()
                self.publish_event(AgentEvent(
                    event_type=AgentEventType.AGENT_READY,
                    agent_id=session.agent_id,
                    agent_session_id=session.session_id,
                    project_id=session.project_id,
                    severity=EventSeverity.INFO,
                    payload={"transition": "RUNNING", "generation": session.generation},
                ))
                return session
            except InvalidStateTransitionError:
                raise
            except Exception as exc:
                session.status = AgentSessionStatus.FAILED
                self.publish_event(AgentEvent(
                    event_type=AgentEventType.AGENT_FAILED,
                    agent_id=session.agent_id,
                    agent_session_id=session.session_id,
                    project_id=session.project_id,
                    severity=EventSeverity.ERROR,
                    payload={"error": str(exc)},
                ))
                raise AgentStartFailedError(session_id, str(exc)) from exc

    async def stop_session(self, session_id: str) -> AgentSession:
        """Transition to STOPPING, then STOPPED or FAILED. Uses bounded timeout."""
        session_lock = self._get_session_lock(session_id)
        async with session_lock:
            session = self.get_session(session_id)
            if session.status in (AgentSessionStatus.STOPPED, AgentSessionStatus.FAILED):
                return session
            if session.status == AgentSessionStatus.STOPPING:
                return session  # already in progress

            # Flush any pending buffered output before stopping
            self._flush_output_buffer(session_id)

            adapter = self.registry.get(session.agent_id)
            adapter.require_capability(AgentCapability.STOP)
            self._validate_transition(session.status, AgentSessionStatus.STOPPING)

            session.status = AgentSessionStatus.STOPPING
            self.publish_event(AgentEvent(
                event_type=AgentEventType.AGENT_STATUS_CHANGED,
                agent_id=session.agent_id,
                agent_session_id=session.session_id,
                project_id=session.project_id,
                severity=EventSeverity.INFO,
                payload={"transition": "STOPPING"},
            ))

            try:
                await asyncio.wait_for(adapter.stop(session_id), timeout=STOP_TIMEOUT_SECONDS)
                self._validate_transition(AgentSessionStatus.STOPPING, AgentSessionStatus.STOPPED)
                session.status = AgentSessionStatus.STOPPED
                session.stopped_at = time.time()
                self.publish_event(AgentEvent(
                    event_type=AgentEventType.AGENT_STOPPED,
                    agent_id=session.agent_id,
                    agent_session_id=session.session_id,
                    project_id=session.project_id,
                    severity=EventSeverity.INFO,
                    payload={"transition": "STOPPED"},
                ))
            except asyncio.TimeoutError:
                session.status = AgentSessionStatus.FAILED
                self.publish_event(AgentEvent(
                    event_type=AgentEventType.AGENT_STOPPED,
                    agent_id=session.agent_id,
                    agent_session_id=session.session_id,
                    project_id=session.project_id,
                    severity=EventSeverity.WARNING,
                    payload={"transition": "STOPPED", "timeout": True},
                ))
            except InvalidStateTransitionError:
                raise
            except Exception as exc:
                session.status = AgentSessionStatus.FAILED
                self.publish_event(AgentEvent(
                    event_type=AgentEventType.AGENT_FAILED,
                    agent_id=session.agent_id,
                    agent_session_id=session.session_id,
                    project_id=session.project_id,
                    severity=EventSeverity.ERROR,
                    payload={"error": str(exc)},
                ))
                raise AgentStopFailedError(session_id, str(exc)) from exc
            finally:
                await self.workspace_locks.release_session_locks(session_id)

            return session

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        trace_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Route prompt to the correct session with stale-generation protection."""
        session = self.get_session(session_id)

        # Reject stale requests
        if not self._check_generation(session_id, generation):
            log.warning("Stale generation %s for session %s (expected %s)",
                        generation, session_id, self._session_generations.get(session_id))
            return

        if session.status not in (
            AgentSessionStatus.RUNNING,
            AgentSessionStatus.WAITING,
            AgentSessionStatus.BLOCKED,
        ):
            raise AgentStartFailedError(
                session_id,
                f"Session is {session.status.value}, cannot accept prompt",
            )

        adapter = self.registry.get(session.agent_id)
        adapter.require_capability(AgentCapability.SEND_PROMPT)

        self.publish_event(AgentEvent(
            event_type=AgentEventType.AGENT_PROGRESS,
            agent_id=session.agent_id,
            agent_session_id=session.session_id,
            project_id=session.project_id,
            severity=EventSeverity.INFO,
            payload={"action": "send_prompt", "prompt_length": len(prompt)},
            trace_id=trace_id,
        ))
        try:
            await adapter.send_prompt(session_id, prompt)
        except Exception as exc:
            self.publish_event(AgentEvent(
                event_type=AgentEventType.AGENT_FAILED,
                agent_id=session.agent_id,
                agent_session_id=session.session_id,
                project_id=session.project_id,
                severity=EventSeverity.ERROR,
                payload={"error": str(exc)},
            ))
            raise

    async def get_session_status(self, session_id: str) -> AgentSessionStatus:
        """Retrieve latest status for the session, reconciling with adapter when possible."""
        session = self.get_session(session_id)
        adapter = self.registry.get(session.agent_id)
        if AgentCapability.GET_STATUS in adapter.capabilities():
            try:
                live_status = await adapter.get_status(session_id)
                session.status = live_status
            except Exception:
                pass
        return session.status

    # ------------------------------------------------------------------
    # Event system
    # ------------------------------------------------------------------

    def _adapter_event_callback(self, event: AgentEvent) -> None:
        """Used as the on_event callback for adapters — with coalescing."""
        self._handle_adapter_event(event)

    def _handle_adapter_event(self, event: AgentEvent) -> None:
        """Process an event from an adapter: drop stale events, coalesce outputs, distribute."""
        session_id = event.agent_session_id
        session = self._sessions.get(session_id)

        # 1. Stale-generation and lifecycle rejection (Issue 07)
        if session is not None:
            event_gen = event.payload.get("generation")
            if isinstance(event_gen, int) and event_gen < session.generation:
                log.warning("Dropping stale event from generation %d (current %d)", event_gen, session.generation)
                return

            if session.status in (AgentSessionStatus.STOPPED, AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                if event.event_type in (AgentEventType.AGENT_OUTPUT, AgentEventType.AGENT_PROGRESS):
                    log.debug("Dropping late output from terminated session %s (%s)", session_id, session.status.value)
                    return

        # 2. Output event coalescing and debouncing (Issue 05)
        if event.event_type == AgentEventType.AGENT_OUTPUT:
            self._buffer_output(session_id, event)
            # Notify supervisor callback for activity tracking
            if self._supervisor_callback is not None:
                try:
                    self._supervisor_callback(event)
                except Exception as exc:
                    log.debug("Supervisor callback error: %s", exc)
            return

        # 3. High-priority & lifecycle events: flush pending buffered output first
        self._flush_output_buffer(session_id)

        # Lifecycle events are always published
        if event.event_type in (
            AgentEventType.AGENT_STARTED,
            AgentEventType.AGENT_READY,
            AgentEventType.AGENT_TURN_COMPLETED,
            AgentEventType.AGENT_COMPLETED,
            AgentEventType.AGENT_FAILED,
            AgentEventType.AGENT_STOPPED,
            AgentEventType.AGENT_BLOCKED,
            AgentEventType.AGENT_STATUS_CHANGED,
            AgentEventType.PERMISSION_REQUIRED,
            AgentEventType.FILE_CHANGED,
            AgentEventType.TEST_STARTED,
            AgentEventType.TEST_PASSED,
            AgentEventType.TEST_FAILED,
        ):
            self.publish_event(event)

        # Notify supervisor callback
        if self._supervisor_callback is not None:
            try:
                self._supervisor_callback(event)
            except Exception as exc:
                log.debug("Supervisor callback error: %s", exc)

    def _buffer_output(self, session_id: str, event: AgentEvent) -> None:
        """Buffer and debounce rapid output events, flushing on chunk/size limits or timer."""
        sanitized = self._sanitize_payload(dict(event.payload))
        event = AgentEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            agent_id=event.agent_id,
            agent_session_id=event.agent_session_id,
            project_id=event.project_id,
            timestamp=event.timestamp or time.time(),
            sequence=event.sequence,
            severity=event.severity,
            payload=sanitized,
            trace_id=event.trace_id,
        )

        buf = self._coalesce_buffer.setdefault(session_id, [])
        buf.append(event)

        total_chars = sum(len(str(e.payload.get("text", ""))) for e in buf)
        if len(buf) >= MAX_AGENT_OUTPUT_CHUNKS or total_chars >= 3000:
            self._flush_output_buffer(session_id)
            return

        # Ensure timer is active
        timer = self._coalesce_timers.get(session_id)
        if timer is None or timer.done():
            self._coalesce_timers[session_id] = asyncio.create_task(
                self._coalesce_flush_timer(session_id, OUTPUT_FLUSH_INTERVAL)
            )

    async def _coalesce_flush_timer(self, session_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self._flush_output_buffer(session_id)
        except asyncio.CancelledError:
            pass

    def _flush_output_buffer(self, session_id: str) -> None:
        """Flush any pending buffered output events for a session."""
        buf = self._coalesce_buffer.pop(session_id, None)
        timer = self._coalesce_timers.pop(session_id, None)
        if timer and not timer.done():
            timer.cancel()

        if not buf:
            return

        if len(buf) == 1:
            self.publish_event(buf[0])
            return

        combined_text = "\n".join(
            str(e.payload.get("text", "")) for e in buf if "text" in e.payload
        )
        combined = AgentEvent(
            event_type=AgentEventType.AGENT_OUTPUT,
            agent_id=buf[-1].agent_id,
            agent_session_id=session_id,
            project_id=buf[-1].project_id,
            severity=EventSeverity.INFO,
            payload={
                "stream": buf[-1].payload.get("stream", "stdout"),
                "text": combined_text,
                "coalesced": True,
                "chunks_batched": len(buf),
            },
        )
        self.publish_event(combined)

    def publish_event(self, event: AgentEvent) -> AgentEvent:
        """Publish a canonical event with a monotonic per-session sequence number."""
        session_id = event.agent_session_id
        current_seq = self._event_sequences.get(session_id, 0) + 1
        self._event_sequences[session_id] = current_seq

        sanitized_payload = self._sanitize_payload(dict(event.payload))
        sequenced_event = AgentEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            agent_id=event.agent_id,
            agent_session_id=event.agent_session_id,
            project_id=event.project_id,
            timestamp=event.timestamp or time.time(),
            sequence=current_seq,
            severity=event.severity,
            payload=sanitized_payload,
            trace_id=event.trace_id,
        )

        if session_id in self._sessions:
            self._sessions[session_id].last_event_at = sequenced_event.timestamp
            # Track activity for output/progress/status events
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
            if sequenced_event.event_type in activity_event_types:
                self._sessions[session_id].last_activity_at = sequenced_event.timestamp
            if sequenced_event.event_type == AgentEventType.AGENT_OUTPUT:
                self._sessions[session_id].last_output_at = sequenced_event.timestamp

        subscribers = list(self._event_queues.get(session_id, []))
        for queue in subscribers:
            try:
                queue.put_nowait(sequenced_event)
            except asyncio.QueueFull:
                pass

        return sequenced_event

    def subscribe(self, session_id: str) -> asyncio.Queue[AgentEvent]:
        """Subscribe to live events for a session. Returns a bounded queue."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        self._event_queues.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[AgentEvent]) -> None:
        """Remove a subscriber queue."""
        if session_id in self._event_queues:
            self._event_queues[session_id] = [q for q in self._event_queues[session_id] if q is not queue]
            if not self._event_queues[session_id]:
                del self._event_queues[session_id]

    def cancel_session_tasks(self, session_id: str) -> None:
        """Cancel all background tasks owned by a session."""
        tasks = self._session_background_tasks.get(session_id, [])
        for task in tasks:
            if not task.done():
                task.cancel()
        self._session_background_tasks[session_id] = []

    def register_background_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        """Track a background task owned by a session for cleanup."""
        self._session_background_tasks.setdefault(session_id, []).append(task)

    def set_supervisor_callback(self, callback: Callable[[AgentEvent], None] | None) -> None:
        """Register a callback to receive a copy of every adapter event for supervision."""
        self._supervisor_callback = callback

    # ------------------------------------------------------------------
    # Session cleanup
    # ------------------------------------------------------------------

    def cleanup_session(self, session_id: str) -> None:
        """Remove all state, queues, subscriptions, and task references for a session."""
        self.cancel_session_tasks(session_id)
        self._sessions.pop(session_id, None)
        self._event_queues.pop(session_id, None)
        self._event_sequences.pop(session_id, None)
        self._session_locks.pop(session_id, None)
        self._session_generations.pop(session_id, None)
        self._session_background_tasks.pop(session_id, None)
        self._coalesce_buffer.pop(session_id, None)
        log.debug("Cleaned up session %s", session_id)
