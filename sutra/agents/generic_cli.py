"""Generic sandboxed CLI adapter for SŪTRA."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any

from .adapter import AgentAdapter
from .errors import (
    AgentProcessFailedError,
    AgentStartFailedError,
    AgentStopFailedError,
    AgentTimeoutError,
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
from .security import SafeProcessConfig, redact_secrets

log = logging.getLogger(__name__)

MAX_OUTPUT_CHUNK_CHARS = 4096
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_GRACEFUL_STOP_TIMEOUT = 3.0
MAX_OUTPUT_HISTORY_LINES = 500


class _SessionProcessState:
    """Internal runtime state tracking an active subprocess for one session."""

    def __init__(
        self,
        session: AgentSession,
        process: asyncio.subprocess.Process,
        on_event: Callable[[AgentEvent], None] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.session = session
        self.process = process
        self.on_event = on_event
        self.timeout_seconds = timeout_seconds
        self.is_stopping = False
        self.started_at = time.time()
        self.event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=100)
        self.output_history: deque[str] = deque(maxlen=MAX_OUTPUT_HISTORY_LINES)
        self.reader_tasks: list[asyncio.Task[Any]] = []
        self.watchdog_task: asyncio.Task[Any] | None = None
        self.lock = asyncio.Lock()


class GenericCLIAdapter(AgentAdapter):
    """Secure, sandboxed adapter executing commands as discrete subprocesses.
    
    Adheres strictly to the SŪTRA security model:
    - Never uses shell=True, os.system, or string concatenation.
    - Uses discrete argument arrays with SafeProcessConfig.
    - Sanitizes environment variables and redacts secrets from logs and events.
    - Manages bounded streaming output and deterministic lifecycle.
    - Enforces strict session isolation with no global mutable process state.
    """

    def __init__(
        self,
        agent_id: str = "generic_cli",
        display_name: str = "Generic CLI",
        executable: str | None = None,
        default_args: list[str] | None = None,
        capabilities: set[AgentCapability] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        graceful_stop_timeout: float = DEFAULT_GRACEFUL_STOP_TIMEOUT,
    ) -> None:
        self._agent_id = agent_id
        self._display_name = display_name
        self.executable = executable
        self.default_args = list(default_args or [])
        self.timeout_seconds = timeout_seconds
        self.graceful_stop_timeout = graceful_stop_timeout
        self._caps = capabilities if capabilities is not None else {
            AgentCapability.START,
            AgentCapability.STOP,
            AgentCapability.SEND_PROMPT,
            AgentCapability.STREAM_OUTPUT,
            AgentCapability.GET_STATUS,
            AgentCapability.CLI,
        }
        self._active_sessions: dict[str, _SessionProcessState] = {}
        self._session_statuses: dict[str, AgentSessionStatus] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def descriptor(self) -> AgentDescriptor:
        """Return the descriptor detailing identity, provider, and declared capabilities."""
        avail = AgentAvailability.AVAILABLE
        if self.executable:
            found = shutil.which(self.executable) or (Path(self.executable).is_file())
            avail = AgentAvailability.INSTALLED if found else AgentAvailability.UNAVAILABLE

        return AgentDescriptor(
            agent_id=self._agent_id,
            provider="Generic",
            display_name=self._display_name,
            version=self.detect_version(),
            executable=self.executable,
            availability=avail,
            capabilities=tuple(sorted(c.value for c in self._caps)),
            installation_notes="Generic sandboxed command-line process adapter.",
            status="Connected" if bool(self._active_sessions) else "Not Connected",
            implementation="Generic CLI Adapter",
        )

    def is_available(self) -> bool:
        """Check if the adapter's executable is present on the host."""
        if not self.executable:
            return True
        return bool(shutil.which(self.executable) or Path(self.executable).is_file())

    def detect_version(self) -> str | None:
        """Safely probe version if an executable is specified."""
        if not self.executable:
            return None
        resolved = shutil.which(self.executable)
        if not resolved:
            return None
        return None

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _emit_event(self, state: _SessionProcessState, event: AgentEvent) -> None:
        """Emit an event through the callback and local subscriber queue."""
        if state.on_event:
            try:
                state.on_event(event)
            except Exception as exc:
                log.debug("Event callback error: %s", exc)

        try:
            state.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        """Start a new sandboxed subprocess session within the verified project root."""
        self.require_capability(AgentCapability.START)

        session_lock = self._get_session_lock(session.session_id)
        async with session_lock:
            if session.session_id in self._active_sessions:
                existing = self._active_sessions[session.session_id]
                if existing.process.returncode is None:
                    return session

            # 1. Resolve executable and command arguments
            raw_exe = (
                kwargs.get("executable")
                or self.executable
                or session.metadata.get("executable")
            )
            if not raw_exe:
                raise AgentStartFailedError(session.session_id, "No executable specified for Generic CLI")

            resolved_exe = shutil.which(str(raw_exe))
            if not resolved_exe:
                candidate = Path(str(raw_exe))
                if candidate.is_file():
                    resolved_exe = str(candidate.resolve())
            if not resolved_exe:
                raise AgentUnavailableError(self._agent_id, f"Executable '{raw_exe}' not found on system PATH")

            passed_args = kwargs.get("args")
            if passed_args is None:
                passed_args = session.metadata.get("args", self.default_args)
            if isinstance(passed_args, str):
                raise ValueError("Subprocess args must be a list of strings, not a raw shell command string")
            cmd_args = [resolved_exe] + [str(a) for a in (passed_args or [])]

            # 2. Validate project working directory
            cwd = Path(session.project_root)
            if not cwd.is_dir():
                raise AgentStartFailedError(session.session_id, f"Project directory does not exist: {cwd}")

            # 3. Build safe process configuration with sanitized environment
            timeout_val = float(kwargs.get("timeout_seconds") or self.timeout_seconds)
            custom_env = dict(kwargs.get("env") or {})
            try:
                cfg = SafeProcessConfig(
                    args=cmd_args,
                    cwd=cwd,
                    env=custom_env,
                    timeout_seconds=timeout_val,
                )
            except (ValueError, TypeError) as exc:
                raise AgentStartFailedError(session.session_id, f"Invalid process configuration: {exc}") from exc

            sanitized_env = cfg.build_sanitized_env()

            # 4. Launch subprocess asynchronously (never shell=True)
            try:
                process_group_options: dict[str, Any] = {}
                if os.name != "nt":
                    process_group_options["start_new_session"] = True
                process = await asyncio.create_subprocess_exec(
                    *cfg.args,
                    cwd=str(cfg.cwd),
                    env=sanitized_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **process_group_options,
                )
            except Exception as exc:
                log.error("Failed to spawn process %s: %s", cfg.args, exc)
                raise AgentStartFailedError(session.session_id, f"Process execution failed: {exc}") from exc

            on_event_cb = kwargs.get("on_event")
            state = _SessionProcessState(
                session=session,
                process=process,
                on_event=on_event_cb,
                timeout_seconds=timeout_val,
            )
            self._active_sessions[session.session_id] = state

            # 5. Launch background stream readers for stdout and stderr
            if process.stdout:
                state.reader_tasks.append(
                    asyncio.create_task(self._stream_reader(state, process.stdout, "stdout"))
                )
            if process.stderr:
                state.reader_tasks.append(
                    asyncio.create_task(self._stream_reader(state, process.stderr, "stderr"))
                )

            # 6. Launch watchdog task for timeout and completion handling
            state.watchdog_task = asyncio.create_task(self._process_watchdog(state))

            session.status = AgentSessionStatus.RUNNING
            session.started_at = time.time()
            log.info("Started generic CLI process for session %s (PID %s)", session.session_id, process.pid)
            return session

    async def _stream_reader(
        self,
        state: _SessionProcessState,
        stream: asyncio.StreamReader,
        stream_name: str,
    ) -> None:
        """Asynchronously read stream lines, redacting secrets and emitting bounded events."""
        in_private_key = False
        try:
            while not stream.at_eof():
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue

                # Handle multi-line private key blocks in line-by-line streaming
                if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
                    in_private_key = True
                    sanitized_text = "[REDACTED PRIVATE KEY]"
                elif in_private_key:
                    if "-----END " in line and "PRIVATE KEY-----" in line:
                        in_private_key = False
                    sanitized_text = "[REDACTED PRIVATE KEY]"
                else:
                    # Redact sensitive tokens and secrets
                    sanitized_text = redact_secrets(line)

                if len(sanitized_text) > MAX_OUTPUT_CHUNK_CHARS:
                    sanitized_text = sanitized_text[:MAX_OUTPUT_CHUNK_CHARS] + " [TRUNCATED]"

                state.output_history.append(f"[{stream_name}] {sanitized_text}")

                event = AgentEvent(
                    event_type=AgentEventType.AGENT_OUTPUT,
                    agent_id=state.session.agent_id,
                    agent_session_id=state.session.session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.INFO if stream_name == "stdout" else EventSeverity.WARNING,
                    payload={"stream": stream_name, "text": sanitized_text},
                )
                self._emit_event(state, event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("Stream reader exception for session %s: %s", state.session.session_id, exc)

    async def _process_watchdog(self, state: _SessionProcessState) -> None:
        """Monitor process execution, applying timeouts and generating termination events."""
        session_id = state.session.session_id
        try:
            try:
                returncode = await asyncio.wait_for(
                    state.process.wait(),
                    timeout=state.timeout_seconds,
                )
            except asyncio.TimeoutError:
                log.warning("Session %s exceeded timeout of %ss; terminating", session_id, state.timeout_seconds)
                state.is_stopping = True
                await self._terminate_process(state.process)
                state.session.status = AgentSessionStatus.FAILED
                self._session_statuses[session_id] = AgentSessionStatus.FAILED
                event = AgentEvent(
                    event_type=AgentEventType.AGENT_FAILED,
                    agent_id=state.session.agent_id,
                    agent_session_id=session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.ERROR,
                    payload={"error": f"Process execution timed out after {state.timeout_seconds}s"},
                )
                self._emit_event(state, event)
                return

            # Wait briefly for remaining stream data to drain
            if state.reader_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*state.reader_tasks, return_exceptions=True),
                        timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    for t in state.reader_tasks:
                        if not t.done():
                            t.cancel()

            if state.is_stopping:
                event = AgentEvent(
                    event_type=AgentEventType.AGENT_STOPPED,
                    agent_id=state.session.agent_id,
                    agent_session_id=session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.INFO,
                    payload={"exit_code": returncode},
                )
                self._emit_event(state, event)
                state.session.status = AgentSessionStatus.STOPPED
                self._session_statuses[session_id] = AgentSessionStatus.STOPPED
            elif returncode == 0:
                event = AgentEvent(
                    event_type=AgentEventType.AGENT_COMPLETED,
                    agent_id=state.session.agent_id,
                    agent_session_id=session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.INFO,
                    payload={"exit_code": returncode},
                )
                self._emit_event(state, event)
                state.session.status = AgentSessionStatus.COMPLETED
                self._session_statuses[session_id] = AgentSessionStatus.COMPLETED
            else:
                recent_err = "\n".join(list(state.output_history)[-10:])
                event = AgentEvent(
                    event_type=AgentEventType.AGENT_FAILED,
                    agent_id=state.session.agent_id,
                    agent_session_id=session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.ERROR,
                    payload={"exit_code": returncode, "recent_output": recent_err},
                )
                self._emit_event(state, event)
                state.session.status = AgentSessionStatus.FAILED
                self._session_statuses[session_id] = AgentSessionStatus.FAILED
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("Watchdog error for session %s: %s", session_id, exc)
        finally:
            async with self._global_lock:
                self._active_sessions.pop(session_id, None)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        """Cleanly terminate process tree with graceful timeout, escalating to force kill only if needed."""
        if process.returncode is not None:
            return

        pid = process.pid
        if pid is None or pid <= 4:
            return

        # 1. Attempt graceful shutdown first
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=self.graceful_stop_timeout)
        except asyncio.TimeoutError:
            log.warning("Process PID %s did not terminate gracefully; force killing process tree", pid)
            # 2. Force terminate child tree
            if os.name == "nt":
                try:
                    kill_proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(kill_proc.wait(), timeout=5.0)
                except Exception as exc:
                    log.debug("taskkill error for PID %s: %s", pid, exc)
            else:
                try:
                    import signal
                    sigkill = getattr(signal, "SIGKILL", None)
                    killpg = getattr(os, "killpg", None)
                    getpgid = getattr(os, "getpgid", None)
                    if sigkill is not None and callable(killpg) and callable(getpgid):
                        killpg(getpgid(pid), sigkill)
                except Exception:
                    pass

            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass

    async def stop(self, session_id: str) -> AgentSession:
        """Gracefully stop a running session and clean up process references."""
        self.require_capability(AgentCapability.STOP)

        session_lock = self._get_session_lock(session_id)
        async with session_lock:
            state = self._active_sessions.get(session_id)
            if not state:
                return AgentSession(session_id=session_id, status=AgentSessionStatus.STOPPED)

            state.is_stopping = True
            if state.watchdog_task and not state.watchdog_task.done():
                state.watchdog_task.cancel()

            # Close stdin
            if state.process.stdin and not state.process.stdin.is_closing():
                try:
                    state.process.stdin.close()
                except Exception:
                    pass

            await self._terminate_process(state.process)

            # Cancel remaining readers
            for task in state.reader_tasks:
                if not task.done():
                    task.cancel()

            state.session.status = AgentSessionStatus.STOPPED
            state.session.stopped_at = time.time()
            self._session_statuses[session_id] = AgentSessionStatus.STOPPED

            event = AgentEvent(
                event_type=AgentEventType.AGENT_STOPPED,
                agent_id=state.session.agent_id,
                agent_session_id=session_id,
                project_id=state.session.project_id,
                severity=EventSeverity.INFO,
                payload={"transition": "STOPPED", "exit_code": state.process.returncode},
            )
            self._emit_event(state, event)

            async with self._global_lock:
                self._active_sessions.pop(session_id, None)

            return state.session

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Send prompt to the running process over standard input."""
        self.require_capability(AgentCapability.SEND_PROMPT)

        state = self._active_sessions.get(session_id)
        if not state or state.process.returncode is not None or not state.process.stdin:
            raise AgentProcessFailedError(
                self._agent_id,
                exit_code=state.process.returncode if state else None,
                stderr="Cannot send prompt: session process is not running or stdin is closed",
            )

        try:
            encoded = prompt.encode("utf-8")
            if not encoded.endswith(b"\n"):
                encoded += b"\n"
            state.process.stdin.write(encoded)
            await state.process.stdin.drain()

            event = AgentEvent(
                event_type=AgentEventType.AGENT_PROGRESS,
                agent_id=state.session.agent_id,
                agent_session_id=session_id,
                project_id=state.session.project_id,
                severity=EventSeverity.INFO,
                payload={"action": "send_prompt", "prompt_length": len(prompt)},
            )
            self._emit_event(state, event)
        except Exception as exc:
            raise AgentProcessFailedError(
                self._agent_id,
                exit_code=state.process.returncode,
                stderr=f"Failed writing to stdin: {exc}",
            ) from exc

    async def get_status(self, session_id: str) -> AgentSessionStatus:
        """Query live runtime status for the session."""
        self.require_capability(AgentCapability.GET_STATUS)

        state = self._active_sessions.get(session_id)
        if state:
            if state.process.returncode is None:
                return AgentSessionStatus.RUNNING
            if state.is_stopping:
                return AgentSessionStatus.STOPPED
            if state.process.returncode == 0:
                return AgentSessionStatus.COMPLETED
            return AgentSessionStatus.FAILED

        if session_id in self._session_statuses:
            return self._session_statuses[session_id]

        return AgentSessionStatus.IDLE

    async def subscribe(self, session_id: str) -> AsyncIterator[AgentEvent]:
        """Subscribe to live events emitted by this session."""
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
                if state.process.returncode is not None and state.event_queue.empty():
                    break
            except asyncio.CancelledError:
                break
