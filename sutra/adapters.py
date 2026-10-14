"""Adapter contracts and registry bridge for SŪTRA."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents import (
    AgentAdapter,
    AgentAvailability,
    AgentCapability,
    AgentDescriptor,
    AgentEvent,
    AgentEventType,
    AgentManager,
    AgentRegistry,
    AgentSession,
    AgentSessionStatus,
    AntigravityAdapter,
    CodexAdapter,
    CursorAdapter,
    GenericCLIAdapter,
    JulesAdapter,
    PlaceholderAdapter,
    get_default_adapters,
    get_default_descriptors,
)


@dataclass(frozen=True)
class AdapterInfo:
    name: str
    status: str = "Not Connected"
    implementation: str = "Not Implemented"
    capabilities: tuple[str, ...] = ()


__all__ = [
    "AdapterInfo",
    "AgentAdapter",
    "AgentAvailability",
    "AgentCapability",
    "AgentDescriptor",
    "AgentEvent",
    "AgentEventType",
    "AgentManager",
    "AgentRegistry",
    "AgentSession",
    "AgentSessionStatus",
    "AntigravityAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "GenericCLIAdapter",
    "JulesAdapter",
    "PlaceholderAdapter",
    "get_default_adapters",
    "get_default_descriptors",
]
