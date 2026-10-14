"""Base abstractions for SŪTRA Bot Applications."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


class AppStatus(str, Enum):
    """Normalized operational status for bot integrations."""
    ENABLED = "enabled"
    DISABLED = "disabled"
    PLANNED = "planned"
    CONNECTED = "connected"
    OFFLINE = "offline"


class BaseBotApp(ABC):
    """Abstract base class for SŪTRA ChatOps and bot integrations."""

    def __init__(self, name: str, status: AppStatus = AppStatus.DISABLED):
        self.name = name
        self.status = status

    @abstractmethod
    def is_enabled(self) -> bool:
        """Return True if this bot application is configured and enabled."""
        raise NotImplementedError

    @abstractmethod
    async def run(self) -> None:
        """Run the bot event loop or polling loop."""
        raise NotImplementedError

    @abstractmethod
    async def aclose(self) -> None:
        """Clean up resources, open HTTP clients, and background tasks."""
        raise NotImplementedError

    async def __aenter__(self) -> BaseBotApp:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
