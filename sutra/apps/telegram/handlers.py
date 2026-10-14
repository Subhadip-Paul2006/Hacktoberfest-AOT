"""Update and command handlers for SŪTRA Telegram ChatOps."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

from .keyboards import HOME, SECTIONS

log = logging.getLogger(__name__)


async def valid_project_context(bot: Any, user_id: int, context: Dict[str, Any]) -> bool:
    """Verify that a path is bounded within an authorized project."""
    try:
        device_id = bot._select_device(str(context["device_id"]), user_id)
        project_root = str(context["project"]["absolute_path"])
        path = str(context.get("path") or project_root)
        result = await bot._dispatch_command(device_id, "file.list",
            {"project_root": project_root, "path": path})
        return result.get("status") == "success"
    except (HTTPException, KeyError, TypeError, ValueError):
        return False


async def dispatch_command(
    bot: Any,
    device_id: str,
    command: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch a control-plane command to a connected device."""
    return await bot.control_plane.dispatch_command(
        device_id,
        command,
        payload or {},
        message_id=bot._active_message_id.get(),
    )


async def process_update(
    bot: Any,
    update: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Any:
    """Entry point for an update, managing context and error boundaries."""
    callback = update.get("callback_query")
    message = ((callback or {}).get("message") if isinstance(callback, dict)
               else update.get("message")) or {}
    message_id = message.get("message_id") if isinstance(message.get("message_id"), int) else None
    token = bot._active_message_id.set(str(message_id) if message_id is not None else None)
    try:
        return await bot._process_update(update, client)
    finally:
        bot._active_message_id.reset(token)


async def internal_process_update(
    bot: Any,
    update: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Any:
    """Process update with deduplication and idempotency guards."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    idem_key = f"tg_update:{update_id}"
    action, cached = await bot.idempotency_store.check_and_start(idem_key, update)
    if action == "COMPLETED":
        return cached
    if action == "PROCESSING":
        if isinstance(cached, asyncio.Future):
            try:
                return await asyncio.wait_for(asyncio.shield(cached), timeout=15.0)
            except Exception:
                return None
        return None

    if not bot.control_plane.store.mark_telegram_update(update_id):
        await bot.idempotency_store.complete(idem_key, None)
        return None

    try:
        res = await bot._do_process_update(update, client)
        await bot.idempotency_store.complete(idem_key, res)
        return res
    except BaseException as exc:
        await bot.idempotency_store.fail(idem_key, exc=exc)
        raise


async def do_process_update(
    bot: Any,
    update: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Any:
    """Route callback query or text command update."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from") or {}
        user_id = sender.get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if (not isinstance(user_id, int) or user_id not in bot.allowed_user_ids
                or chat.get("type") != "private" or not isinstance(chat_id, int)):
            return None
        allowed, retry_after = bot.rate_limiter.is_allowed(f"user:{user_id}")
        if not allowed:
            wait_sec = max(1, int(math.ceil(retry_after)))
            callback_id = callback.get("id") if isinstance(callback.get("id"), str) else None
            message_id = message.get("message_id") if isinstance(message.get("message_id"), int) else None
            return bot._reply(
                f"⚠️ Rate limit exceeded. Please wait {wait_sec}s.",
                chat_id, user_id, bot._nav_buttons(),
                message_id=message_id, callback_query_id=callback_id
            )
        started_ns = time.perf_counter_ns()
        result = await bot._handle_callback(update, user_id, chat_id)
        duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        bot.control_plane.metrics.record("telegram_callback_processing", duration_ms)
        bot.control_plane.metrics.record("telegram_handler_ms", duration_ms)
        return result

    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(user_id, int) or user_id not in bot.allowed_user_ids or chat.get("type") != "private" or not isinstance(chat_id, int):
        return None
    allowed, retry_after = bot.rate_limiter.is_allowed(f"user:{user_id}")
    if not allowed:
        wait_sec = max(1, int(math.ceil(retry_after)))
        return f"⚠️ Rate limit exceeded. Please wait {wait_sec}s."
    return await handle_message_command(bot, message, user_id, chat_id)


async def handle_message_command(
    bot: Any,
    message: Dict[str, Any],
    user_id: int,
    chat_id: int,
) -> Any:
    """Process standard text commands and persistent reply-keyboard presses."""
    text = message.get("text", "")
    pending_prompts = getattr(bot, "pending_agent_prompts", {})
    pending = pending_prompts.get(user_id) if isinstance(pending_prompts, dict) else None
    max_text = 4096
    if not isinstance(text, str) or len(text) > max_text:
        return "Unsupported request."
    label = text.strip()
    if pending:
        if time.time() - float(pending.get("created_at", 0)) > 600:
            pending_prompts.pop(user_id, None)
            return "Agent prompt expired. Reopen the Agents screen and try again."
        if pending.get("chat_id") != chat_id:
            return "This prompt belongs to another chat. Reopen the Agents screen here."
        if label == "/cancel":
            pending_prompts.pop(user_id, None)
            return "Agent prompt cancelled. No task was submitted."
        if not label:
            return "Prompt must not be empty. Send the task text or reopen the Agents screen."
        pending = pending_prompts.pop(user_id)
        if len(bot.agent_delivery_tasks) >= 100:
            return "Output delivery is at its active-session limit. Wait for a session to finish before starting another."
        device_id = str(pending["device_id"])
        bot.selected_devices[user_id] = device_id
        try:
            project_root = str(pending["project"]["absolute_path"])
            baseline = None
            try:
                baseline_result = await bot._dispatch_command(device_id, "git.status", {"project_root": project_root})
                if baseline_result.get("status") == "success":
                    baseline = _git_status_paths(baseline_result.get("payload", {}))
            except Exception:
                pass
            if pending["mode"] == "start":
                result = await bot._dispatch_command(device_id, "agent.start", {
                    "agent_id": pending["agent_id"],
                    "project_root": pending["project"]["absolute_path"],
                    "prompt": text,
                })
                if result.get("status") != "success":
                    return "Agent start was not accepted by the paired runtime."
                session = result.get("payload", {}).get("session", {})
                session_id = str(session.get("session_id", ""))
            else:
                session_id = str(pending["session_id"])
                status_result = await bot._dispatch_command(device_id, "agent.status", {"session_id": session_id})
                session = status_result.get("payload", {}).get("session", {})
                if session.get("status") in {"RUNNING", "WAITING", "BLOCKED"}:
                    result = await bot._dispatch_command(device_id, "agent.prompt", {"session_id": session_id, "prompt": text})
                    if result.get("status") != "success":
                        return "The existing agent session did not accept the prompt."
                else:
                    result = await bot._dispatch_command(device_id, "agent.start", {
                        "agent_id": str(pending["agent_id"] or session.get("agent_id", "")),
                        "project_root": pending["project"]["absolute_path"],
                        "prompt": text,
                    })
                    if result.get("status") != "success":
                        return "The previous execution is complete and a new execution was not accepted."
                    session = result.get("payload", {}).get("session", {})
                    session_id = str(session.get("session_id", ""))
            if not session_id:
                return "The runtime accepted the request but returned no session identifier. Check Agents → Status."
            task_key = f"{device_id}:{session_id}"
            if pending["mode"] == "start" or session_id != str(pending.get("session_id", "")):
                bot.agent_file_baselines[task_key] = baseline
            current_task = bot.agent_delivery_tasks.get(task_key)
            if current_task and not current_task.done():
                return "This agent session already has an output delivery task."
            task = asyncio.create_task(_deliver_agent_events(bot, user_id, chat_id, device_id, session_id))
            bot.agent_delivery_tasks[task_key] = task
            task.add_done_callback(lambda _task, key=task_key: bot.agent_delivery_tasks.pop(key, None))
            await bot._send_text(None, chat_id, f"✅ Agent accepted the prompt. Session: {session_id}\nPrompt limit: 4,096 characters.")
            return None
        except (HTTPException, KeyError, TypeError, ValueError) as exc:
            return f"Agent request was not accepted: {getattr(exc, 'detail', str(exc))}"
    parts = label.split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command in ("/start", "/menu") or label == HOME:
        return await bot._screen("home", {}, chat_id, user_id, show_navigation=True)
    if label in SECTIONS:
        return await bot._screen(SECTIONS[label], {}, chat_id, user_id,
            bot.screen_messages.get((chat_id, user_id)))
    if command == "/help":
        return "Use the navigation buttons. Compatibility commands: /pair CODE · /devices · /status [DEVICE_ID] · /workspaces · /projects · /revoke DEVICE_ID · /jump LINE · /search TERM"
    if command == "/jump":
        if len(parts) != 2:
            return "Usage: /jump LINE_NUMBER"
        try:
            line_no = int(parts[1])
        except ValueError:
            return "Invalid line number."
        ctx = bot.active_file_context.get(user_id)
        if not ctx:
            return "No active file selected. Navigate to a file in 📁 Projects first."
        return await bot._screen("preview", {**ctx, "start_line": max(1, line_no)}, chat_id, user_id)
    if command == "/search":
        if len(parts) < 2:
            return "Usage: /search SEARCH_TERM"
        query_text = " ".join(parts[1:])
        ctx = bot.active_file_context.get(user_id)
        if not ctx:
            return "No active file selected. Navigate to a file in 📁 Projects first."
        try:
            res = await bot._dispatch_command(ctx["device_id"], "file.search", {
                "project_root": ctx["project"]["absolute_path"],
                "path": ctx["path"],
                "query": query_text,
            })
            search_data = res.get("payload", {})
        except HTTPException as exc:
            return str(exc.detail)
        return await bot._screen("search_results", {**ctx, "query": query_text, "search_result": search_data}, chat_id, user_id)
    if command == "/commit":
        if len(parts) < 2:
            return "Usage: /commit <commit message>"
        msg = " ".join(parts[1:])
        ctx = bot.active_git_context.get(user_id)
        if not ctx:
            return "No active Git repository selected. Navigate to 🔀 Git in 📁 Projects first."
        device_id = ctx["device_id"]
        project = ctx["project"]
        p_root = project["absolute_path"]

        pe = getattr(bot.control_plane, "permission_engine", None)
        if pe is not None:
            from ...permissions import PermissionRequest, PermissionRiskLevel
            import uuid
            req = PermissionRequest(
                request_id=str(uuid.uuid4()),
                action_type="git_commit",
                resource_path=p_root,
                command=f'git commit -m "{msg}"',
                agent_id="Git ChatOps",
                session_id=str(user_id),
                user_id=user_id,
                risk_level=PermissionRiskLevel.HIGH,
                description=f"Commit staged changes for {project.get('name')}",
            )
            allowed, reason = pe.check_permission(req, workspace_path=p_root)
            if not allowed:
                bot.pending_permissions[req.request_id] = {
                    "id": req.request_id,
                    "agent": "Git ChatOps",
                    "project": project.get("name"),
                    "action": "git_commit",
                    "summary": f'Commit: "{msg}"',
                    "user_id": user_id,
                    "device_id": device_id,
                }
                return await bot._screen("permissions", {}, chat_id, user_id)

        try:
            res = await bot._dispatch_command(device_id, "git.commit", {"project_root": p_root, "message": msg})
            payload = res.get("payload", {})
            if payload.get("success"):
                return f"✅ Commit created: `{payload.get('commit_hash', '')[:8]}`\n\n{msg}"
            return f"❌ Commit failed: {payload.get('error', 'Unknown error')}"
        except HTTPException as exc:
            return str(exc.detail)
    if command == "/pr":
        if len(parts) < 2:
            return "Usage: /pr <title> OR /pr comment <number> <message> OR /pr merge <number> [merge|squash|rebase]"

        subcmd = parts[1].lower()
        if subcmd == "comment":
            if len(parts) < 4:
                return 'Usage: /pr comment <number> "<comment message>"'
            try:
                pr_number = int(parts[2])
            except ValueError:
                return f"Invalid PR number: {parts[2]}. Must be a positive integer."
            comment_text = " ".join(parts[3:]).strip()
            if (comment_text.startswith('"') and comment_text.endswith('"')) or (comment_text.startswith("'") and comment_text.endswith("'")):
                comment_text = comment_text[1:-1].strip()

            ctx = bot.active_git_context.get(user_id)
            if not ctx:
                return "No active Git repository selected. Navigate to 🔀 Git in 📁 Projects first."
            device_id = ctx["device_id"]
            project = ctx["project"]
            p_root = project["absolute_path"]

            pe = getattr(bot.control_plane, "permission_engine", None)
            if pe is not None:
                from ...permissions import PermissionRequest, PermissionRiskLevel
                import uuid
                req = PermissionRequest(
                    request_id=str(uuid.uuid4()),
                    action_type="git_pr_comment",
                    resource_path=p_root,
                    command=f'add_pr_comment(#{pr_number}, "{comment_text[:50]}")',
                    agent_id="Git ChatOps",
                    session_id=str(user_id),
                    user_id=user_id,
                    risk_level=PermissionRiskLevel.HIGH,
                    description=f"Add comment to PR #{pr_number} for {project.get('name')}",
                )
                allowed, reason = pe.check_permission(req, workspace_path=p_root)
                if not allowed:
                    bot.pending_permissions[req.request_id] = {
                        "id": req.request_id,
                        "agent": "Git ChatOps",
                        "project": project.get("name"),
                        "action": "git_pr_comment",
                        "summary": f'PR #{pr_number} comment: "{comment_text[:40]}"',
                        "user_id": user_id,
                        "device_id": device_id,
                    }
                    return await bot._screen("permissions", {}, chat_id, user_id)

            try:
                res = await bot._dispatch_command(device_id, "git.pr_comment_create", {
                    "project_root": p_root,
                    "number": pr_number,
                    "comment": comment_text,
                })
                payload = res.get("payload", {})
                if payload.get("success"):
                    return f"✅ Comment posted to PR #{pr_number}!\n\n\"{comment_text}\""
                return f"❌ Failed to post comment: {payload.get('error', 'Remote error')}"
            except HTTPException as exc:
                return str(exc.detail)

        if subcmd == "merge":
            if len(parts) < 3:
                return "Usage: /pr merge <number> [merge|squash|rebase]"
            try:
                pr_number = int(parts[2])
            except ValueError:
                return f"Invalid PR number: {parts[2]}. Must be a positive integer."
            merge_method = parts[3].lower() if len(parts) > 3 else "merge"
            if merge_method not in {"merge", "squash", "rebase"}:
                return f"Unsupported merge method: '{merge_method}'. Choose from: merge, squash, rebase."

            ctx = bot.active_git_context.get(user_id)
            if not ctx:
                return "No active Git repository selected. Navigate to 🔀 Git in 📁 Projects first."
            device_id = ctx["device_id"]
            project = ctx["project"]
            p_root = project["absolute_path"]

            pe = getattr(bot.control_plane, "permission_engine", None)
            if pe is not None:
                from ...permissions import PermissionRequest, PermissionRiskLevel
                import uuid
                req = PermissionRequest(
                    request_id=str(uuid.uuid4()),
                    action_type="git_pr_merge",
                    resource_path=p_root,
                    command=f'merge_pull_request(#{pr_number}, method="{merge_method}")',
                    agent_id="Git ChatOps",
                    session_id=str(user_id),
                    user_id=user_id,
                    risk_level=PermissionRiskLevel.HIGH,
                    description=f"Merge PR #{pr_number} via {merge_method} for {project.get('name')}",
                )
                allowed, reason = pe.check_permission(req, workspace_path=p_root)
                if not allowed:
                    bot.pending_permissions[req.request_id] = {
                        "id": req.request_id,
                        "agent": "Git ChatOps",
                        "project": project.get("name"),
                        "action": "git_pr_merge",
                        "summary": f'Merge PR #{pr_number} ({merge_method})',
                        "user_id": user_id,
                        "device_id": device_id,
                    }
                    return await bot._screen("permissions", {}, chat_id, user_id)

            try:
                res = await bot._dispatch_command(device_id, "git.pr_merge", {
                    "project_root": p_root,
                    "number": pr_number,
                    "merge_method": merge_method,
                })
                payload = res.get("payload", {})
                if payload.get("success"):
                    return f"✅ Pull Request #{pr_number} merged successfully!\n\nMethod: {merge_method.upper()}\nSHA: `{payload.get('sha', '')[:8]}`"
                return f"❌ Merge rejected: {payload.get('error', 'Remote error')}"
            except HTTPException as exc:
                return str(exc.detail)

        # Standard PR creation: /pr <title>
        title = " ".join(parts[1:])
        ctx = bot.active_git_context.get(user_id)
        if not ctx:
            return "No active Git repository selected. Navigate to 🔀 Git in 📁 Projects first."
        device_id = ctx["device_id"]
        project = ctx["project"]
        p_root = project["absolute_path"]

        pe = getattr(bot.control_plane, "permission_engine", None)
        if pe is not None:
            from ...permissions import PermissionRequest, PermissionRiskLevel
            import uuid
            req = PermissionRequest(
                request_id=str(uuid.uuid4()),
                action_type="git_pr_create",
                resource_path=p_root,
                command=f'create_pull_request("{title}")',
                agent_id="Git ChatOps",
                session_id=str(user_id),
                user_id=user_id,
                risk_level=PermissionRiskLevel.HIGH,
                description=f"Create GitHub Pull Request for {project.get('name')}",
            )
            allowed, reason = pe.check_permission(req, workspace_path=p_root)
            if not allowed:
                bot.pending_permissions[req.request_id] = {
                    "id": req.request_id,
                    "agent": "Git ChatOps",
                    "project": project.get("name"),
                    "action": "git_pr_create",
                    "summary": f'PR: "{title}"',
                    "user_id": user_id,
                    "device_id": device_id,
                }
                return await bot._screen("permissions", {}, chat_id, user_id)

        try:
            res = await bot._dispatch_command(device_id, "git.pr_create", {
                "project_root": p_root,
                "title": title,
                "body": "",
                "base": "main",
            })
            payload = res.get("payload", {})
            if payload.get("success"):
                pr_num = payload.get("number")
                pr_url = payload.get("html_url") or ""
                return f"✅ Pull Request created!\n\n#{pr_num}: {payload.get('title')}\n{payload.get('head')} → {payload.get('base')}\n\n{pr_url}"
            return f"❌ Pull Request creation failed: {payload.get('error', 'Remote API error')}"
        except HTTPException as exc:
            return str(exc.detail)

    if command == "/pair":
        if len(parts) != 2:
            return "Usage: /pair CODE"
        try:
            bot.control_plane.pairing.approve(parts[1])
            return ("✅ Pairing approved\n"
                    "🔐 Waiting for runtime authentication...")
        except HTTPException:
            return "Pairing code is invalid, expired, or already used."
    if command == "/devices":
        return bot._format_devices()
    if command == "/revoke":
        if len(parts) != 2:
            return "Usage: /revoke DEVICE_ID"
        if not any(item["device_id"] == parts[1] for item in bot._devices()):
            return "Device not found or already revoked."
        if not await bot.control_plane.revoke_device(parts[1]):
            return "Device not found or already revoked."
        return "Device credential revoked."
    if command == "/access_root":
        if len(parts) >= 2:
            req_path = parts[2] if len(parts) >= 3 and parts[1].lower() == "add" else parts[1]
            device_id = bot._select_device(user_id=user_id)
            return await bot._screen("access_root_confirm", {"device_id": device_id, "root": req_path}, chat_id, user_id)
        return await bot._screen("access_roots", {}, chat_id, user_id)
    command_map = {"/status": "runtime.status", "/workspaces": "workspace.list", "/projects": "project.list"}
    if command not in command_map:
        return "Unknown request. Use /help or the navigation buttons."
    requested_device = parts[1] if len(parts) > 1 else None
    if len(parts) > 2:
        return "Usage: " + parts[0] + " [DEVICE_ID]"
    try:
        device_id = bot._select_device(requested_device, user_id)
        response = await bot._dispatch_command(device_id, command_map[command], {})
        return bot._format_result(command, response.get("payload", {}))
    except HTTPException as exc:
        return str(exc.detail)


async def _deliver_agent_events(bot: Any, user_id: int, chat_id: int, device_id: str, session_id: str) -> None:
    """Deliver bounded AgentManager events polled through the paired runtime."""
    started = time.monotonic()
    sent_output = False
    while time.monotonic() - started < 6 * 60 * 60:
        try:
            result = await bot._dispatch_command(device_id, "agent.events", {"session_id": session_id, "limit": 30})
            if result.get("status") != "success":
                await asyncio.sleep(2)
                continue
            body = result.get("payload", {})
            events = body.get("events", [])
            for event in events:
                payload = event.get("payload", {})
                if event.get("event_type") == "AGENT_OUTPUT" and isinstance(payload, dict) and payload.get("text"):
                    sent_output = True
                    stream = payload.get("stream")
                    prefix = "⚠️ Agent diagnostic output:\n" if stream == "stderr" else ""
                    delivered = await bot._send_text(None, chat_id, prefix + str(payload["text"]))
                    if delivered is None:
                        log.warning("Telegram output delivery failed session=%s stream=%s", session_id, stream or "stdout")
            if body.get("turn_completed") and not body.get("finished"):
                session = body.get("session", {})
                metadata = session.get("metadata", {})
                final = metadata.get("final_response") if isinstance(metadata, dict) else None
                summary = f"Agent turn completed. Session remains available: {session_id}"
                if final:
                    summary += "\n\nFinal response:\n" + str(final)
                project_root = str(session.get("project_root", ""))
                try:
                    after_result = await bot._dispatch_command(device_id, "git.status", {"project_root": project_root})
                    after = _git_status_paths(after_result.get("payload", {})) if after_result.get("status") == "success" else None
                    baseline = bot.agent_file_baselines.get(f"{device_id}:{session_id}")
                    if after is None or baseline is None:
                        summary += "\n\nChanged files: unavailable (Git status snapshot unavailable)."
                    else:
                        changed = [(path, kind) for path, kind in after.items() if baseline.get(path) != kind]
                        summary += "\n\nGit status changes since the pre-run snapshot:"
                        summary += "\n" + ("\n".join(f"• {kind}: {path}" for path, kind in changed[:40])
                            if changed else "No newly visible Git status changes.")
                        if len(changed) > 40:
                            summary += f"\n…and {len(changed) - 40} more"
                        summary += "\nExisting dirty files are not attributed to this run."
                except Exception:
                    summary += "\n\nChanged files: unavailable (Git status could not be read)."
                context = {"device_id": device_id, "project": {
                    "absolute_path": session.get("project_root", ""),
                    "name": session.get("project_id", "Project")},
                    "agent_id": session.get("agent_id", ""), "session_id": session_id}
                button = bot._target_button("➕ Enter next prompt", user_id, chat_id, "agent_prompt_next", context)
                reply = bot._reply(summary, chat_id, user_id, [[button], bot._nav_buttons(back="nav:projects")])
                await bot._send_reply(None, reply)
                return
            if body.get("finished"):
                session = body.get("session", {})
                metadata = session.get("metadata", {})
                final = metadata.get("final_response") if isinstance(metadata, dict) else None
                terminal_event = next((event for event in events
                    if event.get("event_type") in {"AGENT_COMPLETED", "AGENT_FAILED", "AGENT_STOPPED"}), {})
                event_type = terminal_event.get("event_type")
                status = "completed" if event_type == "AGENT_COMPLETED" else "failed or stopped"
                payload = terminal_event.get("payload", {})
                summary = f"Agent session {status}. Session: {session_id}"
                if isinstance(payload, dict) and "exit_code" in payload:
                    summary += f"\nExit code: {payload['exit_code']}"
                if isinstance(payload, dict) and payload.get("error"):
                    summary += "\nFailure: " + str(payload["error"])
                if final:
                    summary += "\n\nFinal response:\n" + str(final)
                elif not sent_output:
                    summary += "\nNo final response was captured."
                project_root = str(session.get("project_root", ""))
                try:
                    after_result = await bot._dispatch_command(device_id, "git.status", {"project_root": project_root})
                    if after_result.get("status") == "success":
                        after = _git_status_paths(after_result.get("payload", {}))
                        baseline = bot.agent_file_baselines.get(f"{device_id}:{session_id}")
                        if baseline is None:
                            summary += "\n\nChanged files: unavailable (workspace snapshot could not be read)."
                        else:
                            changed = [(path, kind) for path, kind in after.items()
                                       if baseline.get(path) != kind]
                            summary += "\n\nGit status changes since the pre-run snapshot:"
                            if changed:
                                summary += "\n" + "\n".join(f"• {kind}: {path}" for path, kind in changed[:40])
                                if len(changed) > 40:
                                    summary += f"\n…and {len(changed) - 40} more"
                            else:
                                summary += "\nNo newly visible Git status changes."
                            summary += "\nThis compares status paths to the pre-run snapshot; existing dirty files are not attributed to this run."
                    else:
                        summary += "\n\nChanged files: unavailable (Git status failed)."
                except Exception:
                    summary += "\n\nChanged files: unavailable (Git status could not be read)."
                if status == "completed":
                    context = {"device_id": device_id, "project": {
                        "absolute_path": session.get("project_root", ""),
                        "name": session.get("project_id", "Project")},
                        "agent_id": session.get("agent_id", ""), "session_id": session_id}
                    button = bot._target_button("➕ Enter next prompt", user_id, chat_id, "agent_prompt_next", context)
                    reply = bot._reply(summary, chat_id, user_id, [[button], bot._nav_buttons(back="nav:projects")])
                    await bot._send_reply(None, reply)
                else:
                    await bot._send_text(None, chat_id, summary)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Agent output delivery retry session=%s error=%s", session_id, type(exc).__name__)
        await asyncio.sleep(1)


def _git_status_paths(payload: Any) -> Dict[str, str] | None:
    """Return bounded path/status entries without reading file contents."""
    if not isinstance(payload, dict) or not payload.get("is_git"):
        return None
    result: Dict[str, str] = {}
    for key, fallback in (("staged_files", "staged"), ("unstaged_files", "modified"),
                          ("untracked_files", "added"), ("conflicted_files", "conflicted")):
        entries = payload.get(key, [])
        if isinstance(entries, list):
            for entry in entries[:5000]:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    result[entry["path"][:4096]] = str(entry.get("change_type") or fallback)[:32]
    return result
