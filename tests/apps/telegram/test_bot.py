"""Unit tests for SŪTRA modular Telegram application package."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from sutra.apps.telegram import (
    TelegramBot as ModularTelegramBot,
    MenuReply,
    HOME,
    PROJECTS,
    AGENTS,
    NAVIGATION_KEYBOARD,
)
from sutra.telegram_bot import TelegramBot as ShimTelegramBot
from sutra.apps.telegram.keyboards import build_button, build_nav_buttons


class TelegramModularArchitectureTests(unittest.TestCase):
    def test_import_compatibility_shim_identical(self):
        """Verify the legacy sutra.telegram_bot shim re-exports the exact class."""
        self.assertIs(ModularTelegramBot, ShimTelegramBot)

    def test_menu_reply_dataclass(self):
        reply = MenuReply(
            text="Test",
            inline_keyboard=[[{"text": "Btn", "callback_data": "data"}]],
            chat_id=123,
            user_id=456,
        )
        self.assertEqual(reply.text, "Test")
        self.assertEqual(reply.chat_id, 123)
        self.assertEqual(reply.user_id, 456)
        self.assertFalse(reply.show_navigation)

    def test_keyboards_and_nav_buttons(self):
        btn = build_button("Test", "act:test")
        self.assertEqual(btn, {"text": "Test", "callback_data": "act:test"})

        nav = build_nav_buttons(back="nav:projects")
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0][0]["callback_data"], "nav:projects")
        self.assertEqual(nav[0][1]["callback_data"], "nav:home")

        self.assertIn(HOME, [btn for row in NAVIGATION_KEYBOARD["keyboard"] for btn in row])
        self.assertIn(PROJECTS, [btn for row in NAVIGATION_KEYBOARD["keyboard"] for btn in row])
        self.assertIn(AGENTS, [btn for row in NAVIGATION_KEYBOARD["keyboard"] for btn in row])

    def test_bot_initialization_status(self):
        cp = MagicMock()
        cp.sessions = {}
        cp.store.list_devices.return_value = []
        bot_enabled = ModularTelegramBot(cp, "token123", {42})
        self.assertTrue(bot_enabled.is_enabled())
        self.assertEqual(bot_enabled.name, "telegram")

        bot_disabled = ModularTelegramBot(cp, "", set())
        self.assertFalse(bot_disabled.is_enabled())
