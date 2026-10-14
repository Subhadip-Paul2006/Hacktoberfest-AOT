"""Callback query routing and handlers for SŪTRA Telegram ChatOps."""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import HTTPException

from .formatting import _access_root_key
from .keyboards import MAX_CALLBACK_AGE_SECONDS


async def handle_callback(bot: Any, update: Dict[str, Any], user_id: int, chat_id: int) -> Any:
    """Handle and route an incoming Telegram callback query."""
    query = update["callback_query"]
    message = query.get("message") or {}
    message_id = message.get("message_id") if isinstance(message.get("message_id"), int) else None
    callback_id = query.get("id") if isinstance(query.get("id"), str) else None
    data = query.get("data")
    if not isinstance(data, str) or len(data.encode("utf-8")) > 64:
        return bot._reply("Invalid or expired button.", chat_id, user_id, bot._nav_buttons(),
                          message_id=message_id, callback_query_id=callback_id)

    if data.startswith("target:"):
        target = bot.callback_targets.pop(data[7:], None)
        if (not target or target["user_id"] != user_id or target["chat_id"] != chat_id
                or target["created_at"] < time.time() - MAX_CALLBACK_AGE_SECONDS):
            return bot._reply("This button is invalid or expired.", chat_id, user_id, bot._nav_buttons(),
                              message_id=message_id, callback_query_id=callback_id)
        context = target["context"]
        action = target["action"]
        if action in {"agent_prompt_start", "agent_prompt_next"}:
            pending_prompts = getattr(bot, "pending_agent_prompts", {})
            active_prompt = pending_prompts.get(user_id) if isinstance(pending_prompts, dict) else None
            if active_prompt and time.time() - float(active_prompt.get("created_at", 0)) <= 600:
                return bot._reply("A prompt is already being composed for this user. Send it, or use /cancel first.",
                                  chat_id, user_id, bot._nav_buttons(back="nav:projects"),
                                  message_id=message_id, callback_query_id=callback_id)
            device_id = str(context.get("device_id", ""))
            project = context.get("project")
            if not device_id or not isinstance(project, dict):
                return bot._reply("Agent selection expired. Reopen the project agent screen.", chat_id, user_id,
                                  bot._nav_buttons(back="nav:projects"), message_id=message_id,
                                  callback_query_id=callback_id)
            bot.selected_devices[user_id] = device_id
            bot.pending_agent_prompts[user_id] = {
                "mode": "start" if action == "agent_prompt_start" else "next",
                "device_id": device_id,
                "project": project,
                "agent_id": str(context.get("agent_id", "")),
                "session_id": str(context.get("session_id", "")),
                "chat_id": chat_id,
                "created_at": time.time(),
            }
            return bot._reply("Send the task as your next Telegram message (up to 4,096 characters). Use /cancel to discard it.",
                chat_id, user_id, bot._nav_buttons(back="nav:projects"), message_id=message_id,
                callback_query_id=callback_id)
        if target.get("session_id") and not bot.control_plane.session_matches(
                str(context.get("device_id", "")), str(target["session_id"]),
                int(target.get("generation", -1))):
            return bot._reply("This session is no longer active. A fresh pairing is required.",
                chat_id, user_id, bot._nav_buttons(back="nav:devices"), message_id=message_id,
                callback_query_id=callback_id)
        screen = {"project": "project", "files": "files", "folder": "files", "file": "file",
            "preview": "preview", "download": "download", "device": "device", "agent_detail": "agent_detail",
            "project_agent": "project_agent", "git": "git", "project_activity": "project_activity",
            "project_status": "project_status", "permission": "permission_decision",
            "revoke_confirm": "revoke_confirm", "device_select": "device",
            "jump_menu": "jump_menu", "search_menu": "search_menu", "symbols": "symbols",
            "search_results": "search_results",
            "git_status": "git_status", "git_diff": "git_diff", "git_branches": "git_branches",
            "git_stage": "git_stage", "git_commit": "git_commit", "git_push": "git_push", "git_pr": "git_pr",
            "git_github_hub": "git_github_hub", "git_pr_create_screen": "git_pr_create_screen",
            "git_pr_list": "git_pr_list", "git_pr_detail": "git_pr_detail",
            "git_pr_files": "git_pr_files", "git_pr_comments": "git_pr_comments",
            "git_pr_reviews": "git_pr_reviews", "git_pr_merge_confirm": "git_pr_merge_confirm",
            "git_ci": "git_ci", "git_ci_run_detail": "git_ci_run_detail",
            "git_ci_logs": "git_ci_logs", "git_releases": "git_releases"}.get(action)
        if action in {"projects_retry", "projects_refresh"}:
            if action == "projects_refresh":
                try:
                    await bot._call_device(user_id, "project.refresh")
                except HTTPException:
                    pass
            return await bot._screen("projects", {}, chat_id, user_id, message_id, callback_id)
        if action == "status_retry":
            return await bot._screen("home", {}, chat_id, user_id, message_id, callback_id)
        if action == "stop_session_confirm":
            return await bot._screen("stop_session_confirm", context, chat_id, user_id,
                                      message_id, callback_id)
        if action == "stop_session_execute":
            stopped, reachable = await bot.control_plane.stop_session(str(context.get("device_id", "")))
            if not stopped:
                message_text = "This session is no longer active. A fresh pairing is required."
            elif reachable:
                message_text = "Remote session stopped and its credential invalidated. A fresh pairing is required."
            else:
                message_text = "Session credential invalidated, but the runtime was unreachable. A fresh pairing is required before it can reconnect."
            keyboard = [
                [bot._button("🔐 Pair Runtime", "action:pair_runtime")],
                [bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(message_text, chat_id, user_id,
                keyboard, message_id=message_id,
                callback_query_id=callback_id)
        if action == "repair_confirm":
            return await bot._screen("repair_confirm", context, chat_id, user_id, message_id, callback_id)
        if action == "repair_execute":
            device_id = str(context.get("device_id", ""))
            stopped, _ = await bot.control_plane.stop_session(device_id)
            keyboard = [
                [bot._button("🔐 Pair Runtime", "action:pair_runtime")],
                [bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply("Current session has been invalidated.\nA new pairing will be required before reconnecting.",
                chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)
        if action == "revoke_execute":
            device_id = context.get("device_id")
            if not any(item["device_id"] == device_id and item["connection_status"] != "REVOKED" for item in bot._devices()):
                return bot._reply("Device selection is invalid or expired.", chat_id, user_id,
                    bot._nav_buttons(back="nav:devices"), message_id=message_id, callback_query_id=callback_id)
            if not await bot.control_plane.revoke_device(device_id):
                return bot._reply("Device could not be revoked.", chat_id, user_id,
                    bot._nav_buttons(back="nav:devices"), message_id=message_id, callback_query_id=callback_id)
            return bot._reply("Device credential revoked; the active session was closed.", chat_id, user_id,
                bot._nav_buttons(back="nav:devices"), message_id=message_id, callback_query_id=callback_id)
        if action == "device_select":
            device_id = context.get("device_id")
            if not any(item["device_id"] == device_id and item["connection_status"] != "REVOKED" for item in bot._devices()):
                return bot._reply("Device selection is invalid or expired.", chat_id, user_id,
                    bot._nav_buttons(back="nav:devices"), message_id=message_id, callback_query_id=callback_id)
            bot.selected_devices[user_id] = device_id
            return await bot._screen("home", {}, chat_id, user_id, message_id, callback_id)
        if action == "permission":
            req_id = str(context.get("id"))
            permission = bot.pending_permissions.get(req_id)
            if not permission or permission.get("user_id") != user_id or permission.get("device_id") != context.get("device_id"):
                return bot._reply("Permission request is no longer pending.", chat_id, user_id, bot._nav_buttons(back="nav:home"),
                                   message_id=message_id, callback_query_id=callback_id)
            decision = str(context.get("decision", "deny"))
            from ...permissions import PermissionDecision
            try:
                parsed_decision = PermissionDecision(decision)
            except ValueError:
                return bot._reply("This permission decision is invalid.", chat_id, user_id,
                    bot._nav_buttons(back="nav:home"), message_id=message_id,
                    callback_query_id=callback_id)

            permission_engine = getattr(bot.control_plane, "permission_engine", None)
            apply_decision = getattr(permission_engine, "apply_decision", None)
            if not callable(apply_decision):
                return bot._reply("Permission decisions are unavailable right now. Try again later.",
                    chat_id, user_id, bot._nav_buttons(back="nav:home"), message_id=message_id,
                    callback_query_id=callback_id)
            try:
                applied = apply_decision(req_id, parsed_decision)
            except Exception:
                return bot._reply("The permission decision could not be recorded. Try again.",
                    chat_id, user_id, bot._nav_buttons(back="nav:home"), message_id=message_id,
                    callback_query_id=callback_id)
            if not applied:
                bot.pending_permissions.pop(req_id, None)
                return bot._reply("Permission request is no longer pending.", chat_id, user_id,
                    bot._nav_buttons(back="nav:home"), message_id=message_id,
                    callback_query_id=callback_id)
            bot.pending_permissions.pop(req_id, None)
            return bot._reply(
                f"Permission decision recorded: {decision.replace('_', ' ').title()}.",
                chat_id, user_id, bot._nav_buttons(back="nav:home"), message_id=message_id,
                callback_query_id=callback_id,
            )
        if action == "access_root_request":
            return await bot._screen("access_root_confirm", context, chat_id, user_id, message_id, callback_id)
        if action == "access_root_allow":
            root = context["root"]
            try:
                from ...security import validate_access_root
                validated = validate_access_root(root)
                canonical = str(validated)
                _, roots = await bot._call_device(user_id, "workspace.list")
                if any(_access_root_key(str(item)) == _access_root_key(canonical)
                       for item in roots.get("items", [])):
                    return await bot._screen("access_roots", {
                        "status_message": f"{canonical} is already authorized."}, chat_id, user_id,
                        message_id, callback_id)
                _, result = await bot._call_device(user_id, "workspace.add", {"path": canonical})
                status_msg = f"{canonical} ✅ Authorized"
            except (ValueError, OSError, HTTPException) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                status_msg = f"Failed to authorize {root}: {detail}"
            return await bot._screen("access_roots", {"status_message": status_msg},
                                      chat_id, user_id, message_id, callback_id)
        if action == "access_root_reject":
            root = context["root"]
            try:
                from ...security import validate_access_root
                canonical = str(validate_access_root(root))
                _, roots = await bot._call_device(user_id, "workspace.list")
                already_authorized = any(_access_root_key(str(item)) == _access_root_key(canonical)
                                         for item in roots.get("items", []))
            except (ValueError, OSError, HTTPException) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                return bot._reply(f"Could not verify access root: {detail}", chat_id, user_id,
                    bot._nav_buttons(back="nav:access_roots"), message_id=message_id,
                    callback_query_id=callback_id)
            status_msg = (f"{canonical} is already authorized." if already_authorized else
                f"Access to {root} was not granted.\n\n{root} ❌ Not Authorized")
            return await bot._screen("access_roots", {"status_message": status_msg},
                                      chat_id, user_id, message_id, callback_id)

        if action == "git_stage_toggle":
            file_path = context.get("file")
            if file_path:
                selection = bot.active_stage_selection.setdefault(user_id, set())
                if file_path in selection:
                    selection.remove(file_path)
                else:
                    selection.add(file_path)
            return await bot._screen("git_stage", context, chat_id, user_id, message_id, callback_id)

        if action == "git_stage_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            selection = list(bot.active_stage_selection.get(user_id, set()))
            if selection:
                await bot._dispatch_command(device_id, "git.stage", {"project_root": project["absolute_path"], "files": selection})
                bot.active_stage_selection[user_id] = set()
            return await bot._screen("git_stage", context, chat_id, user_id, message_id, callback_id)

        if action == "git_stage_all":
            device_id = context.get("device_id")
            project = context.get("project")
            st = await bot._dispatch_command(device_id, "git.status", {"project_root": project["absolute_path"]})
            payload = st.get("payload", {})
            unstaged = [f.get("path") for f in payload.get("unstaged_files", [])]
            untracked = [f.get("path") for f in payload.get("untracked_files", [])]
            all_files = list(dict.fromkeys(unstaged + untracked))
            if all_files:
                await bot._dispatch_command(device_id, "git.stage", {"project_root": project["absolute_path"], "files": all_files})
                bot.active_stage_selection[user_id] = set()
            return await bot._screen("git_stage", context, chat_id, user_id, message_id, callback_id)

        if action == "git_unstage_all":
            device_id = context.get("device_id")
            project = context.get("project")
            st = await bot._dispatch_command(device_id, "git.status", {"project_root": project["absolute_path"]})
            payload = st.get("payload", {})
            staged = [f.get("path") for f in payload.get("staged_files", [])]
            if staged:
                await bot._dispatch_command(device_id, "git.unstage", {"project_root": project["absolute_path"], "files": staged})
            return await bot._screen("git_stage", context, chat_id, user_id, message_id, callback_id)

        if action == "git_commit_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            msg = context.get("message") or "Update changes"
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
                    return await bot._screen("permissions", {}, chat_id, user_id, message_id, callback_id)

            res = await bot._dispatch_command(device_id, "git.commit", {"project_root": p_root, "message": msg})
            payload = res.get("payload", {})
            if payload.get("success"):
                commit_hash = payload.get("commit_hash", "")[:8]
                resp_text = f"✅ Commit created: `{commit_hash}`\n\n{msg}"
            else:
                resp_text = f"❌ Commit failed: {payload.get('error', 'Unknown error')}"

            keyboard = [
                [bot._target_button("🚀 Push Changes", user_id, chat_id, "git_push", context)],
                [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", context),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(resp_text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)

        if action == "git_push_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            p_root = project["absolute_path"]
            pe = getattr(bot.control_plane, "permission_engine", None)
            if pe is not None:
                from ...permissions import PermissionRequest, PermissionRiskLevel
                import uuid
                req = PermissionRequest(
                    request_id=str(uuid.uuid4()),
                    action_type="git_push",
                    resource_path=p_root,
                    command="git push",
                    agent_id="Git ChatOps",
                    session_id=str(user_id),
                    user_id=user_id,
                    risk_level=PermissionRiskLevel.HIGH,
                    description=f"Push commits to remote for {project.get('name')}",
                )
                allowed, reason = pe.check_permission(req, workspace_path=p_root)
                if not allowed:
                    bot.pending_permissions[req.request_id] = {
                        "id": req.request_id,
                        "agent": "Git ChatOps",
                        "project": project.get("name"),
                        "action": "git_push",
                        "summary": "Push commits to remote",
                        "user_id": user_id,
                        "device_id": device_id,
                    }
                    return await bot._screen("permissions", {}, chat_id, user_id, message_id, callback_id)

            res = await bot._dispatch_command(device_id, "git.push", {"project_root": p_root})
            payload = res.get("payload", {})
            if payload.get("success"):
                resp_text = f"✅ Push succeeded:\n{payload.get('message', 'Changes pushed to remote.')}"
            else:
                resp_text = f"❌ Push failed:\n{payload.get('error', 'Remote error')}"

            keyboard = [
                [bot._target_button("🔀 Create PR", user_id, chat_id, "git_pr", context)],
                [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", context),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(resp_text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)

        if action == "git_pr_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            p_root = project["absolute_path"]
            title = context.get("title") or "Update"
            body = context.get("body") or ""
            head = context.get("head")
            base = context.get("base", "main")
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
                    return await bot._screen("permissions", {}, chat_id, user_id, message_id, callback_id)

            res = await bot._dispatch_command(device_id, "git.pr_create", {
                "project_root": p_root,
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            })
            payload = res.get("payload", {})
            if payload.get("success"):
                pr_num = payload.get("number")
                pr_url = payload.get("html_url") or ""
                resp_text = f"✅ Pull Request created!\n\n#{pr_num}: {payload.get('title')}\n{payload.get('head')} → {payload.get('base')}\n\n{pr_url}"
            else:
                resp_text = f"❌ Pull Request creation failed:\n{payload.get('error', 'Remote API error')}"

            keyboard = [
                [bot._target_button("⬅️ Back to Git", user_id, chat_id, "git", context),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(resp_text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)

        if action == "git_pr_merge_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            p_root = project["absolute_path"]
            pr_number = int(context.get("number", 0))
            merge_method = str(context.get("merge_method", "merge"))

            # Section 17 & 21: Check PermissionEngine and Stale State
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
                    return await bot._screen("permissions", {}, chat_id, user_id, message_id, callback_id)

            res = await bot._dispatch_command(device_id, "git.pr_merge", {
                "project_root": p_root,
                "number": pr_number,
                "merge_method": merge_method,
            })
            payload = res.get("payload", {})
            if payload.get("success"):
                resp_text = f"✅ Pull Request #{pr_number} merged successfully!\n\nSHA: `{payload.get('sha', '')[:8]}`\nMethod: {merge_method.upper()}"
            else:
                resp_text = f"❌ Merge failed:\n{payload.get('error', 'Remote API rejected merge')}"

            keyboard = [
                [bot._target_button(f"⬅️ Back to PR #{pr_number}", user_id, chat_id, "git_pr_detail", context),
                 bot._target_button("📋 PR List", user_id, chat_id, "git_pr_list", context)],
                [bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(resp_text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)

        if action == "git_ci_rerun_confirm":
            run_id = int(context.get("run_id", 0))
            text = f"⚠️ Confirm Rerun Workflow Run #{run_id}?\n\nThis will trigger a rerun of this workflow on GitHub Actions."
            buttons = [
                [bot._target_button("🔄 Yes, Rerun Run", user_id, chat_id, "git_ci_rerun_execute", context)],
                [bot._target_button("❌ Cancel", user_id, chat_id, "git_ci_run_detail", context)]
            ]
            return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_id)

        if action == "git_ci_rerun_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            p_root = project["absolute_path"]
            run_id = int(context.get("run_id", 0))

            pe = getattr(bot.control_plane, "permission_engine", None)
            if pe is not None:
                from ...permissions import PermissionRequest, PermissionRiskLevel
                import uuid
                req = PermissionRequest(
                    request_id=str(uuid.uuid4()),
                    action_type="git_ci_rerun",
                    resource_path=p_root,
                    command=f'rerun_workflow_run(#{run_id})',
                    agent_id="Git ChatOps",
                    session_id=str(user_id),
                    user_id=user_id,
                    risk_level=PermissionRiskLevel.HIGH,
                    description=f"Rerun GitHub Actions run #{run_id} for {project.get('name')}",
                )
                allowed, reason = pe.check_permission(req, workspace_path=p_root)
                if not allowed:
                    bot.pending_permissions[req.request_id] = {
                        "id": req.request_id,
                        "agent": "Git ChatOps",
                        "project": project.get("name"),
                        "action": "git_ci_rerun",
                        "summary": f'Rerun workflow #{run_id}',
                        "user_id": user_id,
                        "device_id": device_id,
                    }
                    return await bot._screen("permissions", {}, chat_id, user_id, message_id, callback_id)

            res = await bot._dispatch_command(device_id, "git.ci_run_rerun", {
                "project_root": p_root,
                "run_id": run_id,
            })
            payload = res.get("payload", {})
            if payload.get("success"):
                resp_text = f"✅ Workflow run #{run_id} rerun initiated."
            else:
                resp_text = f"❌ Rerun failed: {payload.get('error', 'Remote error')}"

            keyboard = [
                [bot._target_button(f"⬅️ Back to Run #{run_id}", user_id, chat_id, "git_ci_run_detail", context),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(resp_text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)

        if action == "git_ci_cancel_confirm":
            run_id = int(context.get("run_id", 0))
            text = f"⚠️ Confirm Cancel Workflow Run #{run_id}?\n\nThis will stop all running jobs for this workflow."
            buttons = [
                [bot._target_button("⏹ Yes, Cancel Run", user_id, chat_id, "git_ci_cancel_execute", context)],
                [bot._target_button("❌ Abort", user_id, chat_id, "git_ci_run_detail", context)]
            ]
            return bot._reply(text, chat_id, user_id, buttons, message_id=message_id, callback_query_id=callback_id)

        if action == "git_ci_cancel_execute":
            device_id = context.get("device_id")
            project = context.get("project")
            p_root = project["absolute_path"]
            run_id = int(context.get("run_id", 0))

            pe = getattr(bot.control_plane, "permission_engine", None)
            if pe is not None:
                from ...permissions import PermissionRequest, PermissionRiskLevel
                import uuid
                req = PermissionRequest(
                    request_id=str(uuid.uuid4()),
                    action_type="git_ci_cancel",
                    resource_path=p_root,
                    command=f'cancel_workflow_run(#{run_id})',
                    agent_id="Git ChatOps",
                    session_id=str(user_id),
                    user_id=user_id,
                    risk_level=PermissionRiskLevel.HIGH,
                    description=f"Cancel GitHub Actions run #{run_id} for {project.get('name')}",
                )
                allowed, reason = pe.check_permission(req, workspace_path=p_root)
                if not allowed:
                    bot.pending_permissions[req.request_id] = {
                        "id": req.request_id,
                        "agent": "Git ChatOps",
                        "project": project.get("name"),
                        "action": "git_ci_cancel",
                        "summary": f'Cancel workflow #{run_id}',
                        "user_id": user_id,
                        "device_id": device_id,
                    }
                    return await bot._screen("permissions", {}, chat_id, user_id, message_id, callback_id)

            res = await bot._dispatch_command(device_id, "git.ci_run_cancel", {
                "project_root": p_root,
                "run_id": run_id,
            })
            payload = res.get("payload", {})
            if payload.get("success"):
                resp_text = f"✅ Workflow run #{run_id} cancelled."
            else:
                resp_text = f"❌ Cancellation failed: {payload.get('error', 'Remote error')}"

            keyboard = [
                [bot._target_button(f"⬅️ Back to Run #{run_id}", user_id, chat_id, "git_ci_run_detail", context),
                 bot._button("🏠 Home", "nav:home")]
            ]
            return bot._reply(resp_text, chat_id, user_id, keyboard, message_id=message_id, callback_query_id=callback_id)

        if screen is None:
            return bot._reply("Unsupported button action.", chat_id, user_id, bot._nav_buttons(),
                               message_id=message_id, callback_query_id=callback_id)
        if action == "project":
            try:
                _, available = await bot._call_device(user_id, "project.list")
                active = next((item for item in available.get("items", [])
                               if item.get("absolute_path") == context.get("project", {}).get("absolute_path")), None)
                if not active:
                    raise HTTPException(404, "project is no longer available")
                context = {**context, "project": active}
                bot.selected_devices[user_id] = str(context["device_id"])
                bot.active_file_context.pop(user_id, None)
            except HTTPException:
                return bot._reply("Project is no longer available on this device.", chat_id, user_id,
                    bot._nav_buttons(back="nav:projects"), message_id=message_id, callback_query_id=callback_id)
        if action in ("folder", "file", "files", "preview", "download", "jump_menu", "search_menu", "symbols", "search_results"):
            if not await bot._valid_project_context(user_id, context):
                return bot._reply("This path is outside the selected authorized project.", chat_id, user_id,
                    bot._nav_buttons(back="nav:projects"), message_id=message_id, callback_query_id=callback_id)
        if action == "folder":
            context = {**context, "path": context["path"]}
            screen = "files"
        if action in ("preview", "download", "jump_menu", "search_menu", "symbols", "search_results"):
            screen = action
        return await bot._screen(screen, context, chat_id, user_id, message_id, callback_id)

    if data in ("nav:home", "nav:projects", "nav:agents", "nav:connectors", "nav:usage",
                "nav:permissions", "nav:activity", "nav:devices", "nav:settings",
                "nav:access_roots", "nav:add_access_root"):
        screen = data.split(":", 1)[1]
        history = bot.navigation_history.setdefault(user_id, [])
        if screen != "home":
            history.append((screen, {}))
        return await bot._screen(screen, {}, chat_id, user_id, message_id, callback_id)
    if data == "nav:back":
        history = bot.navigation_history.setdefault(user_id, [])
        screen, context = history.pop() if history else ("home", {})
        return await bot._screen(screen, context, chat_id, user_id, message_id, callback_id)
    if data in ("device:pair", "action:pair_runtime"):
        return await bot._screen("pair_runtime", {}, chat_id, user_id, message_id, callback_id)
    if data == "action:cancel_pairing":
        bot.control_plane.pairing.cancel_pending()
        return await bot._screen("home", {}, chat_id, user_id, message_id, callback_id)
    if data.startswith("setting:"):
        try:
            index = int(data.split(":", 1)[1])
        except ValueError:
            index = -1
        return await bot._screen("setting", {"index": index}, chat_id, user_id, message_id, callback_id)
    if data.startswith("unavailable:"):
        feature = data.split(":", 1)[1]
        return bot._reply(f"{feature.title()} actions are ⚪ Not Implemented.", chat_id, user_id,
            bot._nav_buttons(), message_id=message_id, callback_query_id=callback_id)
    return bot._reply("Invalid or expired button.", chat_id, user_id, bot._nav_buttons(),
                       message_id=message_id, callback_query_id=callback_id)
