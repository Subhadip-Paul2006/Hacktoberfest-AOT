# SŪTRA — Comprehensive File-by-File Inventory

> **Document:** `FILE_BY_FILE_INVENTORY.md`  
> **Repository:** `sutra-local` (SAMANVAYA · SŪTRA)  
> **Total Files Inventoried:** 263 files  
> **Audited Commit:** `c9917e5afdf55f19214645dfcb1441f21d5fad11`  
> **Classification Legend:**  
> - **IMPLEMENTED**: Substantive, working implementation exists.  
> - **PARTIALLY IMPLEMENTED**: Substantive code exists, but some planned behavior is missing or incomplete.  
> - **PLACEHOLDER**: Structural scaffold, TODO, or NotImplemented stubs.  
> - **MOCKED**: Implementation relies on mock/simulator responses.  
> - **GENERATED**: Build, compiler, or metadata artifact.  
> - **POSSIBLY UNUSED**: No active runtime or test references detected.  
> - **OBSOLETE / DUPLICATE**: Legacy or superseded code.  

---

## 1. Root Configuration, Entry Points, and Packaging

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`pyproject.toml`](file:///d:/practice-file/pyproject.toml) | TOML | IMPLEMENTED | Package build definition for `sutra-local` (v0.1.0); CLI script entry point `sutra = sutra.cli:main` | `setuptools>=69`, `fastapi`, `uvicorn`, `websockets`, `rich`, `keyring` | Packaging / import tests | No CI or lint/type-check tools declared in dev dependencies |
| [`sutra_entry.py`](file:///d:/practice-file/sutra_entry.py) | Python | IMPLEMENTED | Direct CLI script entry point invoking `sutra.cli.main()` | [sutra/cli.py](file:///d:/practice-file/sutra/cli.py) | `tests/test_cli_ui.py` | None |
| [`agentbridge_entry.py`](file:///d:/practice-file/agentbridge_entry.py) | Python | OBSOLETE / DUPLICATE | Backward-compatibility shim forwarding to `sutra_entry.py` | [sutra_entry.py](file:///d:/practice-file/sutra_entry.py) | Compatibility tests | Retained for legacy backwards compatibility |
| [`config.example.yaml`](file:///d:/practice-file/config.example.yaml) | YAML | IMPLEMENTED | Reference YAML configuration template for local runtime and control plane | `sutra/cli.py` | Manual verification | Reference only |
| [`.env.example`](file:///d:/practice-file/.env.example) | Shell/Env | IMPLEMENTED | Comprehensive template for all environment variables (`SUTRA_*`, `TELEGRAM_*`) | Read by `python-dotenv` | `tests/test_ai_providers.py` | Contains no live secrets; well documented |
| [`Dockerfile`](file:///d:/practice-file/Dockerfile) | Docker | IMPLEMENTED | Container image definition for running SŪTRA in containerized environments | Python 3.11-slim base | Build checks | SŪTRA runs natively without mandatory Docker |
| [`docker-compose.yml`](file:///d:/practice-file/docker-compose.yml) | YAML | IMPLEMENTED | Orchestration configuration for local runtime, control plane, and mock servers | `Dockerfile` | Multi-container tests | Optional convenience tooling |
| [`.dockerignore`](file:///d:/practice-file/.dockerignore) | Text | IMPLEMENTED | Excludes git metadata, caches, `.env`, virtualenvs from Docker context | Docker daemon | Build checks | Properly excludes sensitive files |
| [`.gitignore`](file:///d:/practice-file/.gitignore) | Text | IMPLEMENTED | Standard git ignore rules for Python, virtual environments, dev certs, and `.env` | Git engine | Git checks | Well configured |
| [`.gitattributes`](file:///d:/practice-file/.gitattributes) | Text | IMPLEMENTED | Line ending and diff configurations | Git engine | Git checks | Enforces LF / text attributes |
| [`PRD.md`](file:///d:/practice-file/PRD.md) | Markdown | IMPLEMENTED | Authoritative Product Requirements Document defining SAMANVAYA · SŪTRA | Foundational spec | Document review | Complete, approved specification |
| [`TRD.md`](file:///d:/practice-file/TRD.md) | Markdown | IMPLEMENTED | Authoritative Technical Requirements Document | Architectural spec | Document review | Technical foundation for implementation |
| [`SECURITY.md`](file:///d:/practice-file/SECURITY.md) | Markdown | IMPLEMENTED | Authoritative Security Architecture & Threat Model | Security specification | Threat audit | Core security invariants enforced |
| [`PHASES.md`](file:///d:/practice-file/PHASES.md) | Markdown | IMPLEMENTED | 13-Phase Implementation Roadmap | Roadmap tracker | Progress audit | Fully tracked through Phase 12 |
| [`AI_INSTRUCTIONS.md`](file:///d:/practice-file/AI_INSTRUCTIONS.md) | Markdown | IMPLEMENTED | Operating rules and code style standards for AI coding agents | Agent guidance | Protocol audit | Strictly aligned with AGENTS.md |
| [`AGENTS.md`](file:///d:/practice-file/AGENTS.md) | Markdown | IMPLEMENTED | Mandatory operational protocols and Graphify rules | Antigravity, Cursor, Codex | Protocol audit | Active and enforced |
| [`README.md`](file:///d:/practice-file/README.md) | Markdown | IMPLEMENTED | Comprehensive project overview, quick start, architecture, and feature guide | Public documentation | Documentation audit | Single committed file in git HEAD |
| [`SETUP.md`](file:///d:/practice-file/SETUP.md) | Markdown | IMPLEMENTED | Detailed environment bootstrap, TLS generation, and quickstart instructions | User guide | Setup verification | Accurate and verified |
| [`USAGE.md`](file:///d:/practice-file/USAGE.md) | Markdown | IMPLEMENTED | Command reference and user journey guide | User documentation | Manual testing | Matches CLI capabilities |
| [`CODE_OF_CONDUCT.md`](file:///d:/practice-file/CODE_OF_CONDUCT.md) | Markdown | IMPLEMENTED | Standard Contributor Covenant Code of Conduct | Project governance | Compliance | Standard document |
| [`AUDIT_IMPLEMENTATION_REPORT.md`](file:///d:/practice-file/AUDIT_IMPLEMENTATION_REPORT.md) | Markdown | IMPLEMENTED | Prior audit report tracking milestone implementation status | Historical report | Audit verification | Historical record |

---

## 2. SŪTRA Core Runtime Modules (`sutra/`)

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`sutra/__init__.py`](file:///d:/practice-file/sutra/__init__.py) | Python | IMPLEMENTED | Package initializer; exports version `__version__ = "0.1.0"` | Package consumers | Import tests | None |
| [`sutra/__main__.py`](file:///d:/practice-file/sutra/__main__.py) | Python | IMPLEMENTED | Module execution hook (`python -m sutra`) invoking `cli.main()` | `sutra.cli` | `tests/test_cli_ui.py` | None |
| [`sutra/cli.py`](file:///d:/practice-file/sutra/cli.py) | Python | IMPLEMENTED | Command-line interface with subcommands (`local`, `control-plane`, `setup`, `doctor`, `workspace`, `connectors`) | `sutra.ui`, `sutra.runtime`, `sutra.control_plane` | `tests/test_cli_ui.py` | Extensive, well-tested CLI |
| [`sutra/ui.py`](file:///d:/practice-file/sutra/ui.py) | Python | IMPLEMENTED | Terminal interface rendering using Rich, animated startup spinners, status panels | `rich.console`, `rich.panel` | `tests/test_cli_ui.py` | Graceful fallback when unicode unsupported |
| [`sutra/runtime.py`](file:///d:/practice-file/sutra/runtime.py) | Python | IMPLEMENTED | Local runtime daemon coordinating loopback REST API, WebSocket uplink, and agent execution | `sutra.pairing`, `sutra.connection`, `sutra.state` | `tests/test_local_runtime.py` | Robust lifecycle management |
| [`sutra/control_plane.py`](file:///d:/practice-file/sutra/control_plane.py) | Python | IMPLEMENTED | FastAPI-based TLS control plane for remote pairing, device approval, and WebSocket message routing | `fastapi`, `uvicorn`, `websockets` | `tests/test_control_plane.py` | 70 passed tests; uses legacy websockets API |
| [`sutra/connection.py`](file:///d:/practice-file/sutra/connection.py) | Python | IMPLEMENTED | `ConnectionManager` handling outbound WebSocket connection to control plane with backoff reconnect | `websockets` | `tests/test_connection_pool.py` | Monitored heartbeat ping/pong |
| [`sutra/connectivity.py`](file:///d:/practice-file/sutra/connectivity.py) | Python | IMPLEMENTED | Network connectivity monitoring, gateway reachability, and DNS probing | `socket`, `httpx` | `tests/test_resilience.py` | Resilient error handling |
| [`sutra/pairing.py`](file:///d:/practice-file/sutra/pairing.py) | Python | IMPLEMENTED | Loopback pairing handshake; generates 300s TTL pairing code; SHA-256 digested in RAM | `hashlib`, `secrets` | `tests/test_local_runtime.py` | Zero persistent plaintext secrets |
| [`sutra/remote_pairing.py`](file:///d:/practice-file/sutra/remote_pairing.py) | Python | IMPLEMENTED | Remote pairing protocol implementation with control plane | `sutra.control_plane` | `tests/test_control_plane.py` | Clean mutual verification |
| [`sutra/state.py`](file:///d:/practice-file/sutra/state.py) | Python | IMPLEMENTED | SQLite state store (`~/.sutra/state.sqlite3`) for device identity, approved workspaces, and bearer tokens | `sqlite3` (WAL mode) | `tests/test_local_runtime.py` | Thread-safe with proper connection closing |
| [`sutra/security.py`](file:///d:/practice-file/sutra/security.py) | Python | IMPLEMENTED | Path confinement validation, URL decoding, symlink resolution, access root verification | `pathlib.Path.resolve` | `tests/test_system_hardening.py` | 4-pass recursive URL decoding |
| [`sutra/file_access.py`](file:///d:/practice-file/sutra/file_access.py) | Python | IMPLEMENTED | Sandboxed file system access operations (read, preview, metadata) | `sutra.security` | `tests/test_codebase_explorer.py` | Strict sandbox containment |
| [`sutra/file_limits.py`](file:///d:/practice-file/sutra/file_limits.py) | Python | IMPLEMENTED | File size and preview boundaries (max file size, head/tail slicing limits) | `sutra.file_access` | `tests/test_large_file_semantics.py` | Prevents memory exhaustion |
| [`sutra/chunking.py`](file:///d:/practice-file/sutra/chunking.py) | Python | IMPLEMENTED | Telegram text chunking preserving markdown code fences and byte constraints | `sutra.apps.telegram` | `tests/test_system_hardening.py` | Code fences preserved across chunks |
| [`sutra/credentials.py`](file:///d:/practice-file/sutra/credentials.py) | Python | IMPLEMENTED | Secure credential storage using system `keyring` or encrypted fallback file | `keyring`, `os.environ` | `tests/test_ai_providers.py` | Masks secrets from logs |
| [`sutra/redaction.py`](file:///d:/practice-file/sutra/redaction.py) | Python | IMPLEMENTED | Regex-based secret redaction engine scrubbing API tokens, private keys, passwords | `re` | `tests/test_system_hardening.py` | Replaces secrets with `[REDACTED]` |
| [`sutra/permissions.py`](file:///d:/practice-file/sutra/permissions.py) | Python | IMPLEMENTED | Permission models, risk level classifications (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) | `dataclasses` | `tests/test_permission_policies.py` | CRITICAL operations can never auto-approve |
| [`sutra/policy_engine.py`](file:///d:/practice-file/sutra/policy_engine.py) | Python | IMPLEMENTED | Deterministic evaluation of proposed operations against configured rules | `sutra.permissions` | `tests/test_permission_policies.py` | Fail-closed decision logic |
| [`sutra/policy_store.py`](file:///d:/practice-file/sutra/policy_store.py) | Python | IMPLEMENTED | Persistent SQLite policy store for human authorization decisions and rule caching | `sqlite3` | `tests/test_permission_policies.py` | Audit records preserved |
| [`sutra/activity_store.py`](file:///d:/practice-file/sutra/activity_store.py) | Python | IMPLEMENTED | SQLite audit log tracking every tool execution, request ID, duration, and status | `sqlite3` | `tests/test_activity_store.py` | Complete chronological trace |
| [`sutra/task_executor.py`](file:///d:/practice-file/sutra/task_executor.py) | Python | IMPLEMENTED | Per-project serial task execution queue with global concurrency limit | `asyncio.Queue` | `tests/test_task_executor.py` | Deduplicates identical task requests |
| [`sutra/task_verification.py`](file:///d:/practice-file/sutra/task_verification.py) | Python | IMPLEMENTED | Verification of completed tasks via exit code, test pass rate, or file changes | `sutra.task_executor` | `tests/test_system_hardening.py` | Structured result verification |
| [`sutra/workspace_lock.py`](file:///d:/practice-file/sutra/workspace_lock.py) | Python | IMPLEMENTED | Concurrency lock guarding registered workspace roots from overlapping mutating runs | `threading.Lock` | `tests/test_system_hardening.py` | Released properly on error/cancellation |
| [`sutra/discovery.py`](file:///d:/practice-file/sutra/discovery.py) | Python | IMPLEMENTED | Shallow discovery of software projects in registered workspace folders | `os.walk` (depth bounded) | `tests/test_local_runtime.py` | Scans for Git, package.json, pyproject |
| [`sutra/project_index.py`](file:///d:/practice-file/sutra/project_index.py) | Python | IMPLEMENTED | Project metadata cache and fast symbol indexing | `sutra.discovery` | `tests/test_codebase_explorer.py` | Bounded retrieval |
| [`sutra/context_budget.py`](file:///d:/practice-file/sutra/context_budget.py) | Python | IMPLEMENTED | Dynamic token budget manager trimming excessive context for model prompts | Token estimators | `tests/test_system_hardening.py` | Respects max context window |
| [`sutra/context_retrieval.py`](file:///d:/practice-file/sutra/context_retrieval.py) | Python | IMPLEMENTED | Bounded context retrieval from stack traces, symbol definitions, and diffs | `sutra.file_access` | `tests/test_system_hardening.py` | Exact line slicing |
| [`sutra/ai_providers.py`](file:///d:/practice-file/sutra/ai_providers.py) | Python | PARTIALLY IMPLEMENTED | Configuration, model routing, and probe checks for Ollama, LMStudio, OpenAI, Claude, Gemini | `httpx`, `os.environ` | `tests/test_ai_providers.py` | **DEFECT**: `effective_for_agent` casefold bug breaks model overrides |
| [`sutra/dev_certificate.py`](file:///d:/practice-file/sutra/dev_certificate.py) | Python | IMPLEMENTED | Generates self-signed TLS development certificates for localhost/127.0.0.1 | `cryptography` | `tests/test_control_plane.py` | Clean fallback and generation |
| [`sutra/doctor.py`](file:///d:/practice-file/sutra/doctor.py) | Python | IMPLEMENTED | Environment diagnostic suite checking Python version, dependencies, certs, disk | `sutra.ui` | `tests/test_cli_ui.py` | Clear diagnostics output |
| [`sutra/health.py`](file:///d:/practice-file/sutra/health.py) | Python | IMPLEMENTED | Runtime health snapshot, component status evaluation, heartbeat checks | Health checkers | `tests/test_resilience.py` | Health metrics collection |
| [`sutra/latency.py`](file:///d:/practice-file/sutra/latency.py) | Python | IMPLEMENTED | Operation latency tracking and percentile estimation | `time.monotonic` | `tests/test_observability.py` | Lightweight rolling window |
| [`sutra/loop_detector.py`](file:///d:/practice-file/sutra/loop_detector.py) | Python | IMPLEMENTED | Detects repetitive commands or circular failure cycles during agent runs | Rolling command history | `tests/test_system_hardening.py` | Prevents runaway agent loops |
| [`sutra/metrics.py`](file:///d:/practice-file/sutra/metrics.py) | Python | IMPLEMENTED | Structured metrics collector for requests, errors, durations, tokens | Counter / Gauges | `tests/test_observability.py` | Thread-safe counters |
| [`sutra/resilience.py`](file:///d:/practice-file/sutra/resilience.py) | Python | IMPLEMENTED | Circuit breakers, exponential backoff retries, and fallback handlers | Decorators & classes | `tests/test_resilience.py` | Well tested circuit breaker logic |
| [`sutra/degradation.py`](file:///d:/practice-file/sutra/degradation.py) | Python | IMPLEMENTED | Graceful degradation manager switching to read-only mode during failures | `sutra.health` | `tests/test_resilience.py` | Protects system stability |
| [`sutra/cache.py`](file:///d:/practice-file/sutra/cache.py) | Python | IMPLEMENTED | TTL-based in-memory cache for project discovery and policy lookups | Time eviction | `tests/test_cache_lifecycle.py` | Thread-safe LRU/TTL |
| [`sutra/adapters.py`](file:///d:/practice-file/sutra/adapters.py) | Python | IMPLEMENTED | Legacy compatibility shim exporting AgentRegistry and AgentAdapter | `sutra.agents` | `tests/test_agents.py` | Backwards compatibility |
| [`sutra/telegram_bot.py`](file:///d:/practice-file/sutra/telegram_bot.py) | Python | OBSOLETE / DUPLICATE | Legacy entry point forwarding to `sutra.apps.telegram.bot` | `sutra.apps.telegram` | Backward compatibility tests | Replaced by `sutra/apps/telegram/` |

---

## 3. Agent Adapters (`sutra/agents/`)

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`sutra/agents/__init__.py`](file:///d:/practice-file/sutra/agents/__init__.py) | Python | IMPLEMENTED | Exports agent models, manager, registry, and errors | Adapter consumers | Import tests | None |
| [`sutra/agents/adapter.py`](file:///d:/practice-file/sutra/agents/adapter.py) | Python | IMPLEMENTED | Base abstract class `AgentAdapter` defining agent execution interface | Base class | `tests/test_agents.py` | Clean interface |
| [`sutra/agents/antigravity.py`](file:///d:/practice-file/sutra/agents/antigravity.py) | Python | IMPLEMENTED | Adapter for Google Antigravity IDE and CLI agent integration | `AgentAdapter` | `tests/test_antigravity_adapter.py` | Comprehensive adapter |
| [`sutra/agents/codex.py`](file:///d:/practice-file/sutra/agents/codex.py) | Python | IMPLEMENTED | Adapter for OpenAI Codex CLI agent | `AgentAdapter` | `tests/test_codex_adapter.py` | Handles streaming tokens |
| [`sutra/agents/cursor.py`](file:///d:/practice-file/sutra/agents/cursor.py) | Python | IMPLEMENTED | Adapter for Cursor IDE agent integration | `AgentAdapter` | `tests/test_cursor_adapter.py` | Workspace integration |
| [`sutra/agents/jules.py`](file:///d:/practice-file/sutra/agents/jules.py) | Python | IMPLEMENTED | Adapter for Google Jules Cloud Agent via async API client | `AgentAdapter`, `httpx` | `tests/test_jules_adapter.py` | Mock tested |
| [`sutra/agents/generic_cli.py`](file:///d:/practice-file/sutra/agents/generic_cli.py) | Python | IMPLEMENTED | Generic subprocess adapter for arbitrary external CLI coding agents | `subprocess`, `asyncio` | `tests/test_generic_cli_adapter.py` | POSIX session group handling |
| [`sutra/agents/manager.py`](file:///d:/practice-file/sutra/agents/manager.py) | Python | IMPLEMENTED | `AgentManager` controlling multi-agent session lifecycle, routing, and termination | `sutra.agents.registry` | `tests/test_session_lifecycle.py` | Strong concurrency controls |
| [`sutra/agents/models.py`](file:///d:/practice-file/sutra/agents/models.py) | Python | IMPLEMENTED | Dataclasses for `AgentSession`, `AgentEvent`, `AgentStatus`, and `ExecutionPlan` | Model definitions | `tests/test_agents.py` | Well typed |
| [`sutra/agents/registry.py`](file:///d:/practice-file/sutra/agents/registry.py) | Python | IMPLEMENTED | Discoverable registry of all available agent adapters | `sutra.agents.adapter` | `tests/test_agents.py` | Dynamic registration |
| [`sutra/agents/security.py`](file:///d:/practice-file/sutra/agents/security.py) | Python | IMPLEMENTED | Agent-specific sandbox boundaries and execution scope validator | `sutra.security` | `tests/test_system_hardening.py` | Enforces authorized scope |
| [`sutra/agents/supervisor.py`](file:///d:/practice-file/sutra/agents/supervisor.py) | Python | IMPLEMENTED | Watchdog supervisor detecting stalled agent sessions and initiating recovery | Background monitor | `tests/test_supervisor.py` | 40 passed supervisor tests |
| [`sutra/agents/errors.py`](file:///d:/practice-file/sutra/agents/errors.py) | Python | IMPLEMENTED | Exception classes for agent lifecycle, timeout, and execution failures | Error handling | `tests/test_agents.py` | Clear error hierarchies |

---

## 4. Bot Applications (`sutra/apps/`)

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`sutra/apps/common/base.py`](file:///d:/practice-file/sutra/apps/common/base.py) | Python | IMPLEMENTED | Abstract base class `BaseBotApp` defining bot lifecycle, hooks, and routing | App base | `tests/apps/test_bot_apps.py` | Clean lifecycle interface |
| [`sutra/apps/common/registry.py`](file:///d:/practice-file/sutra/apps/common/registry.py) | Python | IMPLEMENTED | Central registry for chat applications (Telegram, Slack, Discord) | App management | `tests/apps/test_bot_apps.py` | Dynamic lookup |
| [`sutra/apps/telegram/bot.py`](file:///d:/practice-file/sutra/apps/telegram/bot.py) | Python | IMPLEMENTED | Full Telegram bot engine handling long-polling and webhook event loops | `httpx`, `sutra.control_plane` | `tests/apps/telegram/test_bot.py` | 28 tests pass; live bot token unverified |
| [`sutra/apps/telegram/handlers.py`](file:///d:/practice-file/sutra/apps/telegram/handlers.py) | Python | IMPLEMENTED | Telegram command dispatchers (`/start`, `/status`, `/workspaces`, `/run`, `/approve`) | `sutra.control_plane` | `tests/apps/telegram/test_bot.py` | Enforces user whitelist |
| [`sutra/apps/telegram/keyboards.py`](file:///d:/practice-file/sutra/apps/telegram/keyboards.py) | Python | IMPLEMENTED | Inline keyboard layout builders for interactive permission approvals | Telegram API schema | `tests/apps/telegram/test_bot.py` | Generates approve/deny buttons |
| [`sutra/apps/telegram/callbacks.py`](file:///d:/practice-file/sutra/apps/telegram/callbacks.py) | Python | IMPLEMENTED | Processes button clicks from inline keyboards; applies decisions to policy engine | `sutra.policy_engine` | `tests/apps/telegram/test_bot.py` | Stale callback protection |
| [`sutra/apps/telegram/formatting.py`](file:///d:/practice-file/sutra/apps/telegram/formatting.py) | Python | IMPLEMENTED | Message formatters converting agent output into clean Telegram MarkdownV2/HTML | String escaping | `tests/apps/telegram/test_bot.py` | Escapes special chars |
| [`sutra/apps/slack/bot.py`](file:///d:/practice-file/sutra/apps/slack/bot.py) | Python | PLACEHOLDER | Scaffold for future Slack bot integration | `BaseBotApp` | `tests/apps/test_bot_apps.py` | Intentional scaffold (Scope Freeze) |
| [`sutra/apps/discord/bot.py`](file:///d:/practice-file/sutra/apps/discord/bot.py) | Python | PLACEHOLDER | Scaffold for future Discord bot integration | `BaseBotApp` | `tests/apps/test_bot_apps.py` | Intentional scaffold (Scope Freeze) |

---

## 5. Connectors & MCP Framework (`sutra/connectors/`)

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`sutra/connectors/interface.py`](file:///d:/practice-file/sutra/connectors/interface.py) | Python | IMPLEMENTED | `BaseConnector` abstract interface for local and remote tool providers | Base interface | `tests/test_connectors.py` | Standard connector protocol |
| [`sutra/connectors/registry.py`](file:///d:/practice-file/sutra/connectors/registry.py) | Python | IMPLEMENTED | Central connector registry managing configuration, connection states, and tools | `sutra.connectors.interface` | `tests/test_connector_registry.py` | Thread-safe registry |
| [`sutra/connectors/cli.py`](file:///d:/practice-file/sutra/connectors/cli.py) | Python | IMPLEMENTED | CLI commands for `sutra connectors` (`add`, `auth`, `test`, `status`, `tools`, `list`, `remove`) | `sutra.cli` | `tests/test_cli_ui.py` | Rich interactive outputs |
| [`sutra/connectors/models.py`](file:///d:/practice-file/sutra/connectors/models.py) | Python | IMPLEMENTED | Dataclasses for connector configuration, status, capabilities, and parameters | Data models | `tests/test_connectors.py` | Well typed |
| [`sutra/connectors/errors.py`](file:///d:/practice-file/sutra/connectors/errors.py) | Python | IMPLEMENTED | Specialized exception types for connector connectivity and authentication failures | Error handling | `tests/test_connectors.py` | Clear exceptions |
| [`sutra/connectors/terminal/connector.py`](file:///d:/practice-file/sutra/connectors/terminal/connector.py) | Python | IMPLEMENTED | Sandboxed local command execution connector (`shell=False`, tokenized argument array) | `subprocess`, `asyncio` | `tests/test_terminal_connector.py` | 24 rigorous security tests pass |
| [`sutra/connectors/terminal/security.py`](file:///d:/practice-file/sutra/connectors/terminal/security.py) | Python | IMPLEMENTED | Command whitelisting, shell injection prevention, path boundary enforcement | `sutra.security` | `tests/test_terminal_connector.py` | Absolute path arguments not checked |
| [`sutra/connectors/terminal/models.py`](file:///d:/practice-file/sutra/connectors/terminal/models.py) | Python | IMPLEMENTED | Terminal execution request and response data models | Data models | `tests/test_terminal_connector.py` | Bounded output payload |
| [`sutra/connectors/mcp/client.py`](file:///d:/practice-file/sutra/connectors/mcp/client.py) | Python | IMPLEMENTED | JSON-RPC 2.0 client managing remote MCP server handshakes and tool dispatch | `asyncio`, JSON-RPC | `tests/test_mcp_client.py` | Proper session lifecycle |
| [`sutra/connectors/mcp/stdio.py`](file:///d:/practice-file/sutra/connectors/mcp/stdio.py) | Python | IMPLEMENTED | MCP stdio transport communicating over subprocess stdin/stdout streams | `asyncio.subprocess` | `tests/test_mcp_stdio.py` | Tested against mock server |
| [`sutra/connectors/mcp/http.py`](file:///d:/practice-file/sutra/connectors/mcp/http.py) | Python | IMPLEMENTED | MCP HTTP/SSE transport communicating over streamed HTTP endpoints | `httpx` | `tests/test_mcp_http.py` | Tested against mock server |
| [`sutra/connectors/mcp/github.py`](file:///d:/practice-file/sutra/connectors/mcp/github.py) | Python | IMPLEMENTED | Specialized GitHub MCP connector exposing issues, pull requests, and file content | `sutra.connectors.mcp.connector` | `tests/test_mcp_github.py` | Tested against mock server |
| [`sutra/connectors/mcp/supabase.py`](file:///d:/practice-file/sutra/connectors/mcp/supabase.py) | Python | IMPLEMENTED | Specialized Supabase MCP connector exposing database queries and schema inspection | `sutra.connectors.mcp.connector` | `tests/test_mcp_supabase.py` | Tested against mock server |
| [`sutra/connectors/mcp/oauth.py`](file:///d:/practice-file/sutra/connectors/mcp/oauth.py) | Python | IMPLEMENTED | OAuth flow helpers for connector authentication | Token exchange | Unit tests | Token masking verified |
| [`sutra/connectors/analyzer/pipeline.py`](file:///d:/practice-file/sutra/connectors/analyzer/pipeline.py) | Python | IMPLEMENTED | Multi-step diagnostics pipeline validating connector configurations and reachability | `sutra.connectors.analyzer` | `tests/test_connector_analyzer.py` | Robust diagnostic reporting |
| [`sutra/connectors/analyzer/probes.py`](file:///d:/practice-file/sutra/connectors/analyzer/probes.py) | Python | IMPLEMENTED | Individual probe checkers (TCP reachability, HTTP handshake, credential validity) | Diagnostic probes | `tests/test_connector_analyzer.py` | Non-destructive checks |

---

## 6. Git & GitHub Integration (`sutra/git/`)

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`sutra/git/local.py`](file:///d:/practice-file/sutra/git/local.py) | Python | IMPLEMENTED | Safe local Git repository wrapper executing tokenized `git` commands | `sutra.connectors.terminal` | `tests/test_git_integration.py` | Read-only & safe mutating commands |
| [`sutra/git/github.py`](file:///d:/practice-file/sutra/git/github.py) | Python | IMPLEMENTED | GitHub REST API client for repository metadata, issues, and pull requests | `httpx` | `tests/test_github_collaboration.py` | Redacts bearer tokens |
| [`sutra/git/security.py`](file:///d:/practice-file/sutra/git/security.py) | Python | IMPLEMENTED | Git-specific safety policies (preventing `git push --force` or branch deletion) | Policy rules | `tests/test_git_integration.py` | Prevents destructive git operations |
| [`sutra/git/resilience.py`](file:///d:/practice-file/sutra/git/resilience.py) | Python | IMPLEMENTED | Retry and backoff policies for network-dependent Git push/fetch operations | `sutra.resilience` | `tests/test_git_integration.py` | Handles transient network errors |
| [`sutra/git/models.py`](file:///d:/practice-file/sutra/git/models.py) | Python | IMPLEMENTED | Dataclasses for Git branch, commit, status, diff, and PR entities | Model definitions | `tests/test_git_integration.py` | Clean typed models |

---

## 7. Research Intelligence Layer (`sutra/research/`)

| File Path | Language | Classification | Purpose & Core Responsibility | Dependencies / Callers | Relevant Tests | Issues / Follow-up |
|---|---|---|---|---|---|---|
| [`sutra/research/agent.py`](file:///d:/practice-file/sutra/research/agent.py) | Python | IMPLEMENTED | Multi-source research coordinator aggregating findings from web, GitHub, and forums | `sutra.research.sources` | `tests/test_research.py` | Deduplicates and ranks results |
| [`sutra/research/sources.py`](file:///d:/practice-file/sutra/research/sources.py) | Python | IMPLEMENTED | Source adapters for GitHub, Reddit, HackerNews, and web search | Source interfaces | `tests/test_research.py` | Fallback when sources unavailable |
| [`sutra/research/retrieval.py`](file:///d:/practice-file/sutra/research/retrieval.py) | Python | IMPLEMENTED | Sandboxed HTML fetcher and text extractor stripping scripts and style tags | `httpx` | `tests/test_research.py` | Blocks localhost / private IP access |
| [`sutra/research/adapters.py`](file:///d:/practice-file/sutra/research/adapters.py) | Python | IMPLEMENTED | Concrete search adapter implementations | `sutra.research.sources` | `tests/test_research.py` | Handles rate-limiting gracefully |

---

## 8. Test Suites (`tests/`)

All 37 test modules and 5 mock servers located in `tests/` were discovered and inventoried.

| Test File | Test Count | Suite Purpose | Observed Outcome |
|---|---|---|---|
| [`tests/test_activity_store.py`](file:///d:/practice-file/tests/test_activity_store.py) | 30 | SQLite audit logging, event sequencing, correlation IDs | **30 PASSED** |
| [`tests/test_agents.py`](file:///d:/practice-file/tests/test_agents.py) | 28 | Agent registry, base adapter contracts, model validation | **28 PASSED** |
| [`tests/test_ai_providers.py`](file:///d:/practice-file/tests/test_ai_providers.py) | 26 | AI provider configuration, model routing, probes | **25 PASSED, 1 FAILED** (`effective_for_agent`) |
| [`tests/test_antigravity_adapter.py`](file:///d:/practice-file/tests/test_antigravity_adapter.py) | 18 | Antigravity agent CLI adapter lifecycle | **18 PASSED** |
| [`tests/test_cache_lifecycle.py`](file:///d:/practice-file/tests/test_cache_lifecycle.py) | 16 | In-memory TTL cache eviction, expiration | **16 PASSED** |
| [`tests/test_cli_ui.py`](file:///d:/practice-file/tests/test_cli_ui.py) | 22 | CLI subcommands, Rich UI rendering, doctor | **22 PASSED** |
| [`tests/test_codebase_explorer.py`](file:///d:/practice-file/tests/test_codebase_explorer.py) | 25 | Sandboxed file access, path traversal prevention | **24 PASSED, 1 SKIPPED** (Windows symlink) |
| [`tests/test_codex_adapter.py`](file:///d:/practice-file/tests/test_codex_adapter.py) | 18 | OpenAI Codex CLI adapter lifecycle | **18 PASSED** |
| [`tests/test_connection_pool.py`](file:///d:/practice-file/tests/test_connection_pool.py) | 14 | WebSocket pool management, reconnection | **14 PASSED** |
| [`tests/test_connectors.py`](file:///d:/practice-file/tests/test_connectors.py) | 24 | Base connector lifecycle, models, errors | **24 PASSED** |
| [`tests/test_connector_analyzer.py`](file:///d:/practice-file/tests/test_connector_analyzer.py) | 28 | Multi-step diagnostics analyzer pipeline | **28 PASSED** |
| [`tests/test_connector_registry.py`](file:///d:/practice-file/tests/test_connector_registry.py) | 20 | Central connector registry operations | **20 PASSED** |
| [`tests/test_control_plane.py`](file:///d:/practice-file/tests/test_control_plane.py) | 70 | TLS control plane, WSS routing, device approval | **70 PASSED** (13 subtests passed) |
| [`tests/test_cursor_adapter.py`](file:///d:/practice-file/tests/test_cursor_adapter.py) | 18 | Cursor IDE adapter lifecycle | **18 PASSED** |
| [`tests/test_generic_cli_adapter.py`](file:///d:/practice-file/tests/test_generic_cli_adapter.py) | 18 | Generic subprocess CLI adapter | **18 PASSED** |
| [`tests/test_github_collaboration.py`](file:///d:/practice-file/tests/test_github_collaboration.py) | 20 | GitHub PR creation, issue inspection | **20 PASSED** |
| [`tests/test_git_integration.py`](file:///d:/practice-file/tests/test_git_integration.py) | 22 | Safe local git commands, branch management | **22 PASSED** |
| [`tests/test_hardening_regressions.py`](file:///d:/practice-file/tests/test_hardening_regressions.py) | 16 | Hardening regression tests | **15 PASSED, 1 SKIPPED** (POSIX process groups) |
| [`tests/test_idempotency.py`](file:///d:/practice-file/tests/test_idempotency.py) | 14 | Idempotent registration and setup | **14 PASSED** |
| [`tests/test_jules_adapter.py`](file:///d:/practice-file/tests/test_jules_adapter.py) | 18 | Google Jules Cloud agent adapter | **18 PASSED** |
| [`tests/test_large_file_semantics.py`](file:///d:/practice-file/tests/test_large_file_semantics.py) | 16 | Large file paging, memory bounding | **16 PASSED** |
| [`tests/test_local_runtime.py`](file:///d:/practice-file/tests/test_local_runtime.py) | 25 | Local daemon startup, pairing REST API | **24 PASSED, 1 SKIPPED** (Windows symlink) |
| [`tests/test_mcp_client.py`](file:///d:/practice-file/tests/test_mcp_client.py) | 24 | JSON-RPC 2.0 client protocol handling | **24 PASSED** |
| [`tests/test_mcp_github.py`](file:///d:/practice-file/tests/test_mcp_github.py) | 18 | GitHub MCP connector tool invocation | **18 PASSED** |
| [`tests/test_mcp_http.py`](file:///d:/practice-file/tests/test_mcp_http.py) | 18 | MCP HTTP/SSE transport tests | **18 PASSED** |
| [`tests/test_mcp_stdio.py`](file:///d:/practice-file/tests/test_mcp_stdio.py) | 18 | MCP Stdio subprocess transport tests | **18 PASSED** |
| [`tests/test_mcp_supabase.py`](file:///d:/practice-file/tests/test_mcp_supabase.py) | 18 | Supabase MCP connector tool invocation | **18 PASSED** |
| [`tests/test_observability.py`](file:///d:/practice-file/tests/test_observability.py) | 20 | Telemetry metrics and latency counters | **20 PASSED** |
| [`tests/test_permission_policies.py`](file:///d:/practice-file/tests/test_permission_policies.py) | 32 | Deterministic permission policy evaluations | **32 PASSED** |
| [`tests/test_reliability_hardening.py`](file:///d:/practice-file/tests/test_reliability_hardening.py) | 22 | Failure injection, backpressure | **22 PASSED** |
| [`tests/test_research.py`](file:///d:/practice-file/tests/test_research.py) | 35 | Research intelligence web retrieval | **35 PASSED in isolation; 16 failed in full run** |
| [`tests/test_resilience.py`](file:///d:/practice-file/tests/test_resilience.py) | 26 | Circuit breakers, retries, degradation | **26 PASSED** |
| [`tests/test_session_lifecycle.py`](file:///d:/practice-file/tests/test_session_lifecycle.py) | 26 | Multi-session concurrency, timeouts | **26 PASSED** |
| [`tests/test_supervisor.py`](file:///d:/practice-file/tests/test_supervisor.py) | 40 | Agent stall detection, supervisor watchdog | **40 PASSED** |
| [`tests/test_system_hardening.py`](file:///d:/practice-file/tests/test_system_hardening.py) | 16 | System design hardening validations | **16 PASSED** |
| [`tests/test_task_executor.py`](file:///d:/practice-file/tests/test_task_executor.py) | 17 | Serial task queues and retry bounds | **17 PASSED** |
| [`tests/test_terminal_connector.py`](file:///d:/practice-file/tests/test_terminal_connector.py) | 24 | Shell injection, argument boundaries | **24 PASSED** |
| [`tests/apps/test_bot_apps.py`](file:///d:/practice-file/tests/apps/test_bot_apps.py) | 16 | Chat bot registry and lifecycle | **16 PASSED** |
| [`tests/apps/telegram/test_bot.py`](file:///d:/practice-file/tests/apps/telegram/test_bot.py) | 28 | Telegram bot commands, approval keyboards | **28 PASSED** |
| [`tests/mocks/mcp_stdio_test_server.py`](file:///d:/practice-file/tests/mocks/mcp_stdio_test_server.py) | - | Mock MCP stdio test server | Harness server |
| [`tests/mocks/mcp_http_test_server.py`](file:///d:/practice-file/tests/mocks/mcp_http_test_server.py) | - | Mock MCP HTTP test server | Harness server |
| [`tests/mocks/mcp_github_test_server.py`](file:///d:/practice-file/tests/mocks/mcp_github_test_server.py) | - | Mock GitHub MCP test server | Harness server |
| [`tests/mocks/mcp_supabase_test_server.py`](file:///d:/practice-file/tests/mocks/mcp_supabase_test_server.py) | - | Mock Supabase MCP test server | Harness server |

---

## 9. Explicit Exclusions

The following file paths were excluded from source code analysis:
1. `.git/`: Internal Git object database and index.
2. `sutra_local.egg-info/`: Package distribution metadata generated by setuptools.
3. `.pytest_cache/` and `__pycache__/`: Python bytecode and pytest run state.
4. `.dev-certs/`: Ephemeral development certificates and private keys generated during local testing.
5. `graphify-out/`: Graphify knowledge graph metadata and extracted AST caches.
