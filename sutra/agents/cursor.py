"""Cursor agent adapter for SŪTRA."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import os
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
from .security import redact_secrets

log = logging.getLogger(__name__)

DEFAULT_CURSOR_CAPABILITIES = {
    AgentCapability.START,
    AgentCapability.STOP,
    AgentCapability.STREAM_OUTPUT,
    AgentCapability.GET_STATUS,
    AgentCapability.HEADLESS,
    AgentCapability.CLI,
}


class CursorAdapter(GenericCLIAdapter):
    """Cursor agent CLI adapter (`agent -p --force`) with structured output parsing.
    
    Adheres strictly to the SŪTRA security and lifecycle model:
    - Never uses shell=True or string concatenation.
    - Resolves the Cursor CLI executable (`agent`) safely from PATH or explicit configuration.
    - Honors project-scoped working directory and environment sanitization.
    - Captures stdout/stderr, parsing JSON telemetry when present.
    - Redacts API keys (e.g. CURSOR_API_KEY) and secrets from events and logs.
    """

    def __init__(
        self,
        agent_id: str = "cursor",
        display_name: str = "Cursor",
        executable: str | None = "agent",
        capabilities: set[AgentCapability] | None = None,
        timeout_seconds: float = 300.0,
        graceful_stop_timeout: float = 3.0,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            display_name=display_name,
            executable=executable,
            capabilities=capabilities if capabilities is not None else DEFAULT_CURSOR_CAPABILITIES,
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
            provider="Cursor / Anysphere",
            display_name=self._display_name,
            version=self.detect_version(),
            executable=self.executable,
            availability=avail,
            capabilities=tuple(sorted(c.value for c in self._caps)),
            installation_notes="Cursor Agent CLI (agent -p --force) headless runner.",
            status="Connected" if bool(self._active_sessions) else "Not Connected",
            implementation="Cursor CLI Adapter",
            metadata={"cli": "agent", "headless": True},
        )

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        """Start a Cursor agent session within the validated project scope."""
        self.require_capability(AgentCapability.START)

        # Check availability before launching
        if not self.is_available():
            raise AgentUnavailableError(
                self._agent_id,
                f"Cursor agent CLI executable '{self.executable}' not found on system PATH",
            )

        # Build discrete arguments for Cursor agent
        # Cursor agent headless: agent -p <prompt> --force
        custom_args = kwargs.get("args")
        prompt = kwargs.get("prompt") or session.metadata.get("prompt")
        force = kwargs.get("force", True)

        if custom_args is not None:
            final_args = list(custom_args)
        else:
            final_args = []
            if force:
                final_args.append("--force")
            if prompt:
                final_args.extend(["-p", str(prompt)])

        kwargs_copy = dict(kwargs)
        kwargs_copy["args"] = final_args
        kwargs_copy.setdefault("executable", self.executable)

        # Ensure CURSOR_API_KEY from environment is preserved if present but stripped of display leaks
        custom_env = dict(kwargs_copy.get("env") or {})
        if "CURSOR_API_KEY" in os.environ and "CURSOR_API_KEY" not in custom_env:
            custom_env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]
        kwargs_copy["env"] = custom_env

        return await super().start(session, **kwargs_copy)

    async def _stream_reader(
        self,
        state: Any,
        stream: asyncio.StreamReader,
        stream_name: str,
    ) -> None:
        """Read Cursor agent stream lines, parsing JSON telemetry when present."""
        in_private_key = False
        try:
            while not stream.at_eof():
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue

                # Multi-line private key block handling
                if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
                    in_private_key = True
                    sanitized_text = "[REDACTED PRIVATE KEY]"
                elif in_private_key:
                    if "-----END " in line and "PRIVATE KEY-----" in line:
                        in_private_key = False
                    sanitized_text = "[REDACTED PRIVATE KEY]"
                else:
                    sanitized_text = redact_secrets(line)

                # Attempt to parse structured JSON telemetry from Cursor agent
                parsed_json: dict[str, Any] | None = None
                if sanitized_text.startswith("{") and sanitized_text.endswith("}"):
                    try:
                        raw_data = json.loads(sanitized_text)
                        if isinstance(raw_data, dict):
                            # Redact any sensitive keys in JSON payload
                            parsed_json = self._redact_json_dict(raw_data)
                    except Exception:
                        pass

                payload: dict[str, Any] = {"stream": stream_name, "text": sanitized_text}
                if parsed_json is not None:
                    payload["structured"] = parsed_json

                event = AgentEvent(
                    event_type=AgentEventType.AGENT_OUTPUT,
                    agent_id=state.session.agent_id,
                    agent_session_id=state.session.session_id,
                    project_id=state.session.project_id,
                    severity=EventSeverity.INFO if stream_name == "stdout" else EventSeverity.WARNING,
                    payload=payload,
                )
                self._emit_event(state, event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("Cursor stream reader error for session %s: %s", state.session.session_id, exc)

    def _redact_json_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Recursively redact sensitive values in JSON telemetry objects."""
        clean: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str):
                clean[k] = redact_secrets(v)
            elif isinstance(v, dict):
                clean[k] = self._redact_json_dict(v)
            elif isinstance(v, list):
                clean[k] = [
                    redact_secrets(item) if isinstance(item, str)
                    else self._redact_json_dict(item) if isinstance(item, dict)
                    else item
                    for item in v
                ]
            else:
                clean[k] = v
        return clean
