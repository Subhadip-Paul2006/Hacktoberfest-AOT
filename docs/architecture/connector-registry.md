# AgentBridge — Connector Registry & Lifecycle Management (Phase 7.3)

## 1. Executive Summary & Objective

The **Connector Registry** (`sutra/connectors/registry.py`) is the central in-process authority for managing integration instances in AgentBridge.

While a `Connector` defines **how** a specific integration behaves (transports, tools, and execution), the `ConnectorRegistry` answers **what exists, what is enabled, and what state each instance is in**.

```
    ┌───────────────────────────┐
    │     Internal Caller       │ (Telegram / Autonomous Agent / CLI)
    └─────────────┬─────────────┘
                  │
                  ▼
    ┌───────────────────────────┐
    │  PermissionEngine Check   │ (Centralized Risk & Authorization Policy)
    └─────────────┬─────────────┘
                  │
                  ▼
    ┌───────────────────────────┐
    │     ConnectorRegistry     │
    │  ┌─────────────────────┐  │
    │  │ Instance Registry   │  │ ──> Multiple instances (github-work, github-personal)
    │  │ Lifecycle FSM       │  │ ──> REGISTERED ➔ CONNECTING ➔ READY ➔ DISCONNECTED
    │  │ Availability Guard  │  │ ──> Enabled vs Disabled
    │  │ Health Aggregator   │  │ ──> Non-blocking diagnostics & fault isolation
    │  │ Inspection Metadata │  │ ──> Universal contract for Phase 7.4 Generic Analyzer
    │  └─────────────────────┘  │
    └─────────────┬─────────────┘
                  │
         ┌────────┴────────┐
         ▼                 ▼
   [ActivityStore]   [Metrics Registry]
   (connector_events) (bounded counters)
```

---

## 2. Architectural Boundaries & Principles

1. **Connector vs. Registry Distinction**:
   - `Connector`: Encapsulates provider protocol details (MCP JSON-RPC, REST HTTP, CLI Subprocess) and exposes declared capabilities.
   - `ConnectorRegistry`: Tracks lifecycle, enables/disables instances, resolves lookups, orchestrates health diagnostics, and provides inspection descriptors.
2. **Provider-Neutrality**:
   - The Registry contains **zero** provider-specific branches (`if github:`, `if notion:`, `if supabase:`).
   - Transports, providers, and capabilities are generic attributes.
3. **No Duplicate Subsystems**:
   - The Registry does not implement retry loops, cache engines, or circuit breakers. Resilience wrapping is delegated to [`sutra.resilience`](../../sutra/resilience.py).
   - Does not implement authorization logic; delegates to [`sutra.permissions`](../../sutra/permissions.py) and [`sutra.policy_engine`](../../sutra/policy_engine.py).
4. **Untrusted Data & Secret Safety**:
   - Metadata is sanitized via `sanitize_metadata` and [`redact_secrets`](../../sutra/redaction.py).
   - Secret material (tokens, passwords, keys) is never stored in memory descriptors or persisted to SQLite.

---

## 3. Instance Identity & Multiple Instances

Connector instances have distinct, immutable IDs separated from provider names and transport types:

| Concept | Example A | Example B | Example C |
| :--- | :--- | :--- | :--- |
| **`connector_id`** | `github-personal` | `github-work` | `notion-docs` |
| **`provider`** | `github` | `github` | `notion` |
| **`type`** | `REST` | `REST` | `MCP` |
| **`transport`** | `http` | `http` | `stdio` |

Multiple instances of the same provider run completely independently:
- `github-personal` can be `READY` while `github-work` is `DISCONNECTED` or `ERROR`.
- Actions on one instance do not affect any other instance.

---

## 4. Lifecycle State Machine

The registry enforces deterministic lifecycle transitions using `ConnectorStatus`:

```
             ┌─────────────────────────┐
             │       REGISTERED        │
             └────────────┬────────────┘
                          │ connect()
                          ▼
             ┌─────────────────────────┐
             │       CONNECTING        │
             └──────┬───────────┬──────┘
       success      │           │ failure
       ┌────────────┘           └────────────┐
       ▼                                     ▼
┌──────────────┐                       ┌──────────────┐
│    READY     │ ◄──── reconnect ───── │    ERROR     │
└──────┬───────┘                       └──────┬───────┘
       │           ┌──────────────┐           │
       ├──────────►│   DEGRADED   │           │
       │           └──────┬───────┘           │
       │                  │                   │
       ▼ disconnect()     ▼ disconnect()      ▼ disconnect()
┌─────────────────────────────────────────────────────┐
│                    DISCONNECTED                     │
└─────────────────────────────────────────────────────┘
```

### Valid Transition Matrix

| Source State | Permitted Destination States |
| :--- | :--- |
| `REGISTERED` | `CONNECTING`, `DISCONNECTED` |
| `CONNECTING` | `READY`, `DEGRADED`, `ERROR`, `DISCONNECTED` |
| `READY` | `DEGRADED`, `DISCONNECTED`, `ERROR` |
| `DEGRADED` | `READY`, `DISCONNECTED`, `ERROR` |
| `DISCONNECTED` | `CONNECTING` |
| `ERROR` | `CONNECTING`, `DISCONNECTED` |

Illegal transitions (e.g. `REGISTERED` ➔ `READY` without invoking `connect()`) raise `ConnectorLifecycleError`.

---

## 5. Availability Model: Enabled vs. Connected

Availability and connectivity are distinct dimensions:

- **`enabled: bool`**: Administrative state controlled by operators or policies.
  - `enabled = False`: The connector is intentionally disabled. Connection attempts and executions are rejected immediately with `ConnectorDisabledError`.
  - Calling `disable()` on an active connector gracefully disconnects it.
- **`status: ConnectorStatus`**: Runtime technical connection state (`REGISTERED`, `CONNECTING`, `READY`, `DEGRADED`, `DISCONNECTED`, `ERROR`).

This allows operators and future UI to distinguish:
1. Intentionally disabled (`enabled=False`, `status=DISCONNECTED`)
2. Enabled but not yet connected (`enabled=True`, `status=REGISTERED`)
3. Enabled and healthy (`enabled=True`, `status=READY`)
4. Enabled but failing upstream (`enabled=True`, `status=ERROR`)

---

## 6. Health Model & Fault Isolation

- **Point-in-Time Diagnostics**: `ConnectorHealth` reports latency, status, healthy flag, and diagnostic messages.
- **Fault Isolation**:
  - If a connector raises an exception during a health check, the exception is caught, logged, and isolated.
  - The registry records the instance as unhealthy and updates status to `ERROR` or `DEGRADED`.
  - Batch diagnostics via `health_all()` executes across all connectors concurrently; one crashing connector **never** fails the batch.

---

## 7. Persistence Model

- **Principle of Non-Live Persistence**: Live Python connector instances, open sockets, and child processes are never serialized to disk.
- **Safe SQLite Configuration Store**:
  - The registry stores safe descriptor configurations in table `connector_configs` within the shared control-plane SQLite database.
  - Fields: `connector_id`, `name`, `connector_type`, `provider`, `enabled`, `version`, `description`, `metadata`, `created_at`, `updated_at`.
  - Prohibited: Passwords, bearer tokens, API keys, or private keys are scrubbed before storage.
  - Reconstructable: `load_persisted_configs()` allows reconstructing registered descriptors upon process restart.

---

## 8. Integration with AgentBridge Platform Foundations

### 8.1 Activity Store Integration
All registry lifecycle events are recorded to the shared SQLite [`ActivityStore`](../../sutra/activity_store.py) under `connector_events`:
- Event types: `registered`, `unregistered`, `enabled`, `disabled`, `connect_started`, `connected`, `disconnected`, `degraded`, `error`.
- Every event captures the monotonic timestamp, instance status, sanitized metadata, and correlated `request_id`.
- History writes are non-blocking: database errors never crash connector workflows.

### 8.2 Permission Boundary
Administrative operations on the registry require explicit authorization:
- Helper: `create_permission_request(operation, connector_id, ...)`
- Action type: `"connector_admin"`
- Resource path: `connector_id`
- Command pattern: `"connector.<operation>"` (e.g. `connector.connect`, `connector.disable`)
- Policy checks are evaluated centrally via [`PolicyEngine`](../../sutra/policy_engine.py).

### 8.3 Resilience Boundary
- The Registry reuses [`with_timeout_and_retry`](../../sutra/resilience.py) and `RetryPolicy` from Phase 6C.
- Registration is decoupled from network reachability: a connector can be `REGISTERED` even if its remote provider is offline.

### 8.4 Metrics Subsystem
Bounded event counters emitted to [`Metrics`](../../sutra/metrics.py):
- `connector_registered`
- `connector_unregistered`
- `connector_connect_success`
- `connector_connect_error`
- `connector_disconnect`
- `connector_health_check`
- `connector_enable`
- `connector_disable`

---

## 9. Future Analyzer Extension Point (Phase 7.4)

Phase 7.4 introduces the **Generic Environment & Connector Analyzer**.

Instead of provider-specific analyzers (`GitHubAnalyzer`, `NotionAnalyzer`, `SupabaseAnalyzer`), AgentBridge utilizes a single generic inspection pipeline:

```
                  Connector
                      │
                      ▼
               ConnectorRegistry
                      │
                      ▼
        get_analysis_descriptor()
                      │
                      ▼
        ConnectorAnalysisDescriptor
                      │
       ┌──────────────┼──────────────┐
       ▼              ▼              ▼
 Configuration   Connection   Authentication
     Checks        Checks         Checks
       │              │              │
       └──────────────┼──────────────┘
                      ▼
                 Capabilities
                      │
                      ▼
             Environment / Client
                   Analysis
                      │
                      ▼
               VerificationState
```

The `ConnectorAnalysisDescriptor` exposes:
- Identity, type, provider, and transport hints (`stdio`, `http`, `subprocess`)
- Enabled and lifecycle status
- Declared environment variables (`declared_env_vars`)
- Declared binary / CLI dependencies (`declared_binary_names`)
- Capabilities and sanitised metadata

This enables Phase 7.4 to evaluate:
- **Configured** (all required env vars present)
- **Connected** (network reachable)
- **Authenticated** (credentials valid)
- **Verified** (tools verified end-to-end)
- **Applicable to Cursor / CLI / IDE** (local tooling detected)

without requiring provider-specific branching.

---

## 10. Downstream Architecture Links & Non-Goals

- **Phase 7.4 (Completed)**: [`ConnectorAnalyzer`](environment-analyzer.md) consumes `get_analysis_descriptor()` to execute the 12-stage provider-neutral environment and connector verification pipeline.
- **Phase 7.5+ (Deferred Non-Goals)**:
  - MCP Protocol Client, JSON-RPC, stdio transport, Streamable HTTP
  - Real third-party connectors (Stitch, Supabase, Vercel, Notion)
  - Telegram UI commands (`/connectors`, `/history`)
  - OAuth authentication flows
