# AgentBridge — Generic Environment & Connector Analyzer Architecture

**Document Version:** 1.0.0
**Phase:** 7.4 — Generic Environment & Connector Analyzer
**Status:** COMPLETE & VERIFIED
**Date:** 2026-10-08

---

## 1. Executive Summary

Phase 7.4 introduces the **Generic Environment & Connector Analyzer** to AgentBridge (`sutra/connectors/analyzer/`).
The analyzer provides an evidence-based, provider-neutral inspection engine that audits local workstation environments, evaluates connector descriptors, probes reachability and credentials non-destructively, measures latency diagnostics, assesses workstation client applicability, and correlates historical usage from the local SQLite `ActivityStore`.

### Core Architectural Principles
1. **Single Generic Verification Pipeline**: No provider-specific conditionals (`if github:`, `if notion:`, `if supabase:`) in verification logic. All connectors are analyzed via uniform interfaces using `ConnectorAnalysisDescriptor`.
2. **Evidence-Based Grounding**: The analyzer never guesses or assumes. States are strictly separated:
   - `CONFIGURED` does **not** imply `CONNECTED`
   - `CONNECTED` does **not** imply `REACHABLE`
   - `REACHABLE` does **not** imply `AUTHENTICATED`
   - `AUTHENTICATED` does **not** imply `VERIFIED`
   - `VERIFIED` does **not** imply `OBSERVED_IN_USE`
3. **First-Class Unknown State**: If an item cannot be tested or verified (e.g. offline dry run or absence of an upstream echo endpoint), the status is recorded as `UNKNOWN`, **never** degraded to `FAIL`.
4. **Zero-Guesswork Usage Evidence**: Usage is derived exclusively from actual tool execution records in `ActivityStore`. Usage is never inferred from configuration or presence of credentials.
5. **Secret Safety**: No secrets, tokens, or credentials are ever recorded into analysis check results, logs, or `ActivityStore` events. All evidence is sanitized with `sanitize_metadata`.
6. **Bounded Concurrency & Fault Isolation**: Multi-connector analyses are bounded by an `asyncio.Semaphore` (default 5 concurrent). A crash or exception in one connector analysis never aborts the batch or affects neighboring connectors.

---

## 2. Component Architecture

```
                             +------------------------+
                             |  Control Plane / CLI   |
                             +-----------+------------+
                                         |
                                         v
                         +-------------------------------+
                         |       ConnectorAnalyzer       |
                         | (sutra/connectors/analyzer/)  |
                         +---------------+---------------+
                                         |
       +--------------------+------------+------------+--------------------+
       |                    |                         |                    |
       v                    v                         v                    v
+--------------+   +-----------------+       +-----------------+   +----------------+
| Environment  |   |    CLIProbe     |       |   ClientProbe   |   |   Connector    |
|    Probe     |   | (which / where) |       | (Cursor/VSCode/ |   | Health/Connect |
| (OS / Arch)  |   | (Safe Versions) |       |  SUTRA Runtime) |   |  (Non-mutating)|
+--------------+   +-----------------+       +-----------------+   +----------------+
       |                    |                         |                    |
       +--------------------+------------+------------+--------------------+
                                         |
                                         v
                         +-------------------------------+
                         |   Evidence Aggregation FSM    |
                         |  (models.VerificationState)   |
                         +---------------+---------------+
                                         |
                       +-----------------+-----------------+
                       |                                   |
                       v                                   v
             +-------------------+               +-------------------+
             |   ActivityStore   |               |      Metrics      |
             | (SQLite Auditing) |               | (analyzer_run_*)  |
             +-------------------+               +-------------------+
```

---

## 3. The 12-Stage Verification Pipeline

Every connector analysis executes through 12 deterministic check stages:

| Stage | Category | Name | Description | Status Rules |
|-------|----------|------|-------------|--------------|
| 1 | `DISCOVERY` | `env_discovery` | Discovers OS family, architecture, Python version, working directory, and writable temp directories. Safe; never dumps raw environment. | `PASS` |
| 2 | `CONFIGURATION` | `config_check` | Checks declared environment variables (`declared_env_vars`) against current process and connector config. | `PASS` if all present; `FAIL` if missing required vars. |
| 3 | `PARAMETERS` | `param_check` | Validates configuration parameters and declared schema against required config keys. | `PASS` / `FAIL` |
| 4 | `DEPENDENCIES` | `dep_check` | Checks package and runtime dependencies without external network calls. | `PASS` |
| 5 | `CONNECTION` | `conn_check` | Performs non-destructive connection probe. Verifies transport reachability. | `PASS` if reachable; `FAIL` if refused/unreachable; `SKIPPED` if dry run. |
| 6 | `AUTHENTICATION` | `auth_check` | Validates credentials against upstream challenge. | `PASS` if accepted; `FAIL` if rejected; `UNKNOWN` if untested. |
| 7 | `CAPABILITIES` | `cap_check` | Discovers and inventories declared tools and resources without mutating external state. | `PASS` if discovered; `UNKNOWN` if offline. |
| 8 | `CLI_APPLICABILITY` | `cli_applicability` | Safe non-destructive binary verification using `which`/`where.exe` and bounded `--version` checks (3s timeout). | `PASS` if found; `UNKNOWN` if not found. |
| 9 | `CLIENT_APPLICABILITY` | `client_applicability` | Discovers client tool integrations: Cursor, VS Code, AgentBridge Runtime, and CLI agents. | `PASS` |
| 10 | `HEALTH` | `health_check` | Evaluates point-in-time diagnostic health check via `connector.health_check()`. | `PASS` if healthy; `FAIL` if degraded/error. |
| 11 | `LATENCY` | `latency_check` | Measures roundtrip latency of connection and health diagnostics in milliseconds. | `PASS` (stores `latency_ms`). |
| 12 | `USAGE_EVIDENCE` | `usage_evidence` | Queries `ActivityStore` for recent tool-call executions (`tool_call_start`). | `PASS` with state `OBSERVED_IN_USE` or `NOT_OBSERVED`; `UNKNOWN` if store absent. |

---

## 4. Verification States & Deterministic State Machine

The analyzer resolves the connector into one of **7 strict verification states**:

1. `UNCONFIGURED`: Required configuration parameters or declared environment variables are missing.
2. `CONFIGURED`: Configuration parameters and environment variables are present and syntactically valid, but network reachability and authentication have not been verified.
3. `CONNECTED`: Transport connection was successfully established to the remote endpoint or local process, but credentials have not yet been validated.
4. `AUTHENTICATED`: Credentials or tokens were explicitly validated and accepted by the service, but capabilities have not yet been enumerated.
5. `CAPABILITY_DISCOVERED`: Connector is connected, authenticated, and its declared tools, resources, and schemas have been discovered.
6. `VERIFIED`: Complete end-to-end verification succeeded: configuration valid, transport connected, credentials authenticated, capabilities discovered, and health diagnostics passed.
7. `FAILED`: A definitive failure occurred (e.g. invalid credentials, endpoint unreachable, or diagnostic health error).

### Orthogonal Usage Evidence States
Usage is tracked independently in `report.usage_evidence`:
- `OBSERVED_IN_USE`: At least 1 tool invocation recorded in `ActivityStore`.
- `NOT_OBSERVED`: 0 tool invocations recorded in `ActivityStore`.
- `UNKNOWN`: No `ActivityStore` available to evaluate history.

---

## 5. Probing Infrastructure

### EnvironmentProbe
- Collects `os_family` (`nt`, `posix`), `platform` (`win32`, `linux`, `darwin`), `architecture` (`AMD64`, `arm64`), `python_version`, `cwd`, and `temp_dir_writable`.
- **Safe**: Explicitly avoids dumping `os.environ` or capturing secret tokens.

### CLIProbe
- Resolves binary paths using `shutil.which`.
- Executes bounded version checks with strict 3-second timeouts.
- Never runs arbitrary scripts or executes untrusted binaries.

### ClientProbe
- Discovers installed workstation clients:
  - **Cursor**: Checks `~/.cursor` and application executable.
  - **VS Code**: Checks `~/.vscode` and `code` executable.
  - **AgentBridge Runtime**: Verifies active AgentBridge process and environment.
  - **CLI Agent**: Checks generic CLI executable availability.

---

## 6. Observability & Auditing

- **ActivityStore Integration**: Every completed analysis is logged as a structured event (`event_type="connector_analysis"`) in the local SQLite `ActivityStore` with redacted metadata.
- **Metrics**: Recorded via `sutra/metrics.py`:
  - `analyzer_run_total`: Total analysis executions initiated.
  - `analyzer_run_success`: Analyses resulting in non-failed verification states.
  - `analyzer_run_error`: Analyses resulting in `VerificationState.FAILED`.
- **Tracing**: Each analysis request accepts an optional `request_id` or generates a unique identifier formatted as `req_an_<hex10>`, propagating through all check results and audit logs.

---

## 7. MCP Connector Integration (Phase 7.5)

`MCPConnector` implements the canonical `analysis_descriptor()` method returning a `ConnectorAnalysisDescriptor`:
- **Provider Declaration**: `provider="mcp"`, `type=ConnectorType.MCP`.
- **Zero Special Cases**: `ConnectorAnalyzer` verifies MCP connectors across all 12 stages without branching into provider-specific conditionals.
- **Diagnostics**: Non-destructive `ping()` verifies transport round-trip latency, while `discover()` inspects tools, resources, and prompts.
- **Usage Evidence**: Audited tool executions in `ActivityStore` populate `USAGE_EVIDENCE` checks identically to any native connector.
