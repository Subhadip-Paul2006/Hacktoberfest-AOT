"""Message and screen formatting for SŪTRA Telegram ChatOps."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .keyboards import (
    CONNECTOR_NAMES,
    SETTING_NAMES,
    build_button,
    build_nav_buttons,
)

log = logging.getLogger(__name__)

_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_TELEGRAM_BOT_PATH = re.compile(r"(?i)(/bot)[^/\s]+")


def _access_root_key(path: str | Path) -> str:
    """Return a stable comparison key for access-root paths."""
    value = str(Path(path).expanduser().resolve(strict=False)).rstrip("\\/")
    return value.casefold()


class _CredentialRedactionFilter(logging.Filter):
    """Logging filter to scrub bot tokens and bearer credentials."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token:
            message = message.replace(self.token, "[REDACTED]")
        message = _TELEGRAM_BOT_PATH.sub(r"\1[REDACTED]", message)
        message = _BEARER_VALUE.sub(r"\1[REDACTED]", message)
        record.msg, record.args = message, ()
        return True


def format_devices(devices: List[Dict[str, Any]]) -> str:
    """Format list of paired devices for text message."""
    if not devices:
        return "No paired devices. Start a runtime with the control-plane URL to create a pairing code."
    return "\n".join(f"{d['device_name']} · {d['device_id']} · {d['connection_status']}" for d in devices[:20])


def format_time(timestamp: Any) -> str:
    """Format unix timestamp into local human-readable datetime string."""
    if not isinstance(timestamp, (int, float)):
        return "Never"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def format_result(command: str, payload: Any) -> str:
    """Format command result payload into user-facing text."""
    if command == "/status":
        if not isinstance(payload, dict):
            return "Runtime status unavailable."
        return (
            f"SŪTRA\nDevice: {payload.get('device') or 'unpaired'}\n"
            f"Connection: {payload.get('connection', 'unknown')}\n"
            f"Runtime: {payload.get('runtime_version', 'unknown')}\n"
            f"Registered workspaces: {payload.get('workspaces', 0)}"
        )
    values = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
    if not values:
        return "No registered workspaces or projects."
    lines = []
    for item in values[:30]:
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, dict):
            name = item.get("name", "Project")
            git = "Git" if item.get("is_git_repository") else "No Git"
            discovery = "Candidate project" if item.get("discovery") == "candidate" else "Project"
            lines.append(f"{name} · {discovery} · {git}")
    return "\n".join(lines) or "No registered workspaces or projects."


def connectivity_label(guard: Any) -> str:
    """Render connectivity snapshot status label."""
    if guard is None:
        return "⚫ Offline · metrics unavailable"
    snapshot = guard.snapshot()
    labels = {
        "HEALTHY": "🟢 Healthy",
        "DEGRADED": "🟡 Degraded",
        "UNUSABLE": "🔴 Unusable",
        "RECOVERING": "🟡 Recovering",
        "DISCONNECTED": "⚫ Offline",
    }
    rtt = snapshot.get("rtt_ms")
    return f"{labels.get(snapshot['state'], snapshot['state'])} · RTT: {rtt if rtt is not None else 'unavailable'} ms"


async def render_screen(
    bot: Any,
    screen: str,
    context: Dict[str, Any],
    chat_id: int,
    user_id: int,
    message_id: Optional[int] = None,
    callback_query_id: Optional[str] = None,
    show_navigation: bool = False,
) -> Any:
    """Construct MenuReply for the requested interactive screen."""
    from ..telegram.keyboards import build_button, build_nav_buttons
    from ..telegram.formatting import format_time, format_devices, connectivity_label, _access_root_key

    if screen == "home":
        pending = bot.control_plane.pairing.get_pending()
        devices = bot._devices()
        device_id = bot.selected_devices.get(user_id)
        device = next((d for d in devices if d["device_id"] == device_id), None)
        if not device:
            connected = [d for d in devices if d["connection_status"] == "CONNECTED"]
            device = connected[0] if connected else (devices[0] if devices else None)
            if device:
                bot.selected_devices[user_id] = device["device_id"]

        if device and device["connection_status"] == "CONNECTED":
            project_count = 0
            try:
                _, projects = await asyncio.wait_for(
                    bot._call_device(user_id, "project.list"), timeout=2.0)
                project_count = projects.get("total_count", len(projects.get("items", [])))
            except Exception:
                pass
            name = device.get("device_name", "SŪTRA PC")
            session_id = device.get("session_id") or "unknown"
            session_trunc = session_id[:8] + "..." if len(session_id) > 8 else session_id
            since = format_time(device.get("connected_at") or device.get("session_created_at"))
            text = (f"🏠 SŪTRA\n\n"
                    f"🟢 Session: CONNECTED\n"
                    f"Device: {name}\n"
                    f"Session: {session_trunc}\n"
                    f"Since: {since}\n"
                    f"Projects: {project_count}")
            keyboard = [
                [bot._button("📁 Projects", "nav:projects"), bot._button("🤖 Agents", "nav:agents"), bot._button("🔗 Connectors", "nav:connectors")],
                [bot._target_button("⛔ Stop Session", user_id, chat_id, "stop_session_confirm", {"device_id": device["device_id"]}),
                 bot._target_button("🔄 Re-pair", user_id, chat_id, "repair_confirm", {"device_id": device["device_id"]})],
                [bot._button("🖥️ Devices", "nav:devices")],
                [bot._button("⚙️ Settings", "nav:settings")]
            ]
        elif pending and pending.get("state") == "WAITING":
            code = pending.get("pairing_code", "UNKNOWN")
            expires_in = pending.get("expires_in", 0)
            mm, ss = divmod(expires_in, 60)
            text = (f"🏠 SŪTRA\n\n"
                    f"🟡 Session: WAITING FOR PAIRING\n\n"
                    f"Pairing code:\n{code}\n\n"
                    f"Expires in:\n{mm:02d}:{ss:02d}")
            keyboard = [
                [bot._button("Cancel Pairing", "action:cancel_pairing")],
                [bot._button("🔄 Refresh", "nav:home")],
                [bot._button("🖥️ Devices", "nav:devices")],
                [bot._button("⚙️ Settings", "nav:settings")]
            ]
        elif (pending and pending.get("state") == "APPROVED") or (device and device.get("connection_status") == "WAITING_FOR_PAIRING"):
            text = "🏠 SŪTRA\n\n🟡 Session: CONNECTING\n\nAuthenticating runtime connection..."
            keyboard = [
                [bot._button("🔄 Refresh", "nav:home")],
                [bot._button("🖥️ Devices", "nav:devices")],
                [bot._button("⚙️ Settings", "nav:settings")]
            ]
        else:
            text = "🏠 SŪTRA\n\n🔴 Session: NOT CONNECTED"
            keyboard = [
                [bot._button("🔐 Pair Runtime", "action:pair_runtime")],
                [bot._button("🔄 Retry", "nav:home")],
                [bot._button("🖥️ Devices", "nav:devices")],
                [bot._button("⚙️ Settings", "nav:settings")]
            ]
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id,
                          callback_query_id=callback_query_id, show_navigation=show_navigation)

    if screen == "projects":
        devices = bot._devices()
        device_id = bot.selected_devices.get(user_id)
        connected_device = next((d for d in devices if d["device_id"] == device_id and d["connection_status"] == "CONNECTED"), None)
        if not connected_device:
            any_connected = [d for d in devices if d["connection_status"] == "CONNECTED"]
            if any_connected:
                connected_device = any_connected[0]
                bot.selected_devices[user_id] = connected_device["device_id"]

        if not connected_device:
            keyboard = [
                [bot._button("🔐 Pair Runtime", "action:pair_runtime")],
                [bot._button("🔄 Retry", "nav:projects")],
                [bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply("Runtime is currently disconnected.", chat_id, user_id, keyboard,
                              message_id=message_id, callback_query_id=callback_query_id)

        retry_needed = False
        try:
            device_id, result = await bot._call_device(user_id, "project.list",
                message_id=message_id, chat_id=chat_id, warning_title="Projects")
            bot.selected_devices[user_id] = device_id
            items = result.get("items", [])
        except HTTPException as exc:
            if exc.status_code in (409, 503):
                keyboard = [
                    [bot._button("🔐 Pair Runtime", "action:pair_runtime")],
                    [bot._button("🔄 Retry", "nav:projects")],
                    [bot._button("🏠 Home", "nav:home")]
                ]
                return bot._reply("Runtime is currently disconnected.", chat_id, user_id, keyboard,
                                  message_id=message_id, callback_query_id=callback_query_id)
            device_id, items = "", []
            if exc.status_code == 504:
                error = ("⏱ Request timed out.\nThe runtime did not respond within the configured deadline.\n"
                         "Please try again.")
                retry_needed = True
            else:
                error = str(exc.detail)
        else:
            error = ""
        keyboard = []
        for project in items[:30]:
            if not isinstance(project, dict) or not isinstance(project.get("absolute_path"), str):
                continue
            label = "🟡 Candidate · " if project.get("discovery") == "candidate" else "📁 "
            button = bot._target_button(label + str(project.get("name", "Project"))[:38],
                user_id, chat_id, "project", {"device_id": device_id, "project": project})
            keyboard.append([button])
        refreshing = bool(result.get("refreshing")) if device_id else False
        if device_id and refreshing:
            error = (error + "\n" if error else "") + "Projects index is refreshing; showing the latest available list."
        if device_id:
            keyboard.append([bot._target_button("🔄 Refresh Projects", user_id, chat_id,
                "projects_refresh", {"device_id": device_id})])
        elif retry_needed:
            keyboard.append([bot._target_button("🔄 Retry Projects", user_id, chat_id,
                "projects_retry", {"device_id": bot.selected_devices.get(user_id, "")})])
        keyboard.extend(bot._nav_buttons())
        total = result.get("total_count", len(items)) if device_id else len(items)
        text = "📁 Projects\n\n" + (error or (f"{total} discovered project(s)." if items else "No projects found in authorized access roots."))
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "project":
        project = context["project"]
        device_id = context["device_id"]
        pctx = {"device_id": device_id, "project": project}
        buttons = [[bot._target_button("📂 Files", user_id, chat_id, "files", pctx),
                    bot._target_button("🤖 Agent", user_id, chat_id, "project_agent", pctx)],
                   [bot._target_button("🔀 Git", user_id, chat_id, "git", pctx),
                    bot._target_button("🕒 Activity", user_id, chat_id, "project_activity", pctx)],
                   [bot._target_button("📊 Status", user_id, chat_id, "project_status", pctx)]]
        buttons.extend(bot._nav_buttons(back="nav:projects"))
        git_status = "Available" if project.get("is_git_repository") else "Not a Git repository"
        discovery_state = "Candidate project" if project.get("discovery") == "candidate" else "Discovered project"
        return bot._reply(f"📁 {project.get('name', 'Project')}\n{discovery_state}\n"
            f"Git: {git_status}\nCurrent agent: None\nAgent status: ⚪ Not Connected",
            chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "agents" or screen == "project_agent":
        device_id = str(context.get("device_id") or bot.selected_devices.get(user_id) or "")
        project = context.get("project") if isinstance(context.get("project"), dict) else None
        entries = []
        error = ""
        if screen == "project_agent" or device_id:
            try:
                if not device_id:
                    device_id = bot._select_device(user_id=user_id)
                _, payload = await bot._call_device(user_id, "agent.list")
                entries = payload.get("items", [])
            except (HTTPException, KeyError, TypeError) as exc:
                error = str(getattr(exc, "detail", "Paired runtime is unavailable."))
        else:
            error = "Select a paired runtime to discover its installed adapters."
        keyboard = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            label = str(item.get("display_name") or item.get("name") or item.get("agent_id") or "Agent")
            availability = str(item.get("availability", "UNKNOWN"))
            implementation = str(item.get("implementation", ""))
            capabilities = item.get("capabilities", [])
            supported = (availability in {"INSTALLED", "AVAILABLE"}
                         and "START" in capabilities
                         and implementation != "Not Implemented"
                         and "simulator" not in implementation.casefold())
            if screen == "agents":
                keyboard.append([bot._target_button(("🤖 " if supported else "⚪ ") + label[:35], user_id, chat_id,
                    "agent_detail", {"agent_id": item.get("agent_id"), "device_id": device_id})])
        if screen == "project_agent":
            text = f"🤖 Agents for {project.get('name') if project else 'selected project'}\n\n"
            text += error or ("Installed adapters from the paired runtime:" if entries else "No adapters are available on this runtime.")
            for item in entries:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("display_name") or item.get("name") or item.get("agent_id") or "Agent")
                availability = str(item.get("availability", "UNKNOWN"))
                implementation = str(item.get("implementation", ""))
                capabilities = item.get("capabilities", [])
                supported = (availability in {"INSTALLED", "AVAILABLE"}
                             and "START" in capabilities
                             and implementation != "Not Implemented"
                             and "simulator" not in implementation.casefold())
                state = "available" if supported else (
                    "simulator · not live" if "simulator" in implementation.casefold() else availability.lower())
                text += f"\n• {label}: {state}"
                if supported:
                    keyboard.append([bot._target_button("▶ Start · " + label[:30], user_id, chat_id,
                        "agent_prompt_start", {"device_id": device_id, "project": project,
                                               "agent_id": item.get("agent_id")})])
        else:
            text = "🤖 Agents\n\n" + (error or "Adapters reported by the paired runtime.")
            for item in entries:
                if isinstance(item, dict):
                    text += f"\n• {item.get('display_name', item.get('agent_id'))}: {item.get('availability', 'UNKNOWN')}"
        keyboard.extend(bot._nav_buttons(back="nav:home"))
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "agent_detail":
        agent_id = str(context.get("agent_id", ""))
        device_id = str(context.get("device_id") or bot.selected_devices.get(user_id) or "")
        try:
            if not device_id:
                device_id = bot._select_device(user_id=user_id)
            _, body = await bot._call_device(user_id, "agent.status", {"agent_id": agent_id})
            adapter = body.get("agent", {})
        except Exception:
            adapter = {}
        label = str(adapter.get("display_name") or agent_id or "Agent")
        keyboard = []
        if isinstance(context.get("project"), dict) and adapter.get("availability") in {"INSTALLED", "AVAILABLE"}:
            keyboard.append([bot._target_button("▶ Start", user_id, chat_id, "agent_prompt_start",
                {"device_id": device_id, "project": context["project"], "agent_id": agent_id})])
        keyboard.extend(bot._nav_buttons(back="nav:agents"))
        project_name = context.get("project", {}).get("name", "Select a project to start")
        return bot._reply(f"🤖 {label}\nProvider: {adapter.get('provider', 'unknown')}\n"
            f"Availability: {adapter.get('availability', 'UNKNOWN')}\n"
            f"Implementation: {adapter.get('implementation', 'unknown')}\n"
            f"Project: {project_name}\nCapabilities: {', '.join(adapter.get('capabilities', [])) or 'none'}",
            chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "files":
        project = context["project"]
        path = context.get("path") or project["absolute_path"]
        device_id = context["device_id"]
        page = int(context.get("page", 0))
        page_size = int(context.get("page_size", 20))
        try:
            result = await bot._dispatch_command(device_id, "file.list",
                {"project_root": project["absolute_path"], "path": path, "page": page, "page_size": page_size})
            payload = result.get("payload", {})
            items = payload.get("items", [])
            has_more = payload.get("has_more", False)
            has_prev = payload.get("has_prev", False)
            total_items = payload.get("total_items", len(items))
            total_pages = payload.get("total_pages", 1)
        except HTTPException as exc:
            items = []
            has_more = False
            has_prev = False
            total_items = 0
            total_pages = 1
            error = str(exc.detail)
        else:
            error = ""
        keyboard = []
        for entry in items:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            is_dir = entry.get("type") == "directory"
            button = bot._target_button(("📁 " if is_dir else "📄 ") +
                str(entry.get("name", "item"))[:44], user_id, chat_id,
                "folder" if is_dir else "file",
                {"device_id": device_id, "project": project, "path": entry["path"], "name": entry.get("name", "item")})
            keyboard.append([button])
        if has_prev or has_more:
            page_row = []
            if has_prev:
                page_row.append(bot._target_button("⬅️ Previous", user_id, chat_id, "files", {**context, "page": page - 1}))
            if has_more:
                page_row.append(bot._target_button("Next ➡️", user_id, chat_id, "files", {**context, "page": page + 1}))
            keyboard.append(page_row)
        if not items and not error:
            error = "This folder is empty, or it contains only excluded private/build folders."
        project_root = Path(project["absolute_path"])
        if Path(path).resolve() == project_root.resolve():
            back_button = bot._target_button("⬅️ Back to project", user_id, chat_id,
                "project", {"device_id": device_id, "project": project})
        else:
            back_button = bot._target_button("⬅️ Back", user_id, chat_id,
                "folder", {"device_id": device_id, "project": project,
                            "path": str(Path(path).parent), "name": Path(path).parent.name})
        keyboard.append([back_button, bot._button("🏠 Home", "nav:home")])
        name = project.get("name", "Project")
        display_path = Path(path).relative_to(project_root).as_posix() or "."
        paging_info = f" · Page {page + 1}/{total_pages} ({total_items} items)" if total_items > 0 else ""
        return bot._reply(f"📂 {name} · {display_path}{paging_info}\n\n{error or f'{len(items)} item(s) listed.'}",
            chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "file":
        ctx = context
        bot.active_file_context[user_id] = ctx
        project = ctx["project"]
        path = ctx["path"]
        device_id = ctx["device_id"]
        try:
            result = await bot._dispatch_command(device_id, "file.info",
                {"project_root": project["absolute_path"], "path": path})
            info = result.get("payload", {})
        except HTTPException:
            info = {}

        is_binary = info.get("is_binary", False)
        size_bytes = info.get("size_bytes", 0)
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        mtime_str = format_time(info.get("modified_at"))
        category = info.get("category", "binary")
        mime = info.get("mime_type", "application/octet-stream")
        rel_path = info.get("relative_path") or Path(path).name

        if is_binary:
            text = (f"📄 {info.get('name', ctx.get('name', 'file'))}\n"
                    f"Path: {rel_path}\n"
                    f"Type: Binary ({category} · {mime})\n"
                    f"Size: {size_str}\n"
                    f"Modified: {mtime_str}\n\n"
                    "Binary file — text preview unavailable.")
            keyboard = []
            if info.get("export_available", info.get("download_available", True)):
                dl_label = "📦 Export Parts" if info.get("is_multipart") else "📥 Download"
                keyboard.append([bot._target_button(dl_label, user_id, chat_id, "download", ctx)])
            keyboard.append([
                bot._target_button("⬅️ Back to folder", user_id, chat_id, "files",
                    {"device_id": device_id, "project": project, "path": str(Path(path).parent)}),
                bot._button("🏠 Home", "nav:home")
            ])
            return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

        total_lines = info.get("total_lines")
        lines_str = f"{total_lines:,} lines" if total_lines is not None else "Unknown lines"
        lang = info.get("language", "Text")
        text = (f"📄 {info.get('name', ctx.get('name', 'file'))}\n"
                f"Path: {rel_path}\n"
                f"Type: {lang}\n"
                f"Lines: {lines_str}\n"
                f"Size: {size_str}\n"
                f"Modified: {mtime_str}\n\n"
                "Read-only codebase explorer.")
        dl_label = "📦 Export Parts" if info.get("is_multipart") else "📥 Download"
        keyboard = [
            [bot._target_button("👁 Preview", user_id, chat_id, "preview", {**ctx, "start_line": 1, "file_version": info.get("file_version")}),
             bot._target_button("🧩 Symbols", user_id, chat_id, "symbols", ctx)],
            [bot._target_button("🔍 Search", user_id, chat_id, "search_menu", ctx),
             bot._target_button(dl_label, user_id, chat_id, "download", ctx)],
            [bot._target_button("⬅️ Back to folder", user_id, chat_id, "files",
                {"device_id": device_id, "project": project, "path": str(Path(path).parent)}),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "preview":
        file_context = context
        bot.active_file_context[user_id] = file_context
        device_id = file_context["device_id"]
        project = file_context["project"]
        path = file_context["path"]
        start_line = int(file_context.get("start_line", 1))
        expected_ver = file_context.get("expected_version") or file_context.get("file_version")

        try:
            result = await bot._dispatch_command(device_id, "file.preview", {
                "project_root": project["absolute_path"],
                "path": path,
                "start_line": start_line,
                "max_lines": 35,
                "expected_version": expected_ver,
                "line_preview": True,
            })
        except HTTPException:
            result = {"status": "error", "payload": {}}

        payload = result.get("payload", {})
        if result.get("status") != "success":
            return bot._reply("File content is unavailable.", chat_id, user_id, bot._nav_buttons(back="nav:home"),
                              message_id=message_id, callback_query_id=callback_query_id)

        # Check if file changed during preview
        if payload.get("version_changed"):
            new_ver = payload.get("file_version")
            keyboard = [
                [bot._target_button("🔄 Reload Preview", user_id, chat_id, "preview",
                    {**file_context, "start_line": start_line, "file_version": new_ver, "expected_version": new_ver})],
                [bot._target_button("⬅️ Back to file", user_id, chat_id, "file", file_context),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(
                f"⚠️ File changed since this preview was opened.\n\n"
                f"📄 {file_context.get('name', 'file')}\n"
                "The file on disk has been modified. Reload to inspect the updated contents.",
                chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id
            )

        # Legacy character preview fallback if "text" is returned instead of "content"
        if "content" not in payload and "text" in payload:
            page = payload.get("page", 0)
            pages = payload.get("pages", 1)
            back = []
            page_buttons = []
            if page > 0:
                page_buttons.append(bot._target_button("⬅️ Previous", user_id, chat_id,
                    "preview", {**file_context, "page": page - 1}))
            if page + 1 < pages:
                page_buttons.append(bot._target_button("Next ➡️", user_id, chat_id,
                    "preview", {**file_context, "page": page + 1}))
            if page_buttons:
                back.append(page_buttons)
            back.append([bot._target_button("⬅️ Back to file", user_id, chat_id, "file", file_context),
                         bot._button("🏠 Home", "nav:home")])
            return bot._reply(f"👁 Preview · {file_context['name']} · {page + 1}/{pages}\n\n{payload.get('text', 'Preview unavailable.')}",
                chat_id, user_id, back, message_id=message_id,
                callback_query_id=callback_query_id)

        # Bounded line preview
        total_lines = payload.get("total_lines", 0)
        start = payload.get("start_line", start_line)
        end = payload.get("end_line", start)
        file_ver = payload.get("file_version")
        has_more = payload.get("has_more", False)
        has_prev = payload.get("has_prev", False)
        content = payload.get("content", "")

        suffix = Path(path).suffix.casefold()
        lang_map = {".py": "python", ".js": "javascript", ".ts": "typescript", ".html": "html", ".css": "css", ".json": "json", ".md": "markdown", ".rs": "rust", ".go": "go", ".sql": "sql"}
        code_lang = lang_map.get(suffix, "")

        if len(content) > 3500:
            content = content[:3490] + "\n...[truncated]"

        trunc_alert = "⚠️ Preview truncated by size limit.\n\n" if payload.get("truncated_by_bytes") else ""
        code_block = f"```{code_lang}\n{content}\n```" if content else "_Empty file or range_"
        text = f"{trunc_alert}📄 {file_context.get('name', 'file')}\nLines {start}–{end} of {total_lines}\n\n{code_block}"

        keyboard = []
        page_buttons = []
        if has_prev:
            prev_line = max(1, start - 35)
            page_buttons.append(bot._target_button("⬅️ Prev", user_id, chat_id, "preview",
                {**file_context, "start_line": prev_line, "file_version": file_ver}))
        if has_more:
            next_line = end + 1
            page_buttons.append(bot._target_button("Next ➡️", user_id, chat_id, "preview",
                {**file_context, "start_line": next_line, "file_version": file_ver}))
        if page_buttons:
            keyboard.append(page_buttons)

        tools_row = [
            bot._target_button("🔢 Jump", user_id, chat_id, "jump_menu",
                {**file_context, "total_lines": total_lines, "start_line": start, "file_version": file_ver}),
            bot._target_button("🔍 Search", user_id, chat_id, "search_menu", file_context),
            bot._target_button("🧩 Symbols", user_id, chat_id, "symbols", file_context),
        ]
        keyboard.append(tools_row)
        dl_btn_label = "📦 Export Parts" if file_context.get("is_multipart") else "📥 Download"
        keyboard.append([bot._target_button(dl_btn_label, user_id, chat_id, "download", file_context)])
        keyboard.append([bot._target_button("⬅️ Back to file", user_id, chat_id, "file", file_context),
                         bot._button("🏠 Home", "nav:home")])

        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "jump_menu":
        total_lines = context.get("total_lines", 100)
        cur_line = context.get("start_line", 1)
        name = context.get("name", "file")
        text = (f"🔢 Jump to Line · {name}\n\n"
                f"Total lines: {total_lines:,}\n"
                f"Current position: Line {cur_line}\n\n"
                "Choose a position below or reply with `/jump <line>` in chat.")
        jump_buttons = [bot._target_button("Top (L1)", user_id, chat_id, "preview", {**context, "start_line": 1})]
        if total_lines > 70:
            mid = total_lines // 2
            jump_buttons.append(bot._target_button(f"Middle (L{mid})", user_id, chat_id, "preview", {**context, "start_line": mid}))
        if total_lines > 35:
            end_jump = max(1, total_lines - 34)
            jump_buttons.append(bot._target_button(f"End (L{end_jump})", user_id, chat_id, "preview", {**context, "start_line": end_jump}))
        keyboard = [jump_buttons, [bot._target_button("⬅️ Back to preview", user_id, chat_id, "preview", context), bot._button("🏠 Home", "nav:home")]]
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "search_menu":
        name = context.get("name", "file")
        text = (f"🔍 Search in {name}\n\n"
                "To search this file, send:\n"
                "`/search <term>`\n\n"
                "Example: `/search authenticate`")
        keyboard = [[bot._target_button("⬅️ Back to preview", user_id, chat_id, "preview", context), bot._button("🏠 Home", "nav:home")]]
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "search_results":
        name = context.get("name", "file")
        query = context.get("query", "")
        res = context.get("search_result", {})
        matches = res.get("matches", [])
        total_matches = res.get("total_matches", len(matches))

        if not matches:
            text = f"🔍 Search: \"{query}\" in {name}\n\n0 matches found."
            keyboard = [[bot._target_button("⬅️ Back to preview", user_id, chat_id, "preview", context), bot._button("🏠 Home", "nav:home")]]
            return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

        lines_text = "\n".join(f"`{m['line']:>4}`  {m['snippet']}" for m in matches[:10])
        text = f"🔍 Search: \"{query}\" in {name}\n\n{total_matches} match(es) found:\n\n{lines_text}"

        match_buttons = []
        for m in matches[:6]:
            btn = bot._target_button(f"Line {m['line']}", user_id, chat_id, "preview", {**context, "start_line": m["line"]})
            match_buttons.append(btn)
        button_rows = [match_buttons[i:i + 3] for i in range(0, len(match_buttons), 3)]
        button_rows.append([bot._target_button("⬅️ Back to preview", user_id, chat_id, "preview", context), bot._button("🏠 Home", "nav:home")])
        return bot._reply(text, chat_id, user_id, button_rows, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "symbols":
        name = context.get("name", "file")
        device_id = context["device_id"]
        project = context["project"]
        path = context["path"]

        try:
            res = await bot._dispatch_command(device_id, "file.symbols", {
                "project_root": project["absolute_path"],
                "path": path,
            })
            data = res.get("payload", {})
        except HTTPException:
            data = {}

        classes = data.get("classes", [])
        functions = data.get("functions", [])
        lang = data.get("language", "Source")

        symbol_rows: list[str] = []
        if classes:
            symbol_rows.append("Classes:")
            for c in classes[:10]:
                symbol_rows.append(f"• {c['name']} (line {c['line']})")
        if functions:
            symbol_rows.append("\nFunctions:")
            for f in functions[:15]:
                symbol_rows.append(f"• {f['name']}() (line {f['line']})")

        if not classes and not functions:
            text = (f"🧩 Symbols · {name} ({lang})\n\n"
                    "No symbols detected with deterministic parsing.\n"
                    "Use text search to locate symbols.")
            keyboard = [
                [bot._target_button("🔍 Search File", user_id, chat_id, "search_menu", context)],
                [bot._target_button("⬅️ Back to file", user_id, chat_id, "file", context), bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

        text = f"🧩 Symbols · {name} ({lang})\n\n" + "\n".join(symbol_rows)
        keyboard = []
        sym_buttons = []
        for c in classes[:4]:
            sym_buttons.append(bot._target_button(f"🏛️ {c['name'][:14]} (L{c['line']})", user_id, chat_id, "preview", {**context, "start_line": c["line"]}))
        for f in functions[:6]:
            sym_buttons.append(bot._target_button(f"⚡ {f['name'][:14]} (L{f['line']})", user_id, chat_id, "preview", {**context, "start_line": f["line"]}))
        for i in range(0, len(sym_buttons), 2):
            keyboard.append(sym_buttons[i:i + 2])
        keyboard.append([bot._target_button("⬅️ Back to file", user_id, chat_id, "file", context), bot._button("🏠 Home", "nav:home")])
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "download":
        file_context = context
        part_idx = file_context.get("part_index")
        download_payload: dict[str, Any] = {
            "project_root": file_context["project"]["absolute_path"],
            "path": file_context["path"],
        }
        if part_idx is not None:
            download_payload["part_index"] = part_idx

        try:
            result = await bot._dispatch_command(file_context["device_id"], "file.download", download_payload)
        except HTTPException:
            result = {"status": "error", "payload": {}}
        payload = result.get("payload", {})
        if result.get("status") != "success":
            return bot._reply("This file cannot be downloaded.", chat_id, user_id, bot._nav_buttons(back="nav:home"),
                              message_id=message_id, callback_query_id=callback_query_id)

        # Check if multipart export prepared
        if payload.get("status") == "multipart_export" or payload.get("is_multipart"):
            manifest = payload.get("manifest", {})
            parts = manifest.get("parts", [])
            part_count = len(parts)
            orig_size = manifest.get("original_size", 0)
            orig_name = manifest.get("original_filename", file_context.get("name", "file"))
            size_mb = orig_size / (1024 * 1024)

            text = (
                f"📦 Large File Export · {orig_name}\n\n"
                f"Size: {size_mb:.1f} MB ({orig_size:,} bytes)\n"
                f"Parts: {part_count} chunk(s)\n"
                f"SHA-256:\n`{manifest.get('whole_file_sha256', '')}`\n\n"
                "This file exceeds the single download chunk limit (10 MB). "
                "Select a part below to export:"
            )
            keyboard = []
            part_buttons = []
            for p in parts[:12]:
                idx = p["index"]
                btn = bot._target_button(
                    f"📦 Part {idx}/{part_count}", user_id, chat_id, "download",
                    {**file_context, "part_index": idx}
                )
                part_buttons.append(btn)
            for i in range(0, len(part_buttons), 2):
                keyboard.append(part_buttons[i:i + 2])

            keyboard.append([
                bot._target_button("⬅️ Back to file", user_id, chat_id, "file", file_context),
                bot._button("🏠 Home", "nav:home")
            ])
            return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

        try:
            document_bytes = base64.b64decode(payload["content_base64"], validate=True)
        except (KeyError, ValueError, TypeError):
            return bot._reply("This file cannot be safely exported.", chat_id, user_id,
                bot._nav_buttons(back="nav:home"), message_id=message_id, callback_query_id=callback_query_id)
        doc_name = payload.get("name", file_context.get("name", "export.txt"))
        back = [[bot._target_button("⬅️ Back to file", user_id, chat_id, "file", file_context),
                 bot._button("🏠 Home", "nav:home")]]
        return bot._reply(f"📥 Exported: {doc_name}",
            chat_id, user_id, back, message_id=message_id,
            callback_query_id=callback_query_id, document_name=doc_name, document_bytes=document_bytes)

    if screen == "devices":
        devices = bot._devices()
        keyboard = []
        for device in devices[:30]:
            target = bot._target_button("🖥️ " + device["device_name"][:40], user_id, chat_id,
                "device", {"device_id": device["device_id"]})
            keyboard.append([target])
        keyboard.append([bot._button("🔐 Pair Runtime", "action:pair_runtime")])
        keyboard.extend(bot._nav_buttons())
        text = "🖥️ Devices\n\n" + ("\n".join(
            f"{device['device_name']} · {device['device_id']} · {device['connection_status']} · Runtime {device['runtime_version']} · Last seen {format_time(device['last_seen'])}"
            for device in devices[:20]) or "No paired devices.")
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "device":
        device = next((item for item in bot._devices() if item["device_id"] == context["device_id"]), None)
        if not device:
            return bot._reply("Device is no longer paired.", chat_id, user_id, bot._nav_buttons(back="nav:devices"),
                              message_id=message_id, callback_query_id=callback_query_id)
        keyboard = [[bot._target_button("✅ Select Device", user_id, chat_id, "device_select",
                                           {"device_id": device["device_id"]})]]
        if device["connection_status"] == "CONNECTED":
            keyboard.append([bot._target_button("⛔ Stop Session", user_id, chat_id,
                "stop_session_confirm", {"device_id": device["device_id"]})])
            keyboard.append([bot._target_button("🔄 Re-pair", user_id, chat_id,
                "repair_confirm", {"device_id": device["device_id"]})])
        else:
            keyboard.append([bot._button("🔐 Pair Runtime", "action:pair_runtime")])
        keyboard.append([bot._target_button("⛔ Revoke Device", user_id, chat_id, "revoke_confirm",
                                             {"device_id": device["device_id"]})])
        keyboard.extend(bot._nav_buttons(back="nav:devices"))
        status_icon = "🟢" if device["connection_status"] == "CONNECTED" else "🔴"
        session_id = device.get("session_id") or "None"
        session_id_disp = session_id[:8] + "..." if len(session_id) > 8 else session_id
        session_started = format_time(device.get("connected_at") or device.get("session_created_at"))
        return bot._reply(f"Device:\n{device['device_name']}\n\n"
            f"Device ID: {device['device_id']}\n"
            f"Current Session:\n{status_icon} {device['connection_status']}\n\n"
            f"Session ID: {session_id_disp}\n"
            f"Session Started:\n{session_started}\n\n"
            f"Runtime: {device['runtime_version']}\n"
            f"Session generation: {device.get('session_generation', 0)}\n"
            f"Connectivity: {connectivity_label(bot.control_plane.connectivity.get(device['device_id']))}\n"
            f"Last seen: {format_time(device['last_seen'])}", chat_id, user_id, keyboard,
            message_id=message_id, callback_query_id=callback_query_id)

    if screen == "stop_session_confirm":
        device_id = context.get("device_id")
        device = next((item for item in bot._devices() if item["device_id"] == device_id), None)
        if not device or device["connection_status"] != "CONNECTED":
            return bot._reply("This session is no longer active. A fresh pairing is required.",
                chat_id, user_id, bot._nav_buttons(back="nav:devices"), message_id=message_id,
                callback_query_id=callback_query_id)
        keyboard = [
            [bot._target_button("⛔ Stop Session", user_id, chat_id, "stop_session_execute", {"device_id": device_id})],
            [bot._button("Cancel", "nav:home")]
        ]
        return bot._reply("⚠️ Stop Remote Session?\n\nThis will terminate the current SŪTRA session.\n"
            "The current session credential will be invalidated.\n"
            "A new pairing will be required before reconnecting.\n\n"
            "This does not stop a coding-agent process.", chat_id, user_id, keyboard,
            message_id=message_id, callback_query_id=callback_query_id)

    if screen == "repair_confirm":
        device_id = context.get("device_id")
        device = next((item for item in bot._devices() if item["device_id"] == device_id), None)
        if not device:
            return bot._reply("This session is no longer active. A fresh pairing is required.",
                chat_id, user_id, bot._nav_buttons(back="nav:devices"), message_id=message_id,
                callback_query_id=callback_query_id)
        keyboard = [
            [bot._target_button("Continue", user_id, chat_id, "repair_execute", {"device_id": device_id})],
            [bot._button("Cancel", "nav:home")]
        ]
        return bot._reply("⚠️ Re-pair Runtime?\n\nThis will invalidate the current remote session and require a new pairing.",
            chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "pair_runtime":
        devices = bot._devices()
        connected = [d for d in devices if d["connection_status"] == "CONNECTED"]
        if connected:
            keyboard = [
                [bot._target_button("🔄 Re-pair", user_id, chat_id, "repair_confirm", {"device_id": connected[0]["device_id"]})],
                [bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply("Runtime is already connected.\nUse Re-pair if you want to invalidate the current session and create a new pairing.",
                chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)
        pending = bot.control_plane.pairing.get_pending()
        if pending:
            code = pending["pairing_code"]
            expires_in = pending["expires_in"]
            mm, ss = divmod(expires_in, 60)
            keyboard = [
                [bot._button("Cancel Pairing", "action:cancel_pairing")],
                [bot._button("🔄 Refresh", "action:pair_runtime")],
                [bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(
                f"🟡 Session: WAITING FOR PAIRING\n\nPairing code:\n{code}\n\nExpires in:\n{mm:02d}:{ss:02d}\n\n"
                f"Approve this code using:\n/pair {code}",
                chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)
        keyboard = [
            [bot._button("🔄 Check Again", "action:pair_runtime")],
            [bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(
            "No pending pairing request found.\n\n"
            "Start `sutra local` on your machine to initiate a pairing session.",
            chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "revoke_confirm":
        device_id = context["device_id"]
        device = next((item for item in bot._devices() if item["device_id"] == device_id), None)
        if not device:
            return bot._reply("Device is unavailable.", chat_id, user_id, bot._nav_buttons(back="nav:devices"),
                              message_id=message_id, callback_query_id=callback_query_id)
        keyboard = [[bot._target_button("Confirm Revoke", user_id, chat_id, "revoke_execute",
                                         {"device_id": device_id}),
                     bot._button("Cancel", "nav:devices")], *bot._nav_buttons()]
        return bot._reply(f"Revoke {device['device_name']} ({device_id})? Its active session will close.",
            chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "connectors":
        text = "🔌 CONNECTORS\n\n" + "\n".join(f"{name} · ⚪ Not Implemented" for name in CONNECTOR_NAMES)
        return bot._reply(text, chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

    if screen == "usage":
        text = ("📊 Usage\n\nProvider: ⚪ Unavailable\nModel: ⚪ Unavailable\nContext usage: ⚪ Unavailable\n"
                "Input tokens: ⚪ Unavailable\nOutput tokens: ⚪ Unavailable\nTotal tokens: ⚪ Unavailable\n"
                "Subscription quota: ⚪ Unavailable\nRate limits: ⚪ Unavailable")
        return bot._reply(text, chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

    if screen == "permissions":
        pending = [item for item in bot.pending_permissions.values() if item.get("user_id") == user_id]
        if not pending:
            policy_summary = ""
            pe = getattr(bot.control_plane, "permission_engine", None)
            if pe is not None and hasattr(pe, "list_policies"):
                try:
                    policies = pe.list_policies(enabled_only=True)
                    if policies:
                        lines = [f"  {p.scope_type}/{p.action_type} → {p.decision}" for p in policies[:10]]
                        policy_summary = "\n\n📋 Persistent Policies:\n" + "\n".join(lines)
                        if len(policies) > 10:
                            policy_summary += f"\n  … and {len(policies) - 10} more"
                    else:
                        policy_summary = "\n\n📋 No persistent policies configured (using defaults)."
                except Exception:
                    pass
            text = f"🔐 Permissions\n\nNo pending permission requests (Not Implemented).{policy_summary}"
            keyboard = [[bot._target_button("📋 View Policies", user_id, chat_id, "nav", {"screen": "policy_list"})],
                        *bot._nav_buttons()]
        else:
            permission = pending[0]
            permission_context = {"id": permission["id"], "device_id": permission.get("device_id")}
            text = (f"⚠️ PERMISSION REQUIRED\nAgent: {permission.get('agent', 'Unknown')}\n"
                f"Project: {permission.get('project', 'Unknown')}\nAction: {permission.get('action', 'Unknown')}\n"
                f"Command: {permission.get('summary', 'Unavailable')}")
            keyboard = [[bot._target_button("✅ Allow Once", user_id, chat_id, "permission", {**permission_context, "decision": "allow_once"}),
                         bot._target_button("✅ Allow Session", user_id, chat_id, "permission", {**permission_context, "decision": "allow_session"})],
                        [bot._target_button("❌ Deny", user_id, chat_id, "permission", {**permission_context, "decision": "deny"}),
                         bot._target_button("✏️ Modify", user_id, chat_id, "permission", {**permission_context, "decision": "modify"})],
                        *bot._nav_buttons()]
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "policy_list":
        pe = getattr(bot.control_plane, "permission_engine", None)
        if pe is None or not hasattr(pe, "list_policies"):
            text = "🔐 Policy Engine not initialized."
        else:
            try:
                policies = pe.list_policies()
                if not policies:
                    text = "🔐 Permission Policies\n\nNo persistent policies. Defaults apply:\n• LOW → Auto-allow\n• MEDIUM/HIGH → Ask\n• CRITICAL → Always explicit"
                else:
                    enabled_p = [p for p in policies if p.enabled]
                    disabled_p = [p for p in policies if not p.enabled]
                    lines = [f"✅ [{p.scope_type}/{p.scope_id or 'global'}] {p.action_type} → {p.decision}" for p in enabled_p]
                    lines += [f"⭕ [{p.scope_type}/{p.scope_id or 'global'}] {p.action_type} → {p.decision} (disabled)" for p in disabled_p]
                    text = "🔐 Permission Policies\n\n" + "\n".join(lines[:20])
                    if len(policies) > 20:
                        text += f"\n… and {len(policies) - 20} more"
            except Exception as exc:
                text = f"🔐 Policy list error: {exc}"
        return bot._reply(text, chat_id, user_id,
                           [[bot._button("← Permissions", "nav:permissions")]],
                           message_id=message_id, callback_query_id=callback_query_id)

    if screen == "activity" or screen == "project_activity":
        device_id = context.get("device_id") or bot.selected_devices.get(user_id)
        events = [event for event in bot.control_plane.events if event.get("device_id") == device_id][-20:]
        lines = [f"{format_time(event.get('timestamp'))} · {event.get('event', 'EVENT')} · "
                 f"{(event.get('payload') or {}).get('connection_status', 'status updated')}" for event in events]
        title = context.get("project", {}).get("name", "Recent")
        text = f"🕒 Activity · {title}\n\n" + ("\n".join(lines) if lines else "No recent normalized device events.")
        return bot._reply(text, chat_id, user_id, bot._nav_buttons(back="nav:home"), message_id=message_id, callback_query_id=callback_query_id)

    if screen == "project_status":
        try:
            _, status = await bot._call_device(user_id, "runtime.status")
            body = (f"📊 Runtime status\nConnection: {status.get('connection')}\n"
                    f"Runtime: {status.get('runtime_version')}\nWorkspaces: {status.get('workspaces')}")
        except HTTPException as exc:
            body = str(exc.detail)
        return bot._reply(body, chat_id, user_id, bot._nav_buttons(back="nav:home"), message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        if not project or not device_id:
            return bot._reply("No project selected. Select a project from 📁 Projects first.",
                             chat_id, user_id, bot._nav_buttons(back="nav:projects"),
                             message_id=message_id, callback_query_id=callback_query_id)
        pctx = {"device_id": device_id, "project": project}
        bot.active_git_context[user_id] = pctx

        try:
            res = await bot._dispatch_command(device_id, "git.status", {"project_root": project["absolute_path"]})
            status = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Git status unavailable: {exc.detail}", chat_id, user_id,
                             bot._nav_buttons(back="nav:projects"),
                             message_id=message_id, callback_query_id=callback_query_id)

        if not status.get("is_git"):
            keyboard = [
                [bot._target_button("⬅️ Back to Project", user_id, chat_id, "project", pctx),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(f"🌿 Git\n\n📁 {project.get('name', 'Project')}\n\nNot a Git repository.",
                             chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

        branch = status.get("current_branch") or "unknown"
        tracking = status.get("tracking_branch")
        tracking_str = f"Tracking: {tracking}" if tracking else "No tracking upstream"
        ahead = status.get("commits_ahead", 0)
        behind = status.get("commits_behind", 0)
        ahead_str = f" · Ahead: ↑{ahead}" if ahead else ""
        behind_str = f" · Behind: ↓{behind}" if behind else ""

        staged_count = len(status.get("staged_files", []))
        unstaged_count = len(status.get("unstaged_files", []))
        untracked_count = len(status.get("untracked_files", []))
        conflicted_count = len(status.get("conflicted_files", []))

        state_line = "State: ✅ Clean working tree" if status.get("is_clean") else f"State: 📝 Staged: {staged_count} · Unstaged: {unstaged_count} · Untracked: {untracked_count}"
        conflict_line = f"\n⚠️ Conflicts: {conflicted_count} file(s)" if conflicted_count else ""

        text = (f"🌿 Git · {project.get('name', 'Project')}\n\n"
                f"Branch: {branch}\n{tracking_str}{ahead_str}{behind_str}\n"
                f"{state_line}{conflict_line}")

        buttons = [
            [bot._target_button("📊 Status", user_id, chat_id, "git_status", pctx),
             bot._target_button("📝 Diff", user_id, chat_id, "git_diff", {**pctx, "staged": False})],
            [bot._target_button("🌿 Branches", user_id, chat_id, "git_branches", pctx),
             bot._target_button("☑️ Stage", user_id, chat_id, "git_stage", pctx)],
            [bot._target_button("💾 Commit", user_id, chat_id, "git_commit", pctx),
             bot._target_button("🚀 Push", user_id, chat_id, "git_push", pctx)],
            [bot._target_button("🔀 GitHub PR", user_id, chat_id, "git_pr", pctx)],
            [bot._target_button("⬅️ Back to Project", user_id, chat_id, "project", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_status":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.status", {"project_root": project["absolute_path"]})
            status = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Git status unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        staged = status.get("staged_files", [])
        unstaged = status.get("unstaged_files", [])
        untracked = status.get("untracked_files", [])
        conflicted = status.get("conflicted_files", [])

        lines = [f"📊 Git Status · {project.get('name')}\nBranch: {status.get('current_branch', 'unknown')}\n"]
        if staged:
            lines.append(f"Staged changes ({len(staged)}):")
            for f in staged[:6]:
                lines.append(f"  ✅ {f.get('path')} ({f.get('change_type')})")
            if len(staged) > 6:
                lines.append(f"  … and {len(staged) - 6} more")
        if unstaged:
            lines.append(f"\nUnstaged changes ({len(unstaged)}):")
            for f in unstaged[:6]:
                lines.append(f"  📝 {f.get('path')} ({f.get('change_type')})")
            if len(unstaged) > 6:
                lines.append(f"  … and {len(unstaged) - 6} more")
        if untracked:
            lines.append(f"\nUntracked files ({len(untracked)}):")
            for f in untracked[:6]:
                lines.append(f"  ❓ {f.get('path')}")
            if len(untracked) > 6:
                lines.append(f"  … and {len(untracked) - 6} more")
        if conflicted:
            lines.append(f"\n⚠️ Conflicts ({len(conflicted)}):")
            for f in conflicted[:6]:
                lines.append(f"  💥 {f.get('path')}")
        if status.get("is_clean"):
            lines.append("\n✅ Working tree clean. Nothing to commit.")

        text = "\n".join(lines)
        buttons = [
            [bot._target_button("📝 Diff", user_id, chat_id, "git_diff", {**pctx, "staged": False}),
             bot._target_button("☑️ Stage", user_id, chat_id, "git_stage", pctx)],
            [bot._target_button("💾 Commit", user_id, chat_id, "git_commit", pctx),
             bot._target_button("🚀 Push", user_id, chat_id, "git_push", pctx)],
            [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_diff":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        staged = bool(context.get("staged", False))
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.diff", {
                "project_root": project["absolute_path"],
                "staged": staged,
                "max_lines": 80,
            })
            payload = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Diff unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        diff_text = payload.get("diff_text", "").strip()
        stat = payload.get("stat_summary", "").strip()
        label = "Staged (Index)" if staged else "Working Tree (Unstaged)"
        if not diff_text:
            text = f"📝 Git Diff · {label}\n\n📁 {project.get('name')}\n\nNo changes in this diff."
        else:
            stat_part = f"\n{stat}\n" if stat else ""
            text = f"📝 Git Diff · {label}\n\n📁 {project.get('name')}{stat_part}\n```diff\n{diff_text}\n```"

        toggle_button = bot._target_button("🔄 View Working Tree Diff" if staged else "🔄 View Staged Diff",
                                           user_id, chat_id, "git_diff", {**pctx, "staged": not staged})
        buttons = [
            [toggle_button],
            [bot._target_button("☑️ Stage", user_id, chat_id, "git_stage", pctx),
             bot._target_button("💾 Commit", user_id, chat_id, "git_commit", pctx)],
            [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_branches":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.branches", {"project_root": project["absolute_path"]})
            branches = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Branches unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        current = branches.get("current", "unknown")
        tracking = branches.get("tracking_remote") or "None"
        local_b = branches.get("local_branches", [])
        text = (f"🌿 Git Branches · {project.get('name')}\n\n"
                f"Current: 🟢 {current}\nTracking: {tracking}\n\n"
                f"Local Branches ({len(local_b)}):\n" + "\n".join(f"• {b}" for b in local_b[:15]))
        buttons = [
            [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_stage":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.status", {"project_root": project["absolute_path"]})
            status = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Status unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        unstaged = [f.get("path") for f in status.get("unstaged_files", [])]
        untracked = [f.get("path") for f in status.get("untracked_files", [])]
        staged = [f.get("path") for f in status.get("staged_files", [])]
        candidates = list(dict.fromkeys(unstaged + untracked))

        selected = bot.active_stage_selection.setdefault(user_id, set())
        selected.intersection_update(set(candidates))

        buttons = []
        for f in candidates[:6]:
            checked = f in selected
            box = "☑️" if checked else "☐"
            btn_label = f"{box} {Path(f).name}"
            buttons.append([bot._target_button(btn_label, user_id, chat_id, "git_stage_toggle", {**pctx, "file": f})])

        action_row = []
        if selected:
            action_row.append(bot._target_button("Stage Selected", user_id, chat_id, "git_stage_execute", pctx))
        if candidates:
            action_row.append(bot._target_button("Stage All", user_id, chat_id, "git_stage_all", pctx))
        if staged:
            action_row.append(bot._target_button("Unstage All", user_id, chat_id, "git_unstage_all", pctx))
        if action_row:
            buttons.append(action_row)

        buttons.append([
            bot._target_button("📝 Review Diff", user_id, chat_id, "git_diff", {**pctx, "staged": True}),
            bot._target_button("💾 Commit", user_id, chat_id, "git_commit", pctx)
        ])
        buttons.append([
            bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
            bot._button("🏠 Home", "nav:home")
        ])

        summary_text = (f"🌿 Mobile Staging · {project.get('name')}\n\n"
                        f"Staged files: {len(staged)}\n"
                        f"Changes available to stage: {len(candidates)}\n"
                        f"Selected for staging: {len(selected)}")
        return bot._reply(summary_text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_commit":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.status", {"project_root": project["absolute_path"]})
            status = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Status unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        staged = status.get("staged_files", [])
        if not staged:
            text = (f"💾 Safe Commit · {project.get('name')}\n\n"
                    "⚠️ No staged changes found.\n"
                    "Please stage files first using the Stage menu.")
            buttons = [
                [bot._target_button("☑️ Stage Files", user_id, chat_id, "git_stage", pctx)],
                [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

        file_list = "\n".join(f"• {f.get('path')} ({f.get('change_type')})" for f in staged[:8])
        text = (f"💾 Safe Commit · {project.get('name')}\n\n"
                f"Staged changes ({len(staged)} files):\n{file_list}\n\n"
                "Send /commit <message> in chat, or tap Quick Commit below:")
        buttons = [
            [bot._target_button("💾 Quick Commit ('Update changes')", user_id, chat_id, "git_commit_execute", {**pctx, "message": "Update changes"})],
            [bot._target_button("📝 Review Staged Diff", user_id, chat_id, "git_diff", {**pctx, "staged": True}),
             bot._target_button("☑️ Stage Files", user_id, chat_id, "git_stage", pctx)],
            [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_push":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.push_preview", {"project_root": project["absolute_path"]})
            preview = res.get("payload", {})
        except HTTPException as exc:
            return bot._reply(f"Push preview unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        branch = preview.get("branch", "unknown")
        remote = preview.get("remote", "origin")
        remote_url = preview.get("remote_url", "")
        ahead = preview.get("commits_ahead", 0)
        summaries = preview.get("commit_summaries", [])
        warning = preview.get("warning") or "⚠️ This will update the remote repository."

        text = (f"🌿 Push Changes\n\n"
                f"Repository: {project.get('name')}\n"
                f"Remote: {remote} ({remote_url or 'no URL'})\n"
                f"Branch: {branch}\n"
                f"Commits ahead: {ahead}\n\n" +
                ("\n".join(f"• {c}" for c in summaries[:5]) + "\n\n" if summaries else "") +
                f"{warning}")

        buttons = []
        if preview.get("ready_to_push") or ahead > 0:
            buttons.append([bot._target_button("🚀 Confirm Push", user_id, chat_id, "git_push_execute", pctx)])
        buttons.append([
            bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
            bot._button("🏠 Home", "nav:home")
        ])
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen in ("git_pr", "git_github_hub"):
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            preview_res = await bot._dispatch_command(device_id, "git.push_preview", {"project_root": project["absolute_path"]})
            preview = preview_res.get("payload", {})
            remote_url = preview.get("remote_url", "")
            branch = preview.get("branch", "main")
        except HTTPException as exc:
            return bot._reply(f"GitHub PR check unavailable: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        from ...git.github import parse_github_remote
        info = parse_github_remote(remote_url)
        if not info.is_github:
            text = (f"🔀 GitHub Integration\n\n"
                    f"Repository: {project.get('name')}\n\n"
                    f"No GitHub remote is configured for this repository ({remote_url or 'no remote'}).\n"
                    "Local Git features (status, diff, branch, stage, commit, push) continue to work normally.")
            buttons = [
                [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

        text = (f"🔀 GitHub Collaboration\n\n"
                f"Repository: {info.owner}/{info.repo}\n"
                f"Current branch: {branch}\n\n"
                "Manage Pull Requests, inspect CI workflows, review comments, and check releases without leaving Telegram:")
        buttons = [
            [bot._target_button("📋 Pull Requests", user_id, chat_id, "git_pr_list", pctx),
             bot._target_button("➕ Open PR", user_id, chat_id, "git_pr_create_screen", pctx)],
            [bot._target_button("⚙️ CI / Actions", user_id, chat_id, "git_ci", pctx),
             bot._target_button("🏷️ Releases", user_id, chat_id, "git_releases", pctx)],
            [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_create_screen":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        preview_res = await bot._dispatch_command(device_id, "git.push_preview", {"project_root": project["absolute_path"]})
        preview = preview_res.get("payload", {})
        branch = preview.get("branch", "main")
        text = (f"🔀 Create Pull Request\n\n"
                f"Head: {branch}\n"
                f"Base: main\n\n"
                "Send /pr <title> in chat, or tap Create PR below:")
        buttons = [
            [bot._target_button("🔀 Create PR", user_id, chat_id, "git_pr_execute", {**pctx, "title": f"Update {branch}", "head": branch, "base": "main"})],
            [bot._target_button("⬅️ Back to GitHub", user_id, chat_id, "git_github_hub", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_list":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        state_filter = str(context.get("state", "open"))
        pctx = {"device_id": device_id, "project": project, "state": state_filter}
        try:
            res = await bot._dispatch_command(device_id, "git.pr_list", {
                "project_root": project["absolute_path"],
                "state": state_filter,
                "per_page": 8,
            })
            items = res.get("payload", {}).get("items", [])
        except HTTPException as exc:
            return bot._reply(f"PR listing failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        text = f"📋 Pull Requests ({state_filter.upper()}) · {project.get('name')}\n\n"
        if not items:
            text += f"No {state_filter} pull requests found."
        else:
            text += f"Showing {len(items)} pull requests. Tap any PR to inspect details:\n"

        buttons = []
        for pr in items[:6]:
            pr_num = pr.get("number")
            pr_title = (pr.get("title") or "Untitled")[:24]
            btn_label = f"#{pr_num} {pr_title}"
            buttons.append([bot._target_button(btn_label, user_id, chat_id, "git_pr_detail", {**pctx, "number": pr_num})])

        filter_row = []
        if state_filter != "open":
            filter_row.append(bot._target_button("🟢 Open", user_id, chat_id, "git_pr_list", {**pctx, "state": "open"}))
        if state_filter != "closed":
            filter_row.append(bot._target_button("⚪ Closed", user_id, chat_id, "git_pr_list", {**pctx, "state": "closed"}))
        if state_filter != "all":
            filter_row.append(bot._target_button("🟣 All", user_id, chat_id, "git_pr_list", {**pctx, "state": "all"}))
        if filter_row:
            buttons.append(filter_row)

        buttons.append([
            bot._target_button("➕ New PR", user_id, chat_id, "git_pr_create_screen", pctx),
            bot._target_button("⬅️ Back to GitHub", user_id, chat_id, "git_github_hub", pctx),
        ])
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_detail":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pr_number = int(context.get("number", 0))
        pctx = {"device_id": device_id, "project": project, "number": pr_number}

        try:
            # 1. PR Details
            pr_res = await bot._dispatch_command(device_id, "git.pr_get", {
                "project_root": project["absolute_path"],
                "number": pr_number,
            })
            pr = pr_res.get("payload", {}).get("pr", {})
            if not pr:
                return bot._reply(f"PR #{pr_number} not found.", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

            # 2. Reviews
            rev_res = await bot._dispatch_command(device_id, "git.pr_reviews", {
                "project_root": project["absolute_path"],
                "number": pr_number,
            })
            reviews = rev_res.get("payload", {})

            # 3. CI Runs for head branch
            head_branch = pr.get("head_branch")
            ci_runs = []
            if head_branch:
                ci_res = await bot._dispatch_command(device_id, "git.ci_runs", {
                    "project_root": project["absolute_path"],
                    "branch": head_branch,
                    "per_page": 3,
                })
                ci_runs = ci_res.get("payload", {}).get("runs", [])

        except HTTPException as exc:
            return bot._reply(f"PR inspection failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        ci_summary_lines = []
        if ci_runs:
            for r in ci_runs[:3]:
                icon = "✅" if r.get("conclusion") == "success" else ("❌" if r.get("conclusion") == "failure" else "🟡")
                ci_summary_lines.append(f"{icon} {r.get('name')} ({r.get('conclusion') or r.get('status')})")
        else:
            ci_summary_lines.append("⚪ No runs reported")

        merge_status = "🟣 Merged" if pr.get("merged") else ("⚪ Closed" if pr.get("state") == "closed" else ("🔴 Blocked" if pr.get("mergeable") is False else "🟢 Clean"))

        text = (f"🔀 PR #{pr.get('number')} · {pr.get('title')}\n"
                f"{pr.get('head_branch')} → {pr.get('base_branch')}\n"
                f"Author: @{pr.get('author')}\n"
                f"Status: {pr.get('state', '').upper()}\n\n"
                f"Changes:\n"
                f"{pr.get('changed_files_count', 0)} files (+{pr.get('additions', 0)} / -{pr.get('deletions', 0)})\n\n"
                f"Reviews:\n"
                f"{reviews.get('summary_text', '⚪ No reviews')}\n\n"
                f"CI / Checks:\n" + "\n".join(ci_summary_lines) + "\n\n"
                f"Merge Status: {merge_status}")

        buttons = [
            [bot._target_button("📁 Files", user_id, chat_id, "git_pr_files", pctx),
             bot._target_button("💬 Comments", user_id, chat_id, "git_pr_comments", pctx)],
            [bot._target_button("🔍 Reviews", user_id, chat_id, "git_pr_reviews", pctx),
             bot._target_button("⚙️ Checks", user_id, chat_id, "git_ci", {**pctx, "branch": head_branch})],
        ]
        if not pr.get("merged") and pr.get("state") == "open":
            buttons.append([bot._target_button("🔀 Merge PR", user_id, chat_id, "git_pr_merge_confirm", pctx)])
        buttons.append([
            bot._target_button("⬅️ Back to PRs", user_id, chat_id, "git_pr_list", pctx),
            bot._button("🏠 Home", "nav:home")
        ])
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_files":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pr_number = int(context.get("number", 0))
        pctx = {"device_id": device_id, "project": project, "number": pr_number}
        try:
            res = await bot._dispatch_command(device_id, "git.pr_files", {
                "project_root": project["absolute_path"],
                "number": pr_number,
                "per_page": 20,
            })
            files = res.get("payload", {}).get("files", [])
        except HTTPException as exc:
            return bot._reply(f"File listing failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        lines = [f"📁 PR #{pr_number} Changed Files ({len(files)}):\n"]
        for f in files[:15]:
            status_char = "+" if f.get("status") == "added" else ("-" if f.get("status") == "removed" else "•")
            lines.append(f"{status_char} {f.get('filename')} (+{f.get('additions', 0)} / -{f.get('deletions', 0)})")
        if len(files) > 15:
            lines.append(f"... and {len(files) - 15} more files")

        text = "\n".join(lines)
        buttons = [
            [bot._target_button("⬅️ Back to PR #" + str(pr_number), user_id, chat_id, "git_pr_detail", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_comments":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pr_number = int(context.get("number", 0))
        pctx = {"device_id": device_id, "project": project, "number": pr_number}
        try:
            res = await bot._dispatch_command(device_id, "git.pr_comments", {
                "project_root": project["absolute_path"],
                "number": pr_number,
                "per_page": 10,
            })
            comments = res.get("payload", {}).get("comments", [])
        except HTTPException as exc:
            return bot._reply(f"Comment listing failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        lines = [f"💬 PR #{pr_number} Comments ({len(comments)}):\n"]
        if not comments:
            lines.append("No comments on this PR yet.\n")
        else:
            for c in comments[-5:]:
                body_preview = (c.get("body") or "")[:120].replace("\n", " ")
                lines.append(f"@{c.get('author')}: {body_preview}\n")

        lines.append("To post a comment, send:\n/pr comment " + str(pr_number) + ' "<message>"')
        text = "\n".join(lines)
        buttons = [
            [bot._target_button("⬅️ Back to PR #" + str(pr_number), user_id, chat_id, "git_pr_detail", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_reviews":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pr_number = int(context.get("number", 0))
        pctx = {"device_id": device_id, "project": project, "number": pr_number}
        try:
            res = await bot._dispatch_command(device_id, "git.pr_reviews", {
                "project_root": project["absolute_path"],
                "number": pr_number,
            })
            rev_data = res.get("payload", {})
            reviews = rev_data.get("reviews", [])
            requested = rev_data.get("requested_reviewers", [])
        except HTTPException as exc:
            return bot._reply(f"Reviews inspection failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        lines = [f"🔍 PR #{pr_number} Review State:\n", f"{rev_data.get('summary_text', '')}\n"]
        if requested:
            lines.append(f"Requested reviewers: {', '.join('@' + r for r in requested)}\n")
        if reviews:
            lines.append("Submitted reviews:")
            for r in reviews:
                lines.append(f"• @{r.get('reviewer')}: {r.get('state')}")
        text = "\n".join(lines)
        buttons = [
            [bot._target_button("⬅️ Back to PR #" + str(pr_number), user_id, chat_id, "git_pr_detail", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_pr_merge_confirm":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pr_number = int(context.get("number", 0))
        pctx = {"device_id": device_id, "project": project, "number": pr_number}

        # Freshness check before displaying confirmation
        res = await bot._dispatch_command(device_id, "git.pr_get", {
            "project_root": project["absolute_path"],
            "number": pr_number,
        })
        pr = res.get("payload", {}).get("pr", {})
        if pr.get("merged"):
            return bot._reply(f"PR #{pr_number} is already merged.", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)
        if pr.get("mergeable") is False:
            return bot._reply(f"PR #{pr_number} has unresolved conflicts and cannot be merged.", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        text = (f"⚠️ Confirm Pull Request Merge\n\n"
                f"PR #{pr_number}: {pr.get('title')}\n"
                f"{pr.get('head_branch')} → {pr.get('base_branch')}\n\n"
                f"Merge requires explicit human authorization.\n"
                "Select merge method:")
        buttons = [
            [bot._target_button("🔀 Merge Commit", user_id, chat_id, "git_pr_merge_execute", {**pctx, "merge_method": "merge"}),
             bot._target_button("📦 Squash & Merge", user_id, chat_id, "git_pr_merge_execute", {**pctx, "merge_method": "squash"})],
            [bot._target_button("🔄 Rebase & Merge", user_id, chat_id, "git_pr_merge_execute", {**pctx, "merge_method": "rebase"})],
            [bot._target_button("❌ Cancel", user_id, chat_id, "git_pr_detail", pctx)]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_ci":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        branch = context.get("branch")
        pctx = {"device_id": device_id, "project": project, "branch": branch}
        payload = {"project_root": project["absolute_path"], "per_page": 6}
        if branch:
            payload["branch"] = branch
        try:
            res = await bot._dispatch_command(device_id, "git.ci_runs", payload)
            runs = res.get("payload", {}).get("runs", [])
        except HTTPException as exc:
            return bot._reply(f"CI runs query failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        text = f"⚙️ GitHub Actions / CI · {project.get('name')}\n\n"
        if branch:
            text += f"Filtered to branch: {branch}\n\n"
        if not runs:
            text += "No workflow runs reported."
        else:
            text += f"Showing {len(runs)} recent workflow runs:\n"

        buttons = []
        for r in runs:
            r_id = r.get("id")
            icon = "✅" if r.get("conclusion") == "success" else ("❌" if r.get("conclusion") == "failure" else "🟡")
            label = f"{icon} {r.get('name')} (#{r_id})"
            buttons.append([bot._target_button(label, user_id, chat_id, "git_ci_run_detail", {**pctx, "run_id": r_id})])

        buttons.append([
            bot._target_button("⬅️ Back to GitHub", user_id, chat_id, "git_github_hub", pctx),
            bot._button("🏠 Home", "nav:home")
        ])
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_ci_run_detail":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        run_id = int(context.get("run_id", 0))
        pctx = {"device_id": device_id, "project": project, "run_id": run_id}
        try:
            res = await bot._dispatch_command(device_id, "git.ci_run_get", {
                "project_root": project["absolute_path"],
                "run_id": run_id,
            })
            run = res.get("payload", {}).get("run", {})
        except HTTPException as exc:
            return bot._reply(f"CI run fetch failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        icon = "✅" if run.get("conclusion") == "success" else ("❌" if run.get("conclusion") == "failure" else "🟡")
        text = (f"⚙️ Workflow Run #{run_id}\n\n"
                f"Workflow: {run.get('name')}\n"
                f"Branch: {run.get('head_branch')} ({run.get('head_sha')})\n"
                f"Status: {run.get('status')} · {icon} {run.get('conclusion') or 'pending'}\n"
                f"Event: {run.get('event')}\n"
                f"Run date: {run.get('created_at')}")

        buttons = [
            [bot._target_button("📜 View Logs", user_id, chat_id, "git_ci_logs", pctx)],
            [bot._target_button("🔄 Rerun Workflow", user_id, chat_id, "git_ci_rerun_confirm", pctx),
             bot._target_button("⏹ Cancel Run", user_id, chat_id, "git_ci_cancel_confirm", pctx)],
            [bot._target_button("⬅️ Back to CI Runs", user_id, chat_id, "git_ci", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_ci_logs":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        run_id = int(context.get("run_id", 0))
        pctx = {"device_id": device_id, "project": project, "run_id": run_id}
        try:
            res = await bot._dispatch_command(device_id, "git.ci_run_logs", {
                "project_root": project["absolute_path"],
                "run_id": run_id,
                "max_lines": 50,
            })
            log_data = res.get("payload", {})
            log_text = log_data.get("log_text") or "No log output recorded."
        except HTTPException as exc:
            return bot._reply(f"Log retrieval failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        text = f"📜 Logs · Run #{run_id}\n\n```\n{log_text[:3500]}\n```"
        buttons = [
            [bot._target_button("⬅️ Back to Run #" + str(run_id), user_id, chat_id, "git_ci_run_detail", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "git_releases":
        project = context.get("project") or bot.active_git_context.get(user_id, {}).get("project")
        device_id = context.get("device_id") or bot.active_git_context.get(user_id, {}).get("device_id")
        pctx = {"device_id": device_id, "project": project}
        try:
            res = await bot._dispatch_command(device_id, "git.releases_list", {
                "project_root": project["absolute_path"],
                "per_page": 5,
            })
            releases = res.get("payload", {}).get("releases", [])
        except HTTPException as exc:
            return bot._reply(f"Releases listing failed: {exc.detail}", chat_id, user_id, bot._nav_buttons(), message_id=message_id, callback_query_id=callback_query_id)

        lines = [f"🏷️ Releases & Tags · {project.get('name')}\n"]
        if not releases:
            lines.append("No releases found.")
        else:
            for r in releases:
                date = (r.get("published_at") or "")[:10]
                lines.append(f"• {r.get('tag_name')} ({r.get('name') or 'Release'}) · {date}\n  {r.get('html_url')}")

        text = "\n".join(lines)
        buttons = [
            [bot._target_button("⬅️ Back to GitHub", user_id, chat_id, "git_github_hub", pctx),
             bot._button("🏠 Home", "nav:home")]
        ]
        return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_query_id)


    if screen == "settings":
        keyboard = [[bot._button(name, "setting:" + str(index))] for index, name in enumerate(SETTING_NAMES)]
        keyboard.extend(bot._nav_buttons())
        return bot._reply("⚙️ Settings\n\nChoose a settings area.", chat_id, user_id, keyboard,
                           message_id=message_id, callback_query_id=callback_query_id)

    if screen == "setting":
        index = context.get("index")
        if not isinstance(index, int) or not 0 <= index < len(SETTING_NAMES):
            return bot._reply("Settings selection expired.", chat_id, user_id, bot._nav_buttons(back="nav:settings"),
                               message_id=message_id, callback_query_id=callback_query_id)
        name = SETTING_NAMES[index]
        if index == 0:
            return await bot._screen("access_roots", {}, chat_id, user_id, message_id=message_id, callback_query_id=callback_query_id)
        elif index == 2:
            detail = "Connector registry is ⚪ Not Implemented.\n\n" + "\n".join(CONNECTOR_NAMES)
        elif index == 6:
            detail = "Device credentials are never shown here. Revoke a paired device from Devices. TLS and allowlist configuration are managed by the operator."
        else:
            detail = "This settings area is ⚪ Not Implemented."
        return bot._reply(f"{name}\n\n{detail}", chat_id, user_id,
            bot._nav_buttons(back="nav:settings"), message_id=message_id, callback_query_id=callback_query_id)

    if screen == "access_roots":
        try:
            device_id, roots = await bot._call_device(user_id, "workspace.list")
            root_items = roots.get("items", [])
        except HTTPException as exc:
            root_items = []
            error = str(exc.detail)
        else:
            error = ""
        status_msg = context.get("status_message", "")
        lines = []
        for path in root_items:
            lines.append(f"• {path} ✅ Authorized")
        root_display = "\n".join(lines) if lines else "No authorized access roots."
        text_parts = ["🔐 Access Roots\n"]
        if status_msg:
            text_parts.append(status_msg + "\n")
        text_parts.append("Roots are scanned only for bounded project discovery. File access remains strictly limited to discovered projects.\n")
        text_parts.append(error or root_display)
        text = "\n".join(text_parts)
        keyboard = [[bot._button("➕ Add Access Root", "nav:add_access_root")]]
        keyboard.extend(bot._nav_buttons(back="nav:settings"))
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "add_access_root":
        candidates: list[str] = []
        if os.name == "nt":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if Path(drive).exists():
                    candidates.append(drive)
            if "D:\\" not in candidates:
                candidates.append("D:\\")
        else:
            candidates.append("/")
        try:
            device_id, roots = await bot._call_device(user_id, "workspace.list")
            existing_roots = {_access_root_key(str(r)) for r in roots.get("items", [])}
        except HTTPException as exc:
            return bot._reply(f"Could not check authorized roots: {exc.detail}", chat_id, user_id,
                bot._nav_buttons(back="nav:access_roots"), message_id=message_id,
                callback_query_id=callback_query_id)
        keyboard = []
        for cand in candidates:
            if _access_root_key(cand) in existing_roots:
                continue
            label = f"📁 {cand}"
            keyboard.append([bot._target_button(label, user_id, chat_id,
                "access_root_request", {"device_id": device_id, "root": cand})])
        keyboard.extend(bot._nav_buttons(back="nav:access_roots"))
        prompt = ("All detected access roots are already authorized." if not any(
            row and row[0].get("callback_data", "").startswith("target:") for row in keyboard)
            else "Select an access root below to authorize project discovery:")
        text = "➕ Add Access Root\n\n" + prompt
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "access_root_confirm":
        from ...security import validate_access_root
        try:
            root = str(context["root"])
            canonical = str(validate_access_root(root))
            _, roots = await bot._call_device(user_id, "workspace.list")
        except (KeyError, TypeError, ValueError, OSError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return bot._reply(f"Could not verify access root: {detail}", chat_id, user_id,
                bot._nav_buttons(back="nav:access_roots"), message_id=message_id,
                callback_query_id=callback_query_id)
        if any(_access_root_key(str(item)) == _access_root_key(canonical)
               for item in roots.get("items", [])):
            return await bot._screen("access_roots", {
                "status_message": f"{canonical} is already authorized."}, chat_id, user_id,
                message_id, callback_query_id)
        device_id = context.get("device_id") or bot.selected_devices.get(user_id)
        pctx = {"device_id": device_id, "root": root}
        text = (f"⚠️ Access Root Authorization\n\n"
                f"You are about to allow SŪTRA to discover projects under:\n\n"
                f"{root}\n\n"
                f"This does NOT automatically grant an agent unrestricted access to the entire drive.")
        keyboard = [
            [bot._target_button(f"✅ Allow {root}", user_id, chat_id, "access_root_allow", pctx)],
            [bot._target_button("❌ Reject", user_id, chat_id, "access_root_reject", pctx)],
            *bot._nav_buttons(back="nav:add_access_root")
        ]
        return bot._reply(text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_query_id)

    if screen == "device_pair":
        return bot._reply("Pair New Device\n\nStart `sutra local` with the control-plane URL on the new device, then approve its short-lived code here with /pair CODE.",
            chat_id, user_id, bot._nav_buttons(back="nav:devices"), message_id=message_id, callback_query_id=callback_query_id)

    return bot._reply("This screen is unavailable.", chat_id, user_id, bot._nav_buttons(),
                       message_id=message_id, callback_query_id=callback_query_id)
