"""Persistent permission policy storage backed by the existing SQLite infrastructure.

Extends the ControlPlaneStore database with two new tables:
  - permission_policies: global and project-scoped persistent allow/ask/deny rules
  - policy_audit_log:    immutable audit trail for every policy mutation

Schema is versioned via a ``schema_meta`` table; migrations run
automatically on first open and are non-destructive to existing tables.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any

log = logging.getLogger(__name__)

# Increment when schema changes require a migration.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enums (policy-layer vocabulary, independent of PermissionRiskLevel)
# ---------------------------------------------------------------------------

class PolicyScope(str, Enum):
    """The scope at which a policy applies."""
    GLOBAL = "GLOBAL"
    WORKSPACE = "WORKSPACE"   # keyed by canonical workspace/project path


class PolicyDefaultDecision(str, Enum):
    """The persistent default decision for a matched action pattern."""
    AUTO_ALLOW = "AUTO_ALLOW"   # system may execute without asking
    ASK = "ASK"                 # always prompt the user
    DENY = "DENY"               # always reject without asking


class PolicyAuditEventType(str, Enum):
    POLICY_CREATED = "POLICY_CREATED"
    POLICY_UPDATED = "POLICY_UPDATED"
    POLICY_DELETED = "POLICY_DELETED"
    POLICY_RESET   = "POLICY_RESET"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PermissionPolicy:
    """A persistent permission policy record."""
    id: str
    scope_type: str          # PolicyScope value
    scope_id: str            # "" for GLOBAL, workspace path for WORKSPACE
    action_type: str         # e.g. "file_read", "shell_execution", or "*"
    risk_level: str          # PermissionRiskLevel value ("LOW", "MEDIUM", …)
    decision: str            # PolicyDefaultDecision value
    enabled: bool
    created_at: float
    updated_at: float
    description: str = ""


@dataclass
class PolicyAuditRecord:
    id: str
    event_type: str          # PolicyAuditEventType value
    policy_id: str
    scope_type: str
    scope_id: str
    action_type: str
    risk_level: str
    actor: str               # user/device ID or "system"
    timestamp: float
    old_state: str           # JSON snapshot of previous policy (or "")
    new_state: str           # JSON snapshot of new policy (or "")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class PolicyStore:
    """Thread-safe, versioned SQLite store for persistent permission policies.

    Designed to share the *same database file* as ControlPlaneStore so there
    is only one SQLite file to back up.  Call ``PolicyStore(db_connection,
    lock)`` with the existing connection and RLock from ControlPlaneStore, OR
    call ``PolicyStore.from_path(path)`` to open a standalone connection (for
    tests and CLI use).
    """

    def __init__(self, db: sqlite3.Connection, lock: RLock) -> None:
        self._db = db
        self._lock = lock
        self._migrate()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_path(cls, path: str | Path) -> "PolicyStore":
        """Open a standalone SQLite connection (for tests / CLI)."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return cls(db, RLock())

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            self._db.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
            row = self._db.execute(
                "SELECT value FROM schema_meta WHERE key='policy_schema_version'"
            ).fetchone()
            current_version = int(row[0]) if row else 0

            if current_version < 1:
                self._apply_v1()
                self._db.execute(
                    "INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)",
                    ("policy_schema_version", str(SCHEMA_VERSION)),
                )
            self._db.commit()

    def _apply_v1(self) -> None:
        """Apply schema version 1 — non-destructive: only CREATE IF NOT EXISTS."""
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS permission_policies (
                id          TEXT PRIMARY KEY,
                scope_type  TEXT NOT NULL,
                scope_id    TEXT NOT NULL DEFAULT '',
                action_type TEXT NOT NULL,
                risk_level  TEXT NOT NULL,
                decision    TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                description TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_policies_scope
                ON permission_policies (scope_type, scope_id, action_type);

            CREATE TABLE IF NOT EXISTS policy_audit_log (
                id          TEXT PRIMARY KEY,
                event_type  TEXT NOT NULL,
                policy_id   TEXT NOT NULL,
                scope_type  TEXT NOT NULL,
                scope_id    TEXT NOT NULL DEFAULT '',
                action_type TEXT NOT NULL,
                risk_level  TEXT NOT NULL,
                actor       TEXT NOT NULL DEFAULT 'system',
                timestamp   REAL NOT NULL,
                old_state   TEXT NOT NULL DEFAULT '',
                new_state   TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON policy_audit_log (timestamp DESC);
        """)

    # ------------------------------------------------------------------
    # CRUD — Policies
    # ------------------------------------------------------------------

    def create_policy(
        self,
        scope_type: PolicyScope,
        scope_id: str,
        action_type: str,
        risk_level: str,
        decision: PolicyDefaultDecision,
        *,
        enabled: bool = True,
        description: str = "",
        actor: str = "system",
    ) -> PermissionPolicy:
        """Create a new persistent policy.  Raises ``ValueError`` for invalid inputs."""
        self._validate_inputs(scope_type, action_type, risk_level, decision)
        now = time.time()
        policy = PermissionPolicy(
            id=str(uuid.uuid4()),
            scope_type=scope_type.value,
            scope_id=scope_id,
            action_type=action_type,
            risk_level=risk_level,
            decision=decision.value,
            enabled=enabled,
            description=description,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._db.execute(
                """INSERT INTO permission_policies
                   (id,scope_type,scope_id,action_type,risk_level,decision,
                    enabled,description,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (policy.id, policy.scope_type, policy.scope_id, policy.action_type,
                 policy.risk_level, policy.decision, int(policy.enabled),
                 policy.description, policy.created_at, policy.updated_at),
            )
            self._write_audit(
                event_type=PolicyAuditEventType.POLICY_CREATED,
                policy=policy, actor=actor, old_state="",
            )
            self._db.commit()
        log.info("Policy created: id=%s scope=%s/%s action=%s decision=%s",
                 policy.id, policy.scope_type, policy.scope_id,
                 policy.action_type, policy.decision)
        return policy

    def get_policy(self, policy_id: str) -> PermissionPolicy | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM permission_policies WHERE id=?", (policy_id,)
            ).fetchone()
        return self._row_to_policy(row) if row else None

    def list_policies(
        self,
        scope_type: PolicyScope | None = None,
        scope_id: str | None = None,
        action_type: str | None = None,
        enabled_only: bool = False,
    ) -> list[PermissionPolicy]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_type is not None:
            clauses.append("scope_type=?"); params.append(scope_type.value)
        if scope_id is not None:
            clauses.append("scope_id=?"); params.append(scope_id)
        if action_type is not None:
            clauses.append("action_type=?"); params.append(action_type)
        if enabled_only:
            clauses.append("enabled=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM permission_policies {where} ORDER BY scope_type, scope_id, action_type",
                params,
            ).fetchall()
        return [self._row_to_policy(r) for r in rows]

    def update_policy(
        self,
        policy_id: str,
        *,
        decision: PolicyDefaultDecision | None = None,
        enabled: bool | None = None,
        description: str | None = None,
        actor: str = "system",
    ) -> PermissionPolicy | None:
        """Update mutable fields of an existing policy."""
        with self._lock:
            existing = self.get_policy(policy_id)
            if existing is None:
                return None
            old_json = json.dumps(asdict(existing))
            updates: list[str] = ["updated_at=?"]
            params: list[Any] = [time.time()]
            if decision is not None:
                # CRITICAL can never become AUTO_ALLOW
                if existing.risk_level == "CRITICAL" and decision == PolicyDefaultDecision.AUTO_ALLOW:
                    raise ValueError("CRITICAL risk policies cannot be set to AUTO_ALLOW")
                updates.append("decision=?"); params.append(decision.value)
            if enabled is not None:
                updates.append("enabled=?"); params.append(int(enabled))
            if description is not None:
                updates.append("description=?"); params.append(description)
            params.append(policy_id)
            self._db.execute(
                f"UPDATE permission_policies SET {', '.join(updates)} WHERE id=?",
                params,
            )
            updated = self.get_policy(policy_id)
            assert updated is not None
            self._write_audit(
                event_type=PolicyAuditEventType.POLICY_UPDATED,
                policy=updated, actor=actor, old_state=old_json,
            )
            self._db.commit()
        log.info("Policy updated: id=%s", policy_id)
        return updated

    def delete_policy(self, policy_id: str, actor: str = "system") -> bool:
        """Delete a policy by ID.  Returns True if a row was removed."""
        with self._lock:
            existing = self.get_policy(policy_id)
            if existing is None:
                return False
            self._db.execute(
                "DELETE FROM permission_policies WHERE id=?", (policy_id,)
            )
            self._write_audit(
                event_type=PolicyAuditEventType.POLICY_DELETED,
                policy=existing, actor=actor,
                old_state=json.dumps(asdict(existing)), new_state="",
            )
            self._db.commit()
        log.info("Policy deleted: id=%s", policy_id)
        return True

    def reset_policies(
        self,
        scope_type: PolicyScope,
        scope_id: str = "",
        actor: str = "system",
    ) -> int:
        """Delete all policies for a given scope.  Returns count removed."""
        with self._lock:
            existing = self.list_policies(scope_type=scope_type, scope_id=scope_id)
            if not existing:
                return 0
            self._db.execute(
                "DELETE FROM permission_policies WHERE scope_type=? AND scope_id=?",
                (scope_type.value, scope_id),
            )
            for p in existing:
                self._db.execute(
                    """INSERT INTO policy_audit_log
                       (id,event_type,policy_id,scope_type,scope_id,action_type,
                        risk_level,actor,timestamp,old_state,new_state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), PolicyAuditEventType.POLICY_RESET.value,
                     p.id, p.scope_type, p.scope_id, p.action_type,
                     p.risk_level, actor, time.time(),
                     json.dumps(asdict(p)), ""),
                )
            self._db.commit()
        log.info("Reset %d policies for scope=%s/%s", len(existing), scope_type.value, scope_id)
        return len(existing)

    # ------------------------------------------------------------------
    # Query for resolution
    # ------------------------------------------------------------------

    def resolve_policy(
        self,
        action_type: str,
        risk_level: str,
        workspace_path: str | None = None,
    ) -> PolicyDefaultDecision | None:
        """Return the effective persistent decision for the given action, using precedence:
        1. Workspace/project specific policy (if workspace_path given)
        2. Global policy
        Returns None if no enabled policy matches (caller should use defaults).
        """
        with self._lock:
            # --- Workspace scope first ---
            if workspace_path:
                row = self._db.execute(
                    """SELECT decision FROM permission_policies
                       WHERE scope_type=? AND scope_id=?
                         AND (action_type=? OR action_type='*')
                         AND enabled=1
                       ORDER BY CASE WHEN action_type=? THEN 0 ELSE 1 END
                       LIMIT 1""",
                    (PolicyScope.WORKSPACE.value, workspace_path,
                     action_type, action_type),
                ).fetchone()
                if row:
                    return PolicyDefaultDecision(row[0])

            # --- Global scope ---
            row = self._db.execute(
                """SELECT decision FROM permission_policies
                   WHERE scope_type=? AND scope_id=''
                     AND (action_type=? OR action_type='*')
                     AND enabled=1
                   ORDER BY CASE WHEN action_type=? THEN 0 ELSE 1 END
                   LIMIT 1""",
                (PolicyScope.GLOBAL.value, action_type, action_type),
            ).fetchone()
            if row:
                return PolicyDefaultDecision(row[0])
        return None

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def list_audit_log(
        self,
        policy_id: str | None = None,
        limit: int = 100,
    ) -> list[PolicyAuditRecord]:
        where = "WHERE policy_id=?" if policy_id else ""
        params: list[Any] = [policy_id] if policy_id else []
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM policy_audit_log {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_audit(r) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_audit(
        self,
        *,
        event_type: PolicyAuditEventType,
        policy: PermissionPolicy,
        actor: str,
        old_state: str = "",
        new_state: str | None = None,
    ) -> None:
        """Write an audit record inside an already-held lock."""
        new_json = new_state if new_state is not None else json.dumps(asdict(policy))
        self._db.execute(
            """INSERT INTO policy_audit_log
               (id,event_type,policy_id,scope_type,scope_id,action_type,
                risk_level,actor,timestamp,old_state,new_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), event_type.value, policy.id,
             policy.scope_type, policy.scope_id, policy.action_type,
             policy.risk_level, actor, time.time(), old_state, new_json),
        )

    @staticmethod
    def _validate_inputs(
        scope_type: PolicyScope,
        action_type: str,
        risk_level: str,
        decision: PolicyDefaultDecision,
    ) -> None:
        from .permissions import PermissionRiskLevel
        if not isinstance(scope_type, PolicyScope):
            raise ValueError(f"Invalid scope_type: {scope_type!r}")
        if not isinstance(decision, PolicyDefaultDecision):
            raise ValueError(f"Invalid decision: {decision!r}")
        if action_type not in ("*",) and not action_type.replace("_", "").isalnum():
            raise ValueError(f"Invalid action_type: {action_type!r}")
        try:
            PermissionRiskLevel(risk_level)
        except ValueError:
            raise ValueError(f"Invalid risk_level: {risk_level!r}")
        # CRITICAL can never be AUTO_ALLOW
        if risk_level == "CRITICAL" and decision == PolicyDefaultDecision.AUTO_ALLOW:
            raise ValueError("CRITICAL risk policies cannot be set to AUTO_ALLOW")

    @staticmethod
    def _row_to_policy(row: sqlite3.Row) -> PermissionPolicy:
        return PermissionPolicy(
            id=row["id"],
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            action_type=row["action_type"],
            risk_level=row["risk_level"],
            decision=row["decision"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            description=row["description"] if row["description"] else "",
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> PolicyAuditRecord:
        return PolicyAuditRecord(
            id=row["id"],
            event_type=row["event_type"],
            policy_id=row["policy_id"],
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            action_type=row["action_type"],
            risk_level=row["risk_level"],
            actor=row["actor"],
            timestamp=row["timestamp"],
            old_state=row["old_state"],
            new_state=row["new_state"],
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass
