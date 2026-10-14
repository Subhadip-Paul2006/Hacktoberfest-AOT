"""Authenticated control-plane API, pairing broker, and device session router."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
import sqlite3
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .file_limits import MAX_PREVIEW_PAGE
from .connectivity import ConnectivityGuard, ConnectivityPolicy
from .latency import LatencyMetrics
from .metrics import MetricsRegistry, _EVENT_COUNTER_NAMES
from .cache import (CacheStore, SingleFlight, SAFE_CACHE_COMMAND_TTLS, COALESCIBLE_COMMANDS,
                     make_cache_key, session_cache_prefix, device_cache_prefix)
from .resilience import (
    CircuitBreaker,
    CircuitOpenError,
    IdempotencyConflictError,
    IdempotencyState,
    IdempotencyStore,
    RetryPolicy,
    SlidingWindowRateLimiter,
    with_timeout_and_retry,
)
from .degradation import (
    degraded_response,
    can_degrade,
    can_use_cache_fallback,
    should_degrade_on_dependency_failure,
)
from .permissions import PermissionEngine
from .policy_engine import PolicyEngine
from .policy_store import PolicyStore
from .activity_store import ActivityStore

log = logging.getLogger(__name__)
PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ALLOWED_COMMANDS = {
    "runtime.status", "runtime.health", "workspace.list", "workspace.add",
    "project.list", "project.refresh", "file.list", "file.preview", "file.download",
    "file.info", "file.search", "file.symbols",
    "agent.list", "agent.status", "agent.start", "agent.stop", "agent.prompt", "agent.events",
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
PAIRING_TTL_SECONDS = 300
FRAME_MAX_AGE_SECONDS = 60
HEARTBEAT_TIMEOUT_SECONDS = 60
DEVICE_CREDENTIAL_TTL_SECONDS = 30 * 24 * 60 * 60
CONTROL_REQUEST_TIMEOUT_SECONDS = 7


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    device_id: str | None = Field(default=None, pattern=r"^AB-PC-[A-Fa-f0-9]{6}$")
    device_name: str = Field(default="SŪTRA PC", min_length=1, max_length=80)
    runtime_version: str = Field(default=__version__, max_length=32)
    platform: str = Field(default="unknown", max_length=32)
    poll_secret_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=r"^[0-9a-fA-F-]{36}$")


class PairApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(min_length=8, max_length=9)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class ControlPlaneStore:
    def __init__(self, database_path: str | Path):
        if str(database_path) != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(str(database_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                runtime_version TEXT NOT NULL,
                platform TEXT NOT NULL,
                token_hash TEXT,
                token_expires_at REAL,
                created_at REAL NOT NULL,
                last_seen REAL,
                revoked_at REAL,
                session_id TEXT,
                session_generation INTEGER NOT NULL DEFAULT 0,
                session_status TEXT NOT NULL DEFAULT 'INVALIDATED',
                session_created_at REAL,
                connected_at REAL,
                disconnected_at REAL
            );
            CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY);
        """)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(devices)")}
        for name, sql_type in (("session_id", "TEXT"), ("session_generation", "INTEGER NOT NULL DEFAULT 0"),
                               ("session_status", "TEXT NOT NULL DEFAULT 'INVALIDATED'"),
                               ("session_created_at", "REAL"), ("connected_at", "REAL"),
                               ("disconnected_at", "REAL")):
            if name not in columns:
                self._db.execute(f"ALTER TABLE devices ADD COLUMN {name} {sql_type}")
        self._db.commit()

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register_device(self, device_id: str, name: str, version: str, platform: str,
                        token_hash: str, token_expires_at: float, session_id: str | None = None) -> int:
        session_id = session_id or str(uuid.uuid4())
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT session_generation FROM devices WHERE device_id=?", (device_id,)).fetchone()
            generation = (int(row[0]) if row else 0) + 1
            self._db.execute("""INSERT INTO devices
                (device_id,device_name,runtime_version,platform,token_hash,token_expires_at,created_at,last_seen,revoked_at,
                 session_id,session_generation,session_status,session_created_at,connected_at,disconnected_at)
                VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?, 'WAITING_FOR_PAIRING', ?,NULL,NULL)
                ON CONFLICT(device_id) DO UPDATE SET device_name=excluded.device_name,
                runtime_version=excluded.runtime_version,platform=excluded.platform,
                token_hash=excluded.token_hash,token_expires_at=excluded.token_expires_at,
                created_at=excluded.created_at,last_seen=NULL,revoked_at=NULL,
                session_id=excluded.session_id,session_generation=excluded.session_generation,
                session_status=excluded.session_status,session_created_at=excluded.session_created_at,
                connected_at=NULL,disconnected_at=NULL""",
                (device_id, name, version, platform, token_hash, token_expires_at, now,
                 session_id, generation, now))
            self._db.commit()
        return generation

    def authenticate_device(self, device_id: str, token: str, session_id: str | None = None,
                            generation: int | None = None) -> bool:
        with self._lock:
            row = self._db.execute("SELECT token_hash,token_expires_at,revoked_at,session_id,session_generation,session_status FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not row or row["revoked_at"] is not None or not row["token_hash"]:
            return False
        if row["session_status"] not in {"WAITING_FOR_PAIRING", "CONNECTED", "DISCONNECTED"}:
            return False
        if session_id is not None and row["session_id"] != session_id:
            return False
        if generation is not None and row["session_generation"] != generation:
            return False
        if row["token_expires_at"] is None or row["token_expires_at"] <= time.time():
            return False
        return hmac.compare_digest(row["token_hash"], self.digest(token))

    def set_session_status(self, device_id: str, session_id: str, generation: int,
                           status: str, *, connected: bool = False) -> bool:
        now = time.time()
        with self._lock:
            cursor = self._db.execute("""UPDATE devices SET session_status=?,
                connected_at=CASE WHEN ? THEN ? ELSE connected_at END,
                disconnected_at=CASE WHEN ? THEN ? ELSE disconnected_at END
                WHERE device_id=? AND session_id=? AND session_generation=?""",
                (status, int(connected), now, int(status in {"INVALIDATED", "STOPPED", "DISCONNECTED", "TIMEOUT"}),
                 now, device_id, session_id, generation))
            self._db.commit()
            return cursor.rowcount == 1

    def invalidate_session(self, device_id: str, session_id: str | None = None,
                           generation: int | None = None, status: str = "INVALIDATED") -> bool:
        conditions = ["device_id=?", "revoked_at IS NULL"]
        values: list[Any] = [device_id]
        if session_id is not None:
            conditions.append("session_id=?")
            values.append(session_id)
        if generation is not None:
            conditions.append("session_generation=?")
            values.append(generation)
        with self._lock:
            cursor = self._db.execute(f"UPDATE devices SET token_hash=NULL,token_expires_at=NULL,session_status=?,disconnected_at=? WHERE {' AND '.join(conditions)} AND token_hash IS NOT NULL",
                                      [status, time.time(), *values])
            self._db.commit()
            return cursor.rowcount == 1

    def invalidate_active_sessions(self) -> None:
        with self._lock:
            self._db.execute("UPDATE devices SET token_hash=NULL,token_expires_at=NULL,session_id=NULL,session_status='INVALIDATED',disconnected_at=? WHERE token_hash IS NOT NULL", (time.time(),))
            self._db.commit()

    def current_session(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT session_id,session_generation,session_status FROM devices WHERE device_id=? AND revoked_at IS NULL", (device_id,)).fetchone()
        return dict(row) if row else None

    def heartbeat(self, device_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE devices SET last_seen=? WHERE device_id=? AND revoked_at IS NULL", (time.time(), device_id))
            self._db.commit()

    def list_devices(self, connected_ids: set[str]) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            rows = self._db.execute("SELECT device_id,device_name,runtime_version,platform,created_at,last_seen,token_expires_at,revoked_at,token_hash,session_id,session_generation,session_status,connected_at,disconnected_at FROM devices ORDER BY created_at").fetchall()
        return [{"device_id": row["device_id"], "device_name": row["device_name"],
                 "runtime_version": row["runtime_version"], "platform": row["platform"],
                 "connection_status": "REVOKED" if row["revoked_at"] is not None
                     else "EXPIRED" if row["token_expires_at"] and row["token_expires_at"] <= now
                     else "CONNECTED" if row["device_id"] in connected_ids
                     else "WAITING_FOR_PAIRING" if not row["token_hash"] else "DISCONNECTED",
                 "session_id": row["session_id"], "session_generation": row["session_generation"],
                 "session_status": row["session_status"], "connected_at": row["connected_at"],
                 "disconnected_at": row["disconnected_at"],
                 "last_seen": row["last_seen"], "created_at": row["created_at"]} for row in rows]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("UPDATE devices SET token_hash=NULL,token_expires_at=NULL,revoked_at=? WHERE device_id=? AND revoked_at IS NULL", (time.time(), device_id))
            self._db.commit()
            return cursor.rowcount > 0

    def get_telegram_offset(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT value FROM bot_state WHERE key='update_offset'").fetchone()
        return int(row[0]) if row else 0

    def mark_telegram_update(self, update_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("INSERT OR IGNORE INTO telegram_updates(update_id) VALUES (?)", (update_id,))
            self._db.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES ('update_offset',?)", (str(max(update_id + 1, self.get_telegram_offset())),))
            self._db.commit()
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


class PairingBroker:
    def __init__(self, store: ControlPlaneStore):
        self.store = store
        self.pending: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, deque[float]] = {}

    def create(self, request: PairRequest, poll_hash: str, client_key: str) -> dict[str, Any]:
        now = time.time()
        expired = [key for key, item in self.pending.items() if item["expires_at"] <= now]
        for key in expired:
            del self.pending[key]

        attempts = self.failures.setdefault(client_key, deque())
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "pairing rate limit exceeded")
        attempts.append(now)

        device_id = request.device_id or f"AB-PC-{secrets.token_hex(3).upper()}"

        if request.device_id:
            current_sess = self.store.current_session(request.device_id)
            if current_sess and current_sess.get("session_status") == "CONNECTED":
                raise HTTPException(409, "Device already has an active connected session. Stop or re-pair explicitly first.")

        # Invariant: ONE RUNTIME START = ONE ACTIVE PAIRING SESSION
        # If the same session_id is already pending and still valid, reuse it
        for request_id, pending in self.pending.items():
            if pending.get("session_id") == request.session_id and pending["expires_at"] > now:
                if pending["state"] in ("PAIRING", "APPROVED"):
                    return {
                        "request_id": request_id,
                        "device_id": pending["device_id"],
                        "pairing_code": pending["pairing_code"],
                        "expires_in": max(1, int(pending["expires_at"] - now)),
                    }

        # Do not cancel an active pairing request that is still pending or approved
        for request_id, pending in list(self.pending.items()):
            if pending["device_id"] == device_id:
                if pending["state"] in ("PAIRING", "APPROVED") and pending["expires_at"] > now:
                    return {
                        "request_id": request_id,
                        "device_id": pending["device_id"],
                        "pairing_code": pending["pairing_code"],
                        "expires_in": max(1, int(pending["expires_at"] - now)),
                    }
                del self.pending[request_id]

        code_raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
        pairing_code = f"{code_raw[:4]}-{code_raw[4:]}"
        request_id = str(uuid.uuid4())
        self.pending[request_id] = {
            "device_id": device_id, "name": request.device_name,
            "version": request.runtime_version, "platform": request.platform,
            "session_id": request.session_id,
            "code_hash": ControlPlaneStore.digest(code_raw), "poll_hash": poll_hash,
            "pairing_code": pairing_code,
            "expires_at": now + PAIRING_TTL_SECONDS, "state": "PAIRING", "token": None,
        }
        return {"request_id": request_id, "device_id": device_id,
                "pairing_code": pairing_code, "expires_in": PAIRING_TTL_SECONDS}

    def approve(self, code: str) -> str:
        candidate = ControlPlaneStore.digest(code.replace("-", "").strip().upper())
        now = time.time()
        expired = [key for key, item in self.pending.items() if item["expires_at"] <= now]
        for key in expired:
            del self.pending[key]
        for request_id, item in self.pending.items():
            if item["state"] == "PAIRING" and hmac.compare_digest(item["code_hash"], candidate):
                item["state"] = "APPROVED"
                item["code_hash"] = ""
                token = secrets.token_urlsafe(32)
                item["token"] = token
                generation = self.store.register_device(item["device_id"], item["name"], item["version"], item["platform"],
                    ControlPlaneStore.digest(token), now + DEVICE_CREDENTIAL_TTL_SECONDS, item["session_id"])
                item["generation"] = generation
                return request_id
        raise HTTPException(403, "pairing code is invalid, expired, or already used")

    def complete(self, request_id: str, poll_secret: str) -> tuple[int, dict[str, Any]]:
        item = self.pending.get(request_id)
        now = time.time()
        if not item or item["expires_at"] <= now or not hmac.compare_digest(item["poll_hash"], ControlPlaneStore.digest(poll_secret)):
            self.pending.pop(request_id, None) if item and item["expires_at"] <= now else None
            return 404, {"error": "pairing request not found"}
        if item["state"] != "APPROVED":
            return 202, {"status": "PAIRING"}
        result = {"status": "approved", "device_id": item["device_id"], "device_token": item["token"],
                  "session_id": item["session_id"], "generation": item["generation"]}
        del self.pending[request_id]
        return 200, result

    def cancel_pending(self, identifier: str | None = None) -> bool:
        cancelled = False
        for req_id, pending in list(self.pending.items()):
            if identifier is None or pending.get("pairing_code") == identifier or pending.get("device_id") == identifier or req_id == identifier:
                del self.pending[req_id]
                cancelled = True
        return cancelled

    def get_pending(self, identifier: str | None = None) -> dict[str, Any] | None:
        now = time.time()
        for req_id, pending in self.pending.items():
            if pending["expires_at"] > now:
                if identifier is None or pending.get("device_id") == identifier or pending.get("pairing_code") == identifier:
                    return {**pending, "request_id": req_id, "expires_in": max(1, int(pending["expires_at"] - now))}
        return None


SIDE_EFFECT_COMMANDS = frozenset({
    "workspace.add", "access_root.add", "session.stop",
    "agent.start", "agent.stop", "agent.prompt"
})


class ControlPlane:
    def __init__(self, database_path: str | Path, operator_token: str):
        self.store = ControlPlaneStore(database_path)
        self.store.invalidate_active_sessions()
        self.operator_token_hash = ControlPlaneStore.digest(operator_token) if operator_token else ""
        self.pairing = PairingBroker(self.store)
        self.sessions: dict[str, WebSocket] = {}
        self.active_sessions: dict[str, dict[str, Any]] = {}
        self.pending_responses: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self.pending_context: dict[str, dict[str, Any]] = {}
        self.request_history: dict[str, dict[str, Any]] = {}
        self.request_history_order: deque[str] = deque()
        self.metrics = MetricsRegistry()
        self.policy = ConnectivityPolicy.from_environment()
        self.connectivity: dict[str, ConnectivityGuard] = {}
        self.last_heartbeat_monotonic: dict[str, float] = {}
        self.telegram_tasks: set[asyncio.Task[Any]] = set()
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.cache = CacheStore()
        self.single_flight = SingleFlight()
        self.device_semaphores: dict[str, asyncio.Semaphore] = {}
        self.rate_limiter = SlidingWindowRateLimiter.from_environment()
        self.retry_policy = RetryPolicy.from_environment()
        self.idempotency_store = IdempotencyStore.from_environment("CONTROL_PLANE_IDEMPOTENCY_")
        # Attach PolicyStore to the same SQLite connection (shares WAL, no second file)
        _policy_store = PolicyStore(self.store._db, self.store._lock)
        # permission_engine is kept as the attribute name for backward compatibility;
        # it is now a PolicyEngine (superset of PermissionEngine).
        self.permission_engine = PolicyEngine(_policy_store)
        # Attach ActivityStore to the same SQLite connection (shares WAL, no second file)
        self.activity_store = ActivityStore(self.store._db, self.store._lock, metrics=self.metrics)
        self.started_at = time.time()

    def verify_operator(self, authorization: str | None) -> bool:
        if not self.operator_token_hash or not authorization or not authorization.startswith("Bearer "):
            return False
        return hmac.compare_digest(self.operator_token_hash, ControlPlaneStore.digest(authorization[7:]))

    def device_status(self, device_id: str) -> dict[str, Any] | None:
        return next((item for item in self.store.list_devices(set(self.sessions)) if item["device_id"] == device_id), None)

    def emit_status_event(self, device_id: str, state: str) -> None:
        self.events.append({"type": "event", "event": "device.status_changed", "device_id": device_id,
                            "timestamp": int(time.time()), "payload": {"connection_status": state}})

    def session_matches(self, device_id: str, session_id: str, generation: int) -> bool:
        current = self.active_sessions.get(device_id)
        return bool(current and current["session_id"] == session_id
                    and current["generation"] == generation)

    def _device_semaphore(self, device_id: str) -> asyncio.Semaphore:
        sem = self.device_semaphores.get(device_id)
        if sem is None:
            sem = asyncio.Semaphore(10)
            self.device_semaphores[device_id] = sem
        return sem

    async def _readiness_checks(self) -> dict[str, str]:
        """Return ``{check_name: "ok" | "error"}`` for the /health/ready endpoint.

        Checks use short timeouts and never trigger retries, rebuilds, or
        expensive I/O.  A transient Telegram outage is intentionally NOT
        included because it should not mark the whole service as not-ready.
        """
        checks: dict[str, str] = {}

        # 1. SQLite store is accessible
        try:
            with self.store._lock:
                self.store._db.execute("SELECT 1").fetchone()
            checks["control_plane_store"] = "ok"
        except Exception:
            checks["control_plane_store"] = "error"

        # 2. Pairing broker is initialized (it is always present after __init__)
        checks["pairing_broker"] = "ok" if self.pairing is not None else "error"

        # 3. Cache is initialized (it is always present after __init__)
        checks["cache"] = "ok" if self.cache is not None else "error"

        # 4. SingleFlight (request coalescing) is initialized
        checks["single_flight"] = "ok" if self.single_flight is not None else "error"

        # 5. ProjectIndex module is importable (used for file/workspace commands)
        #    We intentionally skip instantiating it here to avoid heavy init cost;
        #    module availability is a cheap proxy for functionality.
        try:
            import sutra.project_index as _pi_mod  # noqa: F401
            _has_snapshot = hasattr(_pi_mod.ProjectIndex, "snapshot")
            checks["project_index"] = "ok" if _has_snapshot else "error"
        except Exception:
            checks["project_index"] = "error"

        # 6. Rate limiter is initialized
        checks["rate_limiter"] = "ok" if self.rate_limiter is not None else "error"

        # 7. Retry policy is initialized
        checks["retry_policy"] = "ok" if self.retry_policy is not None else "error"

        return checks

    async def dispatch_command(self, device_id: str, command: str, payload: dict[str, Any] | None = None,
                              *, message_id: str | None = None, trace_id: str | None = None,
                              warning_callback: Any = None, identity: Any = None) -> dict[str, Any]:
        overall_started_ns = time.perf_counter_ns()
        request_payload = payload or {}
        try:
            result = await self._do_dispatch_command(device_id, command, request_payload,
                                                     message_id=message_id, trace_id=trace_id,
                                                     warning_callback=warning_callback, identity=identity)
            is_success = result.get("status") == "success"
            duration_ms = (time.perf_counter_ns() - overall_started_ns) / 1_000_000
            self.metrics.record_request(success=is_success, failed=not is_success, duration_ms=duration_ms)
            return result
        except HTTPException as exc:
            duration_ms = (time.perf_counter_ns() - overall_started_ns) / 1_000_000
            is_rate_limited = exc.status_code == 429
            self.metrics.record_request(failed=True, rate_limited=is_rate_limited, duration_ms=duration_ms)
            if is_rate_limited:
                self.metrics.record_rate_limit(rejected=True)
            raise
        except BaseException as exc:
            duration_ms = (time.perf_counter_ns() - overall_started_ns) / 1_000_000
            self.metrics.record_request(failed=True, duration_ms=duration_ms)
            raise

    async def _do_dispatch_command(self, device_id: str, command: str, request_payload: dict[str, Any],
                                   *, message_id: str | None = None, trace_id: str | None = None,
                                   warning_callback: Any = None, identity: Any = None) -> dict[str, Any]:
        overall_started_ns = time.perf_counter_ns()
        try:
            return await self._do_dispatch_command_inner(
                device_id, command, request_payload,
                message_id=message_id, trace_id=trace_id,
                warning_callback=warning_callback, identity=identity,
                overall_started_ns=overall_started_ns,
            )
        except HTTPException as exc:
            raise
        except Exception as exc:
            if can_degrade(command) and should_degrade_on_dependency_failure(command, "control_plane"):
                log.warning("degradation_activated command=%s dependency=control_plane error=%s",
                            command, type(exc).__name__)
                self.metrics.record_degradation_activated(dependency="control_plane", feature=command)
                self.metrics.record_fallback_used(dependency="control_plane", fallback_type="degraded")
                return degraded_response(command, "control_plane", reason=str(exc))
            raise

    async def _do_dispatch_command_inner(self, device_id: str, command: str, request_payload: dict[str, Any],
                                         *, message_id: str | None = None, trace_id: str | None = None,
                                         warning_callback: Any = None, identity: Any = None,
                                         overall_started_ns: int = 0) -> dict[str, Any]:
        file_command = command in {"file.list", "file.preview", "file.download", "file.info", "file.search", "file.symbols"}
        workspace_add_command = command == "workspace.add"
        allowed_file_keys = {
            "project_root", "path", "page", "page_size", "search", "recursive",
            "start_line", "end_line", "max_lines", "lines", "expected_version",
            "allow_binary", "metadata", "query", "case_sensitive", "max_results",
            "line_preview",
        }
        valid_file_payload = (isinstance(request_payload, dict)
                              and {"project_root", "path"}.issubset(request_payload)
                              and set(request_payload).issubset(allowed_file_keys)
                              and all(isinstance(request_payload[key], str) and len(request_payload[key]) <= 4096
                                      for key in ("project_root", "path"))
                              and ("page" not in request_payload or
                                   (isinstance(request_payload["page"], int) and not isinstance(request_payload["page"], bool)
                                    and 0 <= request_payload["page"] <= MAX_PREVIEW_PAGE)))
        valid_workspace_add_payload = (isinstance(request_payload, dict)
                                       and set(request_payload) == {"path"}
                                       and isinstance(request_payload["path"], str)
                                       and 1 <= len(request_payload["path"]) <= 4096)
        agent_status_command = command == "agent.status"
        valid_agent_status_payload = (isinstance(request_payload, dict)
                                      and set(request_payload).issubset({"agent_id", "session_id"})
                                      and all(isinstance(v, str) and 1 <= len(v) <= 256 for v in request_payload.values()))
        agent_start_command = command == "agent.start"
        valid_agent_start_payload = (
            isinstance(request_payload, dict)
            and {"agent_id", "project_root"}.issubset(set(request_payload))
            and set(request_payload).issubset({"agent_id", "project_root", "prompt", "metadata"})
            and isinstance(request_payload["agent_id"], str) and 1 <= len(request_payload["agent_id"]) <= 64
            and isinstance(request_payload["project_root"], str) and 1 <= len(request_payload["project_root"]) <= 4096
            and ("prompt" not in request_payload or (isinstance(request_payload["prompt"], str) and len(request_payload["prompt"]) <= 32768))
            and ("metadata" not in request_payload or isinstance(request_payload["metadata"], dict))
        )
        agent_stop_command = command == "agent.stop"
        valid_agent_stop_payload = (
            isinstance(request_payload, dict)
            and set(request_payload) == {"session_id"}
            and isinstance(request_payload["session_id"], str)
            and 1 <= len(request_payload["session_id"]) <= 256
        )
        agent_prompt_command = command == "agent.prompt"
        valid_agent_prompt_payload = (
            isinstance(request_payload, dict)
            and set(request_payload) == {"session_id", "prompt"}
            and isinstance(request_payload["session_id"], str) and 1 <= len(request_payload["session_id"]) <= 256
            and isinstance(request_payload["prompt"], str) and 1 <= len(request_payload["prompt"]) <= 32768
        )
        agent_events_command = command == "agent.events"
        valid_agent_events_payload = (
            isinstance(request_payload, dict)
            and "session_id" in request_payload
            and set(request_payload).issubset({"session_id", "limit"})
            and isinstance(request_payload["session_id"], str)
            and 1 <= len(request_payload["session_id"]) <= 256
            and ("limit" not in request_payload or
                 (isinstance(request_payload["limit"], int) and not isinstance(request_payload["limit"], bool)
                  and 1 <= request_payload["limit"] <= 50))
        )
        git_command = command in {
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
            isinstance(request_payload, dict)
            and "project_root" in request_payload
            and isinstance(request_payload["project_root"], str)
            and 1 <= len(request_payload["project_root"]) <= 4096
            and set(request_payload).issubset(allowed_git_keys)
            and ("files" not in request_payload or isinstance(request_payload["files"], list))
            and ("message" not in request_payload or isinstance(request_payload["message"], str))
            and ("title" not in request_payload or isinstance(request_payload["title"], str))
        )
        payload_is_valid = (
            (file_command and valid_file_payload)
            or (workspace_add_command and valid_workspace_add_payload)
            or (agent_status_command and valid_agent_status_payload)
            or (agent_start_command and valid_agent_start_payload)
            or (agent_stop_command and valid_agent_stop_payload)
            or (agent_prompt_command and valid_agent_prompt_payload)
            or (agent_events_command and valid_agent_events_payload)
            or (git_command and valid_git_payload)
            or (not file_command and not workspace_add_command and not agent_status_command
                and not agent_start_command and not agent_stop_command and not agent_prompt_command
                and not agent_events_command
                and not git_command
                and request_payload == {})
        )
        if command not in ALLOWED_COMMANDS or not payload_is_valid:
            raise HTTPException(400, "unsupported command")
        websocket = self.sessions.get(device_id)
        if not websocket:
            raise HTTPException(409, "device is offline")
        current = self.active_sessions.get(device_id)
        if not current:
            raise HTTPException(409, "session is no longer active; a fresh pairing is required")
        guard = self.connectivity.setdefault(device_id, ConnectivityGuard(self.policy))
        last_heartbeat = self.last_heartbeat_monotonic.get(device_id)
        if (last_heartbeat is None or time.monotonic() - last_heartbeat
                >= self.policy.probe_interval_seconds * self.policy.failure_probe_count):
            guard.state = "UNUSABLE"
        if not guard.allows_request():
            raise HTTPException(503, "connection quality is currently unusable")

        cache_key = make_cache_key(device_id, current["session_id"], current["generation"], command, request_payload)
        is_cache_eligible = command in SAFE_CACHE_COMMAND_TTLS

        if is_cache_eligible:
            lookup_start = time.perf_counter_ns()
            cached_data = self.cache.get(cache_key)
            self.metrics.record("cache_lookup_ms", (time.perf_counter_ns() - lookup_start) / 1_000_000)
            if cached_data is not None:
                self.metrics.record("cache_hit", 1.0)
                res = dict(cached_data)
                res["trace_id"] = trace_id or str(uuid.uuid4())
                res["cached"] = True
                duration_ms = (time.perf_counter_ns() - overall_started_ns) / 1_000_000
                res["latency_ms"] = round(duration_ms, 3)
                self.metrics.record("control_plane_dispatch_ms", duration_ms)
                return res
            self.metrics.record("cache_miss", 1.0)

        # Rate Limiting Guard before sending to WSS
        rate_id = identity if identity is not None else f"device:{device_id}"
        allowed, retry_after = self.rate_limiter.is_allowed(rate_id)
        if not allowed:
            self.metrics.record("rate_limit_rejected", 1.0)
            wait_sec = max(1, int(math.ceil(retry_after)))
            raise HTTPException(
                429,
                f"rate limit exceeded; try again in {retry_after:.1f}s",
                headers={"Retry-After": str(wait_sec)},
            )

        is_coalescible = command in COALESCIBLE_COMMANDS
        if is_coalescible:
            flight_key = cache_key
            async def _execute_op() -> dict[str, Any]:
                return await self._dispatch_to_device(
                    websocket, device_id, command, request_payload, current,
                    message_id=message_id, trace_id=trace_id, warning_callback=warning_callback
                )
            raw_result = await self.single_flight.execute(
                flight_key, device_id, _execute_op,
                on_coalesced=lambda: self.metrics.record("single_flight_coalesced", 1.0)
            )
            result = dict(raw_result)
            if trace_id:
                result["trace_id"] = trace_id
        else:
            result = await self._dispatch_to_device(
                websocket, device_id, command, request_payload, current,
                message_id=message_id, trace_id=trace_id, warning_callback=warning_callback
            )

        if result.get("status") == "success":
            if is_cache_eligible:
                self.cache.set(cache_key, result, SAFE_CACHE_COMMAND_TTLS[command])
            elif command in ("workspace.add", "access_root.add"):
                self.cache.invalidate_prefix(f"{device_id}:{current['session_id']}:{current['generation']}:workspace.list")
                self.cache.invalidate_prefix(f"{device_id}:{current['session_id']}:{current['generation']}:project.list")
            elif command == "project.refresh":
                self.cache.invalidate_prefix(f"{device_id}:{current['session_id']}:{current['generation']}:project.list")
            elif command in ("agent.start", "agent.stop"):
                self.cache.invalidate_prefix(f"{device_id}:{current['session_id']}:{current['generation']}:agent.list")

        total_dispatch_ms = (time.perf_counter_ns() - overall_started_ns) / 1_000_000
        self.metrics.record("control_plane_dispatch_ms", total_dispatch_ms)
        return result

    async def _dispatch_to_device(
        self, websocket: WebSocket, device_id: str, command: str, request_payload: dict[str, Any],
        current: dict[str, Any], *, message_id: str | None = None, trace_id: str | None = None,
        warning_callback: Any = None
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        trace_id = trace_id or str(uuid.uuid4())
        started_ns = time.perf_counter_ns()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_responses[request_id] = (device_id, future)
        context = {"request_id": request_id, "session_id": current["session_id"],
            "generation": current["generation"], "trace_id": trace_id,
            "message_id": message_id, "created_at": time.time(), "deadline_at": time.time() + self.policy.request_timeout_seconds,
            "state": "PENDING"}
        self.pending_context[request_id] = context
        self.request_history[request_id] = context
        self.request_history_order.append(request_id)
        while len(self.request_history_order) > 1000:
            self.request_history.pop(self.request_history_order.popleft(), None)
        warning_task = None
        if warning_callback:
            warning_task = asyncio.create_task(self._warn_slow_request(request_id, warning_callback))
        try:
            async with self._device_semaphore(device_id):
                await websocket.send_json({"type": "command", "request_id": request_id, "device_id": device_id,
                    "session_id": current["session_id"], "generation": current["generation"],
                    "trace_id": trace_id, "message_id": message_id, "created_at": time.time(),
                    "deadline_at": time.time() + self.policy.request_timeout_seconds,
                    "command": command, "payload": request_payload, "sent_at": int(time.time())})
                result = await asyncio.wait_for(future, timeout=self.policy.request_timeout_seconds)
            context = self.pending_context[request_id]
            context["state"] = "COMPLETED"
            context["completed_at"] = time.time()
            duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            self.metrics.record("wss_roundtrip", duration_ms)
            runtime_duration = result.get("runtime_processing_ms")
            if isinstance(runtime_duration, (int, float)) and not isinstance(runtime_duration, bool):
                self.metrics.record("runtime_processing", float(runtime_duration))
            result["latency_ms"] = round(duration_ms, 3)
            result["trace_id"] = trace_id
            return result
        except asyncio.TimeoutError as exc:
            if request_id in self.pending_context:
                self.pending_context[request_id]["state"] = "TIMED_OUT"
                self.pending_context[request_id]["completed_at"] = time.time()
            self.metrics.record("control_request_timeout", self.policy.request_timeout_seconds * 1000)
            raise HTTPException(504, f"request timed out after {self.policy.request_timeout_seconds:g} seconds") from exc
        except (WebSocketDisconnect, RuntimeError, HTTPException) as exc:
            if request_id in self.pending_context:
                self.pending_context[request_id]["state"] = "FAILED"
                self.pending_context[request_id]["completed_at"] = time.time()
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(409, "device disconnected") from exc
        finally:
            self.pending_responses.pop(request_id, None)
            self.pending_context.pop(request_id, None)
            if warning_task:
                warning_task.cancel()
                await asyncio.gather(warning_task, return_exceptions=True)

    async def _warn_slow_request(self, request_id: str, callback: Any) -> None:
        await asyncio.sleep(self.policy.request_warning_seconds)
        context = self.pending_context.get(request_id)
        if not context or context["state"] != "PENDING":
            return
        self.metrics.record("control_request_warning", self.policy.request_warning_seconds * 1000)
        result = callback(request_id)
        if asyncio.iscoroutine(result):
            await result

    async def stop_session(self, device_id: str) -> tuple[bool, bool]:
        current = self.store.current_session(device_id)
        if (not current or not current["session_id"]
                or current["session_status"] not in {"CONNECTED", "WAITING_FOR_PAIRING"}):
            return False, False
        session_id, generation = current["session_id"], current["session_generation"]
        websocket = self.sessions.get(device_id)
        was_connected = bool(websocket and self.session_matches(device_id, session_id, generation))
        self.store.invalidate_session(device_id, session_id, generation, "STOPPED")
        self.active_sessions.pop(device_id, None)
        self.emit_status_event(device_id, "STOPPED")
        self.cache.invalidate_session(device_id, session_id, generation)
        self.single_flight.cancel_for_session(device_id, session_id, generation, HTTPException(409, "session stopped"))
        if not was_connected or not websocket:
            return True, False
        request_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_responses[request_id] = (device_id, future)
        self.pending_context[request_id] = {"session_id": session_id, "generation": generation,
            "trace_id": trace_id, "message_id": None, "created_at": time.time(),
            "deadline_at": time.time() + self.policy.request_timeout_seconds, "state": "PENDING"}
        try:
            deadline = asyncio.get_running_loop().time() + self.policy.request_timeout_seconds
            await asyncio.wait_for(websocket.send_json({"type": "command", "request_id": request_id,
                "device_id": device_id, "session_id": session_id, "generation": generation,
                "trace_id": trace_id, "message_id": None, "created_at": time.time(),
                "deadline_at": time.time() + self.policy.request_timeout_seconds,
                "command": "session.stop", "payload": {}, "sent_at": int(time.time())}),
                timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
            await asyncio.wait_for(future,
                timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
            return True, True
        except Exception:
            try:
                await websocket.close(code=4003, reason="session invalidated")
            except Exception:
                pass
            return True, False
        finally:
            self.pending_responses.pop(request_id, None)
            self.pending_context.pop(request_id, None)

    async def revoke_device(self, device_id: str) -> bool:
        revoked = self.store.revoke(device_id)
        self.active_sessions.pop(device_id, None)
        self.cache.invalidate_device(device_id)
        self.single_flight.cancel_for_device(device_id, HTTPException(409, "device revoked"))
        websocket = self.sessions.get(device_id)
        if websocket:
            try:
                await websocket.close(code=4003, reason="device revoked")
            except Exception:
                pass
        return revoked

    async def handle_device_socket(self, websocket: WebSocket) -> None:
        device_id = websocket.headers.get("x-agentbridge-device-id", "")
        authorization = websocket.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        session_id = websocket.headers.get("x-agentbridge-session-id", "")
        try:
            generation = int(websocket.headers.get("x-agentbridge-generation", ""))
        except ValueError:
            generation = -1
        if not self.store.authenticate_device(device_id, token, session_id, generation):
            log.warning("Device authentication rejected")
            await websocket.close(code=4401, reason="unauthorized")
            return
        previous = self.sessions.get(device_id)
        await websocket.accept()
        self.sessions[device_id] = websocket
        self.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
        self.cache.create_session_namespace(device_id, session_id, generation)
        self.last_heartbeat_monotonic[device_id] = time.monotonic()
        for req_id, pending in list(self.pairing.pending.items()):
            if pending.get("session_id") == session_id:
                self.pairing.pending.pop(req_id, None)
        self.store.set_session_status(device_id, session_id, generation, "CONNECTED", connected=True)
        self.connectivity[device_id] = ConnectivityGuard(self.policy)
        self.connectivity[device_id].state = "RECOVERING"
        if previous:
            try:
                await previous.close(code=4000, reason="replaced by a new authenticated session")
            except Exception:
                pass
        self.emit_status_event(device_id, "CONNECTED")
        seen_frames: dict[str, float] = {}
        last_heartbeat_at = time.monotonic()
        try:
            while True:
                try:
                    heartbeat_limit = min(HEARTBEAT_TIMEOUT_SECONDS,
                        self.policy.probe_interval_seconds * self.policy.failure_probe_count * 1.5)
                    heartbeat_remaining = max(0.01, heartbeat_limit - (time.monotonic() - last_heartbeat_at))
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=heartbeat_remaining)
                except asyncio.TimeoutError:
                    log.info("Device heartbeat timed out")
                    self.store.invalidate_session(device_id, session_id, generation, "WAITING_FOR_PAIRING")
                    await websocket.close(code=4001, reason="heartbeat timeout")
                    break
                if len(raw.encode("utf-8")) > 32768:
                    await websocket.close(code=1009, reason="message too large")
                    break
                try:
                    frame = json.loads(raw)
                    if not isinstance(frame, dict):
                        raise ValueError
                    if frame.get("type") == "heartbeat":
                        frame_uuid = uuid.UUID(str(frame.get("frame_id", "")))
                        frame_id = str(frame_uuid)
                        sent_at = int(frame.get("sent_at", 0))
                        if (frame_uuid.version != 4 or frame.get("device_id") != device_id
                                or frame.get("session_id") != session_id or frame.get("generation") != generation
                                or abs(time.time() - sent_at) > FRAME_MAX_AGE_SECONDS or frame_id in seen_frames):
                            raise ValueError
                        seen_frames[frame_id] = time.time()
                        seen_frames = {key: stamp for key, stamp in seen_frames.items() if stamp > time.time() - 120}
                        last_heartbeat_at = time.monotonic()
                        self.last_heartbeat_monotonic[device_id] = last_heartbeat_at
                        self.store.heartbeat(device_id)
                        connectivity = frame.get("connectivity")
                        if isinstance(connectivity, dict):
                            reported_rtt = connectivity.get("rtt_ms")
                            if isinstance(reported_rtt, (int, float)) and not isinstance(reported_rtt, bool) and 0 <= reported_rtt <= 60000:
                                guard = self.connectivity.setdefault(device_id, ConnectivityGuard(self.policy))
                                guard.record_success(float(reported_rtt), time.time())
                        await websocket.send_json({"type": "heartbeat_ack", "frame_id": frame_id,
                            "device_id": device_id, "session_id": session_id, "generation": generation,
                            "sent_at": int(time.time())})
                    elif frame.get("type") == "response":
                        request_uuid = uuid.UUID(str(frame.get("request_id", "")))
                        request_id = str(request_uuid)
                        sent_at = int(frame.get("sent_at", 0))
                        if (request_uuid.version != 4 or frame.get("device_id") != device_id
                                or frame.get("session_id") != session_id or frame.get("generation") != generation
                                or abs(time.time() - sent_at) > FRAME_MAX_AGE_SECONDS):
                            raise ValueError
                        response = self.pending_responses.get(request_id)
                        context = self.pending_context.get(request_id)
                        if (response and context and response[0] == device_id and not response[1].done()
                                and frame.get("trace_id") == context["trace_id"]
                                and context["session_id"] == session_id and context["generation"] == generation
                                and time.time() <= context["deadline_at"]):
                            if frame.get("status") not in ("success", "error") or not isinstance(frame.get("payload"), dict):
                                raise ValueError
                            response[1].set_result(frame)
                    elif frame.get("type") == "event":
                        frame_uuid = uuid.UUID(str(frame.get("frame_id", "")))
                        frame_id = str(frame_uuid)
                        sent_at = int(frame.get("timestamp", 0))
                        if (frame_uuid.version != 4 or frame.get("device_id") != device_id or frame.get("event") != "runtime.status_changed"
                                or frame.get("session_id") != session_id or frame.get("generation") != generation
                                or abs(time.time() - sent_at) > FRAME_MAX_AGE_SECONDS):
                            raise ValueError
                        if frame_id in seen_frames:
                            raise ValueError
                        seen_frames[frame_id] = time.time()
                        self.events.append(frame)
                    else:
                        raise ValueError
                except (ValueError, TypeError, json.JSONDecodeError):
                    log.warning("Malformed or replayed device frame rejected")
                    await websocket.close(code=4400, reason="malformed, stale, or replayed frame")
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if self.sessions.get(device_id) is websocket:
                self.sessions.pop(device_id, None)
                self.active_sessions.pop(device_id, None)
                # Always cancel in-flight requests — they cannot complete on a dead socket.
                # Use session-level cancellation so flights from a future session are untouched.
                self.single_flight.cancel_for_session(
                    device_id, session_id, generation, HTTPException(409, "device disconnected")
                )
                current = self.store.current_session(device_id)
                # Determine whether this disconnect ends the session or is a temporary
                # transport interruption.  Only destroy the session cache when the session
                # is truly gone (stopped, invalidated, expired, heartbeat timeout).
                # A temporary WebSocket drop (status -> DISCONNECTED) preserves the cache
                # so a reconnect with the same valid credential gets cache hits.
                session_truly_ended = (
                    not current
                    or current.get("session_status") in ("STOPPED", "INVALIDATED", "WAITING_FOR_PAIRING")
                )
                if session_truly_ended:
                    self.cache.invalidate_session(device_id, session_id, generation)
                # else: preserve session cache for potential reconnect
                if current and current.get("session_status") not in ("STOPPED", "WAITING_FOR_PAIRING"):
                    self.store.set_session_status(device_id, session_id, generation, "DISCONNECTED", connected=False)
                    self.emit_status_event(device_id, "DISCONNECTED")
                self.connectivity.setdefault(device_id, ConnectivityGuard(self.policy)).disconnected()
                self.last_heartbeat_monotonic.pop(device_id, None)
                for req_id, (dev_id, fut) in list(self.pending_responses.items()):
                    if dev_id == device_id and not fut.done():
                        fut.set_exception(HTTPException(409, "device disconnected"))

    def create_app(self, telegram_bot: Any = None) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            bot_task = asyncio.create_task(telegram_bot.run()) if telegram_bot else None
            try:
                yield
            finally:
                for task in list(self.telegram_tasks):
                    task.cancel()
                if self.telegram_tasks:
                    await asyncio.gather(*self.telegram_tasks, return_exceptions=True)
                if bot_task:
                    bot_task.cancel()
                    await asyncio.gather(bot_task, return_exceptions=True)

        app = FastAPI(title="SŪTRA Control Plane", version="0.1.0",
                      docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

        def require_operator(authorization: str | None) -> None:
            if not self.verify_operator(authorization):
                raise HTTPException(401, "operator authentication required")

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"status": "healthy", "service": "agentbridge-control-plane", "version": __version__,
                    "active_devices": len(self.sessions), "uptime_seconds": int(time.time() - self.started_at)}

        @app.get("/health/live")
        async def health_live() -> dict[str, Any]:
            # Must be extremely cheap: no I/O, no ProjectIndex rebuild, no LLM calls.
            return {"status": "ok", "service": "agentbridge-control-plane", "version": __version__}

        @app.get("/health/ready")
        async def health_ready() -> dict[str, Any]:
            checks = await self._readiness_checks()
            all_ok = all(v == "ok" for v in checks.values())
            status_code = 200 if all_ok else 503
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"status": "ready" if all_ok else "not_ready", "service": "agentbridge-control-plane",
                 "version": __version__, "checks": checks},
                status_code=status_code,
            )

        @app.get("/v1/metrics")
        async def metrics(authorization: str | None = Header(default=None)) -> dict[str, Any]:
            require_operator(authorization)
            return {
                "latency": self.metrics.summary(),
                "counters": self.metrics.to_flat_dict(),
                "connectivity": {device_id: guard.snapshot()
                                 for device_id, guard in self.connectivity.items()},
                "requests": list(self.request_history.values())[-100:],
            }

        @app.post("/v1/telegram/webhook")
        async def telegram_webhook(request: Request,
                x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, bool]:
            if not telegram_bot or not telegram_bot.webhook_secret:
                raise HTTPException(404, "Telegram webhook is not configured")
            if (not x_telegram_bot_api_secret_token
                    or not hmac.compare_digest(telegram_bot.webhook_secret, x_telegram_bot_api_secret_token)):
                raise HTTPException(401, "Telegram webhook authentication failed")
            try:
                update = await request.json()
            except ValueError as exc:
                raise HTTPException(400, "invalid Telegram update") from exc
            if not isinstance(update, dict):
                raise HTTPException(400, "invalid Telegram update")
            task = asyncio.create_task(telegram_bot.handle_webhook_update(update))
            self.telegram_tasks.add(task)
            task.add_done_callback(self.telegram_tasks.discard)
            return {"ok": True}

        @app.post("/v1/pairing/requests")
        async def create_pairing_request(body: PairRequest, request: Request) -> dict[str, Any]:
            poll_hash = body.poll_secret_hash
            if body.device_id and body.device_id in self.sessions:
                raise HTTPException(409, "device is already connected")
            return self.pairing.create(body, poll_hash, request.client.host if request.client else "unknown")

        @app.post("/v1/pairing/approve")
        async def approve_pairing(body: PairApproval, authorization: str | None = Header(default=None)) -> dict[str, str]:
            require_operator(authorization)
            request_id = self.pairing.approve(body.code)
            return {"status": "approved", "request_id": request_id}

        @app.get("/v1/pairing/requests/{request_id}/complete")
        async def complete_pairing(request_id: str, authorization: str | None = Header(default=None)) -> Any:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(401, "pairing authentication required")
            status, result = self.pairing.complete(request_id, authorization[7:])
            from fastapi.responses import JSONResponse
            return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})

        @app.get("/v1/devices")
        async def list_devices(authorization: str | None = Header(default=None)) -> Any:
            require_operator(authorization)
            return {"devices": self.store.list_devices(set(self.sessions))}

        @app.post("/v1/devices/{device_id}/revoke")
        async def revoke_device(device_id: str, authorization: str | None = Header(default=None)) -> Any:
            require_operator(authorization)
            if not await self.revoke_device(device_id):
                raise HTTPException(404, "device not found or already revoked")
            return {"device_id": device_id, "connection_status": "REVOKED"}

        @app.post("/v1/devices/{device_id}/commands")
        async def send_command(device_id: str, body: CommandRequest,
                               authorization: str | None = Header(default=None)) -> Any:
            require_operator(authorization)
            return await self.dispatch_command(device_id, body.command, body.payload)

        @app.websocket("/v1/devices/connect")
        async def device_socket(websocket: WebSocket) -> None:
            await self.handle_device_socket(websocket)

        return app
