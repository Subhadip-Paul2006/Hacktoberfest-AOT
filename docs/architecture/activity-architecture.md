# AgentBridge — Phase 7 Architecture: Local Activity & Operational History

## 1. Executive Summary & Objective

Phase 7 establishes the **Durable Operational History & Context Foundation** for AgentBridge.

In AgentBridge:
> **SQLite is the local source of truth for operational history.**

Workstation operations, developer ChatOps interactions via Telegram, autonomous agent tasks, Git/GitHub actions, and future external connectors must leave an auditable, correlated, and durable operational trail.

```
       📱 Telegram       🤖 Coding Agents       🐙 Git / GitHub       🔌 Connectors (Future)
            │                    │                     │                      │
            └────────────────────┴──────────┬──────────┴──────────────────────┘
                                            │
                                            ▼
                                     Activity Context
                                   (Correlation Engine)
                                            │
                                            ▼
                                  Local Activity Store
                                 (Sanitization & Redaction)
                                            │
                                            ▼
                                    Single Local SQLite
                                  (WAL Mode, Thread-Safe)
```

---

## 2. Phase 7 Scope & Phasing Boundary

In accordance with the project roadmap and strict Phase 7.0/7.1 freeze guidelines:

- **Phase 7.0 (Completed)**: Research, current persistence audit, architectural freeze, and documentation.
- **Phase 7.1 (Completed)**: Reusable Local Activity Store foundation, SQLite schema & migrations, correlation model, secret redaction, retention management, paginated querying, and failure isolation.
- **Phase 7.2 (Completed)**: Clean, provider-neutral Connector Abstraction & Contract (MCP, REST, CLI).
- **Phase 7.3 (Completed)**: Connector Registry & Lifecycle Management, availability guards, and Analyzer extension contract.
- **Phase 7.4 (Completed)**: Generic Environment & Connector Analyzer, provider-neutral probing pipeline, 12-stage inspection, and evidence-based verification model.
- **Phase 7.5 (Completed)**: Transport-Agnostic MCP Client Core, session lifecycle FSM, protocol negotiation, error normalization, ActivityStore auditing, and Connector adapter.
- **Phase 7.6 (Completed)**: Concrete MCP stdio Transport, child process lifecycle management, stderr isolation, bounded ring buffering, and safe cross-platform termination.
- **Phase 7.7 (Completed)**: Remote MCP Transport, modern Streamable HTTP, legacy SSE compatibility, dynamic auth injection, bounded streaming, and timeout/cancellation handling.
- **Phase 7.8A (Completed)**: GitHub MCP Connector & Portable Authentication, Streamable HTTP over HTTPS to official GitHub Remote MCP endpoint, OS Keyring token storage, deterministic fallback, and PolicyEngine tool enforcement.
- **Phase 7.8B (Completed)**: Secure Local Terminal Connector, canonical `ConnectorType.CLI` provider (`terminal-local`), strict `shell=False` asynchronous process model, workspace confinement & path traversal guards, PolicyEngine risk enforcement & CRITICAL invariant protection, controlled environment policy, and process tree termination.
- **Phase 7.8C (Completed)**: Supabase Remote MCP Connector, official Streamable HTTP endpoint (`https://mcp.supabase.com/mcp`), project scoping (`project_ref`), read-only default policy gating, feature group filtering, reusable OAuth 2.0 PKCE client with loopback redirect listener, headless PAT resolution with deterministic precedence, and zero secret persistence.
- **Phase 7.8D+ (Deferred Non-Goals)**:
  - Additional third-party providers (Stitch, Vercel, Notion)
  - Telegram `/connectors` UI and `/history` UI

---

## 3. Persistent Storage Architecture Analysis

### 3.1 Single Database Principle
AgentBridge strictly rejects database sprawl. AgentBridge does not create separate database files for each subsystem:
- [`ControlPlaneStore`](../../sutra/control_plane.py) manages the primary SQLite database at `control-plane.sqlite3` (or `:memory:` in testing).
- [`PolicyStore`](../../sutra/policy_store.py) shares this exact database file and connection (`PolicyStore(self.store._db, self.store._lock)`).
- [`ActivityStore`](../../sutra/activity_store.py) connects to the **same database file and connection** (`ActivityStore(self.store._db, self.store._lock)`), sharing the Write-Ahead Log (WAL), locks, and backup boundary.
- A standalone factory (`ActivityStore.from_path(path)`) is provided for testing and CLI usage.

### 3.2 Schema Versioning & Migrations
Database versioning uses the existing `schema_meta` table:
```sql
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```
`PolicyStore` tracks `policy_schema_version`.
`ActivityStore` tracks `activity_schema_version`.

All migrations are purely non-destructive, applying `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`.

---

## 4. Universal Correlation Model

Every logical lifecycle event is bound by a deterministic correlation identifier: **`request_id`**.

```
Telegram Message (inbound) ──► req_a1b2c3d4
       │
       ▼
Prompt Submission          ──► req_a1b2c3d4
       │
       ▼
Permission Evaluation      ──► req_a1b2c3d4 (PermissionRequest.request_id)
       │
       ▼
Operation Execution        ──► req_a1b2c3d4 (TaskResult.request_id)
       │
       ▼
Tool Calls / Sub-tasks     ──► req_a1b2c3d4
       │
       ▼
Telegram Message (outbound) ──► req_a1b2c3d4
```

### Context Entities
- `request_id`: Primary correlation UUID tracking the entire causality chain.
- `project_id`: Project directory name or canonical identifier.
- `session_id`: Active agent or device session UUID.
- `telegram_user_id`: Telegram user identifier.
- `chat_id`: Telegram conversation identifier.

Given a `request_id`, operators can query the complete end-to-end timeline across all tables.

---

## 5. Security & Redaction Invariant

Before any text, argument, error message, or JSON payload is written to SQLite, it must pass through centralized secret sanitization:
- Function: [`redact_secrets`](../../sutra/redaction.py)
- Sanitized entities:
  - Telegram bot tokens (`bot<token>`)
  - GitHub tokens (`ghp_...`, `gho_...`, `ghu_...`, `ghs_...`, `ghr_...`)
  - OpenAI / Anthropic / AI API keys (`sk-...`)
  - AWS access keys (`AKIA...`)
  - Slack tokens (`xox...`)
  - JWT tokens (`eyJ...`)
  - HTTP `Authorization: Bearer ...` and `Basic ...` headers
  - Private key blocks (`-----BEGIN ... PRIVATE KEY-----`)
  - Credential assignments (`password = ...`, `token = ...`)
- **Prohibited**: Raw secrets, tokens, private keys, or pairing codes must **never** touch the database.

---

## 6. Failure Isolation & Degradation Invariant

History and activity logging is strictly **non-blocking and non-critical**:
$$\text{Activity Store Failure} \not\rightarrow \text{Operation Crash}$$

- If an `ActivityStore` write fails (e.g. SQLite disk full, lock busy, or transient I/O error):
  1. The core operation (agent execution, Git command, permission evaluation, Telegram response) **must continue without interruption**.
  2. The failure is logged at `WARNING` level with error details.
  3. The `activity_store_error` metric is incremented.
  4. Under no circumstances may a history error bypass permission checks or crash Telegram.

---

## 7. Retention & Querying Policies

1. **Configuration**:
   - `HISTORY_ENABLED`: Boolean (default: `true`).
   - `HISTORY_RETENTION_DAYS`: Integer (default: `90` days).
2. **Purging**:
   - `purge_older_than(days)` deletes records older than monotonic cutoff across all tables in a single transaction.
3. **Bounded Lookups**:
   - Every read query enforces `LIMIT` (default: 50, max: 200) and `OFFSET` / cursor pagination.
   - Unbounded `SELECT *` without limits is prohibited.
   - Comprehensive composite indexes optimize lookups by `request_id`, `(project_id, created_at)`, `(session_id, created_at)`, and `created_at`.

---

## 8. Connector Abstraction & Contract (Phase 7.2)

### 8.1 Domain Boundary Principle
Phase 7.2 introduces the canonical `Connector` abstraction in `sutra/connectors/`.

The connector contract serves as a strict domain boundary:
- **Provider-Neutral**: Disconnects connector interfaces from specific external provider protocols (MCP, REST, CLI).
- **Zero Duplication**: Does **not** implement duplicate retry, timeout, circuit breaker, metrics, cache, or permission mechanisms.
- **Untrusted Boundary**: Assumes all connector inputs and outputs are untrusted. Prohibits secrets in metadata and scrubs credentials automatically.
- **Async First**: Exposes clean asynchronous APIs (`connect()`, `disconnect()`, `health()`, `discover()`, `execute()`).

### 8.2 Architectural Position
```
Caller (Telegram / Agent / Internal)
               │
               ▼
   [PermissionEngine Evaluation]  ──> Authoritative risk & policy check
               │
               ▼
    [Connector Manager Layer]     ──> (Phase 7.3+) Lifecycle & resolution
               │
               ▼
       Connector Contract         ──> sutra.connectors.Connector
               │
     ┌─────────┼─────────┐
     ▼         ▼         ▼
    MCP      REST       CLI       ──> Concrete Adapters
     │         │         │
     └─────────┼─────────┘
               ▼
     [Shared Infrastructure]
      • with_timeout_and_retry (sutra.resilience)
      • record_event / record_tool_call (sutra.activity_store)
      • redact_secrets (sutra.redaction)
```

For complete technical specifications, see:
- Dedicated architecture guide: [connectors.md](connectors.md)
- Archify visual model: [connector-architecture.workflow.json](diagrams/connector-architecture.workflow.json)

---

## 9. Connector Registry & Lifecycle Management (Phase 7.3)

### 9.1 The In-Process Management Authority
`ConnectorRegistry` serves as the central in-process registry and lifecycle manager:
- **Registry vs Connector**: While `Connector` models provider-neutral integration behavior, `ConnectorRegistry` tracks which instances exist, their availability, and their operational status.
- **Multiple Instances**: Distinct instances of identical providers (e.g. `github-personal` vs `github-work`) are tracked independently without collisions.
- **Availability vs Connectivity**: Distinguishes administrative state (`enabled=True/False`) from technical connection status (`status=REGISTERED|CONNECTING|READY|DEGRADED|DISCONNECTED|ERROR`).
- **Lifecycle FSM**: Validates and serializes state transitions with per-instance async locking.
- **Fault Isolation**: Diagnostics (`health()`, `health_all()`) never let a failing connector crash the registry or impact healthy instances.
- **Durable Audit Trail**: Lifecycle events (`registered`, `unregistered`, `enabled`, `disabled`, `connect_started`, `connected`, `disconnected`, `degraded`, `error`) stream to [`ActivityStore`](../../sutra/activity_store.py) with full `request_id` correlation.
- **Future Analyzer Contract**: Exposes `ConnectorAnalysisDescriptor` providing standardized configuration, transport, and client expectations for the Phase 7.4 Generic Analyzer.

For complete technical specifications, see:
- Dedicated architecture guide: [connector-registry.md](connector-registry.md)
- Archify visual model: [connector-registry.workflow.json](diagrams/connector-registry.workflow.json)

---

## 10. Generic Environment & Connector Analyzer (Phase 7.4)

### 10.1 The Evidence-Based Verification Pipeline
Phase 7.4 implements `ConnectorAnalyzer`, providing a unified, provider-neutral inspection engine:
- **No Provider Branching**: Operates on `ConnectorAnalysisDescriptor` without `if github: elif notion:` conditionals.
- **12 Inspection Stages**: DISCOVERY, CONFIGURATION, PARAMETERS, DEPENDENCIES, CONNECTION, AUTHENTICATION, CAPABILITIES, CLI_APPLICABILITY, CLIENT_APPLICABILITY, HEALTH, LATENCY, and USAGE_EVIDENCE.
- **Strict Evidence Model**: Enforces non-assumption axioms where `CONFIGURED`, `CONNECTED`, `AUTHENTICATED`, `VERIFIED`, and `OBSERVED_IN_USE` are strictly isolated.
- **Unknown as First-Class**: Never treats `UNKNOWN` as `FAIL` when an inspection cannot be performed.
- **Usage Auditing**: Usage evidence is grounded strictly in historical [`ActivityStore`](../../sutra/activity_store.py) tool invocations.
- **Safety & Isolation**: Bounded concurrency (`asyncio.Semaphore`), non-destructive subprocess probing (3-second timeouts), and zero secret leaks.

For complete technical specifications, see:
- Architecture guide: [environment-analyzer.md](environment-analyzer.md)
- Verification model guide: [verification-model.md](verification-model.md)
- Architecture diagram: [analyzer-architecture.architecture.json](diagrams/analyzer-architecture.architecture.json)
- Verification workflow: [connector-verification.workflow.json](diagrams/connector-verification.workflow.json)
- Analysis sequence: [environment-analysis.sequence.json](diagrams/environment-analysis.sequence.json)
- Evidence separation workflow: [connector-evidence.workflow.json](diagrams/connector-evidence.workflow.json)

---

## 11. Model Context Protocol (MCP) Client Core (Phase 7.5)

### 11.1 Transport-Agnostic Protocol Foundation
Phase 7.5 delivers the clean, decoupled MCP client core integrating directly with AgentBridge's canonical connector ecosystem:
- **Clean Decoupling**: Complete isolation of the physical transport via the `MCPTransport` abstract interface. Concrete stdio and HTTP/SSE transports are strictly deferred to Phases 7.6 and 7.7.
- **Protocol Handshake & Negotiation**: Implements handshake negotiation offering the current stable `2026-07-28` specification revision, defaulting to `2025-03-26`, and seamlessly downgrading for older `2024-11-05` servers.
- **Session Lifecycle FSM**: Strict state transitions through `DISCONNECTED`, `CONNECTING`, `READY`, `DEGRADED`, `ERROR`, and `CLOSED`.
- **Domain Client Engine**: `MCPClient` exposes clean, high-level primitives for tools (`list_tools`, `call_tool`), resources (`list_resources`, `read_resource`), and prompts (`list_prompts`, `get_prompt`) with bounded cursor pagination (`max_pages=50`, `max_items=1000`).
- **AgentBridge Connector Adapter**: `MCPConnector` implements the canonical `Connector` contract, routing tool and resource operations with automatic secret scrubbing and SQLite [`ActivityStore`](../../sutra/activity_store.py) auditing.
- **Analyzer Integration**: Exposes `ConnectorAnalysisDescriptor` enabling zero-change verification by the Phase 7.4 Generic Connector Analyzer.
- **Authoritative Security**: Strict integration with [`PolicyEngine`](../../sutra/policy_engine.py) for mutating tool requests, parameter sanitization via `sanitize_metadata`, and output scrubbing via [`redact_secrets`](../../sutra/redaction.py).

For complete technical specifications, see:
- Dedicated architecture guide: [mcp-client.md](mcp-client.md)
- Archify architecture diagram: [mcp-client-architecture.architecture.json](diagrams/mcp-client-architecture.architecture.json)
- Archify lifecycle workflow: [mcp-client-lifecycle.workflow.json](diagrams/mcp-client-lifecycle.workflow.json)
- Archify tool-call sequence: [mcp-tool-call.sequence.json](diagrams/mcp-tool-call.sequence.json)

---

## 12. Concrete Model Context Protocol (MCP) stdio Transport (Phase 7.6)

### 12.1 Local Process Transport Engine
Phase 7.6 implements `StdioMCPTransport`, providing the concrete transport implementation connecting AgentBridge to local MCP server child processes:
- **Subprocess Security**: Explicit executable path and argument arrays. Never invokes `shell=True` or shell command strings. Sanitizes environment variables using `sanitize_metadata`.
- **Stream Isolation**: Strictly separates `stdout` (protocol JSON-RPC 2.0 lines only) from `stderr` (non-blocking diagnostic reader). A runaway or noisy stderr can never deadlock the protocol or corrupt JSON-RPC parsing.
- **Bounded Buffering & Resource Limits**: Enforces `max_frame_bytes` (10MB) for protocol lines and a bounded circular deque ring buffer (200 lines, 64KB) for stderr with automated secret scrubbing via `redact_secrets()`.
- **Robust Process Lifecycle**: Transitions predictably through `DISCONNECTED -> STARTING -> CONNECTED -> CLOSING -> DISCONNECTED` (or `ERROR`). Handles startup timeouts, sudden process crashes, and EOF cleanly.
- **Cross-Platform Graceful Teardown**: Performs orderly stdin closure, soft termination (`SIGTERM` on POSIX / `proc.terminate()` on Windows), and fallback force-kill (`proc.kill()` / `taskkill /F /T` tree-kill) ensuring zero orphan or zombie processes.
- **Full Architecture Integration**: Integrates directly with `MCPSession`, `MCPClient`, `MCPConnector`, `ConnectorRegistry`, `ActivityStore`, and `ConnectorAnalyzer` with zero architectural branching.

For complete technical specifications, see:
- Dedicated architecture guide: [mcp-stdio.md](mcp-stdio.md)
- Archify architecture diagram: [mcp-stdio-architecture.architecture.json](diagrams/mcp-stdio-architecture.architecture.json)
- Archify lifecycle workflow: [mcp-stdio-lifecycle.workflow.json](diagrams/mcp-stdio-lifecycle.workflow.json)
- Archify communication sequence: [mcp-stdio-communication.sequence.json](diagrams/mcp-stdio-communication.sequence.json)

---

## 13. Remote Model Context Protocol (MCP) HTTP Transport (Phase 7.7)

### 13.1 Remote Streamable HTTP Engine
Phase 7.7 implements `HttpMCPTransport`, providing the concrete remote transport connecting AgentBridge to external MCP servers over HTTP/HTTPS:
- **Streamable HTTP Specification**: Standard POST-based request dispatch with session tracking via `mcp-session-id`, handling both `application/json` and `text/event-stream` responses.
- **Legacy SSE Compatibility**: Backward compatibility mode connecting to a GET SSE endpoint, receiving an `endpoint` URI event, and routing subsequent POST requests with responses multiplexed over the SSE event stream.
- **Security & Redaction**: Strict URL scheme validation (`https` required in production; `http` permitted only on loopback/localhost dev endpoints), bounded redirect following (default: 3), and zero credential leakage via header redaction and safe metadata sanitization.
- **Authentication Boundary**: Clean pluggable authentication injection interface via `auth_provider` callable or static headers, ready for Phase 7.8+ token providers without altering `MCPClient`.
- **Bounded Resource Limits**: Enforces hard caps on response payloads (10MB), SSE events (1MB), event buffers (1000 items), and connection limits to protect against adversarial or misconfigured servers.
- **Full Architecture Integration**: Integrates directly with `MCPSession`, `MCPClient`, `MCPConnector`, `ConnectorRegistry`, `ActivityStore`, and `ConnectorAnalyzer` without competing abstractions or bypasses.

For complete technical specifications, see:
- Dedicated architecture guide: [mcp-http.md](mcp-http.md)
- Archify architecture diagram: [mcp-http-architecture.architecture.json](diagrams/mcp-http-architecture.architecture.json)
- Archify lifecycle workflow: [mcp-http-lifecycle.workflow.json](diagrams/mcp-http-lifecycle.workflow.json)
- Archify communication sequence: [mcp-http-communication.sequence.json](diagrams/mcp-http-communication.sequence.json)

