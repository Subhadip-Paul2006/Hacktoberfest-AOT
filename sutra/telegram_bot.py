"""Backward-compatibility shim for SŪTRA TelegramBot.

The Telegram application has been refactored into the modular package `sutra.apps.telegram`.
This module re-exports `TelegramBot` and related types/constants to maintain seamless
backward compatibility with existing imports and tests. No business logic is duplicated here.
"""
from __future__ import annotations

from .apps.telegram import (
    ACTIVITY,
    AGENTS,
    CONNECTOR_NAMES,
    CONNECTORS,
    DEVICES,
    HOME,
    NAVIGATION_KEYBOARD,
    PERMISSIONS,
    PROJECTS,
    SECTIONS,
    SETTING_NAMES,
    SETTINGS,
    USAGE,
    MenuReply,
    TelegramBot,
    _CredentialRedactionFilter,
    _access_root_key,
)

__all__ = [
    "TelegramBot",
    "MenuReply",
    "HOME",
    "PROJECTS",
    "AGENTS",
    "CONNECTORS",
    "USAGE",
    "PERMISSIONS",
    "ACTIVITY",
    "DEVICES",
    "SETTINGS",
    "SECTIONS",
    "NAVIGATION_KEYBOARD",
    "CONNECTOR_NAMES",
    "SETTING_NAMES",
    "_CredentialRedactionFilter",
    "_access_root_key",
]
