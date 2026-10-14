"""Authenticated outbound WebSocket session for a paired runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable

from .pairing import new_frame_id
from .file_limits import MAX_PREVIEW_PAGE
from .connectivity import ConnectivityGuard, ConnectivityPolicy

log = logging.getLogger(__name__)
CommandHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ConnectionManager:
    STATES = {"STARTING", "PAIRING_REQUIRED", "AUTHENTICATING", "CONNECTING", "CONNECTED", "DISCONNECTED", "RECONNECTING", "REVOKED", "SHUTTING_DOWN"}

    def __init__(self, gateway_url: str, token: str | None, device_id: str | None,
                 max_backoff: int = 30, ca_file: str | None = None,
                 command_handler: CommandHandler | None = None, session_id: str | None = None,
                 generation: int = 0, policy: ConnectivityPolicy | None = None,
                 max_concurrency: int = 10):
        self.gateway_url, self.token, self.device_id = gateway_url, token, device_id
        self.session_id, self.generation = session_id, generation
        self.max_backoff = max(1, max_backoff)
        self.ca_file = ca_file
        self.command_handler = command_handler
        self.policy = policy or ConnectivityPolicy.from_environment()
        self.connectivity = ConnectivityGuard(self.policy)
        self.state = "DISCONNECTED"
        self.last_heartbeat: float | None = None
        self._stop = asyncio.Event()
        self._stop_current_session = False
        self.websocket: Any = None
        self._last_probe_at = 0.0
        self._seen_requests: deque[str] = deque(maxlen=2048)
        self._ack_ids: dict[str, float] = {}
        env_concurrency = int(os.environ.get("MAX_RUNTIME_CONCURRENCY", max_concurrency))
        self.max_concurrency = max(1, env_concurrency)
        self._command_semaphore = asyncio.Semaphore(self.max_concurrency)
        self._send_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task[Any]] = set()

    async def _send_raw(self, ws: Any, message: str) -> None:
        async with self._send_lock:
            await ws.send(message)

    async def _send_event(self, ws: Any, state: str) -> None:
        if self.device_id:
            await self._send_raw(ws, json.dumps({"type": "event", "event": "runtime.status_changed",
                "frame_id": new_frame_id(), "device_id": self.device_id,
                "session_id": self.session_id, "generation": self.generation,
                "timestamp": int(time.time()), "payload": {"connection_status": state}}))

    async def _handle_frame(self, ws: Any, raw: str) -> None:
        if len(raw.encode("utf-8")) > 32768:
            raise ValueError("oversize server frame")
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            raise ValueError("invalid server frame")
        kind = frame.get("type")
        if kind == "heartbeat_ack":
            ack_uuid = uuid.UUID(str(frame.get("frame_id", "")))
            ack = str(ack_uuid)
            sent_at = int(frame.get("sent_at", 0))
            if (ack_uuid.version != 4 or frame.get("device_id") != self.device_id
                    or frame.get("session_id") != self.session_id or frame.get("generation") != self.generation
                    or ack not in self._ack_ids
                    or abs(time.time() - sent_at) > 60):
                raise ValueError("invalid heartbeat acknowledgement")
            rtt_ms = (time.perf_counter_ns() - self._ack_ids.pop(ack)) / 1_000_000
            self.connectivity.record_success(rtt_ms, time.time())
            self.last_heartbeat = time.time()
            return
        if kind != "command":
            raise ValueError("unsupported server frame")
        request_uuid = uuid.UUID(str(frame.get("request_id", "")))
        request_id = str(request_uuid)
        if request_id in self._seen_requests:
            raise ValueError("replayed command")
        self._seen_requests.append(request_id)
        sent_at = int(frame.get("sent_at", 0))
        if (request_uuid.version != 4 or frame.get("device_id") != self.device_id
                or frame.get("session_id") != self.session_id or frame.get("generation") != self.generation
                or abs(time.time() - sent_at) > 60):
            raise ValueError("stale or misrouted command")
        command, payload = frame.get("command"), frame.get("payload")
        if command == "session.stop":
            if payload != {}:
                raise ValueError("invalid session stop command")
            result = {"stopped": True}
            response = {"type": "response", "request_id": request_id, "device_id": self.device_id,
                        "session_id": self.session_id, "generation": self.generation,
                        "trace_id": frame.get("trace_id"), "status": "success", "payload": result,
                        "sent_at": int(time.time())}
            await self._send_raw(ws, json.dumps(response))
            self._stop_current_session = True
            await ws.close(code=4003, reason="session stopped")
            return
        trace_id = frame.get("trace_id")
        try:
            trace_uuid = uuid.UUID(str(trace_id))
            trace_valid = trace_uuid.version == 4
        except ValueError:
            trace_valid = False
        deadline_at = frame.get("deadline_at")
        if (not trace_valid or not isinstance(deadline_at, (int, float))
                or isinstance(deadline_at, bool)):
            raise ValueError("invalid command metadata")
        empty_payload_commands = {"runtime.status", "runtime.health", "workspace.list", "project.list", "project.refresh"}
        file_commands = {"file.list", "file.preview", "file.download", "file.info", "file.search", "file.symbols"}
        workspace_add_commands = {"workspace.add"}
        allowed_file_keys = {
            "project_root", "path", "page", "page_size", "search", "recursive",
            "start_line", "end_line", "max_lines", "lines", "expected_version",
            "allow_binary", "metadata", "query", "case_sensitive", "max_results",
            "line_preview",
        }
        valid_file_payload = (isinstance(payload, dict) and {"project_root", "path"}.issubset(payload)
                              and set(payload).issubset(allowed_file_keys)
                              and all(isinstance(payload[key], str) and len(payload[key]) <= 4096
                                      for key in ("project_root", "path"))
                              and ("page" not in payload or
                                   (isinstance(payload["page"], int) and not isinstance(payload["page"], bool)
                                    and 0 <= payload["page"] <= MAX_PREVIEW_PAGE)))
        valid_workspace_add_payload = (isinstance(payload, dict) and set(payload) == {"path"}
                                       and isinstance(payload["path"], str)
                                       and 1 <= len(payload["path"]) <= 4096)
        agent_commands = {"agent.list", "agent.status", "agent.start", "agent.stop", "agent.prompt", "agent.events"}
        valid_agent_payload = False
        if command == "agent.list":
            valid_agent_payload = isinstance(payload, dict) and payload == {}
        elif command == "agent.status":
            valid_agent_payload = (isinstance(payload, dict) and set(payload).issubset({"agent_id", "session_id"})
                                   and all(isinstance(value, str) and 1 <= len(value) <= 256
                                           for value in payload.values()))
        elif command == "agent.start":
            valid_agent_payload = (
                isinstance(payload, dict) and {"agent_id", "project_root"}.issubset(payload)
                and set(payload).issubset({"agent_id", "project_root", "prompt", "metadata"})
                and isinstance(payload["agent_id"], str) and 1 <= len(payload["agent_id"]) <= 64
                and isinstance(payload["project_root"], str) and 1 <= len(payload["project_root"]) <= 4096
                and ("prompt" not in payload or
                     (isinstance(payload["prompt"], str) and 1 <= len(payload["prompt"]) <= 32768))
                and ("metadata" not in payload or isinstance(payload["metadata"], dict))
            )
        elif command == "agent.stop":
            valid_agent_payload = (isinstance(payload, dict) and set(payload) == {"session_id"}
                                   and isinstance(payload["session_id"], str)
                                   and 1 <= len(payload["session_id"]) <= 256)
        elif command == "agent.prompt":
            valid_agent_payload = (isinstance(payload, dict) and set(payload) == {"session_id", "prompt"}
                                   and isinstance(payload["session_id"], str)
                                   and 1 <= len(payload["session_id"]) <= 256
                                   and isinstance(payload["prompt"], str)
                                   and 1 <= len(payload["prompt"]) <= 32768)
        elif command == "agent.events":
            valid_agent_payload = (
                isinstance(payload, dict) and "session_id" in payload
                and set(payload).issubset({"session_id", "limit"})
                and isinstance(payload["session_id"], str) and 1 <= len(payload["session_id"]) <= 256
                and ("limit" not in payload or
                     (isinstance(payload["limit"], int) and not isinstance(payload["limit"], bool)
                      and 1 <= payload["limit"] <= 50))
            )
        git_commands = {
            "git.status", "git.branches", "git.diff", "git.stage", "git.unstage",
            "git.commit", "git.push_preview", "git.push", "git.pr_create", "git.pr_list",
            "git.pr_get", "git.pr_files", "git.pr_comments", "git.pr_comment_create",
            "git.pr_reviews", "git.pr_merge", "git.ci_runs", "git.ci_run_get",
            "git.ci_run_logs", "git.ci_run_rerun", "git.ci_run_cancel", "git.releases_list",
            "github.pr.list", "github.pr.get", "github.pr.files", "github.pr.comments",
            "github.pr.comment.create", "github.pr.reviews", "github.pr.merge",
            "github.ci.runs", "github.ci.run.get", "github.ci.run.logs", "github.ci.run.rerun",
            "github.ci.run.cancel", "github.releases.list"
        }
        allowed_git_keys = {
            "project_root", "staged", "path", "max_lines", "files",
            "message", "author", "remote", "branch", "title", "body", "head", "base", "draft", "state", "force",
            "number", "pr_number", "page", "per_page", "comment", "merge_method", "commit_title", "commit_message",
            "sha", "run_id", "line", "commit_id"
        }
        valid_git_payload = (
            isinstance(payload, dict)
            and "project_root" in payload
            and isinstance(payload["project_root"], str)
            and 1 <= len(payload["project_root"]) <= 4096
            and set(payload).issubset(allowed_git_keys)
            and ("files" not in payload or isinstance(payload["files"], list))
            and ("message" not in payload or isinstance(payload["message"], str))
            and ("title" not in payload or isinstance(payload["title"], str))
        )
        if ((command in empty_payload_commands and payload != {})
                or (command in file_commands and not valid_file_payload)
                or (command in workspace_add_commands and not valid_workspace_add_payload)
                or (command in agent_commands and not valid_agent_payload)
                or (command in git_commands and not valid_git_payload)
                or command not in empty_payload_commands | file_commands | workspace_add_commands | agent_commands | git_commands):
            raise ValueError("unsupported command")

        task = asyncio.create_task(
            self._execute_command_bounded(ws, frame, request_id, command, payload, deadline_at)
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    async def _execute_command_bounded(
        self, ws: Any, frame: dict[str, Any], request_id: str,
        command: str, payload: Any, deadline_at: float
    ) -> None:
        async with self._command_semaphore:
            runtime_started_ns = time.perf_counter_ns()
            try:
                if self.command_handler is None:
                    raise RuntimeError("command handler unavailable")
                if time.time() > deadline_at:
                    raise TimeoutError("request expired before runtime processing")
                result = await self.command_handler(command, payload)
                response = {"type": "response", "request_id": request_id, "device_id": self.device_id,
                            "session_id": self.session_id, "generation": self.generation,
                            "trace_id": frame.get("trace_id"), "status": "success", "payload": result,
                            "runtime_received_at": time.time(),
                            "runtime_processing_ms": (time.perf_counter_ns() - runtime_started_ns) / 1_000_000,
                            "sent_at": int(time.time())}
            except Exception as exc:
                response = {"type": "response", "request_id": request_id, "device_id": self.device_id,
                            "session_id": self.session_id, "generation": self.generation,
                            "trace_id": frame.get("trace_id"), "status": "error",
                            "payload": {"error": type(exc).__name__}, "sent_at": int(time.time())}
            try:
                await self._send_raw(ws, json.dumps(response))
            except Exception as exc:
                log.debug("Failed to send command response (%s)", type(exc).__name__)

    async def _heartbeat_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.policy.probe_interval_seconds)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            now_ns = time.perf_counter_ns()
            expired = [frame_id for frame_id, sent_ns in self._ack_ids.items()
                       if now_ns - sent_ns >= int(self.policy.probe_interval_seconds * 1_500_000_000)]
            for frame_id in expired:
                self._ack_ids.pop(frame_id, None)
                self.connectivity.record_failure()
            if self.connectivity.state == "UNUSABLE":
                log.warning("Runtime connectivity became unusable after repeated missed heartbeats")
                await ws.close(code=4001, reason="connectivity unusable")
                return
            await self._send_heartbeat(ws)

    async def _send_heartbeat(self, ws: Any) -> None:
        frame_id = new_frame_id()
        self._ack_ids[frame_id] = time.perf_counter_ns()
        if len(self._ack_ids) > 16:
            self._ack_ids.clear()
            self._ack_ids[frame_id] = time.perf_counter_ns()
        await self._send_raw(ws, json.dumps({"type": "heartbeat", "frame_id": frame_id,
            "device_id": self.device_id, "session_id": self.session_id,
            "generation": self.generation, "connectivity": self.connectivity.snapshot(),
            "sent_at": int(time.time())}))
        self.last_heartbeat = time.time()

    async def run(self) -> None:
        if not self.gateway_url:
            self.state = "DISCONNECTED"
            return
        if not self.gateway_url.startswith("wss://"):
            raise ValueError("Control-plane URL must use wss://")

        has_connected = False
        reconnect_delay = 1
        reconnect_attempts = 0
        max_reconnect_attempts = 5

        while not self._stop.is_set():
            if not self.token or not self.device_id:
                self.state = "PAIRING_REQUIRED"
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                import websockets
                self.state = "RECONNECTING" if has_connected else "CONNECTING"
                log.info("SESSION_CONNECTING")
                ctx = ssl.create_default_context(cafile=self.ca_file)
                async with websockets.connect(self.gateway_url,
                        additional_headers={"Authorization": f"Bearer {self.token}",
                                            "X-AgentBridge-Device-ID": self.device_id,
                                            "X-AgentBridge-Session-ID": str(self.session_id),
                                            "X-AgentBridge-Generation": str(self.generation)},
                        ssl=ctx, ping_interval=20, ping_timeout=10, max_size=32768) as ws:
                    self.websocket = ws
                    self.state = "CONNECTED"
                    has_connected = True
                    reconnect_delay = 1
                    reconnect_attempts = 0
                    log.info("WSS_CONNECTED")
                    log.info("SESSION_CONNECTED")
                    log.info("Authenticated control-plane session established")
                    await self._send_heartbeat(ws)
                    if not self._stop.is_set():
                        await self._send_event(ws, "CONNECTED")
                    heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        async for raw in ws:
                            await self._handle_frame(ws, raw)
                            if self._stop_current_session:
                                break
                    finally:
                        heartbeat.cancel()
                        if self._active_tasks:
                            for task in list(self._active_tasks):
                                task.cancel()
                            await asyncio.gather(*self._active_tasks, return_exceptions=True)
                        await asyncio.gather(heartbeat, return_exceptions=True)
                        self.websocket = None
                if not self._stop.is_set():
                    if self._stop_current_session:
                        self.state = "WAITING_FOR_PAIRING"
                        self.token = None
                        log.info("SESSION_INVALIDATED")
                        log.info("PAIRING_REQUIRED")
                        return
                    if has_connected and reconnect_attempts < max_reconnect_attempts:
                        reconnect_attempts += 1
                        self.state = "RECOVERING"
                        self.connectivity.disconnected()
                        log.info("Control-plane transport disconnected; attempting bounded recovery (%d/%d) in %ds",
                                 reconnect_attempts, max_reconnect_attempts, reconnect_delay)
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=reconnect_delay)
                        except asyncio.TimeoutError:
                            pass
                        reconnect_delay = min(self.max_backoff, reconnect_delay * 2)
                        continue
                    else:
                        self.state = "WAITING_FOR_PAIRING"
                        self.token = None
                        log.info("SESSION_INVALIDATED")
                        log.info("PAIRING_REQUIRED")
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                msg = str(exc).lower()
                is_unauthorized = "4401" in msg or "401" in msg or "403" in msg or "4003" in msg
                if self._stop_current_session or is_unauthorized:
                    self.state = "WAITING_FOR_PAIRING"
                    self.connectivity.disconnected()
                    self.token = None
                    log.info("SESSION_INVALIDATED")
                    log.info("PAIRING_REQUIRED")
                    log.info("Current control-plane session ended; fresh pairing is required (%s)", type(exc).__name__)
                    return
                if has_connected and reconnect_attempts < max_reconnect_attempts:
                    reconnect_attempts += 1
                    self.state = "RECOVERING"
                    self.connectivity.disconnected()
                    log.info("Control-plane connection interrupted (%s); attempting bounded recovery (%d/%d) in %ds",
                             type(exc).__name__, reconnect_attempts, max_reconnect_attempts, reconnect_delay)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=reconnect_delay)
                    except asyncio.TimeoutError:
                        pass
                    reconnect_delay = min(self.max_backoff, reconnect_delay * 2)
                    continue
                else:
                    self.state = "WAITING_FOR_PAIRING"
                    self.connectivity.disconnected()
                    self.token = None
                    log.info("SESSION_INVALIDATED")
                    log.info("PAIRING_REQUIRED")
                    log.info("Control-plane session ended; fresh pairing is required (%s)", type(exc).__name__)
                    return
        self.state = "SHUTTING_DOWN"

    async def stop(self) -> None:
        self.state = "SHUTTING_DOWN"
        self._stop.set()
        if self._active_tasks:
            for task in list(self._active_tasks):
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        if self.websocket is not None:
            try:
                await self.websocket.close(code=1000, reason="runtime shutdown")
            except Exception:
                pass
