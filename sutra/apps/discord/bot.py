"""Discord ChatOps integration scaffold for SŪTRA.

Status: Planned / Not implemented.
"""
from __future__ import annotations

import logging
from typing import Any
from ..common.base import BaseBotApp, AppStatus

log = logging.getLogger(__name__)


class DiscordBot(BaseBotApp):
    """Scaffold for future Discord ChatOps integration.

    Status: Planned / Not implemented.
    Configuration namespace reserved: SUTRA_DISCORD_*
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(name="discord", status=AppStatus.PLANNED)

    def is_enabled(self) -> bool:
        """Discord is currently planned and not enabled."""
        return False

    async def run(self) -> None:
        raise NotImplementedError("Discord integration is planned but not currently implemented in SŪTRA.")

    async def aclose(self) -> None:
        pass
