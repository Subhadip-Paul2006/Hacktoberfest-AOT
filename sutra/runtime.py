"""Local runtime HTTP status/pairing API and lifecycle."""
from __future__ import annotations
import asyncio
import base64
import ipaddress
import json
import logging
import re
import signal
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse
from .metrics import MetricsRegistry
from . import __version__
from .connection import ConnectionManager
from .pairing import PairingManager
from .remote_pairing import RemotePairingClient
from .discovery import discover_projects
from .file_limits import MAX_PREVIEW_PAGE, PREVIEW_FILE_LIMIT, PREVIEW_PAGE_CHARS
from .security import require_allowed, validate_access_root
from .state import StateStore
from .project_index import ProjectIndex
from .connectivity import ConnectivityGuard, ConnectivityPolicy
from .agents import AgentManager, AgentRegistry, AgentSupervisor, SupervisionPolicy
from .agents.models import AgentSessionStatus
from .degradation import (
    degraded_response,
    can_degrade,
    can_use_cache_fallback,
    is_read_only,
    is_non_critical_failure,
)

log = logging.getLogger(__name__)
from .redaction import (
    BLOCKED_FILE_PARTS,
    BLOCKED_FILE_SUFFIXES,
    HIDDEN_DIRECTORY_NAMES,
    PRIVATE_KEY_BLOCK,
    SECRET_ASSIGNMENT,
    TOKEN_SIGNATURES as SECRET_PREFIX,
    is_sensitive_file,
    redact_secrets,
)
from .file_access import (
    MAX_DOWNLOAD_BYTES,
    MAX_DOWNLOAD_CHUNK_BYTES,
    MAX_TOTAL_EXPORT_BYTES,
    build_export_plan,
    detect_binary_file,
    extract_file_symbols,
    get_binary_metadata,
    get_export_part_bytes,
    get_file_info,
    list_directory_paginated,
    preview_file_bounded,
    search_file_text,
)
from .git import (
    add_pull_request_comment,
    build_ci_aware_pr_summary,
    cancel_workflow_run,
    check_workspace_lock,
    commit as git_commit,
    create_pull_request,
    detect_github_remote,
    get_git_branches,
    get_git_diff,
    get_git_status,
    get_pull_request,
    get_pull_request_files,
    get_pull_request_reviews,
    get_push_preview,
    get_workflow_run,
    get_workflow_run_logs,
    is_git_repository,
    list_pull_request_comments,
    list_pull_requests,
    list_releases,
    list_workflow_runs,
    merge_pull_request,
    push as git_push,
    rerun_workflow_run,
    stage_files,
    unstage_files,
)

BINARY_FILE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz",
    ".7z", ".rar", ".tar", ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".class",
    ".pyc", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".avi",
}
MAX_PROJECT_LIST_ITEMS = 50


class Runtime:
    def __init__(self, home: str, gateway_url: str = "", host: str = "127.0.0.1", port: int = 8765, max_backoff: int = 30,
                 control_plane_url: str = "", ca_file: str | None = None):
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Local status and pairing API must bind to a loopback address")
        self.store = StateStore(Path(home) / "state.sqlite3")
        self.gateway_url, self.control_plane_url, self.ca_file = gateway_url, control_plane_url, ca_file
        self.policy = ConnectivityPolicy.from_environment()
        self.connectivity = ConnectivityGuard(self.policy)
        self.project_index = ProjectIndex(self.store.workspaces())
        self._shutdown = asyncio.Event()
        if gateway_url:
            self.store.set_device("gateway_token", "")
            self.store.set_device("session_id", "")
            self.store.set_device("session_generation", "0")
        self.session_id: str | None = None
        self.runtime_generation = 0
        self.pairing = PairingManager(self.store) if not gateway_url else None
        self.host, self.port = host, port
        self.connection = ConnectionManager(gateway_url, None,
            self.store.get_device("device_id"), max_backoff, ca_file, self.handle_remote_command,
            None, int(self.store.get_device("session_generation") or 0), self.policy)
        self.agent_registry = AgentRegistry()
        self.agent_manager = AgentManager(self.agent_registry)
        self._agent_event_queues: dict[str, asyncio.Queue[Any]] = {}
        self.agent_supervisor = AgentSupervisor(self.agent_manager, self.agent_registry)
        self.agent_manager.set_supervisor_callback(self.agent_supervisor.on_adapter_event)
        self.server: ThreadingHTTPServer | None = None
        self.metrics = MetricsRegistry()

    async def handle_remote_command(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        """Handle a remote command with graceful degradation for non-critical operations."""
        try:
            return await self._execute_command_inner(command, payload)
        except Exception as exc:
            if can_degrade(command) and is_non_critical_failure(exc):
                log.warning("degradation_activated command=%s dependency=project_index error=%s",
                            command, type(exc).__name__)
                self.metrics.record_degradation_activated(dependency="project_index", feature=command)
                return degraded_response(command, "project_index", reason=str(exc))
            raise

    async def _execute_command_inner(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        """Inner command execution (no degradation here — handled at outer level)."""
        if command in ("runtime.status", "runtime.health"):
            if payload: raise ValueError("command payload must be empty")
            return self.status()
        if command == "agent.health":
            if not isinstance(payload, dict) or "session_id" not in payload:
                raise ValueError("agent.health requires session_id in payload")
            session_id = str(payload["session_id"])
            # Ensure supervision is active for this session
            await self.agent_supervisor.supervise_session(session_id)
            snap = self.agent_supervisor.get_health(session_id)
            if snap is None:
                raise ValueError(f"session {session_id} not found")
            # Redact any sensitive info from payload
            safe = snap.to_dict()
            return {"health": safe}
        if command == "agent.list":
            if payload: raise ValueError("command payload must be empty")
            items = [d.to_dict() for d in self.agent_registry.list_descriptors()]
            return {"items": items, "total_count": len(items)}
        if command == "agent.status":
            if not isinstance(payload, dict): raise ValueError("invalid agent status request")
            agent_id = payload.get("agent_id")
            session_id = payload.get("session_id")
            if agent_id:
                adapter = self.agent_registry.find(str(agent_id))
                if not adapter: raise ValueError("agent not found")
                return {"agent": adapter.descriptor().to_dict()}
            if session_id:
                try:
                    session = self.agent_manager.get_session(str(session_id))
                    return {"session": session.to_dict()}
                except Exception as exc:
                    raise ValueError(str(exc)) from exc
            return {
                "agents": [d.to_dict() for d in self.agent_registry.list_descriptors()],
                "active_sessions": [s.to_dict() for s in self.agent_manager.list_sessions()],
            }
        if command == "agent.start":
            if not isinstance(payload, dict) or not {"agent_id", "project_root"}.issubset(set(payload)):
                raise ValueError("invalid agent start request")
            agent_id = str(payload["agent_id"])
            raw_project_root = str(payload["project_root"])
            roots = [validate_access_root(root) for root in await asyncio.to_thread(self.store.workspaces)]
            canonical_project_root = require_allowed(raw_project_root, roots)
            available_projects = {
                str(item["absolute_path"]).casefold()
                for item in self.project_index.snapshot()["items"]
            }
            if str(canonical_project_root).casefold() not in available_projects:
                raise PermissionError("project root is outside discovered project scope")

            session = self.agent_manager.create_session(
                agent_id=agent_id,
                project_root=canonical_project_root,
                authorized_roots=roots,
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )
            prompt = payload.get("prompt")
            kwargs: dict[str, Any] = {}
            if prompt:
                kwargs["prompt"] = str(prompt)
            queue = self.agent_manager.subscribe(session.session_id)
            self._agent_event_queues[session.session_id] = queue
            try:
                started_session = await self.agent_manager.start_session(session.session_id, **kwargs)
            except Exception:
                self._agent_event_queues.pop(session.session_id, None)
                self.agent_manager.unsubscribe(session.session_id, queue)
                self.agent_manager.cleanup_session(session.session_id)
                raise
            return {"session": started_session.to_dict()}
        if command == "agent.stop":
            if not isinstance(payload, dict) or set(payload) != {"session_id"}:
                raise ValueError("invalid agent stop request")
            session_id = str(payload["session_id"])
            stopped_session = await self.agent_manager.stop_session(session_id)
            return {"session": stopped_session.to_dict()}
        if command == "agent.prompt":
            if not isinstance(payload, dict) or set(payload) != {"session_id", "prompt"}:
                raise ValueError("invalid agent prompt request")
            session_id = str(payload["session_id"])
            prompt = str(payload["prompt"])
            if len(prompt) > 32768:
                raise ValueError("prompt exceeds maximum length limit")
            await self.agent_manager.send_prompt(session_id, prompt)
            return {"status": "delivered", "session_id": session_id}
        if command == "agent.events":
            if not isinstance(payload, dict) or set(payload) - {"session_id", "limit"} or "session_id" not in payload:
                raise ValueError("invalid agent events request")
            session_id = str(payload["session_id"])
            queue = self._agent_event_queues.get(session_id)
            if queue is None:
                raise ValueError("agent event stream is unavailable for this session")
            limit = payload.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
                raise ValueError("agent event limit must be between 1 and 50")
            events = []
            terminal = {"AGENT_COMPLETED", "AGENT_FAILED", "AGENT_STOPPED"}
            finished = False
            turn_completed = False
            session = self.agent_manager.get_session(session_id)
            for _ in range(limit):
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                item = event.to_dict()
                event_payload = item.get("payload", {})
                if isinstance(event_payload, dict) and isinstance(event_payload.get("text"), str):
                    event_payload["text"] = redact_secrets(event_payload["text"])
                item["payload"] = event_payload
                events.append(item)
                if item.get("event_type") == "AGENT_TURN_COMPLETED":
                    turn_completed = True
                if item.get("event_type") in terminal:
                    finished = True
                    if item["event_type"] == "AGENT_COMPLETED":
                        session.status = AgentSessionStatus.COMPLETED
                    elif item["event_type"] == "AGENT_STOPPED":
                        session.status = AgentSessionStatus.STOPPED
                    else:
                        session.status = AgentSessionStatus.FAILED
                    session.stopped_at = time.time()
                    break
            if finished:
                self.agent_manager.unsubscribe(session_id, queue)
                self._agent_event_queues.pop(session_id, None)
            return {"events": events, "status": session.status.value, "session": session.to_dict(),
                    "finished": finished, "turn_completed": turn_completed}
        if command == "workspace.list":
            if payload: raise ValueError("command payload must be empty")
            roots = await asyncio.to_thread(self.store.workspaces)
            return {"items": roots}
        if command in ("workspace.add", "access_root.add"):
            if not isinstance(payload, dict) or set(payload) != {"path"} or not isinstance(payload["path"], str):
                raise ValueError("invalid workspace add request")
            root = await asyncio.to_thread(validate_access_root, payload["path"])
            canonical = str(root)
            await asyncio.to_thread(self.store.add_workspace, canonical)
            roots = await asyncio.to_thread(self.store.workspaces)
            self.project_index.set_roots(roots)
            self.project_index.schedule_refresh()
            return {"items": roots, "added": canonical, "refreshing": self.project_index.refreshing}
        if command == "project.list":
            if payload: raise ValueError("command payload must be empty")
            if self.project_index.last_refresh_at is None and not self.project_index.refreshing:
                self.project_index.schedule_refresh()
            data = self.project_index.snapshot()
            items = data.get("items", [])
            return {**data, "items": items[:MAX_PROJECT_LIST_ITEMS], "total_count": len(items)}
        if command == "project.refresh":
            if payload: raise ValueError("command payload must be empty")
            refreshing = self.project_index.schedule_refresh()
            data = self.project_index.snapshot()
            items = data.get("items", [])
            return {**data, "refreshing": refreshing, "items": items[:MAX_PROJECT_LIST_ITEMS], "total_count": len(items)}
        if command == "file.list":
            target, _ = await asyncio.to_thread(self._validated_project_path, payload)
            return await asyncio.to_thread(self._file_list_sync, target, payload)
        if command in ("file.preview", "file.download", "file.info", "file.search", "file.symbols"):
            target, _ = await asyncio.to_thread(self._validated_project_path, payload, allow_page=True)
            return await asyncio.to_thread(self._file_preview_or_download_sync, command, target, payload)
        if command.startswith("git."):
            root_str = payload.get("project_root")
            if not isinstance(root_str, str):
                raise ValueError("missing or invalid project_root")
            canon_root = await asyncio.to_thread(self._validated_git_project_root, root_str)
            return await self._handle_git_command(command, canon_root, payload)
        raise ValueError("unsupported command")

    def _validated_git_project_root(self, root_str: str) -> Path:
        roots = [validate_access_root(root) for root in self.store.workspaces()]
        project_root = require_allowed(root_str, roots)
        available_projects = {str(item["absolute_path"]).casefold()
                              for item in self.project_index.snapshot().get("items", [])}
        if str(project_root).casefold() not in available_projects and not (project_root / ".git").exists():
            raise PermissionError("path is outside a discovered project scope")
        return project_root

    async def _handle_git_command(self, command: str, canon_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        if command == "git.status":
            return await asyncio.to_thread(lambda: get_git_status(canon_root).to_dict())

        if command == "git.branches":
            return await asyncio.to_thread(lambda: get_git_branches(canon_root).to_dict())

        if command == "git.diff":
            staged = bool(payload.get("staged", False))
            filepath = str(payload["path"]) if payload.get("path") else None
            max_lines = int(payload.get("max_lines", 200))
            return await asyncio.to_thread(lambda: get_git_diff(canon_root, staged=staged, filepath=filepath, max_lines=max_lines).to_dict())

        if command in ("git.stage", "git.unstage", "git.commit", "git.push"):
            check_workspace_lock(getattr(self.agent_manager, "workspace_locks", None), canon_root)

        if command == "git.stage":
            files = payload.get("files", [])
            return await asyncio.to_thread(lambda: stage_files(canon_root, files).to_dict())

        if command == "git.unstage":
            files = payload.get("files", [])
            return await asyncio.to_thread(lambda: unstage_files(canon_root, files).to_dict())

        if command == "git.commit":
            msg = str(payload.get("message", ""))
            author = str(payload["author"]) if payload.get("author") else None
            return await asyncio.to_thread(lambda: git_commit(canon_root, message=msg, author=author).to_dict())

        if command == "git.push_preview":
            remote = str(payload.get("remote", "origin"))
            return await asyncio.to_thread(lambda: get_push_preview(canon_root, remote=remote).to_dict())

        if command == "git.push":
            if payload.get("force"):
                raise ValueError("Force push is strictly prohibited by policy invariant.")
            remote = str(payload.get("remote", "origin"))
            branch = str(payload["branch"]) if payload.get("branch") else None
            return await asyncio.to_thread(lambda: git_push(canon_root, remote=remote, branch=branch).to_dict())

        if command == "git.pr_create":
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            branches = get_git_branches(canon_root)
            head = str(payload.get("head") or branches.current or "")
            if not head or head.startswith("HEAD ("):
                return {"success": False, "error": "Cannot create Pull Request from detached HEAD."}
            base = str(payload.get("base", "main"))
            title = str(payload.get("title", ""))
            body = str(payload.get("body", ""))
            draft = bool(payload.get("draft", False))
            res = await create_pull_request(owner=info.owner, repo=info.repo, title=title, body=body, head=head, base=base, draft=draft)
            return res.to_dict()

        if command in ("git.pr_list", "github.pr.list"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"items": [], "error": "No GitHub remote is configured for this repository."}
            state = str(payload.get("state", "open"))
            page = int(payload.get("page", 1))
            per_page = int(payload.get("per_page", 10))
            head = str(payload["head"]) if payload.get("head") else None
            items = await list_pull_requests(owner=info.owner, repo=info.repo, state=state, page=page, per_page=per_page, head=head)
            return {"items": items, "page": page, "per_page": per_page}

        if command in ("git.pr_get", "github.pr.get"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            number = int(payload.get("number") or payload.get("pr_number", 0))
            pr = await get_pull_request(owner=info.owner, repo=info.repo, number=number)
            if not pr:
                return {"success": False, "error": f"Pull Request #{number} not found."}
            return {"success": True, "pr": pr.to_dict()}

        if command in ("git.pr_files", "github.pr.files"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"files": [], "error": "No GitHub remote is configured for this repository."}
            number = int(payload.get("number") or payload.get("pr_number", 0))
            page = int(payload.get("page", 1))
            per_page = int(payload.get("per_page", 30))
            files = await get_pull_request_files(owner=info.owner, repo=info.repo, number=number, page=page, per_page=per_page)
            return {"files": [f.to_dict() for f in files], "total_count": len(files)}

        if command in ("git.pr_comments", "github.pr.comments"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"comments": [], "error": "No GitHub remote is configured for this repository."}
            number = int(payload.get("number") or payload.get("pr_number", 0))
            page = int(payload.get("page", 1))
            per_page = int(payload.get("per_page", 20))
            comments = await list_pull_request_comments(owner=info.owner, repo=info.repo, number=number, page=page, per_page=per_page)
            return {"comments": [c.to_dict() for c in comments], "total_count": len(comments)}

        if command in ("git.pr_comment_create", "github.pr.comment.create"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            number = int(payload.get("number") or payload.get("pr_number", 0))
            body = str(payload.get("comment") or payload.get("body", ""))
            path = str(payload["path"]) if payload.get("path") else None
            line = int(payload["line"]) if payload.get("line") is not None else None
            commit_id = str(payload["commit_id"]) if payload.get("commit_id") else None
            comment = await add_pull_request_comment(owner=info.owner, repo=info.repo, number=number, body=body, path=path, line=line, commit_id=commit_id)
            if not comment:
                return {"success": False, "error": "Failed to create PR comment."}
            return {"success": True, "comment": comment.to_dict()}

        if command in ("git.pr_reviews", "github.pr.reviews"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"reviews": [], "error": "No GitHub remote is configured for this repository."}
            number = int(payload.get("number") or payload.get("pr_number", 0))
            reviews = await get_pull_request_reviews(owner=info.owner, repo=info.repo, number=number)
            return reviews.to_dict()

        if command in ("git.pr_merge", "github.pr.merge"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            number = int(payload.get("number") or payload.get("pr_number", 0))
            merge_method = str(payload.get("merge_method", "merge"))
            commit_title = str(payload["commit_title"]) if payload.get("commit_title") else None
            commit_message = str(payload["commit_message"]) if payload.get("commit_message") else None
            sha = str(payload["sha"]) if payload.get("sha") else None
            res = await merge_pull_request(
                owner=info.owner,
                repo=info.repo,
                number=number,
                merge_method=merge_method,
                commit_title=commit_title,
                commit_message=commit_message,
                sha=sha,
            )
            return res.to_dict()

        if command in ("git.ci_runs", "github.ci.runs"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"runs": [], "error": "No GitHub remote is configured for this repository."}
            branch = str(payload["branch"]) if payload.get("branch") else None
            page = int(payload.get("page", 1))
            per_page = int(payload.get("per_page", 10))
            runs = await list_workflow_runs(owner=info.owner, repo=info.repo, branch=branch, page=page, per_page=per_page)
            return {"runs": [r.to_dict() for r in runs], "total_count": len(runs)}

        if command in ("git.ci_run_get", "github.ci.run.get"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            run_id = int(payload.get("run_id", 0))
            run = await get_workflow_run(owner=info.owner, repo=info.repo, run_id=run_id)
            if not run:
                return {"success": False, "error": f"Workflow run #{run_id} not found."}
            return {"success": True, "run": run.to_dict()}

        if command in ("git.ci_run_logs", "github.ci.run.logs"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"error": "No GitHub remote is configured for this repository."}
            run_id = int(payload.get("run_id", 0))
            max_lines = int(payload.get("max_lines", 100))
            logs = await get_workflow_run_logs(owner=info.owner, repo=info.repo, run_id=run_id, max_lines=max_lines)
            return logs.to_dict()

        if command in ("git.ci_run_rerun", "github.ci.run.rerun"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            run_id = int(payload.get("run_id", 0))
            return await rerun_workflow_run(owner=info.owner, repo=info.repo, run_id=run_id)

        if command in ("git.ci_run_cancel", "github.ci.run.cancel"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"success": False, "error": "No GitHub remote is configured for this repository."}
            run_id = int(payload.get("run_id", 0))
            return await cancel_workflow_run(owner=info.owner, repo=info.repo, run_id=run_id)

        if command in ("git.releases_list", "github.releases.list"):
            info = detect_github_remote(canon_root)
            if not info.is_github or not info.owner or not info.repo:
                return {"releases": [], "error": "No GitHub remote is configured for this repository."}
            page = int(payload.get("page", 1))
            per_page = int(payload.get("per_page", 5))
            releases = await list_releases(owner=info.owner, repo=info.repo, page=page, per_page=per_page)
            return {"releases": [r.to_dict() for r in releases], "total_count": len(releases)}

        raise ValueError("unsupported command")

    def _file_list_sync(self, target: Path, payload: dict[str, object] | None = None) -> dict[str, object]:
        if target.is_file(): return {"items": [], "type": "file"}
        if not target.is_dir(): raise ValueError("folder unavailable")
        payload = payload or {}
        page = int(payload.get("page", 0)) if "page" in payload else 0
        page_size = int(payload.get("page_size", 50)) if "page_size" in payload else 50
        search = str(payload.get("search")) if "search" in payload and payload.get("search") is not None else None
        recursive = bool(payload.get("recursive", False))
        return list_directory_paginated(target, page=page, page_size=page_size, search=search, recursive=recursive)

    def _file_preview_or_download_sync(self, command: str, target: Path, payload: dict[str, object]) -> dict[str, object]:
        if not target.is_file() or self._sensitive_file_name(target.name):
            raise ValueError("file is unavailable for safe text transfer")

        # Binary file handling (Issue 03 & Phase 5)
        is_bin, category, mime = detect_binary_file(target)
        if is_bin:
            if command == "file.info" or payload.get("allow_binary") or payload.get("metadata"):
                return get_binary_metadata(target)
            if command == "file.download":
                # Check if requesting a specific part from a multi-part export
                if "part_index" in payload:
                    part_idx = int(payload["part_index"])
                    manifest = build_export_plan(target)
                    part, data = get_export_part_bytes(target, manifest, part_idx)
                    return {
                        "name": part.filename,
                        "is_binary": True,
                        "mime_type": mime,
                        "part": part.to_dict(),
                        "manifest": manifest.to_dict(),
                        "content_base64": base64.b64encode(data).decode("ascii"),
                    }
                # Check if binary file requires multi-part export
                if target.stat().st_size > MAX_DOWNLOAD_CHUNK_BYTES:
                    if target.stat().st_size > MAX_TOTAL_EXPORT_BYTES:
                        raise ValueError(f"file size ({target.stat().st_size} bytes) exceeds download limit ({MAX_TOTAL_EXPORT_BYTES} bytes)")
                    manifest = build_export_plan(target)
                    return {
                        "status": "multipart_export",
                        "is_multipart": True,
                        "manifest": manifest.to_dict(),
                        "message": f"Binary file ({target.stat().st_size} bytes) exceeds single download chunk size ({MAX_DOWNLOAD_CHUNK_BYTES} bytes). Multi-part export prepared with {manifest.part_count} parts.",
                    }
                raw = target.read_bytes()
                return {
                    "name": target.name,
                    "is_binary": True,
                    "mime_type": mime,
                    "content_base64": base64.b64encode(raw).decode("ascii"),
                }
            raise ValueError("binary file preview is unavailable")

        if command == "file.info":
            return get_file_info(target)

        if command == "file.search":
            query = str(payload.get("query", ""))
            case_sens = bool(payload.get("case_sensitive", False))
            max_res = int(payload.get("max_results", 20))
            return search_file_text(target, query=query, max_results=max_res, case_sensitive=case_sens)

        if command == "file.symbols":
            return extract_file_symbols(target)

        if command == "file.download":
            # Check if requesting a specific part from a multi-part export
            if "part_index" in payload:
                part_idx = int(payload["part_index"])
                manifest = build_export_plan(target)
                part, data = get_export_part_bytes(target, manifest, part_idx)
                try:
                    text_content = data.decode("utf-8")
                    text_content = self._redact_secrets(text_content)
                    data = text_content.encode("utf-8")
                except UnicodeDecodeError:
                    pass
                return {
                    "name": part.filename,
                    "is_binary": False,
                    "part": part.to_dict(),
                    "manifest": manifest.to_dict(),
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
            # Check if text file requires multi-part export
            if target.stat().st_size > MAX_DOWNLOAD_CHUNK_BYTES:
                if target.stat().st_size > MAX_TOTAL_EXPORT_BYTES:
                    raise ValueError(f"file size ({target.stat().st_size} bytes) exceeds download limit ({MAX_TOTAL_EXPORT_BYTES} bytes)")
                manifest = build_export_plan(target)
                return {
                    "status": "multipart_export",
                    "is_multipart": True,
                    "manifest": manifest.to_dict(),
                    "message": f"File ({target.stat().st_size} bytes) exceeds single download chunk size ({MAX_DOWNLOAD_CHUNK_BYTES} bytes). Multi-part export prepared with {manifest.part_count} parts.",
                }
            raw = target.read_bytes()
            try: content = raw.decode("utf-8")
            except UnicodeDecodeError as exc: raise ValueError("file is not UTF-8 text") from exc
            content = self._redact_secrets(content)
            return {"name": target.name + ".redacted.txt",
                    "content_base64": base64.b64encode(content.encode("utf-8")).decode("ascii")}

        # Check if line-based streaming preview requested (Issue 01 & Phase 5)
        if "start_line" in payload or "end_line" in payload or "lines" in payload or payload.get("line_preview"):
            start_line = int(payload.get("start_line", 1))
            end_line = int(payload.get("end_line")) if payload.get("end_line") is not None else None
            max_lines = int(payload.get("max_lines", 35))
            expected_ver = str(payload.get("expected_version")) if "expected_version" in payload else None
            return preview_file_bounded(target, start_line=start_line, end_line=end_line, max_lines=max_lines, expected_version=expected_ver)

        # Standard / legacy character pagination preview
        size_limit = PREVIEW_FILE_LIMIT
        if target.stat().st_size > size_limit:
            raise ValueError("file is unavailable for safe text transfer")
        raw = target.read_bytes()
        try: content = raw.decode("utf-8")
        except UnicodeDecodeError as exc: raise ValueError("file is not UTF-8 text") from exc
        content = self._redact_secrets(content)
        page = payload.get("page", 0)
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            raise ValueError("invalid preview page")
        pages = max(1, (len(content) + PREVIEW_PAGE_CHARS - 1) // PREVIEW_PAGE_CHARS)
        if page >= pages:
            raise ValueError("preview page is unavailable")
        start = page * PREVIEW_PAGE_CHARS
        end = min(start + PREVIEW_PAGE_CHARS, len(content))
        return {"text": content[start:end], "page": page, "pages": pages}

    def _validated_project_path(self, payload: dict[str, object], *, allow_page: bool = False) -> tuple[Path, list[Path]]:
        allowed_keys = {
            "project_root", "path", "page", "page_size", "search", "recursive",
            "start_line", "end_line", "max_lines", "lines", "expected_version",
            "allow_binary", "metadata", "query", "case_sensitive", "max_results",
            "line_preview", "part_index",
        }
        if not isinstance(payload, dict) or not {"project_root", "path"}.issubset(payload):
            raise ValueError("invalid file request")
        if not set(payload).issubset(allowed_keys):
            raise ValueError("invalid file request")
        if not isinstance(payload["project_root"], str) or not isinstance(payload["path"], str):
            raise ValueError("invalid file request")
        if "page" in payload:
            if not isinstance(payload["page"], int) or isinstance(payload["page"], bool) or not 0 <= payload["page"] <= MAX_PREVIEW_PAGE:
                raise ValueError("invalid file request")
        requested_path = Path(str(payload["path"]))
        if (self._sensitive_file_name(requested_path.name)
                or any(part.casefold() in HIDDEN_DIRECTORY_NAMES for part in requested_path.parts[:-1])):
            raise ValueError("path is unavailable")
        roots = [validate_access_root(root) for root in self.store.workspaces()]
        project_root = require_allowed(str(payload["project_root"]), roots)
        target = require_allowed(str(payload["path"]), roots)
        available_projects = {str(item["absolute_path"]).casefold()
                              for item in self.project_index.snapshot()["items"]}
        if str(project_root).casefold() not in available_projects:
            raise PermissionError("path is outside a discovered project scope")
        if not project_root.is_dir() or not target.is_relative_to(project_root):
            raise ValueError("path is outside the selected project")
        relative_parts = target.relative_to(project_root).parts
        if (self._sensitive_file_name(target.name)
                or any(self._sensitive_file_name(part) for part in relative_parts)
                or any(part.casefold() in HIDDEN_DIRECTORY_NAMES for part in relative_parts[:-1])):
            raise ValueError("path is unavailable")
        return target, roots

    @staticmethod
    def _sensitive_file_name(name: str) -> bool:
        return is_sensitive_file(name)

    @staticmethod
    def _redact_secrets(content: str) -> str:
        return redact_secrets(content)

    def status(self) -> dict[str, object]:
        return {"status": "healthy", "device": self.store.get_device("device_id"), "runtime_version": __version__,
                "connection": self.connection.state.lower(), "session_id": self.connection.session_id,
                "session_generation": self.connection.generation,
                "connectivity": self.connection.connectivity.snapshot(),
                "project_index": {"refreshing": self.project_index.refreshing,
                    "last_refresh_at": self.project_index.last_refresh_at,
                    "last_refresh_duration_ms": self.project_index.last_refresh_duration_ms},
                "workspaces": len(self.store.workspaces()), "last_heartbeat": self.connection.last_heartbeat}

    def _health_live(self) -> dict[str, str]:
        """Extremely cheap liveness check — no I/O, no ProjectIndex, no LLM."""
        return {"status": "ok", "service": "agentbridge-runtime", "version": __version__}

    def _health_ready(self) -> tuple[dict[str, object], int]:
        """Lightweight readiness check — in-memory only."""
        checks: dict[str, str] = {}
        try:
            self.store.get_device("device_id")
            checks["state_store"] = "ok"
        except Exception:
            checks["state_store"] = "error"
        try:
            self.project_index.snapshot()
            checks["project_index"] = "ok"
        except Exception:
            checks["project_index"] = "error"
        all_ok = all(v == "ok" for v in checks.values())
        status_code = 200 if all_ok else 503
        return {"status": "ready" if all_ok else "not_ready",
                "service": "agentbridge-runtime", "version": __version__,
                "checks": checks}, status_code

    def start_http(self) -> None:
        runtime = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                log.info("HTTP %s", args[1] if len(args) > 1 else "request")
            def _send(self, code: int, data: object) -> None:
                body = json.dumps(data).encode(); self.send_response(code)
                self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health/live":
                    self._send(200, runtime._health_live())
                elif path == "/health/ready":
                    data, code = runtime._health_ready()
                    self._send(code, data)
                elif path == "/health":
                    # Backward-compatible: always healthy, ignores readiness state
                    self._send(200, {"status": "healthy", "service": "agentbridge-runtime", "version": __version__})
                elif path == "/status":
                    self._send(200, runtime.status())
                else:
                    self._send(404, {"error": "not found"})
            def do_POST(self) -> None:
                if urlparse(self.path).path != "/pair" or runtime.pairing is None:
                    self._send(404, {"error": "pairing unavailable"}); return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size < 1 or size > 4096: raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(size))
                    device_id = runtime.pairing.pair(str(payload.get("code", "")), str(payload.get("device_name", "")))
                    runtime.pairing = None; runtime.connection.device_id = device_id
                    runtime.connection.token = runtime.store.get_device("device_token")
                    runtime.connection.session_id = runtime.store.get_device("session_id")
                    runtime.connection.generation = int(runtime.store.get_device("session_generation") or 1)
                    runtime.connection.state = "DISCONNECTED"
                    log.info("Device paired"); self._send(200, {"device_id": device_id, "paired": True})
                except (PermissionError, ValueError, json.JSONDecodeError) as exc: self._send(403, {"error": str(exc)})
                except Exception: self._send(400, {"error": "invalid pairing request"})
        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        Thread(target=self.server.serve_forever, name="agentbridge-http", daemon=True).start()
    async def run(self) -> None:
        self.start_http(); self.connection.state = "PAIRING_REQUIRED" if self.gateway_url or self.pairing else "DISCONNECTED"
        self.project_index.schedule_refresh()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self._shutdown.set))
                except (OSError, ValueError):
                    pass
        log.info("Runtime started; local API listening at http://%s:%s", self.host, self.port)
        await self.agent_supervisor.start()
        print("SŪTRA Local Runtime\n==========================")
        print(f"\nStatus: {'PAIRING_REQUIRED' if self.pairing else 'DISCONNECTED'}")
        print(f"\nDevice ID: {self.store.get_device('device_id') or '(created after pairing)'}")
        if self.pairing:
            print(f"\nPairing Code: {self.pairing.display_code}\nExpires in: 300 seconds")
            print("Pair locally: POST http://127.0.0.1:8765/pair with JSON {\"code\": \"...\"}")
        print(f"\nWorkspace access: {len(self.store.workspaces())} registered\nConnection: {self.connection.state}")
        print("Health: http://127.0.0.1:8765/health")
        try:
            if self.gateway_url:
                client = RemotePairingClient(self.control_plane_url, self.gateway_url, self.ca_file)
                delay = 1
                while not self._shutdown.is_set():
                    if not self.connection.token:
                        if not self.session_id:
                            import uuid
                            self.session_id = str(uuid.uuid4())
                            self.runtime_generation += 1
                        pair_task = asyncio.create_task(client.pair(self.store, session_id=self.session_id))
                        shutdown_task = asyncio.create_task(self._shutdown.wait())
                        done, _ = await asyncio.wait({pair_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
                        if shutdown_task in done:
                            pair_task.cancel()
                            await asyncio.gather(pair_task, return_exceptions=True)
                            break
                        shutdown_task.cancel()
                        await asyncio.gather(shutdown_task, return_exceptions=True)
                        try:
                            device_id, token = pair_task.result()
                        except Exception as exc:
                            log.info("Pairing unavailable (%s); retrying with bounded backoff", type(exc).__name__)
                            try:
                                await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                            except asyncio.TimeoutError:
                                pass
                            delay = min(self.connection.max_backoff, delay * 2)
                            continue
                        self.connection.device_id = device_id
                        self.connection.token = token
                        self.connection.session_id = self.store.get_device("session_id") or self.session_id
                        self.connection.generation = int(self.store.get_device("session_generation") or 0)
                        self.connection.connectivity = ConnectivityGuard(self.policy)
                        self.connection.state = "CONNECTING"
                        log.info("Fresh ephemeral pairing completed")
                        delay = 1
                    connection_task = asyncio.create_task(self.connection.run())
                    shutdown_task = asyncio.create_task(self._shutdown.wait())
                    done, _ = await asyncio.wait({connection_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
                    if shutdown_task in done:
                        await self.connection.stop()
                        connection_task.cancel()
                    await asyncio.gather(connection_task, return_exceptions=True)
                    shutdown_task.cancel()
                    await asyncio.gather(shutdown_task, return_exceptions=True)
                    if not self._shutdown.is_set():
                        import uuid
                        self.store.set_device("gateway_token", "")
                        self.store.set_device("session_id", "")
                        self.connection.token = None
                        self.connection.session_id = None
                        self.connection.state = "WAITING_FOR_PAIRING"
                        self.session_id = str(uuid.uuid4())
                        self.runtime_generation += 1
                        client.invalidate_session()
            else:
                await self._shutdown.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            log.info("Runtime shutting down")
            await self.agent_supervisor.stop()
            await self.connection.stop()
            if self.server: self.server.shutdown(); self.server.server_close()
            self.store.close()
