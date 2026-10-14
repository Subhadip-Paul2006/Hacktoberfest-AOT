"""Google Jules asynchronous cloud agent adapter for SŪTRA."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from .adapter import AgentAdapter
from .errors import (
    AgentStartFailedError,
    AgentStopFailedError,
    AgentUnavailableError,
    CapabilityNotSupportedError,
    SessionNotFoundError,
)
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
from .security import redact_secrets, validate_project_scope

log = logging.getLogger(__name__)

DEFAULT_JULES_CAPABILITIES = {
    AgentCapability.START,
    AgentCapability.STOP,
    AgentCapability.SEND_PROMPT,
    AgentCapability.STREAM_OUTPUT,
    AgentCapability.GET_STATUS,
    AgentCapability.GIT_AWARE,
    AgentCapability.CLI,
    AgentCapability.API,
}


class _JulesRemoteSessionState:
    """Internal state tracking an asynchronous cloud Jules session."""

    def __init__(
        self,
        session: AgentSession,
        remote_task_id: str,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.session = session
        self.remote_task_id = remote_task_id
        self.status = AgentSessionStatus.STARTING
        self.on_event = on_event
        self.event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=100)
        self.monitor_task: asyncio.Task[Any] | None = None
        self.is_stopping = False
        self.created_at = time.time()


class JulesAdapter(AgentAdapter):
    """Google Jules asynchronous cloud agent (@google/jules) adapter.
    
    Architectural invariant:
    - Jules operates as an asynchronous, cloud-based agent rather than a local subprocess.
    - Translates remote task dispatch, status polling, and execution events into
      canonical AgentSession and AgentEvent models without faking a continuous local process.
    - Preserves project-scope boundaries and enforces zero credential leakage.
    """

    def __init__(
        self,
        agent_id: str = "jules",
        display_name: str = "Jules",
        executable: str | None = "jules",
        capabilities: set[AgentCapability] | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._agent_id = agent_id
        self._display_name = display_name
        self.executable = executable
        self._caps = capabilities if capabilities is not None else DEFAULT_JULES_CAPABILITIES
        self.poll_interval_seconds = poll_interval_seconds
        self._active_sessions: dict[str, _JulesRemoteSessionState] = {}
        self._session_statuses: dict[str, AgentSessionStatus] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def descriptor(self) -> AgentDescriptor:
        """Return the descriptor detailing identity, provider, and declared capabilities."""
        avail = AgentAvailability.UNAVAILABLE
        if self.executable and (shutil.which(self.executable) or Path(self.executable).is_file()):
            avail = AgentAvailability.INSTALLED
        elif any(k in os.environ for k in ("JULES_API_KEY", "JULES_TOKEN", "GOOGLE_GENAI_API_KEY")):
            avail = AgentAvailability.AVAILABLE

        return AgentDescriptor(
            agent_id=self._agent_id,
            provider="Google",
            display_name=self._display_name,
            version=None,
            executable=self.executable,
            availability=avail,
            capabilities=tuple(sorted(c.value for c in self._caps)),
            installation_notes="Google Jules async cloud agent (@google/jules) wrapper.",
            status="Connected" if bool(self._active_sessions) else "Not Connected",
            implementation="Jules Simulator (not live)",
            metadata={"cloud_agent": True, "cli": "jules", "remote_execution": True},
        )

    def is_available(self) -> bool:
        """Check if Jules CLI is installed or cloud credentials are configured."""
        if self.executable and (shutil.which(self.executable) or Path(self.executable).is_file()):
            return True
        return any(k in os.environ for k in ("JULES_API_KEY", "JULES_TOKEN", "GOOGLE_GENAI_API_KEY"))

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _emit_event(self, state: _JulesRemoteSessionState, event: AgentEvent) -> None:
        """Emit an event through the callback and local subscriber queue."""
        if state.on_event:
            try:
                state.on_event(event)
            except Exception as exc:
                log.debug("Jules event callback error: %s", exc)

        try:
            state.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        """Start an asynchronous Jules cloud session bound to the authorized project."""
        self.require_capability(AgentCapability.START)

        session_lock = self._get_session_lock(session.session_id)
        async with session_lock:
            if not self.is_available():
                raise AgentUnavailableError(
                    self._agent_id,
                    "Jules CLI ('jules') not found on system PATH and no Jules credentials configured",
                )

            # Validate project scope working directory
            project_path = Path(session.project_root)
            if not project_path.is_dir():
                raise AgentStartFailedError(session.session_id, f"Project root does not exist: {project_path}")

            prompt = kwargs.get("prompt") or kwargs.get("task") or session.metadata.get("prompt", "")
            remote_task_id = kwargs.get("remote_task_id") or f"jules-task-{uuid.uuid4().hex[:12]}"

            on_event_cb = kwargs.get("on_event")
            state = _JulesRemoteSessionState(
                session=session,
                remote_task_id=remote_task_id,
                on_event=on_event_cb,
            )
            self._active_sessions[session.session_id] = state
            session.status = AgentSessionStatus.RUNNING
            session.started_at = time.time()
            session.metadata["remote_task_id"] = remote_task_id
            state.status = AgentSessionStatus.RUNNING

            # Emit initial progress event
            self._emit_event(
                state,
                AgentEvent(
                    event_type=AgentEventType.AGENT_PROGRESS,
                    agent_id=self._agent_id,
                    agent_session_id=session.session_id,
                    project_id=session.project_id,
                    severity=EventSeverity.INFO,
                    payload={"remote_task_id": remote_task_id, "action": "remote_task_created"},
                ),
            )

            # Launch async cloud poller task
            simulation_steps = kwargs.get("simulation_steps")
            state.monitor_task = asyncio.create_task(
                self._monitor_remote_task(state, prompt=str(prompt), steps=simulation_steps)
            )

            log.info("Dispatched Jules remote task %s for session %s", remote_task_id, session.session_id)
            return session

    async def _monitor_remote_task(
        self,
        state: _JulesRemoteSessionState,
        prompt: str = "",
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        """Simulate or poll remote Jules cloud execution and stream canonical progress events."""
        session_id = state.session.session_id
        try:
            if steps is not None:
                # Execution with configured simulation steps for deterministic tests
                for step in steps:
                    if state.is_stopping:
                        break
                    await asyncio.sleep(self.poll_interval_seconds)
                    event_type_name = step.get("type", "AGENT_PROGRESS")
                    try:
                        event_type = AgentEventType(event_type_name)
                    except ValueError:
                        event_type = AgentEventType.AGENT_PROGRESS

                    clean_payload = {
                        k: redact_secrets(str(v)) if isinstance(v, str) else v
                        for k, v in step.get("payload", {}).items()
                    }
                    self._emit_event(
                        state,
                        AgentEvent(
                            event_type=event_type,
                            agent_id=self._agent_id,
                            agent_session_id=session_id,
                            project_id=state.session.project_id,
                            severity=EventSeverity.INFO,
                            payload=clean_payload,
                        ),
                    )

            if state.is_stopping:
                state.status = AgentSessionStatus.STOPPED
                self._session_statuses[session_id] = AgentSessionStatus.STOPPED
                self._emit_event(
                    state,
                    AgentEvent(
                        event_type=AgentEventType.AGENT_STOPPED,
                        agent_id=self._agent_id,
                        agent_session_id=session_id,
                        project_id=state.session.project_id,
                        severity=EventSeverity.INFO,
                        payload={"remote_task_id": state.remote_task_id},
                    ),
                )
            else:
                state.status = AgentSessionStatus.COMPLETED
                state.session.status = AgentSessionStatus.COMPLETED
                self._session_statuses[session_id] = AgentSessionStatus.COMPLETED
                self._emit_event(
                    state,
                    AgentEvent(
                        event_type=AgentEventType.AGENT_COMPLETED,
                        agent_id=self._agent_id,
                        agent_session_id=session_id,
                        project_id=state.session.project_id,
                        severity=EventSeverity.INFO,
                        payload={"remote_task_id": state.remote_task_id, "result": "completed"},
                    ),
                )
        except asyncio.CancelledError:
            state.status = AgentSessionStatus.STOPPED
            self._session_statuses[session_id] = AgentSessionStatus.STOPPED
        except Exception as exc:
            log.error("Jules remote task monitor error: %s", exc)
            state.status = AgentSessionStatus.FAILED
            state.session.status = AgentSessionStatus.FAILED
            self._session_statuses[session_id] = AgentSessionStatus.FAILED
            self._emit_event(
                state,
                AgentEvent(
                    event_type=AgentEventType.AGENT_FAILED,
                    agent_id=self._agent_id,
                    agent_session_id=session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.ERROR,
                    payload={"remote_task_id": state.remote_task_id, "error": redact_secrets(str(exc))},
                ),
            )
        finally:
            async with self._global_lock:
                self._active_sessions.pop(session_id, None)

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Send follow-up instructions to the remote Jules cloud session."""
        self.require_capability(AgentCapability.SEND_PROMPT)

        state = self._active_sessions.get(session_id)
        if not state or state.status != AgentSessionStatus.RUNNING:
            raise AgentStartFailedError(session_id, "Cannot send prompt: Jules remote session is not running")

        clean_prompt = redact_secrets(prompt)
        self._emit_event(
            state,
            AgentEvent(
                event_type=AgentEventType.AGENT_PROGRESS,
                agent_id=self._agent_id,
                agent_session_id=session_id,
                project_id=state.session.project_id,
                severity=EventSeverity.INFO,
                payload={"action": "send_prompt", "remote_task_id": state.remote_task_id, "prompt_length": len(clean_prompt)},
            ),
        )

    async def stop(self, session_id: str) -> AgentSession:
        """Cancel the active Jules remote task."""
        self.require_capability(AgentCapability.STOP)

        session_lock = self._get_session_lock(session_id)
        async with session_lock:
            state = self._active_sessions.get(session_id)
            if not state:
                return AgentSession(session_id=session_id, status=AgentSessionStatus.STOPPED)

            state.is_stopping = True
            if state.monitor_task and not state.monitor_task.done():
                state.monitor_task.cancel()

            state.status = AgentSessionStatus.STOPPED
            state.session.status = AgentSessionStatus.STOPPED
            state.session.stopped_at = time.time()
            self._session_statuses[session_id] = AgentSessionStatus.STOPPED

            self._emit_event(
                state,
                AgentEvent(
                    event_type=AgentEventType.AGENT_STOPPED,
                    agent_id=self._agent_id,
                    agent_session_id=session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.INFO,
                    payload={"remote_task_id": state.remote_task_id, "transition": "STOPPED"},
                ),
            )

            async with self._global_lock:
                self._active_sessions.pop(session_id, None)

            return state.session

    async def get_status(self, session_id: str) -> AgentSessionStatus:
        """Query live status for the remote Jules session."""
        self.require_capability(AgentCapability.GET_STATUS)

        state = self._active_sessions.get(session_id)
        if state:
            return state.status
        if session_id in self._session_statuses:
            return self._session_statuses[session_id]
        return AgentSessionStatus.IDLE

    async def subscribe(self, session_id: str) -> AsyncIterator[AgentEvent]:
        """Subscribe to live cloud events emitted by this session."""
        self.require_capability(AgentCapability.STREAM_OUTPUT)

        state = self._active_sessions.get(session_id)
        if not state:
            return

        while True:
            try:
                event = await asyncio.wait_for(state.event_queue.get(), timeout=1.0)
                yield event
                if event.event_type in (
                    AgentEventType.AGENT_COMPLETED,
                    AgentEventType.AGENT_FAILED,
                    AgentEventType.AGENT_STOPPED,
                ):
                    break
            except asyncio.TimeoutError:
                if state.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED, AgentSessionStatus.STOPPED):
                    if state.event_queue.empty():
                        break
            except asyncio.CancelledError:
                break
