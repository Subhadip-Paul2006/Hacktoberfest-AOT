"""Keyboards and navigation layouts for SŪTRA Telegram ChatOps."""
from __future__ import annotations

import secrets
import time
from typing import Any, Dict, List

HOME = "🏠 Home"
PROJECTS = "📁 Projects"
AGENTS = "🤖 Agents"
CONNECTORS = "🔌 Connectors"
USAGE = "📊 Usage"
PERMISSIONS = "🔐 Permissions"
ACTIVITY = "🕒 Activity"
DEVICES = "🖥️ Devices"
SETTINGS = "⚙️ Settings"

SECTIONS: Dict[str, str] = {
    HOME: "home",
    PROJECTS: "projects",
    AGENTS: "agents",
    CONNECTORS: "connectors",
    USAGE: "usage",
    PERMISSIONS: "permissions",
    ACTIVITY: "activity",
    DEVICES: "devices",
    SETTINGS: "settings",
}

NAVIGATION_KEYBOARD: Dict[str, Any] = {
    "keyboard": [
        [HOME, PROJECTS, AGENTS],
        [CONNECTORS, USAGE, PERMISSIONS],
        [ACTIVITY, DEVICES, SETTINGS],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

CONNECTOR_NAMES = ("GitHub", "MCP", "Stitch", "Supabase", "Vercel", "Notion")
SETTING_NAMES = (
    "🔐 Access Roots",
    "🤖 Agent Settings",
    "🔌 Connectors",
    "🧠 AI / LLM",
    "🔔 Notifications",
    "🖥️ Device",
    "🔑 Security",
)

MAX_CALLBACK_AGE_SECONDS = 1800


def build_button(label: str, action: str) -> Dict[str, str]:
    """Construct an inline keyboard button."""
    return {"text": label, "callback_data": action}


def build_nav_buttons(back: str = "nav:home") -> List[List[Dict[str, str]]]:
    """Construct standard navigation buttons."""
    return [[build_button("⬅️ Back", back), build_button("🏠 Home", "nav:home")]]


def allocate_target_button(
    callback_targets: Dict[str, Dict[str, Any]],
    label: str,
    user_id: int,
    chat_id: int,
    action: str,
    context: Dict[str, Any],
    control_plane: Any,
) -> Dict[str, str]:
    """Store temporary target context and return inline button data."""
    now = time.time()
    stale_keys = [k for k, item in callback_targets.items() if item["created_at"] <= now - MAX_CALLBACK_AGE_SECONDS]
    for k in stale_keys:
        callback_targets.pop(k, None)

    target_id = secrets.token_urlsafe(7)
    target: Dict[str, Any] = {
        "user_id": user_id,
        "chat_id": chat_id,
        "action": action,
        "context": context,
        "created_at": now,
    }
    device_id = context.get("device_id")
    current = control_plane.store.current_session(device_id) if isinstance(device_id, str) else None
    if current and current.get("session_id") and current.get("session_status") == "CONNECTED":
        target["session_id"] = current["session_id"]
        target["generation"] = current["session_generation"]
    callback_targets[target_id] = target
    return {"text": label, "callback_data": "target:" + target_id}
