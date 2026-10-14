"""Registry for SŪTRA Bot Applications (Telegram, Slack, Discord)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from .base import AppStatus


@dataclass(frozen=True)
class BotAppDescriptor:
    name: str
    display_name: str
    status: AppStatus
    is_implemented: bool
    config_prefix: str
    description: str


class AppRegistry:
    """Registry managing bot integrations and their lifecycle/status."""

    def __init__(self) -> None:
        self._descriptors: Dict[str, BotAppDescriptor] = {
            "telegram": BotAppDescriptor(
                name="telegram",
                display_name="Telegram",
                status=self._detect_telegram_status(),
                is_implemented=True,
                config_prefix="SUTRA_TELEGRAM_",
                description="Telegram ChatOps interactive remote control plane",
            ),
            "slack": BotAppDescriptor(
                name="slack",
                display_name="Slack",
                status=AppStatus.PLANNED,
                is_implemented=False,
                config_prefix="SUTRA_SLACK_",
                description="Slack ChatOps integration (Planned)",
            ),
            "discord": BotAppDescriptor(
                name="discord",
                display_name="Discord",
                status=AppStatus.PLANNED,
                is_implemented=False,
                config_prefix="SUTRA_DISCORD_",
                description="Discord ChatOps integration (Planned)",
            ),
        }

    def _detect_telegram_status(self) -> AppStatus:
        token = os.environ.get("SUTRA_TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        users = os.environ.get("SUTRA_TELEGRAM_ALLOWED_USER_IDS", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")).strip()
        if token and users:
            return AppStatus.ENABLED
        return AppStatus.DISABLED

    def list_apps(self) -> List[BotAppDescriptor]:
        """Return all registered application descriptors."""
        return list(self._descriptors.values())

    def get_app(self, name: str) -> Optional[BotAppDescriptor]:
        """Get descriptor by application key."""
        return self._descriptors.get(name.lower())

    def is_app_enabled(self, name: str) -> bool:
        """Return True if the specified application is implemented and enabled."""
        app = self.get_app(name)
        return app is not None and app.status == AppStatus.ENABLED and app.is_implemented
