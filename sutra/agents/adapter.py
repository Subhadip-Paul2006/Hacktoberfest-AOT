"""Canonical AgentAdapter interface and placeholder implementation."""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

from .errors import AgentUnavailableError, CapabilityNotSupportedError
from .models import (
    AgentCapability,
    AgentDescriptor,
    AgentEvent,
    AgentSession,
    AgentSessionStatus,
)


class AgentAdapter(abc.ABC):
    """Canonical interface that all vendor and generic agent adapters must implement."""

    @abc.abstractmethod
    def descriptor(self) -> AgentDescriptor:
        """Return the descriptor detailing identity, provider, and declared capabilities."""
        ...

    def is_available(self) -> bool:
        """Check if the agent executable or service is available on this system."""
        desc = self.descriptor()
        status_val = desc.availability.value if hasattr(desc.availability, "value") else str(desc.availability)
        return status_val in ("AVAILABLE", "INSTALLED")

    def capabilities(self) -> frozenset[AgentCapability]:
        """Return the set of capabilities supported by this adapter."""
        caps: set[AgentCapability] = set()
        for c in self.descriptor().capabilities:
            try:
                caps.add(AgentCapability(c))
            except ValueError:
                pass
        return frozenset(caps)

    def require_capability(self, capability: AgentCapability) -> None:
        """Raise CapabilityNotSupportedError if this adapter does not declare the capability."""
        if capability not in self.capabilities():
            cap_val = capability.value if hasattr(capability, "value") else str(capability)
            raise CapabilityNotSupportedError(self.descriptor().agent_id, cap_val)

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        """Start a new agent session. Subclasses supporting START must implement this."""
        self.require_capability(AgentCapability.START)
        raise NotImplementedError("Subclasses supporting START must implement start()")

    async def stop(self, session_id: str) -> AgentSession:
        """Stop an active agent session. Subclasses supporting STOP must implement this."""
        self.require_capability(AgentCapability.STOP)
        raise NotImplementedError("Subclasses supporting STOP must implement stop()")

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        """Send a prompt or instructions to a running session."""
        self.require_capability(AgentCapability.SEND_PROMPT)
        raise NotImplementedError("Subclasses supporting SEND_PROMPT must implement send_prompt()")

    async def get_status(self, session_id: str) -> AgentSessionStatus:
        """Query runtime status of the active session."""
        self.require_capability(AgentCapability.GET_STATUS)
        raise NotImplementedError("Subclasses supporting GET_STATUS must implement get_status()")

    async def subscribe(self, session_id: str) -> AsyncIterator[AgentEvent]:
        """Subscribe to real-time events from this session."""
        self.require_capability(AgentCapability.STREAM_OUTPUT)
        if False:
            yield ...  # type: ignore[unreachable]
        raise NotImplementedError("Subclasses supporting STREAM_OUTPUT must implement subscribe()")


class PlaceholderAdapter(AgentAdapter):
    """Honest placeholder adapter indicating an agent is recognized but not yet integrated."""

    def __init__(self, descriptor: AgentDescriptor) -> None:
        self._descriptor = descriptor

    def descriptor(self) -> AgentDescriptor:
        return self._descriptor

    async def start(self, session: AgentSession, **kwargs: Any) -> AgentSession:
        self.require_capability(AgentCapability.START)
        raise AgentUnavailableError(
            self._descriptor.agent_id,
            reason=f"{self._descriptor.display_name} integration is not implemented yet.",
        )

    async def stop(self, session_id: str) -> AgentSession:
        self.require_capability(AgentCapability.STOP)
        raise AgentUnavailableError(
            self._descriptor.agent_id,
            reason=f"{self._descriptor.display_name} integration is not implemented yet.",
        )

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        self.require_capability(AgentCapability.SEND_PROMPT)
        raise AgentUnavailableError(
            self._descriptor.agent_id,
            reason=f"{self._descriptor.display_name} integration is not implemented yet.",
        )

    async def get_status(self, session_id: str) -> AgentSessionStatus:
        self.require_capability(AgentCapability.GET_STATUS)
        return AgentSessionStatus.IDLE
