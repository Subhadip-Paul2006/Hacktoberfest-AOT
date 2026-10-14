"""Google Antigravity agent adapter for SŪTRA."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import shutil
from typing import Any

from .errors import AgentUnavailableError
from .generic_cli import GenericCLIAdapter
from .models import (
    AgentAvailability,
    AgentCapability,
    AgentDescriptor,
    AgentEvent,
    AgentEventType,
    AgentSession,
    EventSeverity,
)
from .errors import AgentProcessFailedError, CapabilityNotSupportedError
from .generic_cli import MAX_OUTPUT_CHUNK_CHARS, GenericCLIAdapter, _SessionProcessState
from .security import redact_secrets

log = logging.getLogger(__name__)
MAX_FINAL_RESPONSE_CHARS = 32768

DEFAULT_ANTIGRAVITY_CAPABILITIES = {
    AgentCapability.START,
    AgentCapability.STOP,
    AgentCapability.SEND_PROMPT,
    AgentCapability.STREAM_OUTPUT,
    AgentCapability.GET_STATUS,
    AgentCapability.PLAN_EVENTS,
    AgentCapability.CLI,
    AgentCapability.HEADLESS,
}


class AntigravityAdapter(GenericCLIAdapter):
    """Official Google Antigravity CLI (`agy`) and SDK adapter.
    
    Adheres strictly to the SŪTRA security and lifecycle model:
    - Never uses shell=True or string concatenation.
    - Probes the `agy` CLI binary safely via PATH.
    - Honest capability declaration: controls headless CLI and SDK workflows without
      claiming remote IDE GUI window manipulation.
    - Confined strictly to validated project scope directory.
    - Redacts credentials and sensitive tokens from all output streams and events.
    """

    def __init__(
        self,
        agent_id: str = "antigravity",
        display_name: str = "Antigravity",
        executable: str | None = "agy",
        capabilities: set[AgentCapability] | None = None,
        timeout_seconds: float = 300.0,
        graceful_stop_timeout: float = 3.0,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            display_name=display_name,
            executable=executable,
            capabilities=capabilities if capabilities is not None else DEFAULT_ANTIGRAVITY_CAPABILITIES,
            timeout_seconds=timeout_seconds,
            graceful_stop_timeout=graceful_stop_timeout,
        )

    def descriptor(self) -> AgentDescriptor:
        """Return the descriptor detailing identity, provider, and declared capabilities."""
        avail = AgentAvailability.UNAVAILABLE
        if self.executable:
            found = shutil.which(self.executable) or (Path(self.executable).is_file())
            avail = AgentAvailability.INSTALLED if found else AgentAvailability.UNAVAILABLE

        return AgentDescriptor(
            agent_id=self._agent_id,
            provider="Google",
            display_name=self._display_name,
            version=self.detect_version(),
            executable=self.executable,
            availability=avail,
            capabilities=tuple(sorted(c.value for c in self._caps)),
            installation_notes="Official Antigravity CLI (agy) and Python SDK integration.",
            status="Connected" if bool(self._active_sessions) else "Not Connected",
            implementation="Antigravity Adapter",
            metadata={"cli": "agy", "headless": True, "gui_automation": False},
        )

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        """Start a structured multi-turn Antigravity print-mode session.

        The provider's stream-json input protocol is used only for the default
        launch. Explicit custom arguments retain their caller-defined behavior.
        Headless tool permission requests remain governed by Antigravity policy;
        this adapter does not turn terminal text into approval requests.
        """
        self.require_capability(AgentCapability.START)

        if not self.is_available():
            raise AgentUnavailableError(
                self._agent_id,
                f"Antigravity CLI executable '{self.executable}' not found on system PATH",
            )

        custom_args = kwargs.get("args")
        prompt = kwargs.get("prompt") or session.metadata.get("prompt")

        if custom_args is not None:
            final_args = list(custom_args)
        else:
            final_args = [
                "--print",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
            ]

        kwargs_copy = dict(kwargs)
        kwargs_copy["args"] = final_args
        kwargs_copy.setdefault("executable", self.executable)
        session.metadata["interaction_protocol"] = (
            "antigravity-stream-json" if custom_args is None else "custom-args"
        )
        # GenericCLIAdapter's watchdog provides the bounded session lifetime.
        # Keep its default unless the caller explicitly requests another bound.
        started = await super().start(session, **kwargs_copy)
        if custom_args is None and prompt:
            try:
                await self.send_prompt(session.session_id, str(prompt))
            except Exception:
                await super().stop(session.session_id)
                raise
        return started

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Deliver one user turn using Antigravity's documented stream-json input."""
        self.require_capability(AgentCapability.SEND_PROMPT)
        state = self._active_sessions.get(session_id)
        if state and state.session.metadata.get("interaction_protocol") != "antigravity-stream-json":
            raise CapabilityNotSupportedError(
                self._agent_id,
                AgentCapability.SEND_PROMPT.value,
                details={"reason": "The active session was started with custom arguments."},
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must not be empty")
        if len(prompt) > 32768:
            raise ValueError("Prompt exceeds maximum length limit")

        if not state or state.process.returncode is not None or not state.process.stdin:
            raise AgentProcessFailedError(
                self._agent_id,
                exit_code=state.process.returncode if state else None,
                stderr="Cannot send prompt: Antigravity stream session is not running",
            )

        request = {"event": "user", "message": {"content": prompt}}
        data = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with state.lock:
            if state.process.returncode is not None or state.process.stdin.is_closing():
                raise AgentProcessFailedError(
                    self._agent_id,
                    exit_code=state.process.returncode,
                    stderr="Cannot send prompt: Antigravity stream session is no longer accepting input",
                )
            state.process.stdin.write(data)
            await state.process.stdin.drain()

    async def _stream_reader(
        self,
        state: _SessionProcessState,
        stream: asyncio.StreamReader,
        stream_name: str,
    ) -> None:
        """Translate documented Antigravity NDJSON response events to agent output."""
        in_private_key = False
        try:
            while not stream.at_eof():
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue

                if stream_name != "stdout":
                    safe_text, in_private_key = self._redact_stream_line(line, in_private_key)
                    self._emit_event(state, AgentEvent(
                        event_type=AgentEventType.AGENT_OUTPUT,
                        agent_id=state.session.agent_id,
                        agent_session_id=state.session.session_id,
                        project_id=state.session.project_id,
                        severity=EventSeverity.WARNING,
                        payload={"stream": stream_name, "text": safe_text[:MAX_OUTPUT_CHUNK_CHARS]},
                    ))
                    continue

                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    # Preserve diagnostics, but never infer approval from text.
                    safe_text, in_private_key = self._redact_stream_line(line, in_private_key)
                    self._emit_event(state, AgentEvent(
                        event_type=AgentEventType.AGENT_OUTPUT,
                        agent_id=state.session.agent_id,
                        agent_session_id=state.session.session_id,
                        project_id=state.session.project_id,
                        severity=EventSeverity.INFO,
                        payload={"stream": stream_name, "text": safe_text[:MAX_OUTPUT_CHUNK_CHARS]},
                    ))
                    continue

                if not isinstance(event, dict):
                    continue
                if event.get("event") == "init":
                    conversation_id = event.get("conversation_id")
                    if isinstance(conversation_id, str) and conversation_id:
                        state.session.metadata["provider_conversation_id"] = conversation_id[:256]
                    continue

                if event.get("event") == "step_update":
                    step = event.get("step_update")
                    if not isinstance(step, dict) or step.get("step_type") != "agent_response":
                        continue
                    text_delta = step.get("text_delta")
                    if isinstance(text_delta, str) and text_delta:
                        safe_text = redact_secrets(text_delta)
                        state.output_history.append(f"[stdout] {safe_text}")
                        self._emit_event(state, AgentEvent(
                            event_type=AgentEventType.AGENT_OUTPUT,
                            agent_id=state.session.agent_id,
                            agent_session_id=state.session.session_id,
                            project_id=state.session.project_id,
                            severity=EventSeverity.INFO,
                            payload={"stream": "stdout", "text": safe_text[:MAX_OUTPUT_CHUNK_CHARS]},
                        ))
                    continue

                if event.get("event") == "result":
                    result = event.get("result")
                    if not isinstance(result, dict):
                        continue
                    response = result.get("response")
                    if isinstance(response, str):
                        state.session.metadata["final_response"] = redact_secrets(response)[:MAX_FINAL_RESPONSE_CHARS]
                    conversation_id = result.get("conversation_id")
                    if isinstance(conversation_id, str) and conversation_id:
                        state.session.metadata["provider_conversation_id"] = conversation_id[:256]
                    state.session.metadata["last_turn_status"] = str(result.get("status", "UNKNOWN"))[:64]
                    self._emit_event(state, AgentEvent(
                        event_type=AgentEventType.AGENT_TURN_COMPLETED,
                        agent_id=state.session.agent_id,
                        agent_session_id=state.session.session_id,
                        project_id=state.session.project_id,
                        severity=EventSeverity.INFO,
                        payload={"turn_status": state.session.metadata["last_turn_status"]},
                    ))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("Antigravity stream reader error for session %s: %s", state.session.session_id, exc)

    @staticmethod
    def _redact_stream_line(line: str, in_private_key: bool) -> tuple[str, bool]:
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            return "[REDACTED PRIVATE KEY]", True
        if in_private_key:
            if "-----END " in line and "PRIVATE KEY-----" in line:
                return "[REDACTED PRIVATE KEY]", False
            return "[REDACTED PRIVATE KEY]", True
        return redact_secrets(line), False
