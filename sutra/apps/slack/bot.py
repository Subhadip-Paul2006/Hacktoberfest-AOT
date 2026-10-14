"""Slack ChatOps integration scaffold for SŪTRA.

Status: Planned / Not implemented.
"""
from __future__ import annotations

import logging
from typing import Any
from ..common.base import BaseBotApp, AppStatus

log = logging.getLogger(__name__)


class SlackBot(BaseBotApp):
    """Scaffold for future Slack ChatOps integration.

    Status: Planned / Not implemented.
    Configuration namespace reserved: SUTRA_SLACK_*
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(name="slack", status=AppStatus.PLANNED)

    def is_enabled(self) -> bool:
        """Slack is currently planned and not enabled."""
        return False

    async def run(self) -> None:
        raise NotImplementedError("Slack integration is planned but not currently implemented in SŪTRA.")

    async def aclose(self) -> None:
        pass
