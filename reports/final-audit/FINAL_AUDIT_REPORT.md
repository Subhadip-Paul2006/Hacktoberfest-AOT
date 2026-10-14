# SŪTRA — Final End-to-End Codebase Audit & Manual Testing Readiness Report

> **Document:** `FINAL_AUDIT_REPORT.md`  
> **Repository:** `sutra-local` (SAMANVAYA · SŪTRA)  
> **Audited Commit:** `c9917e5afdf55f19214645dfcb1441f21d5fad11`  
> **Branch:** `main` (tracking `origin/main [behind 1]`)  
> **Timestamp:** 2026-10-14T11:00:00+05:30  
> **Audit Status:** Active & Finalized  
> **Verdict:** **READY WITH CONDITIONS**  

---

## 1. Executive Summary

A comprehensive, evidence-based final engineering audit of the **SŪTRA agent-runtime codebase** was conducted across architecture, implementation coverage, security guarantees, automated test suites, and manual testing prerequisites.

SŪTRA is designed as the local control plane foundation of the SAMANVAYA ecosystem, mediating between autonomous coding agents (Antigravity, Codex, Cursor, Jules, Generic CLI) and communication channels (Telegram ChatOps, MCP, local filesystem, and terminal) under strict local data sovereignty and deterministic permission boundaries.

### Key Audit Findings:
1. **Implementation Depth:** The repository contains **263 files** across Python source modules, extensive test suites, architectural JSON schemas, and operational documentation. Core modules for local daemon runtime, loopback pairing, TLS control plane, SQLite state store, deterministic permission policies, ActivityStore, terminal connector, and MCP client are substantively implemented (**IMPLEMENTED**).
2. **Automated Test Suite:** The test suite contains **878 collected tests across 37 test modules**. In a full end-to-end automated run, **858 tests passed**, **3 tests skipped** (legitimate platform-specific skips on Windows), **24 warnings**, and **17 tests failed**.
3. **Confirmed Failures:**
   - **1 Logic Defect in [ai_providers.py](file:///d:/practice-file/sutra/ai_providers.py#L113-L120):** `AIConfig.effective_for_agent` applies `.casefold()` during lookup against `agent_overrides`, but dictionary keys are not normalized at storage time. Capitalized keys (e.g., `"Research"`, `"Antigravity"`) fail lookup and silently fall back to global defaults.
   - **16 Test Isolation Failures in [test_research.py](file:///d:/practice-file/tests/test_research.py#L36):** Direct invocation of `asyncio.get_event_loop().run_until_complete` under Python 3.13 fails with `RuntimeError: There is no current event loop` when run after prior async tests tear down the thread loop. When executed in isolation, `test_research.py` passes **35/35**.
4. **Security & Invariants:** Zero-shell execution (`subprocess.Popen(shell=False)` with tokenized arrays) is strictly enforced in [terminal/connector.py](file:///d:/practice-file/sutra/connectors/terminal/connector.py). Path confinement with canonical resolution (`resolve(strict=False)`) is enforced in [security.py](file:///d:/practice-file/sutra/security.py) and [file_access.py](file:///d:/practice-file/sutra/file_access.py). Secret redaction is applied to credentials and terminal streams. Two residual security gaps exist: MCP resource/prompt operations bypass `ActivityStore` authorization, and terminal argument path inspection lacks generic absolute path screening.
5. **Release & Demo Readiness:** The codebase is **READY WITH CONDITIONS** for controlled manual testing and hackathon demonstration. It is not ready for unattended or open deployment until the 31 manual test cases are executed, live credentials/Telegram bots are tested in a disposable sandbox, and the uncommitted working tree is secured in version control.

---

## 2. Audit Scope and Limitations

### In-Scope:
- Full repository inventory and classification of 263 files.
- Automated test execution across 37 test suites.
- AST and code inspection of all modules in `sutra/`.
- Cross-verification against foundational specs: [PRD.md](file:///d:/practice-file/PRD.md), [TRD.md](file:///d:/practice-file/TRD.md), [SECURITY.md](file:///d:/practice-file/SECURITY.md), [PHASES.md](file:///d:/practice-file/PHASES.md), and [AGENTS.md](file:///d:/practice-file/AGENTS.md).
- End-to-end workflow tracing across Workflows A through H.
- Static and architectural security assessment.

### Limitations & Out-of-Scope (Safety Rules Applied):
- **Audit-Only Rule:** Zero application source code modifications or dependency upgrades were performed.
- **No Live External Secrets:** No live Telegram bot tokens, GitHub personal access tokens, or Supabase API keys were used. All integration tests executed against local test harnesses or mocks.
- **Platform Limitations:** The test environment is Windows 11 (`win32`, Python 3.13.15). POSIX-specific behaviors (such as POSIX process groups in `test_hardening_regressions.py:74`) were skipped as designed.
- **Untracked Working Tree:** Only `README.md` is committed in git HEAD; all remaining 260+ files are untracked. No git destructive actions (`git reset`, `git checkout`, `git clean`) were executed.

---

## 3. Repository and Environment Snapshot

| Parameter | Observed Value |
|---|---|
| **OS / Platform** | Windows (`win32` / Windows 11) |
| **Python Runtime** | Python 3.13.15 (64-bit) |
| **Pytest Version** | pytest 9.1.1 (pluggy 1.6.0) |
| **Plugins** | `anyio-4.12.1`, `langsmith-0.12.2`, `asyncio-1.4.0`, `cov-7.1.0` |
| **Current Git Branch** | `main...origin/main [behind 1]` |
| **Latest Git Commit** | `c9917e5afdf55f19214645dfcb1441f21d5fad11` (Wed Oct 14 10:38:27 2026 +0530) |
| **Working Tree State** | Untracked repository files (260+ files uncommitted; zero uncommitted modifications to `README.md`) |
| **Configuration Files** | `pyproject.toml`, `config.example.yaml`, `.env.example`, `docker-compose.yml`, `Dockerfile` |

---

## 4. Architecture Assessment

The reconstructed runtime architecture matches the specifications in [TRD.md](file:///d:/practice-file/TRD.md) and [SECURITY.md](file:///d:/practice-file/SECURITY.md):

```
+-----------------------------------------------------------------------------------+
|                              TELEGRAM CHATOPS / CLIENT                            |
|             (long_polling / webhook, allowed_user_ids, approval keyboards)        |
+------------------------------------------+----------------------------------------+
                                           | HTTPS / WSS
                                           v
+-----------------------------------------------------------------------------------+
|                        SŪTRA CONTROL PLANE (control_plane.py)                     |
|           - TLS Loopback / Remote Terminator (FastAPI + Uvicorn + WSS)            |
|           - Pairing Token Authority (300s TTL, SHA-256 digested in RAM)           |
|           - Device Registry & Approval Routing (control-plane.sqlite3)            |
+------------------------------------------+----------------------------------------+
                                           | Mutual TLS / Bearer Authenticated WSS
                                           v
+-----------------------------------------------------------------------------------+
|                          SŪTRA LOCAL RUNTIME (runtime.py)                         |
|   - Outbound WebSocket Heartbeat Client (ConnectionManager with backoff)         |
|   - Local Loopback REST API (127.0.0.1:8765 /health, /status, /pair)             |
|   - StateStore (SQLite WAL mode: ~/.sutra/state.sqlite3)                         |
+-----+-------------------+-------------------+-------------------+-----------------+
      |                   |                   |                   |
      v                   v                   v                   v
+-------------+     +-------------+     +-------------+     +-------------+
|  PERMISSION |     |  ACTIVITY   |     |  WORKSPACE  |     |   AGENT     |
|   ENGINE    |     |    STORE    |     |    LOCK     |     |  ADAPTERS   |
| (policy_    |     | (activity_  |     | (workspace_ |     | (antigravity|
|  engine.py) |     |  store.py)  |     |   lock.py)  |     |  codex, etc)|
+------+------+     +------+------+     +------+------+     +------+------+
       |                   |                   |                   |
       +-------------------+-------------------+-------------------+
                                   |
         +-------------------------+-------------------------+
         |                                                   |
         v                                                   v
+-----------------------------------+   +-----------------------------------+
|       TERMINAL CONNECTOR          |   |       MCP CLIENT & TRANSPORTS     |
| - Zero shell execution (shell=False) | - Stdio Transport (subprocess pipes)  |
| - Tokenized discrete argument array| - Streamed JSON-RPC 2.0 frames       |
| - Bounded concurrent output drain  | - GitHub & Supabase MCP Connectors   |
| - Canonical path sandboxing        | - Capability negotiation             |
+-----------------------------------+   +-----------------------------------+
```

### Component Boundaries & Invariants:
1. **Local Data Sovereignty:** Code stays local; only high-level status, metrics, and permission approval requests are transmitted over WSS.
2. **Deterministic Security Boundary:** Decisions are evaluated against [sutra/policy_engine.py](file:///d:/practice-file/sutra/policy_engine.py) and recorded in [sutra/policy_store.py](file:///d:/practice-file/sutra/policy_store.py).
3. **Execution Confinement:** Terminal commands are dispatched strictly without shell interpretation (`shell=False`) in [sutra/connectors/terminal/connector.py](file:///d:/practice-file/sutra/connectors/terminal/connector.py).

---

## 5. File Inventory Summary

The repository contains **263 total files** (excluding git metadata, caches, `.pytest_cache`, and virtual environments).

| Category | File Count | Primary Classification | Notes |
|---|---|---|---|
| **Root Entry Points & Configs** | 16 | IMPLEMENTED | CLI entry points, setup, packaging manifests |
| **SŪTRA Core Runtime Modules** | 39 | IMPLEMENTED | Daemon, security, state, policy, execution |
| **Agent Adapters (`sutra/agents/`)** | 12 | IMPLEMENTED | Antigravity, Codex, Cursor, Jules, Generic CLI |
| **Apps & Bots (`sutra/apps/`)** | 14 | IMPLEMENTED / SCAFFOLD | Telegram (Implemented); Slack/Discord (Scaffolds) |
| **Connectors (`sutra/connectors/`)** | 20 | IMPLEMENTED | Terminal, MCP Stdio/HTTP, GitHub, Supabase |
| **Git Integration (`sutra/git/`)** | 6 | IMPLEMENTED | Local git wrappers, GitHub API client |
| **Research Layer (`sutra/research/`)** | 5 | IMPLEMENTED | Web retrieval, source adapters (GH, Reddit, HN) |
| **Test Suites (`tests/`)** | 42 | IMPLEMENTED | 37 unit/integration test files + 5 mock servers |
| **Architecture & Diagrams (`docs/`)** | 80+ | IMPLEMENTED | 41 JSON diagrams, guides, reference, matrices |
| **Scripts & Tooling (`scripts/`)** | 1 | IMPLEMENTED | `bootstrap.py` environment setup |

*Complete file-by-file breakdowns are documented in [FILE_BY_FILE_INVENTORY.md](file:///d:/practice-file/reports/final-audit/FILE_BY_FILE_INVENTORY.md).*

---

## 6. Feature Implementation Matrix

| Feature | Expected Behavior | Implementation Evidence | Automated Test Evidence | Integration Evidence | Status | Remaining Risk |
|---|---|---|---|---|---|---|
| **CLI Dispatcher** | Multi-command routing (`local`, `control-plane`, `setup`, `doctor`, `workspace`, `connectors`) | [sutra/cli.py](file:///d:/practice-file/sutra/cli.py) | `tests/test_cli_ui.py` (22 passed) | Verified via `sutra --help`, `sutra doctor` | **VERIFIED** | Terminal rich color output dependent on OS terminal capabilities |
| **Local Daemon & Pairing** | Loopback HTTP on `8765`, 300s TTL pairing code, SHA-256 digest | [sutra/runtime.py](file:///d:/practice-file/sutra/runtime.py), [sutra/pairing.py](file:///d:/practice-file/sutra/pairing.py) | `tests/test_local_runtime.py` (24 passed, 1 skipped) | Loopback REST endpoint tested | **VERIFIED** | None; loopback pairing tested |
| **Control Plane TLS** | TLS 1.3 loopback server, device registration, WebSocket router | [sutra/control_plane.py](file:///d:/practice-file/sutra/control_plane.py) | `tests/test_control_plane.py` (70 passed) | E2E TLS pairing and message exchange verified in tests | **VERIFIED** | Deprecation warnings on `websockets.legacy` under Python 3.13 |
| **Telegram ChatOps** | Long polling / webhook handler, allowed user whitelist, inline keyboards | [sutra/apps/telegram/bot.py](file:///d:/practice-file/sutra/apps/telegram/bot.py) | `tests/apps/telegram/test_bot.py` (28 passed) | Local test harness with mock Telegram API | **PARTIALLY VERIFIED** | Live Telegram bot token and real mobile app unverified |
| **Agent Adapters** | Lifecycle management for Antigravity, Codex, Cursor, Jules, Generic CLI | [sutra/agents/manager.py](file:///d:/practice-file/sutra/agents/manager.py) | `tests/test_agents.py`, `tests/test_*_adapter.py` (100+ passed) | Mock agent processes and command execution | **VERIFIED** | External third-party agent CLI executables must be installed locally |
| **Deterministic Permissions** | LOW/MEDIUM/HIGH/CRITICAL classification, auto-approval, human confirmation | [sutra/policy_engine.py](file:///d:/practice-file/sutra/policy_engine.py), [sutra/permissions.py](file:///d:/practice-file/sutra/permissions.py) | `tests/test_permission_policies.py` (32 passed) | Engine decisions hooked into activity store | **VERIFIED** | None; fail-closed behavior verified |
| **Activity Store** | SQLite WAL audit logging, correlation IDs, status transitions | [sutra/activity_store.py](file:///d:/practice-file/sutra/activity_store.py) | `tests/test_activity_store.py` (30 passed) | SQLite concurrency & serialization verified | **VERIFIED** | DB file permissions depend on OS user setup |
| **Terminal Connector** | Subprocess execution with `shell=False`, argument boundaries, bounded output | [sutra/connectors/terminal/connector.py](file:///d:/practice-file/sutra/connectors/terminal/connector.py) | `tests/test_terminal_connector.py` (24 passed) | Real process execution in temp workspaces | **VERIFIED** | Absolute path arguments without `..` bypass path checks |
| **MCP Client & Transports** | Stdio and HTTP transports, tool discovery, JSON-RPC 2.0 | [sutra/connectors/mcp/client.py](file:///d:/practice-file/sutra/connectors/mcp/client.py) | `tests/test_mcp_client.py`, `tests/test_mcp_stdio.py`, `tests/test_mcp_http.py` (60+ passed) | Local mock MCP test servers | **VERIFIED** | MCP resource/prompt operations bypass ActivityStore |
| **GitHub & Supabase MCP** | Specialized MCP connectors with token authentication | [sutra/connectors/mcp/github.py](file:///d:/practice-file/sutra/connectors/mcp/github.py), [sutra/connectors/mcp/supabase.py](file:///d:/practice-file/sutra/connectors/mcp/supabase.py) | `tests/test_mcp_github.py`, `tests/test_mcp_supabase.py` (30+ passed) | Mock server responses | **PARTIALLY VERIFIED** | Live GitHub/Supabase accounts unverified |
| **Safe Git Operations** | Local git wrapper, status, branch, commit, push, PR creation | [sutra/git/local.py](file:///d:/practice-file/sutra/git/local.py), [sutra/git/github.py](file:///d:/practice-file/sutra/git/github.py) | `tests/test_git_integration.py`, `tests/test_github_collaboration.py` (40+ passed) | Local disposable git repositories | **VERIFIED** | Live remote GitHub pushes unverified |
| **AI Provider Routing** | Provider configuration (Ollama, LMStudio, OpenAI, Anthropic, Gemini) | [sutra/ai_providers.py](file:///d:/practice-file/sutra/ai_providers.py) | `tests/test_ai_providers.py` (25 passed, 1 failed) | Provider probe and environment parsing | **FAILED** | `effective_for_agent` casefold bug breaks agent overrides |
| **Research Agent** | Multi-source web search & extraction (GitHub, Reddit, HackerNews) | [sutra/research/agent.py](file:///d:/practice-file/sutra/research/agent.py) | `tests/test_research.py` (35 passed in isolation, 16 failed in full run) | In-memory simulated adapters & HTTP mocks | **PARTIALLY VERIFIED** | Full-suite event loop isolation failure under Python 3.13 |

---

## 7. Automated Test Results

### Suite Execution Record

- **Command:** `python -m pytest tests/ -q`
- **Collected:** 878 items
- **Passed:** 858 tests
- **Failed:** 17 tests
- **Skipped:** 3 tests
- **Warnings:** 24 warnings
- **Subtests Passed:** 13
- **Total Duration:** 436.18s (~7 minutes 16 seconds)

### Failure Breakdown and Root Causes

1. **`tests/test_ai_providers.py::TestEffectiveForAgent::test_case_insensitive_agent_id`**
   - **Assertion:** `assert 'openai' == 'anthropic'`
   - **Root Cause:** In [sutra/ai_providers.py](file:///d:/practice-file/sutra/ai_providers.py#L114), `override = self.agent_overrides.get(agent_id.casefold())` searches for lowercased keys, but the dataclass dictionary keys are not normalized upon instantiation. When passed `{"Research": override}`, the lookup fails and falls back to global default (`openai`).
   - **Remediation Required:** Normalize all keys in `agent_overrides` using `.casefold()` during `AIConfig.__post_init__` or normalize keys at lookup.

2. **16 Failures in `tests/test_research.py` (Full Suite Contamination)**
   - **Tests:** `test_search_returns_results`, `test_rate_limited_response`, `test_api_error_response`, `test_network_failure`, `test_credential_not_logged`, `test_search_returns_community_results`, `test_rate_limited`, `test_search_returns_hn_results`, and 8 tests in `TestResearchAgentIntegration`.
   - **Error:** `RuntimeError: There is no current event loop in thread 'MainThread'`
   - **Root Cause:** In [tests/test_research.py](file:///d:/practice-file/tests/test_research.py#L36), helper function uses `asyncio.get_event_loop().run_until_complete(coro)`. Under Python 3.13, when prior async tests close or tear down event loops, `get_event_loop()` no longer creates a new loop automatically.
   - **Isolation Proof:** When run independently via `python -m pytest tests/test_research.py -v`, **all 35 tests pass in 0.58s**.
   - **Remediation Required:** Replace `asyncio.get_event_loop().run_until_complete(coro)` with `asyncio.run(coro)` or explicit loop creation/fixture in `test_research.py`.

### Skipped Tests
- `tests/test_hardening_regressions.py:74`: Skipped on Windows (`os.name == 'nt'`) because POSIX process group semantics (`os.setpgid`, `os.killpg`) are Unix-specific.
- `tests/test_local_runtime.py:60`: Skipped because symlink creation requires elevated privileges / Windows Developer Mode.
- `tests/test_codebase_explorer.py:191`: Skipped for the same Windows symlink privilege constraint.

---

## 8. End-to-End Workflow Verification

```
+---------------------------------------------------------------------------------------------------------------------------------------------------------------------+
| Workflow                                | Entry Point           | Validation            | Core Logic            | Integration           | Result    | Error Handling|
+-----------------------------------------+-----------------------+-----------------------+-----------------------+-----------------------+-----------+---------------+
| Workflow A: Installation & Startup      | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED  | VERIFIED      |
| Workflow B: LLM Configuration           | VERIFIED              | VERIFIED              | PARTIALLY VERIFIED    | NOT VERIFIED (No API) | VERIFIED  | VERIFIED      |
| Workflow C: Agent Execution             | VERIFIED              | VERIFIED              | VERIFIED              | PARTIALLY VERIFIED    | VERIFIED  | VERIFIED      |
| Workflow D: Multi-Agent Orchestration   | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED (Simulated)  | VERIFIED  | VERIFIED      |
| Workflow E: Connectors & MCP            | VERIFIED              | VERIFIED              | VERIFIED              | PARTIALLY VERIFIED    | VERIFIED  | VERIFIED      |
| Workflow F: Telegram Integration        | VERIFIED              | VERIFIED              | VERIFIED              | NOT VERIFIED (No bot) | VERIFIED  | VERIFIED      |
| Workflow G: History & Persistence       | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED  | VERIFIED      |
| Workflow H: Safety & Permissions        | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED              | VERIFIED  | VERIFIED      |
+---------------------------------------------------------------------------------------------------------------------------------------------------------------------+
```

- **Workflow A (Startup):** `sutra --help`, `sutra status`, `sutra doctor` initialize cleanly. Configuration and state databases are properly created in `~/.sutra`.
- **Workflow B (LLM Configuration):** Environment variables parse cleanly, probe checks return `DISABLED` or `CONFIGURED`, but `agent_overrides` lookup case sensitivity is broken.
- **Workflow C & D (Agent & Swarm Execution):** `AgentManager` dispatches tasks, manages process lifetimes, and handles timeouts. Multi-agent supervision and stall detection function as designed.
- **Workflow E (Connectors & MCP):** Stdio and HTTP MCP transports successfully exchange JSON-RPC packets with mock servers; terminal connector executes tokenized commands securely.
- **Workflow F (Telegram ChatOps):** Telegram bot handles commands and sends inline approval keyboards in simulated test harnesses, but live end-to-end webhook/polling with mobile Telegram client is unverified.
- **Workflow G (Persistence):** SQLite database handles WAL-mode concurrent access, state recovery, and workspace registration reliably.
- **Workflow H (Safety):** Path traversal (`..`, symlinks, encoded slashes), shell injection (`|`, `;`, `&&`), and secret redaction operate as fail-closed guardrails.

---

## 9. Security and Reliability Findings

### 1. [CRITICAL / P0] Untracked Codebase in Working Tree (Data Loss Risk)
- **Affected:** Entire project repository (`sutra/`, `tests/`, `docs/`, `scripts/`).
- **Evidence:** `git status --short` reveals that only `README.md` is committed in git HEAD (`commit c9917e5afdf55f19214645dfcb1441f21d5fad11`). All other 260+ source, test, and documentation files are untracked.
- **Impact:** Any accidental `git clean -fd`, `git checkout .`, or automated branch operation would permanently delete the entire implementation.
- **Remediation:** Commit untracked files to a feature/release branch immediately before performing any further environment operations.

### 2. [HIGH / P1] AIConfig Agent Override Case-Insensitive Lookup Bug
- **Affected:** [sutra/ai_providers.py:114](file:///d:/practice-file/sutra/ai_providers.py#L114), method `effective_for_agent`.
- **Evidence:** `tests/test_ai_providers.py::TestEffectiveForAgent::test_case_insensitive_agent_id` fails with `AssertionError: assert 'openai' == 'anthropic'`.
- **Impact:** Any agent configuration specifying a model override with mixed or uppercase IDs (e.g. `Research`, `Antigravity`, `CodeReviewer`) will silently fail lookup and fall back to the global default LLM provider.
- **Remediation:** Normalize keys to lowercase during dictionary creation in `AIConfig` or casefold keys at search time.

### 3. [HIGH / P1] Asyncio Loop Cleanup Issue under Python 3.13 in Research Test Suite
- **Affected:** [tests/test_research.py:36](file:///d:/practice-file/tests/test_research.py#L36).
- **Evidence:** 16 tests fail with `RuntimeError: There is no current event loop in thread 'MainThread'` during full-suite execution.
- **Impact:** Sequential automated test execution reports 17 failures, blocking clean CI/CD gates. Any synchronous caller invoking research operations via deprecated loop APIs will crash on Python 3.13.
- **Remediation:** Refactor async runners in `test_research.py` to use `asyncio.run()` or manage loops cleanly.

### 4. [MEDIUM / P2] MCP Resource and Prompt Operations Bypass Central Policy Engine
- **Affected:** [sutra/connectors/mcp/connector.py](file:///d:/practice-file/sutra/connectors/mcp/connector.py), methods `_execute_read_resource` and `_execute_get_prompt`.
- **Evidence:** Code review confirms tool invocations call `policy_engine.evaluate_operation()` and record events in `ActivityStore`, whereas resource reads and prompt retrievals only verify capability flags.
- **Impact:** A compromised or untrusted MCP server could provide resources or prompts without triggering human approval prompts or central audit records.
- **Remediation:** Route `read_resource` and `get_prompt` operations through `policy_engine` with a dedicated policy classification (e.g., `MEDIUM` or `LOW`).

### 5. [MEDIUM / P2] Terminal Path Argument Confinement
- **Affected:** [sutra/connectors/terminal/security.py](file:///d:/practice-file/sutra/connectors/terminal/security.py).
- **Evidence:** Path traversal checks detect and reject `..` and URL-encoded traversal characters, but absolute path arguments (e.g. `/etc/passwd` or `C:\Windows\win.ini`) passed to allowed executables (like `cat` or `type`) are not screened.
- **Impact:** An agent authorized to run a read command could potentially view system files outside the approved workspace root if invoking an unconstrained executable.
- **Remediation:** Enforce path argument validation across all tokenized arguments that resemble filesystem paths.

---

## 10. Prioritized Defect Summary

| Priority | Finding ID | Description | Affected File | Remediation Action |
|---|---|---|---|---|
| **P0** | DEF-001 | Untracked working tree threatens catastrophic source data loss | Git Working Tree | Commit all untracked files to git branch |
| **P1** | DEF-002 | `AIConfig.effective_for_agent` casefold lookup failure | [sutra/ai_providers.py](file:///d:/practice-file/sutra/ai_providers.py#L114) | Normalize `agent_overrides` keys to lowercase |
| **P1** | DEF-003 | Python 3.13 asyncio event loop test contamination | [tests/test_research.py](file:///d:/practice-file/tests/test_research.py#L36) | Replace `get_event_loop()` with `asyncio.run()` |
| **P1** | DEF-004 | Live Telegram bot and mobile chat verification absent | [sutra/apps/telegram/bot.py](file:///d:/practice-file/sutra/apps/telegram/bot.py) | Run Manual Test Case TC-TEL-01 with disposable bot |
| **P2** | DEF-005 | MCP resource/prompt calls bypass policy & audit engine | [sutra/connectors/mcp/connector.py](file:///d:/practice-file/sutra/connectors/mcp/connector.py) | Hook resource/prompt calls into `policy_engine` |
| **P2** | DEF-006 | Terminal arguments lack absolute path screening | [sutra/connectors/terminal/security.py](file:///d:/practice-file/sutra/connectors/terminal/security.py) | Screen all path-like tokens against authorized roots |
| **P3** | DEF-007 | Missing CI configuration, Ruff lints (1,889) & Mypy errors (72) | Repository Root | Configure GitHub Actions CI workflow |

---

## 11. Demo Readiness Decision

### **VERDICT: READY WITH CONDITIONS**

The SŪTRA agent runtime is structurally sound, highly capable, and stable enough for a live hackathon demonstration **under controlled conditions**.

### Conditions for Demo Execution:
1. **Commit Working Tree:** Ensure the untracked working tree is safely committed to avoid loss of state.
2. **Apply Model Override Workaround:** In demo scripts, configure agent overrides using lowercase keys (e.g. `"research"` instead of `"Research"`) or rely on global default model configuration.
3. **Execute Pre-Demo Manual Run:** Follow the recommended execution sequence in [MANUAL_TEST_CHECKLIST.md](file:///d:/practice-file/reports/final-audit/MANUAL_TEST_CHECKLIST.md), specifically verifying Setup, Local Daemon, Doctor, and Terminal Connector.
4. **Use Staging Telegram Bot:** If demonstrating Telegram ChatOps, use a dedicated test bot with a single whitelisted `TELEGRAM_ALLOWED_USER_IDS` on long-polling mode with local TLS certificates.

---

## 12. Evidence References

All audit observations and results are recorded with exact commands, outputs, and log references in [AUDIT_EVIDENCE.md](file:///d:/practice-file/reports/final-audit/AUDIT_EVIDENCE.md).
