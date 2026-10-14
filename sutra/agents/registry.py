"""Central registry for discovering, registering, and querying agent adapters."""
from __future__ import annotations

import shutil
from typing import Sequence

from .adapter import AgentAdapter, PlaceholderAdapter
from .antigravity import AntigravityAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .errors import AgentNotFoundError, DuplicateAgentError
from .generic_cli import GenericCLIAdapter
from .jules import JulesAdapter
from .models import AgentAvailability, AgentCapability, AgentDescriptor


def _safe_check_executable(executable: str | None) -> AgentAvailability:
    """Safely check if an executable exists in system PATH without executing it."""
    if not executable:
        return AgentAvailability.UNAVAILABLE
    found = shutil.which(executable)
    if found:
        return AgentAvailability.INSTALLED
    return AgentAvailability.UNAVAILABLE


def get_default_adapters() -> list[AgentAdapter]:
    """Return default adapters: real implementations for supported agents and honest placeholders for others."""
    adapters: list[AgentAdapter] = [
        AntigravityAdapter(),
        CursorAdapter(),
        CodexAdapter(),
        JulesAdapter(),
        GenericCLIAdapter(),
    ]
    # Claude Code placeholder until its concrete adapter is implemented
    claude_spec = (
        "claude_code",
        "Anthropic",
        "Claude Code",
        "claude",
        frozenset({
            AgentCapability.START,
            AgentCapability.STOP,
            AgentCapability.SEND_PROMPT,
            AgentCapability.STREAM_OUTPUT,
            AgentCapability.GET_STATUS,
            AgentCapability.TOOL_CALLS,
            AgentCapability.HEADLESS,
            AgentCapability.CLI,
        }),
        "Claude Code headless CLI (claude -p) runner.",
    )
    claude_avail = _safe_check_executable(claude_spec[3])
    claude_desc = AgentDescriptor(
        agent_id=claude_spec[0],
        provider=claude_spec[1],
        display_name=claude_spec[2],
        executable=claude_spec[3],
        availability=claude_avail,
        capabilities=claude_spec[4],
        installation_notes=claude_spec[5],
    )
    adapters.append(PlaceholderAdapter(claude_desc))
    return adapters


def get_default_descriptors() -> list[AgentDescriptor]:
    """Return canonical descriptors for default agents."""
    return [a.descriptor() for a in get_default_adapters()]


class AgentRegistry:
    """Registry maintaining active agent adapters and honest placeholders."""

    def __init__(self, populate_defaults: bool = True) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        self._alias_map: dict[str, str] = {}
        if populate_defaults:
            for adapter in get_default_adapters():
                self.register(adapter)

    def register(self, adapter: AgentAdapter, *, overwrite: bool = False) -> None:
        """Register an adapter. Rejects duplicates unless overwrite=True."""
        desc = adapter.descriptor()
        agent_id = desc.agent_id.casefold()
        if agent_id in self._adapters and not overwrite:
            raise DuplicateAgentError(desc.agent_id)
        self._adapters[agent_id] = adapter
        self._alias_map[desc.display_name.casefold()] = agent_id

    def unregister(self, agent_id: str) -> bool:
        """Remove an adapter from the registry."""
        key = agent_id.casefold()
        if key in self._adapters:
            desc = self._adapters[key].descriptor()
            del self._adapters[key]
            self._alias_map.pop(desc.display_name.casefold(), None)
            return True
        return False

    def find(self, agent_id_or_name: str) -> AgentAdapter | None:
        """Find an adapter by agent_id or display name, returning None if missing."""
        key = agent_id_or_name.casefold()
        if key in self._adapters:
            return self._adapters[key]
        canonical = self._alias_map.get(key)
        if canonical and canonical in self._adapters:
            return self._adapters[canonical]
        return None

    def get(self, agent_id_or_name: str) -> AgentAdapter:
        """Lookup an adapter by agent_id or display name, raising AgentNotFoundError if missing."""
        adapter = self.find(agent_id_or_name)
        if adapter is None:
            raise AgentNotFoundError(agent_id_or_name)
        return adapter

    def list_adapters(self) -> list[AgentAdapter]:
        """Return all registered adapter instances."""
        return list(self._adapters.values())

    def list_descriptors(self) -> list[AgentDescriptor]:
        """Return all registered agent descriptors."""
        return [adapter.descriptor() for adapter in self._adapters.values()]

    def list(self) -> list[AgentDescriptor]:
        """Backward-compatible alias for list_descriptors()."""
        return self.list_descriptors()

    def discover(self) -> list[AgentDescriptor]:
        """Safely re-probe availability of installed agent executables."""
        for key, adapter in list(self._adapters.items()):
            desc = adapter.descriptor()
            new_avail = _safe_check_executable(desc.executable)
            if new_avail != desc.availability:
                updated_desc = AgentDescriptor(
                    agent_id=desc.agent_id,
                    provider=desc.provider,
                    display_name=desc.display_name,
                    version=desc.version,
                    executable=desc.executable,
                    availability=new_avail,
                    capabilities=desc.capabilities,
                    installation_notes=desc.installation_notes,
                    metadata=dict(desc.metadata),
                )
                self._adapters[key] = PlaceholderAdapter(updated_desc)
        return self.list_descriptors()
