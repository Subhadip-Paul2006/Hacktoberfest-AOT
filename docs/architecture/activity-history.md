# AgentBridge — Local Activity Store & History Specification

## 1. Overview & Purpose

The **Local Activity Store** provides persistent, structured, and auditable operational history for AgentBridge. Backed by the existing SQLite infrastructure with Write-Ahead Logging (WAL), the Activity Store captures interactions across Telegram ChatOps, autonomous agent runs, permission decisions, tool calls, and future connectors.

---

## 2. Relational Schema & Table Definitions

All tables share the primary SQLite connection managed by [`ControlPlaneStore`](../../sutra/control_plane.py) and are created non-destructively.

### 2.1 Table: `telegram_messages`
Stores incoming and outgoing Telegram messages and callbacks.
```sql
CREATE TABLE IF NOT EXISTS telegram_messages (
    id                TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL,
    chat_id           TEXT NOT NULL,
    telegram_user_id  TEXT,
    message_id        INTEGER,
    direction         TEXT NOT NULL,  -- 'inbound' | 'outbound'
    message_type      TEXT NOT NULL,  -- 'text' | 'callback' | 'command' | 'status'
    text              TEXT,           -- Redacted text content
    project_id        TEXT,
    session_id        TEXT,
    created_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tg_msgs_request ON telegram_messages (request_id);
CREATE INDEX IF NOT EXISTS idx_tg_msgs_chat_created ON telegram_messages (chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tg_msgs_session ON telegram_messages (session_id, created_at DESC);
```

### 2.2 Table: `prompts`
Captures user or system prompts dispatched to agents.
```sql
CREATE TABLE IF NOT EXISTS prompts (
    id            TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    project_id    TEXT,
    session_id    TEXT,
    agent_id      TEXT,
    source        TEXT NOT NULL,      -- 'telegram' | 'api' | 'cli' | 'system'
    prompt_text   TEXT NOT NULL,      -- Redacted prompt content
    status        TEXT NOT NULL,      -- 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
    created_at    REAL NOT NULL,
    completed_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_prompts_request ON prompts (request_id);
CREATE INDEX IF NOT EXISTS idx_prompts_project_created ON prompts (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts (session_id, created_at DESC);
```

### 2.3 Table: `operations`
Tracks top-level operations (Git commands, task executions, file operations, connector runs).
```sql
CREATE TABLE IF NOT EXISTS operations (
    id              TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL,
    operation_type  TEXT NOT NULL,    -- e.g. 'git.commit', 'agent.run', 'file.preview'
    source          TEXT NOT NULL,    -- 'telegram' | 'control_plane' | 'agent' | 'connector'
    project_id      TEXT,
    session_id      TEXT,
    connector_id    TEXT,
    status          TEXT NOT NULL,    -- 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
    started_at      REAL NOT NULL,
    completed_at    REAL,
    duration_ms     REAL,
    error_code      TEXT,
    error_message   TEXT              -- Redacted error message
);

CREATE INDEX IF NOT EXISTS idx_ops_request ON operations (request_id);
CREATE INDEX IF NOT EXISTS idx_ops_project_created ON operations (project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_session ON operations (session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_status ON operations (status);
```

### 2.4 Table: `permission_events`
Records every evaluation and decision made by [`PolicyEngine`](../../sutra/policy_engine.py).
```sql
CREATE TABLE IF NOT EXISTS permission_events (
    id            TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    operation_id  TEXT,
    risk_level    TEXT NOT NULL,      -- 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
    decision      TEXT NOT NULL,      -- 'allow_once' | 'allow_session' | 'deny' | 'modify'
    reason        TEXT,               -- Redacted rationale / description
    actor         TEXT NOT NULL,      -- 'user' | 'policy' | 'system'
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_perm_events_request ON permission_events (request_id);
CREATE INDEX IF NOT EXISTS idx_perm_events_created ON permission_events (created_at DESC);
```

### 2.5 Table: `connector_events`
Tracks lifecycle and status changes for connectors and integrations.
```sql
CREATE TABLE IF NOT EXISTS connector_events (
    id            TEXT PRIMARY KEY,
    request_id    TEXT,
    connector_id  TEXT NOT NULL,
    event_type    TEXT NOT NULL,      -- 'connected' | 'disconnected' | 'error' | 'sync'
    status        TEXT NOT NULL,
    metadata      TEXT,               -- Redacted JSON metadata
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conn_events_request ON connector_events (request_id);
CREATE INDEX IF NOT EXISTS idx_conn_events_connector ON connector_events (connector_id, created_at DESC);
```

### 2.6 Table: `tool_calls`
Detailed records of individual tool invocations executed by agents or connectors.
```sql
CREATE TABLE IF NOT EXISTS tool_calls (
    id                   TEXT PRIMARY KEY,
    request_id           TEXT NOT NULL,
    connector_id         TEXT,
    server_name          TEXT,
    tool_name            TEXT NOT NULL,
    arguments_redacted   TEXT,        -- Redacted JSON arguments string
    result_redacted      TEXT,        -- Redacted output / result summary
    status               TEXT NOT NULL, -- 'started' | 'completed' | 'failed'
    risk_level           TEXT,
    permission_decision  TEXT,
    started_at           REAL NOT NULL,
    completed_at         REAL,
    duration_ms          REAL,
    error_code           TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_request ON tool_calls (request_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_created ON tool_calls (tool_name, started_at DESC);
```

### 2.7 Table: `audit_events`
Generic system-level operational audit records.
```sql
CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    request_id  TEXT,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    target      TEXT,
    details     TEXT,                 -- Redacted JSON details
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_request ON audit_events (request_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events (created_at DESC);
```

### 2.8 Table: `artifact_metadata`
Stores metadata and checksums for generated or modified files (zero binary blobs in SQLite).
```sql
CREATE TABLE IF NOT EXISTS artifact_metadata (
    id               TEXT PRIMARY KEY,
    request_id       TEXT,
    project_id       TEXT,
    artifact_type    TEXT NOT NULL,   -- e.g. 'diff', 'patch', 'export', 'log'
    file_path        TEXT NOT NULL,
    file_size_bytes  INTEGER,
    sha256           TEXT,
    created_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifact_request ON artifact_metadata (request_id);
CREATE INDEX IF NOT EXISTS idx_artifact_project ON artifact_metadata (project_id, created_at DESC);
```

---

## 3. Query & Retention API

The [`ActivityStore`](../../sutra/activity_store.py) exposes high-level, thread-safe asynchronous and synchronous query methods:

- `get_timeline(request_id: str) -> Dict[str, List[Dict[str, Any]]]`: Returns all correlated messages, prompts, operations, permissions, tool calls, and artifacts for a single `request_id`.
- `list_operations(project_id: str | None, session_id: str | None, status: str | None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]`: Returns paginated operations.
- `list_telegram_messages(chat_id: str | None, request_id: str | None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]`: Returns paginated chat messages.
- `list_tool_calls(request_id: str | None, tool_name: str | None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]`: Returns paginated tool executions.
- `purge_retention(retention_days: int = 90) -> int`: Removes records across all 8 tables older than `now - retention_days * 86400` in a single transaction, returning the total deleted row count.

---

## 4. Failure Isolation Guarantees

All write operations in `ActivityStore` adhere to defensive execution:
```python
try:
    with self._lock:
        self._db.execute(...)
        self._db.commit()
    self.metrics.record("activity_store_write_ok", 1)
except Exception as exc:
    log.warning("activity_store_write_failed table=%s error=%s", table, exc)
    self.metrics.record("activity_store_write_error", 1)
    # Never re-raise: execution must continue uninterrupted!
```
