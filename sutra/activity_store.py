"""Local Activity & Operational History Store backed by SQLite.

Provides a thread-safe, resilient audit and operational timeline for SŪTRA,
capturing Telegram messages, prompts, operations, permission decisions,
tool executions, connector events, audit events, and artifact metadata.

Core Architectural Invariants:
1. Reuses existing ControlPlaneStore SQLite database connection (shares WAL mode,
   zero duplicate database files).
2. Centralized Secret Redaction: secrets are scrubbed before reaching disk.
3. Failure Isolation: History writes are non-critical; database errors log warnings
   and update metrics, but NEVER crash agent workflows or Telegram.
4. Universal Correlation: All related lifecycle records are bound by request_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from .metrics import Metrics, get_metrics
from .redaction import redact_secrets

log = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1


def _redact_str(val: Optional[str]) -> Optional[str]:
    """Sanitize arbitrary string using centralized redaction."""
    if val is None:
        return None
    return redact_secrets(str(val))


def _redact_json(val: Any) -> Optional[str]:
    """Deterministically serialize and redact JSON structures."""
    if val is None:
        return None
    try:
        raw_json = json.dumps(val, sort_keys=True, default=str)
    except Exception:
        raw_json = str(val)
    return redact_secrets(raw_json)


class ActivityStore:
    """Thread-safe SQLite activity store for SŪTRA operational history.

    Shares the same database connection and RLock as ControlPlaneStore to maintain
    a single SQLite WAL file. Can also be instantiated independently via `from_path`
    for testing or CLI usage.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        lock: RLock,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self._db = db
        self._lock = lock
        try:
            self._db.row_factory = sqlite3.Row
        except Exception:
            pass
        self.metrics = metrics or get_metrics()
        self.enabled: bool = os.environ.get("HISTORY_ENABLED", "true").lower() in ("true", "1", "yes")
        self.retention_days: int = int(os.environ.get("HISTORY_RETENTION_DAYS", "90"))
        self._migrate()

    @property
    def schema_version(self) -> int:
        """Current schema version recorded in schema_meta."""
        with self._lock:
            try:
                row = self._db.execute(
                    "SELECT value FROM schema_meta WHERE key='activity_schema_version'"
                ).fetchone()
                return int(row[0]) if row else 0
            except Exception:
                return 0

    def close(self) -> None:
        """Close the underlying SQLite connection if not already closed."""
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    def _apply_migrations(self) -> None:
        """Idempotently re-apply migrations."""
        self._migrate()

    def record_operation_start(self, *args, **kwargs) -> Optional[str]:
        return self.record_operation(*args, **kwargs)

    def record_operation_complete(self, *args, **kwargs) -> bool:
        return self.update_operation(*args, **kwargs)

    def record_prompt_start(self, *args, **kwargs) -> Optional[str]:
        return self.record_prompt(*args, **kwargs)

    def record_prompt_complete(self, *args, **kwargs) -> bool:
        return self.update_prompt(*args, **kwargs)

    def record_tool_call_start(self, *args, **kwargs) -> Optional[str]:
        return self.record_tool_call(*args, **kwargs)

    def record_tool_call_complete(self, *args, **kwargs) -> bool:
        return self.update_tool_call(*args, **kwargs)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_path(cls, path: str | Path, metrics: Optional[Metrics] = None) -> "ActivityStore":
        """Open a standalone SQLite connection (for tests / CLI)."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return cls(db, RLock(), metrics=metrics)

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            self._db.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
            row = self._db.execute(
                "SELECT value FROM schema_meta WHERE key='activity_schema_version'"
            ).fetchone()
            current_version = int(row[0]) if row else 0

            if current_version < 1:
                self._apply_v1()
                self._db.execute(
                    "INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)",
                    ("activity_schema_version", str(SCHEMA_VERSION)),
                )
            self._db.commit()

    def _apply_v1(self) -> None:
        """Apply initial schema version 1 (non-destructive)."""
        self._db.executescript("""
            -- 1. Telegram Messages
            CREATE TABLE IF NOT EXISTS telegram_messages (
                id                TEXT PRIMARY KEY,
                request_id        TEXT NOT NULL,
                chat_id           TEXT NOT NULL,
                telegram_user_id  TEXT,
                message_id        INTEGER,
                direction         TEXT NOT NULL,
                message_type      TEXT NOT NULL,
                text              TEXT,
                project_id        TEXT,
                session_id        TEXT,
                created_at        REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tg_msgs_request ON telegram_messages (request_id);
            CREATE INDEX IF NOT EXISTS idx_tg_msgs_chat_created ON telegram_messages (chat_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tg_msgs_session ON telegram_messages (session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tg_msgs_created ON telegram_messages (created_at DESC);

            -- 2. Prompts
            CREATE TABLE IF NOT EXISTS prompts (
                id            TEXT PRIMARY KEY,
                request_id    TEXT NOT NULL,
                project_id    TEXT,
                session_id    TEXT,
                agent_id      TEXT,
                source        TEXT NOT NULL,
                prompt_text   TEXT NOT NULL,
                status        TEXT NOT NULL,
                created_at    REAL NOT NULL,
                completed_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_prompts_request ON prompts (request_id);
            CREATE INDEX IF NOT EXISTS idx_prompts_project_created ON prompts (project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts (session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_prompts_created ON prompts (created_at DESC);

            -- 3. Operations
            CREATE TABLE IF NOT EXISTS operations (
                id              TEXT PRIMARY KEY,
                request_id      TEXT NOT NULL,
                operation_type  TEXT NOT NULL,
                source          TEXT NOT NULL,
                project_id      TEXT,
                session_id      TEXT,
                connector_id    TEXT,
                status          TEXT NOT NULL,
                started_at      REAL NOT NULL,
                completed_at    REAL,
                duration_ms     REAL,
                error_code      TEXT,
                error_message   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ops_request ON operations (request_id);
            CREATE INDEX IF NOT EXISTS idx_ops_project_created ON operations (project_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ops_session ON operations (session_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ops_status ON operations (status);
            CREATE INDEX IF NOT EXISTS idx_ops_started ON operations (started_at DESC);

            -- 4. Permission Events
            CREATE TABLE IF NOT EXISTS permission_events (
                id            TEXT PRIMARY KEY,
                request_id    TEXT NOT NULL,
                operation_id  TEXT,
                risk_level    TEXT NOT NULL,
                decision      TEXT NOT NULL,
                reason        TEXT,
                actor         TEXT NOT NULL,
                created_at    REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_perm_events_request ON permission_events (request_id);
            CREATE INDEX IF NOT EXISTS idx_perm_events_created ON permission_events (created_at DESC);

            -- 5. Connector Events
            CREATE TABLE IF NOT EXISTS connector_events (
                id            TEXT PRIMARY KEY,
                request_id    TEXT,
                connector_id  TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                status        TEXT NOT NULL,
                metadata      TEXT,
                created_at    REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conn_events_request ON connector_events (request_id);
            CREATE INDEX IF NOT EXISTS idx_conn_events_connector ON connector_events (connector_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conn_events_created ON connector_events (created_at DESC);

            -- 6. Tool Calls
            CREATE TABLE IF NOT EXISTS tool_calls (
                id                   TEXT PRIMARY KEY,
                request_id           TEXT NOT NULL,
                connector_id         TEXT,
                server_name          TEXT,
                tool_name            TEXT NOT NULL,
                arguments_redacted   TEXT,
                result_redacted      TEXT,
                status               TEXT NOT NULL,
                risk_level           TEXT,
                permission_decision  TEXT,
                started_at           REAL NOT NULL,
                completed_at         REAL,
                duration_ms          REAL,
                error_code           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tool_calls_request ON tool_calls (request_id);
            CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_created ON tool_calls (tool_name, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tool_calls_started ON tool_calls (started_at DESC);

            -- 7. Audit Events
            CREATE TABLE IF NOT EXISTS audit_events (
                id          TEXT PRIMARY KEY,
                request_id  TEXT,
                event_type  TEXT NOT NULL,
                action      TEXT,
                actor       TEXT NOT NULL,
                target      TEXT,
                details     TEXT,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_request ON audit_events (request_id);
            CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events (created_at DESC);

            -- 8. Artifact Metadata
            CREATE TABLE IF NOT EXISTS artifact_metadata (
                id               TEXT PRIMARY KEY,
                request_id       TEXT,
                project_id       TEXT,
                artifact_name    TEXT,
                artifact_type    TEXT NOT NULL,
                storage_uri      TEXT,
                file_path        TEXT NOT NULL,
                size_bytes       INTEGER,
                file_size_bytes  INTEGER,
                sha256           TEXT,
                metadata         TEXT,
                created_at       REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_request ON artifact_metadata (request_id);
            CREATE INDEX IF NOT EXISTS idx_artifact_project ON artifact_metadata (project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_artifact_created ON artifact_metadata (created_at DESC);
        """)

    # ------------------------------------------------------------------
    # Writers with Defensive Failure Isolation
    # ------------------------------------------------------------------

    def record_telegram_message(
        self,
        request_id: str,
        chat_id: Any,
        direction: str,
        message_type: str,
        text: Optional[str] = None,
        telegram_user_id: Optional[Any] = None,
        message_id: Optional[int] = None,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        created_at: Optional[float] = None,
        record_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record an inbound or outbound Telegram message with sanitized text."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = created_at or time.time()
        redacted_text = _redact_str(text)
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO telegram_messages
                    (id, request_id, chat_id, telegram_user_id, message_id, direction, message_type, text, project_id, session_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rec_id, request_id, str(chat_id), str(telegram_user_id) if telegram_user_id is not None else None,
                        message_id, direction, message_type, redacted_text, project_id, session_id, ts,
                    ),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=telegram_messages error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def record_prompt(
        self,
        request_id: str,
        prompt_text: str,
        source: str = "telegram",
        status: str = "pending",
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        created_at: Optional[float] = None,
        record_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record an incoming prompt with sanitized body."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = created_at or time.time()
        redacted = _redact_str(prompt_text) or ""
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO prompts
                    (id, request_id, project_id, session_id, agent_id, source, prompt_text, status, created_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (rec_id, request_id, project_id, session_id, agent_id, source, redacted, status, ts),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=prompts error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def update_prompt(
        self,
        prompt_id: str,
        status: str,
        completed_at: Optional[float] = None,
    ) -> bool:
        """Update completion status of a prompt."""
        if not self.enabled:
            return False
        ts = completed_at or time.time()
        try:
            with self._lock:
                cur = self._db.execute(
                    "UPDATE prompts SET status=?, completed_at=? WHERE id=?",
                    (status, ts, prompt_id),
                )
                self._db.commit()
                return cur.rowcount > 0
        except Exception as exc:
            log.warning("activity_store_write_failed table=prompts error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return False

    def record_operation(
        self,
        request_id: str,
        operation_type: str,
        source: str = "telegram",
        status: str = "pending",
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        connector_id: Optional[str] = None,
        started_at: Optional[float] = None,
        record_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record the initiation of an operation."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = started_at or time.time()
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO operations
                    (id, request_id, operation_type, source, project_id, session_id, connector_id,
                     status, started_at, completed_at, duration_ms, error_code, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)""",
                    (rec_id, request_id, operation_type, source, project_id, session_id, connector_id, status, ts),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=operations error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def update_operation(
        self,
        operation_id: str,
        status: str,
        completed_at: Optional[float] = None,
        duration_ms: Optional[float] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update completion status, latency, and error details of an operation."""
        if not self.enabled:
            return False
        ts = completed_at or time.time()
        redacted_err = _redact_str(error_message)
        try:
            with self._lock:
                cur = self._db.execute(
                    """UPDATE operations
                    SET status=?, completed_at=?, duration_ms=?, error_code=?, error_message=?
                    WHERE id=?""",
                    (status, ts, duration_ms, error_code, redacted_err, operation_id),
                )
                self._db.commit()
                return cur.rowcount > 0
        except Exception as exc:
            log.warning("activity_store_write_failed table=operations error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return False

    def record_permission_event(
        self,
        request_id: str,
        risk_level: str,
        decision: str,
        actor: str = "system",
        operation_id: Optional[str] = None,
        reason: Optional[str] = None,
        created_at: Optional[float] = None,
        record_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record an evaluated permission decision with sanitized reasoning."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = created_at or time.time()
        redacted_reason = _redact_str(reason)
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO permission_events
                    (id, request_id, operation_id, risk_level, decision, reason, actor, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rec_id, request_id, operation_id, risk_level, decision, redacted_reason, actor, ts),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=permission_events error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def record_connector_event(
        self,
        connector_id: str,
        event_type: str,
        status: str,
        request_id: Optional[str] = None,
        metadata: Any = None,
        created_at: Optional[float] = None,
        record_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record a connector lifecycle event with sanitized metadata."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = created_at or time.time()
        meta_str = _redact_json(metadata)
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO connector_events
                    (id, request_id, connector_id, event_type, status, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (rec_id, request_id, connector_id, event_type, status, meta_str, ts),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=connector_events error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def record_tool_call(
        self,
        request_id: str,
        tool_name: str,
        status: str = "started",
        connector_id: Optional[str] = None,
        server_name: Optional[str] = None,
        arguments: Any = None,
        result: Any = None,
        risk_level: Optional[str] = None,
        permission_decision: Optional[str] = None,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        duration_ms: Optional[float] = None,
        error_code: Optional[str] = None,
        record_id: Optional[str] = None,
    ) -> Optional[str]:
        """Record an agent or connector tool execution with scrubbed args and results."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = started_at or time.time()
        redacted_args = _redact_json(arguments)
        redacted_res = _redact_json(result) if result is not None else None
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO tool_calls
                    (id, request_id, connector_id, server_name, tool_name, arguments_redacted, result_redacted,
                     status, risk_level, permission_decision, started_at, completed_at, duration_ms, error_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rec_id, request_id, connector_id, server_name, tool_name, redacted_args, redacted_res,
                        status, risk_level, permission_decision, ts, completed_at, duration_ms, error_code,
                    ),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=tool_calls error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def update_tool_call(
        self,
        tool_call_id: str,
        status: str,
        result: Any = None,
        completed_at: Optional[float] = None,
        duration_ms: Optional[float] = None,
        error_code: Optional[str] = None,
    ) -> bool:
        """Update completion status, latency, and result of a tool call."""
        if not self.enabled:
            return False
        ts = completed_at or time.time()
        redacted_res = _redact_json(result) if result is not None else None
        try:
            with self._lock:
                cur = self._db.execute(
                    """UPDATE tool_calls
                    SET status=?, result_redacted=COALESCE(?, result_redacted),
                        completed_at=?, duration_ms=?, error_code=?
                    WHERE id=?""",
                    (status, redacted_res, ts, duration_ms, error_code, tool_call_id),
                )
                self._db.commit()
                return cur.rowcount > 0
        except Exception as exc:
            log.warning("activity_store_write_failed table=tool_calls error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return False

    def record_audit_event(
        self,
        event_type: Optional[str] = None,
        actor: str = "system",
        request_id: Optional[str] = None,
        target: Optional[str] = None,
        details: Any = None,
        created_at: Optional[float] = None,
        record_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> Optional[str]:
        """Record an operational audit event with sanitized details."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = created_at or time.time()
        evt_type = action or event_type or "audit"
        act_name = action or event_type
        det_str = _redact_json(details)
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO audit_events
                    (id, request_id, event_type, action, actor, target, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rec_id, request_id, evt_type, act_name, actor, target, det_str, ts),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=audit_events error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    def record_artifact_metadata(
        self,
        artifact_type: str = "artifact",
        file_path: Optional[str] = None,
        request_id: Optional[str] = None,
        project_id: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        created_at: Optional[float] = None,
        record_id: Optional[str] = None,
        artifact_name: Optional[str] = None,
        storage_uri: Optional[str] = None,
        size_bytes: Optional[int] = None,
        metadata: Any = None,
    ) -> Optional[str]:
        """Record metadata for an external file artifact (never stores raw binary in DB)."""
        if not self.enabled:
            return None
        rec_id = record_id or str(uuid.uuid4())
        ts = created_at or time.time()
        raw_path = storage_uri or file_path or artifact_name or "artifact"
        clean_path = _redact_str(raw_path) or ""
        clean_name = artifact_name or Path(clean_path).name
        clean_uri = storage_uri or clean_path
        clean_size = size_bytes if size_bytes is not None else file_size_bytes
        meta_str = _redact_json(metadata) if metadata is not None else None
        try:
            with self._lock:
                self._db.execute(
                    """INSERT INTO artifact_metadata
                    (id, request_id, project_id, artifact_name, artifact_type, storage_uri, file_path, size_bytes, file_size_bytes, sha256, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rec_id, request_id, project_id, clean_name, artifact_type, clean_uri, clean_path, clean_size, clean_size, sha256, meta_str, ts),
                )
                self._db.commit()
            self.metrics.record("activity_store_write_ok", 1)
            return rec_id
        except Exception as exc:
            log.warning("activity_store_write_failed table=artifact_metadata error=%s", exc)
            self.metrics.record("activity_store_write_error", 1)
            return None

    # ------------------------------------------------------------------
    # Bounded Query API (Strictly Paginated)
    # ------------------------------------------------------------------

    def get_timeline(self, request_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """Retrieve complete correlated timeline across all entities for a request_id."""
        with self._lock:
            tool_calls = []
            for r in self._db.execute("SELECT * FROM tool_calls WHERE request_id=? ORDER BY started_at ASC", (request_id,)):
                d = dict(r)
                if d.get("arguments_redacted"):
                    try:
                        d["arguments"] = json.loads(d["arguments_redacted"])
                    except Exception:
                        d["arguments"] = d["arguments_redacted"]
                else:
                    d["arguments"] = None
                if d.get("result_redacted"):
                    try:
                        d["result"] = json.loads(d["result_redacted"])
                    except Exception:
                        d["result"] = d["result_redacted"]
                else:
                    d["result"] = None
                tool_calls.append(d)

            audit_events = []
            for r in self._db.execute("SELECT * FROM audit_events WHERE request_id=? ORDER BY created_at ASC", (request_id,)):
                d = dict(r)
                d["action"] = d.get("action") or d.get("event_type")
                if d.get("details"):
                    try:
                        d["details"] = json.loads(d["details"])
                    except Exception:
                        pass
                audit_events.append(d)

            return {
                "request_id": request_id,
                "telegram_messages": [
                    dict(r) for r in self._db.execute(
                        "SELECT * FROM telegram_messages WHERE request_id=? ORDER BY created_at ASC", (request_id,)
                    )
                ],
                "prompts": [
                    dict(r) for r in self._db.execute(
                        "SELECT * FROM prompts WHERE request_id=? ORDER BY created_at ASC", (request_id,)
                    )
                ],
                "operations": [
                    dict(r) for r in self._db.execute(
                        "SELECT * FROM operations WHERE request_id=? ORDER BY started_at ASC", (request_id,)
                    )
                ],
                "permission_events": [
                    dict(r) for r in self._db.execute(
                        "SELECT * FROM permission_events WHERE request_id=? ORDER BY created_at ASC", (request_id,)
                    )
                ],
                "tool_calls": tool_calls,
                "connector_events": [
                    dict(r) for r in self._db.execute(
                        "SELECT * FROM connector_events WHERE request_id=? ORDER BY created_at ASC", (request_id,)
                    )
                ],
                "audit_events": audit_events,
                "artifacts": [
                    dict(r) for r in self._db.execute(
                        "SELECT * FROM artifact_metadata WHERE request_id=? ORDER BY created_at ASC", (request_id,)
                    )
                ],
            }

    def list_operations(
        self,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded operations with optional project, session, and status filters."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM operations {where} ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            return [dict(r) for r in self._db.execute(query, params)]

    def list_telegram_messages(
        self,
        chat_id: Optional[Any] = None,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded Telegram messages."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if chat_id is not None:
            clauses.append("chat_id=?")
            params.append(str(chat_id))
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM telegram_messages {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            return [dict(r) for r in self._db.execute(query, params)]

    def list_prompts(
        self,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded prompts."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM prompts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            return [dict(r) for r in self._db.execute(query, params)]

    def list_tool_calls(
        self,
        request_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded tool executions."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)
        if tool_name is not None:
            clauses.append("tool_name=?")
            params.append(tool_name)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM tool_calls {where} ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            rows = []
            for r in self._db.execute(query, params):
                d = dict(r)
                if d.get("arguments_redacted"):
                    try:
                        d["arguments"] = json.loads(d["arguments_redacted"])
                    except Exception:
                        d["arguments"] = d["arguments_redacted"]
                else:
                    d["arguments"] = None
                if d.get("result_redacted"):
                    try:
                        d["result"] = json.loads(d["result_redacted"])
                    except Exception:
                        d["result"] = d["result_redacted"]
                else:
                    d["result"] = None
                rows.append(d)
            return rows

    def list_permission_events(
        self,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded permission events."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM permission_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            return [dict(r) for r in self._db.execute(query, params)]

    def list_connector_events(
        self,
        connector_id: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded connector events."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if connector_id is not None:
            clauses.append("connector_id=?")
            params.append(connector_id)
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM connector_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            rows = []
            for r in self._db.execute(query, params):
                d = dict(r)
                if d.get("metadata"):
                    try:
                        d["metadata"] = json.loads(d["metadata"])
                    except Exception:
                        pass
                rows.append(d)
            return rows

    # Parity alias
    query_connector_events = list_connector_events

    def list_audit_events(
        self,
        request_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List bounded audit events."""
        bounded_limit = min(max(1, limit), 200)
        clauses = []
        params: list[Any] = []
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)
        if event_type is not None:
            clauses.append("(event_type=? OR action=?)")
            params.extend([event_type, event_type])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM audit_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([bounded_limit, offset])

        with self._lock:
            rows = []
            for r in self._db.execute(query, params):
                d = dict(r)
                d["action"] = d.get("action") or d.get("event_type")
                if d.get("details"):
                    try:
                        d["details"] = json.loads(d["details"])
                    except Exception:
                        pass
                rows.append(d)
            return rows

    # ------------------------------------------------------------------
    # Retention Management
    # ------------------------------------------------------------------

    def purge_retention(self, retention_days: Optional[int] = None) -> int:
        """Purge records across all 8 tables older than retention cutoff.

        Returns total number of deleted rows.
        """
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = time.time() - (days * 86400.0)
        total_deleted = 0

        with self._lock:
            try:
                for table, ts_col in [
                    ("telegram_messages", "created_at"),
                    ("prompts", "created_at"),
                    ("operations", "started_at"),
                    ("permission_events", "created_at"),
                    ("connector_events", "created_at"),
                    ("tool_calls", "started_at"),
                    ("audit_events", "created_at"),
                    ("artifact_metadata", "created_at"),
                ]:
                    cur = self._db.execute(
                        f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)
                    )
                    total_deleted += cur.rowcount
                self._db.commit()
                self.metrics.record("activity_store_purge", total_deleted)
                log.info("activity_store_purge_completed cutoff=%.1f deleted=%d", cutoff, total_deleted)
            except Exception as exc:
                log.warning("activity_store_purge_failed error=%s", exc)

        return total_deleted


# ---------------------------------------------------------------------------
# Activity Context (Correlation Engine)
# ---------------------------------------------------------------------------

@dataclass
class ActivityContext:
    """Carries causality and correlation metadata across SŪTRA operations."""

    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    telegram_user_id: Optional[str] = None
    chat_id: Optional[str] = None
    agent_id: Optional[str] = None
    store: Optional[ActivityStore] = None

    def record_inbound_message(
        self,
        text: str,
        message_id: Optional[int] = None,
        message_type: str = "text",
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_telegram_message(
            request_id=self.request_id,
            chat_id=self.chat_id or "unknown",
            direction="inbound",
            message_type=message_type,
            text=text,
            telegram_user_id=self.telegram_user_id,
            message_id=message_id,
            project_id=self.project_id,
            session_id=self.session_id,
        )

    def record_outbound_message(
        self,
        text: str,
        message_id: Optional[int] = None,
        message_type: str = "text",
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_telegram_message(
            request_id=self.request_id,
            chat_id=self.chat_id or "unknown",
            direction="outbound",
            message_type=message_type,
            text=text,
            telegram_user_id=self.telegram_user_id,
            message_id=message_id,
            project_id=self.project_id,
            session_id=self.session_id,
        )

    def record_prompt(
        self,
        prompt_text: str,
        source: str = "telegram",
        status: str = "pending",
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_prompt(
            request_id=self.request_id,
            prompt_text=prompt_text,
            source=source,
            status=status,
            project_id=self.project_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
        )

    def record_permission_event(
        self,
        risk_level: str,
        decision: str,
        reason: Optional[str] = None,
        actor: str = "system",
        operation_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_permission_event(
            request_id=self.request_id,
            risk_level=risk_level,
            decision=decision,
            reason=reason,
            actor=actor,
            operation_id=operation_id,
        )

    def record_tool_call(
        self,
        tool_name: str,
        status: str = "started",
        connector_id: Optional[str] = None,
        server_name: Optional[str] = None,
        arguments: Any = None,
        result: Any = None,
        risk_level: Optional[str] = None,
        permission_decision: Optional[str] = None,
        duration_ms: Optional[float] = None,
        error_code: Optional[str] = None,
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_tool_call(
            request_id=self.request_id,
            tool_name=tool_name,
            status=status,
            connector_id=connector_id,
            server_name=server_name,
            arguments=arguments,
            result=result,
            risk_level=risk_level,
            permission_decision=permission_decision,
            duration_ms=duration_ms,
            error_code=error_code,
        )

    def record_connector_event(
        self,
        connector_id: str,
        event_type: str,
        status: str,
        metadata: Any = None,
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_connector_event(
            connector_id=connector_id,
            event_type=event_type,
            status=status,
            request_id=self.request_id,
            metadata=metadata,
        )

    def record_audit_event(
        self,
        event_type: str,
        actor: str = "system",
        target: Optional[str] = None,
        details: Any = None,
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_audit_event(
            event_type=event_type,
            actor=actor,
            request_id=self.request_id,
            target=target,
            details=details,
        )

    def record_artifact_metadata(
        self,
        artifact_type: str,
        file_path: str,
        file_size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
    ) -> Optional[str]:
        if not self.store:
            return None
        return self.store.record_artifact_metadata(
            artifact_type=artifact_type,
            file_path=file_path,
            request_id=self.request_id,
            project_id=self.project_id,
            file_size_bytes=file_size_bytes,
            sha256=sha256,
        )

    @asynccontextmanager
    async def operation(
        self,
        operation_type: str,
        source: str = "telegram",
        connector_id: Optional[str] = None,
    ) -> AsyncIterator[Optional[str]]:
        """Asynchronous context manager tracking an operation and measuring its latency."""
        start_ts = time.time()
        start_ns = time.perf_counter_ns()
        op_id = None
        if self.store:
            op_id = self.store.record_operation(
                request_id=self.request_id,
                operation_type=operation_type,
                source=source,
                status="running",
                project_id=self.project_id,
                session_id=self.session_id,
                connector_id=connector_id,
                started_at=start_ts,
            )
        try:
            yield op_id
            duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            if self.store and op_id:
                self.store.update_operation(
                    operation_id=op_id,
                    status="completed",
                    completed_at=time.time(),
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            if self.store and op_id:
                self.store.update_operation(
                    operation_id=op_id,
                    status="failed",
                    completed_at=time.time(),
                    duration_ms=duration_ms,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
            raise
