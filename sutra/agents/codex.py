"""OpenAI Codex agent adapter for SŪTRA."""
from __future__ import annotations

import asyncio
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
    AgentSession,
)

log = logging.getLogger(__name__)

DEFAULT_CODEX_CAPABILITIES = {
    AgentCapability.START,
    AgentCapability.STOP,
    AgentCapability.STREAM_OUTPUT,
    AgentCapability.GET_STATUS,
    AgentCapability.API,
    AgentCapability.CLI,
}


class CodexAdapter(GenericCLIAdapter):
    """OpenAI Codex CLI and model assistant bridge.
    
    Adheres strictly to the SŪTRA security and lifecycle model:
    - Never uses shell=True or string concatenation.
    - Resolves Codex CLI executable ('codex') or configured bridge.
    - Strictly confines execution to validated project scope.
    - Never exposes OPENAI_API_KEY or credentials in events, logs, or chat.
    - Manages bounded streaming output and process lifecycle.
    """

    def __init__(
        self,
        agent_id: str = "codex",
        display_name: str = "Codex",
        executable: str | None = "codex",
        capabilities: set[AgentCapability] | None = None,
        timeout_seconds: float = 300.0,
        graceful_stop_timeout: float = 3.0,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            display_name=display_name,
            executable=executable,
            capabilities=capabilities if capabilities is not None else DEFAULT_CODEX_CAPABILITIES,
            timeout_seconds=timeout_seconds,
            graceful_stop_timeout=graceful_stop_timeout,
        )

    def descriptor(self) -> AgentDescriptor:
        """Return the descriptor detailing identity, provider, and declared capabilities."""
        avail = AgentAvailability.UNAVAILABLE
        if self.executable:
            found = shutil.which(self.executable) or (Path(self.executable).is_file())
            if found:
                avail = AgentAvailability.INSTALLED
            elif "OPENAI_API_KEY" in os.environ:
                avail = AgentAvailability.AVAILABLE
        elif "OPENAI_API_KEY" in os.environ:
            avail = AgentAvailability.AVAILABLE

        return AgentDescriptor(
            agent_id=self._agent_id,
            provider="OpenAI",
            display_name=self._display_name,
            version=self.detect_version(),
            executable=self.executable,
            availability=avail,
            capabilities=tuple(sorted(c.value for c in self._caps)),
            installation_notes="OpenAI Codex CLI and model assistant bridge.",
            status="Connected" if bool(self._active_sessions) else "Not Connected",
            implementation="Codex Adapter",
            metadata={"cli": "codex", "api_configured": "OPENAI_API_KEY" in os.environ},
        )

    def is_available(self) -> bool:
        """Check if Codex CLI or API credentials are configured."""
        if self.executable:
            if shutil.which(self.executable) or Path(self.executable).is_file():
                return True
        return "OPENAI_API_KEY" in os.environ

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        """Start a Codex session within the verified project scope."""
        self.require_capability(AgentCapability.START)

        if not self.is_available():
            raise AgentUnavailableError(
                self._agent_id,
                f"Codex executable '{self.executable}' not found on system PATH and OPENAI_API_KEY is not configured",
            )

        custom_args = kwargs.get("args")
        prompt = kwargs.get("prompt") or session.metadata.get("prompt")

        if custom_args is not None:
            final_args = list(custom_args)
        else:
            final_args = []
            if prompt:
                final_args.extend(["exec", str(prompt)])

        kwargs_copy = dict(kwargs)
        kwargs_copy["args"] = final_args
        kwargs_copy.setdefault("executable", self.executable)

        # Preserve OPENAI_API_KEY in process environment without leaking it to logs/events
        custom_env = dict(kwargs_copy.get("env") or {})
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            if key in os.environ and key not in custom_env:
                custom_env[key] = os.environ[key]
        kwargs_copy["env"] = custom_env

        return await super().start(session, **kwargs_copy)
