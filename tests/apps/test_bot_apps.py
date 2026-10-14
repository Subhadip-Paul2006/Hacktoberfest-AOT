"""Unit tests for bot application registry and Slack/Discord scaffolds."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sutra.apps.common.base import AppStatus
from sutra.apps.common.registry import AppRegistry
from sutra.apps.slack.bot import SlackBot
from sutra.apps.discord.bot import DiscordBot


class BotAppsRegistryTests(unittest.TestCase):
    def test_registry_discovers_all_three_platforms(self):
        registry = AppRegistry()
        apps = {app.name: app for app in registry.list_apps()}
        self.assertIn("telegram", apps)
        self.assertIn("slack", apps)
        self.assertIn("discord", apps)

        self.assertTrue(apps["telegram"].is_implemented)
        self.assertFalse(apps["slack"].is_implemented)
        self.assertFalse(apps["discord"].is_implemented)

        self.assertEqual(apps["slack"].status, AppStatus.PLANNED)
        self.assertEqual(apps["discord"].status, AppStatus.PLANNED)

    def test_slack_bot_scaffold(self):
        slack = SlackBot()
        self.assertFalse(slack.is_enabled())
        self.assertEqual(slack.status, AppStatus.PLANNED)
        self.assertEqual(slack.name, "slack")

        import asyncio
        with self.assertRaises(NotImplementedError):
            asyncio.run(slack.run())

    def test_discord_bot_scaffold(self):
        discord = DiscordBot()
        self.assertFalse(discord.is_enabled())
        self.assertEqual(discord.status, AppStatus.PLANNED)
        self.assertEqual(discord.name, "discord")

        import asyncio
        with self.assertRaises(NotImplementedError):
            asyncio.run(discord.run())

    def test_telegram_status_detection_from_env(self):
        with patch.dict(os.environ, {"SUTRA_TELEGRAM_BOT_TOKEN": "tok", "SUTRA_TELEGRAM_ALLOWED_USER_IDS": "123"}, clear=False):
            reg = AppRegistry()
            self.assertEqual(reg.get_app("telegram").status, AppStatus.ENABLED)
            self.assertTrue(reg.is_app_enabled("telegram"))

        with patch.dict(os.environ, {"SUTRA_TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_TOKEN": ""}, clear=False):
            reg2 = AppRegistry()
            self.assertEqual(reg2.get_app("telegram").status, AppStatus.DISABLED)
            self.assertFalse(reg2.is_app_enabled("telegram"))
