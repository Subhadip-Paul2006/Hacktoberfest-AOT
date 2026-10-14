# Product Requirements Document (PRD) — SAMANVAYA · SŪTRA

> **File:** `PRD.md`  
> **Purpose:** Define product vision, target personas, functional requirements (FR), non-functional requirements (NFR), and acceptance criteria for the SŪTRA control plane.  
> **Audience:** Product managers, software engineers, security auditors, and contributors.  
> **Status:** Approved MVP Baseline — Synchronized with repository capabilities.  

---

## Table of Contents

- [1. Executive Summary & Vision](#1-executive-summary--vision)
- [2. Problem Statement & Value Proposition](#2-problem-statement--value-proposition)
- [3. Target Personas & Core Use Cases](#3-target-personas--core-use-cases)
- [4. User Journeys](#4-user-journeys)
- [5. Current MVP Scope vs. Non-Goals](#5-current-mvp-scope-vs-non-goals)
- [6. Functional Requirements (FR)](#6-functional-requirements-fr)
- [7. Non-Functional Requirements (NFR)](#7-non-functional-requirements-nfr)
- [8. Security, Privacy & Compliance Requirements](#8-security-privacy--compliance-requirements)
- [9. Failure Handling & Degraded State Expectations](#9-failure-handling--degraded-state-expectations)
- [10. Acceptance Criteria & Verification Matrix](#10-acceptance-criteria--verification-matrix)
- [11. Known MVP Limitations](#11-known-mvp-limitations)
- [12. Document Traceability & Links](#12-document-traceability--links)

---

## 1. Executive Summary & Vision

**SAMANVAYA · SŪTRA** (historically known as *AgentBridge*) is an open-source, local-first control plane that connects mobile communication platforms (primarily Telegram) with high-privilege coding agents operating on a developer's local workstation.

### Foundational Invariant
> **SŪTRA is the control plane. The coding agent is the coding intelligence.**

SŪTRA does not write or generate code itself; instead, it provides the deterministic coordination, security sandbox, policy enforcement, and audit layer required to run autonomous coding agents safely without human supervision at the physical desk.

---

## 2. Problem Statement & Value Proposition

### The Problem
1. **The Tethered Developer:** Modern autonomous coding agents take minutes or hours to complete large refactoring or test runs. Developers are forced to remain physically at their desk to monitor progress, approve commands, or unblock stuck tasks.
2. **The Runaway Agent Risk:** Autonomous agents with terminal access can execute destructive commands (`rm -rf /`, force pushes), leak private keys to public repositories, or enter infinite prompt loops consuming excessive resources.
3. **The Cloud Relay Vulnerability:** Traditional remote control solutions require routing raw source code, repository files, and `.env` credentials through third-party cloud brokers, violating corporate IP security policies and data sovereignty.

### The Value Proposition
- **Local Data Sovereignty:** Private source code and intellectual property remain 100% on the local physical drive.
- **Deterministic Guardrails:** Hard-coded policy rules evaluate every command, path, and tool invocation prior to execution.
- **Mobile Convenience:** Full supervision, progress notifications, and single-tap approvals delivered directly via Telegram.
- **Zero-Token Local Knowledge:** Integrated AST-based knowledge graphs (Graphify) provide instant codebase context with zero LLM API cost.

---

## 3. Target Personas & Core Use Cases

### Persona 1: The Mobile Autonomous Engineer (Alex)
- **Role:** Senior Software Engineer working on large codebases.
- **Need:** Kicks off an autonomous agent run (e.g., refactoring a module, running end-to-end tests) and steps away for lunch or a commute. Needs to receive status updates, review diffs on mobile, and tap "Approve" when the agent asks to push or run a test suite.

### Persona 2: The Security & Compliance Lead (Priya)
- **Role:** Application Security Lead managing engineering workstation policies.
- **Need:** Must guarantee that autonomous AI tools do not leak API keys, access unapproved disk partitions, or execute unvetted shell scripts. Requires an immutable, correlated SQLite audit trail of every agent action.

### Persona 3: The Low-Resource / Edge Developer (Marco)
- **Role:** Full-stack developer running on a modest 4-core, 8GB RAM laptop.
- **Need:** Wants remote agent capabilities without running heavy cloud infrastructure or memory-hungry container stacks. SŪTRA must maintain a minimal CPU/memory footprint and fail gracefully on slow networks.

---

## 4. User Journeys

### Journey 1: Workstation Setup & Device Pairing
1. Developer installs `sutra` locally and runs `python -m sutra setup`.
2. Developer runs `python -m sutra workspace add <path>` to register explicitly approved project roots.
3. Developer starts the local runtime (`sutra local`) and the control plane (`sutra control-plane`).
4. Developer initiates pairing from Telegram (`/pair`), receiving an ephemeral 8-character high-entropy code (300-second TTL).
5. Workstation approves the pairing; persistent 256-bit mutual bearer tokens are issued; temporary pairing codes are immediately destroyed in RAM.

### Journey 2: Mobile Codebase Exploration & File Preview
1. Developer sends `/projects` or `/files` from Telegram.
2. SŪTRA resolves paths strictly within the registered workspace boundary.
3. When requesting a large file, SŪTRA bounds output to configured size limits (e.g., 50KB chunks), redacting detected secret patterns (`sk-`, `ghp_`, private keys).
4. Developer reviews the file preview safely formatted in Telegram Markdown.

### Journey 3: Interactive Human-in-the-Loop Authorization
1. Autonomous agent attempts to execute a high-risk operation (e.g., `git push origin main` or a mutating database migration).
2. SŪTRA PolicyEngine intercepts the request, assigns risk tier `HIGH`, and blocks execution.
3. An interactive Telegram alert with inline buttons (`ALLOW_ONCE`, `ALLOW_SESSION`, `DENY`) is pushed to the developer's phone.
4. Developer taps `ALLOW_ONCE`. SŪTRA validates the callback nonce and timestamp, issues single-use authorization, executes the command with `shell=False`, and returns the stdout result.

### Journey 4: Guarded MCP Tool Execution
1. Autonomous agent requests an MCP tool call (e.g., querying GitHub PRs or inspecting Supabase tables).
2. SŪTRA MCP Client negotiates the protocol handshake and queries tool capabilities.
3. Tool execution is checked against PolicyEngine. Mutating tools require interactive confirmation; read-only operations are logged.
4. Execution results are recorded in SQLite ActivityStore with correlated session IDs.

---

## 5. Current MVP Scope vs. Non-Goals

### In-Scope (Current MVP)
- Local runtime daemon and loopback status endpoints (`127.0.0.1:8765`).
- Local TLS control plane (`127.0.0.1:8766`) with mutual bearer authentication.
- Explicit workspace registration and multi-stage path canonicalization.
- Telegram bot interface with numeric user allowlisting and interactive keyboards.
- Deterministic 4-tier PolicyEngine (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`).
- Local Git operations (status, diff, branch, stage, commit) with pre-staging secret filtering.
- GitHub REST client (PRs, issues, CI checks) and GitHub MCP Connector.
- Guarded Terminal Connector (`shell=False`, argument array tokenization).
- Supabase MCP Connector with OAuth token resolution and read-only defaults.
- SQLite-backed state and ActivityStore audit logging.

### Out-of-Scope (Explicit Non-Goals)
- **No Cloud Source Code Hosting:** SŪTRA will never store, host, or index developer code on cloud servers.
- **No Agent Model Hosting/Training:** SŪTRA does not train or host proprietary LLMs.
- **No Mandatory Docker Requirement:** SŪTRA runs directly as a native Python process with access to the user's real workspace.
- **No Operational Slack/Discord Bots in MVP:** Slack and Discord modules are scaffolds for future roadmap phases and are not claimed as operational.
- **No Arbitrary OS Shell Sandbox:** SŪTRA enforces strict application-level allowlists and tokenization, but does not provide hardware-level kernel hypervisor isolation.

---

## 6. Functional Requirements (FR)

| ID | Title | Description | Target Component |
| :--- | :--- | :--- | :--- |
| **FR-001** | **Workspace Confinement** | The system MUST reject any path that traverses outside registered workspace roots, resolving symlinks and recursive URL encodings. | `sutra/security.py` |
| **FR-002** | **Cryptographic Pairing** | The system MUST support one-time pairing using 8-character high-entropy alphanumeric codes with a 300-second TTL and SHA-256 digest comparisons. | `sutra/pairing.py`<br>`sutra/control_plane.py` |
| **FR-003** | **Telegram User Allowlist** | The system MUST silently drop or reject messages from any Telegram user ID not present in `TELEGRAM_ALLOWED_USER_IDS`. | `sutra/apps/telegram/bot.py` |
| **FR-004** | **4-Tier Risk Policy Engine** | The system MUST evaluate all operations into `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`. `CRITICAL` operations MUST require double-confirmation and MUST NEVER be auto-approved. | `sutra/policy_engine.py`<br>`sutra/permissions.py` |
| **FR-005** | **Pre-Staging Secret Defense** | The Git manager MUST inspect candidate files and refuse to stage `.env*`, `*.pem`, `*.key`, `*.pfx`, or credentials. | `sutra/git/security.py` |
| **FR-006** | **Guarded Terminal Execution** | Local command execution MUST tokenize arguments with `shlex.split`, prohibit `shell=True`, and reject forbidden shell operators (`;`, `&&`, `\|`, `>`, `<`). | `sutra/connectors/terminal/` |
| **FR-007** | **MCP Client Core** | The system MUST implement a transport-agnostic MCP client supporting `stdio` and streaming `http` transports with protocol version negotiation. | `sutra/connectors/mcp/` |
| **FR-008** | **GitHub & Supabase MCP** | The system MUST provide dedicated MCP connectors for GitHub and Supabase with secure OS Keyring credential resolution. | `sutra/connectors/mcp/github.py`<br>`sutra/connectors/mcp/supabase.py` |
| **FR-009** | **Activity Store & Auditing** | The system MUST persist every operation, approval decision, error, and tool result into an immutable SQLite store with correlation IDs. | `sutra/activity_store.py` |
| **FR-010** | **Pluggable Agent Adapters** | The system MUST provide adapter interfaces for Google Antigravity, Cursor, Codex, Jules, and generic CLI agents. | `sutra/adapters.py`<br>`sutra/agents/` |

---

## 7. Non-Functional Requirements (NFR)

| ID | Title | Description | Target Metric |
| :--- | :--- | :--- | :--- |
| **NFR-001** | **Local Data Sovereignty** | Zero private source code bytes may be transmitted to or stored on cloud relay servers. | 0 bytes remote code storage |
| **NFR-002** | **Resource Footprint** | Idle runtime daemon MUST consume minimal CPU and RAM on standard developer PCs. | $\le 100\text{ MB}$ RAM idle, $\le 2\%\text{ CPU}$ |
| **NFR-003** | **Network Resilience** | System MUST survive intermittent network loss, employing exponential backoff ($2\text{s}$ to $30\text{s}$) with jitter and heartbeat timeouts. | Auto-reconnect within $30\text{s}$ |
| **NFR-004** | **Command Latency** | Local policy evaluation and permission dispatch MUST complete instantaneously. | $\le 50\text{ ms}$ evaluation overhead |
| **NFR-005** | **Zero Secret Leakage** | All logs, Telegram previews, and ActivityStore records MUST sanitize known token patterns (`sk-`, `ghp_`, Bearer headers). | 100% regex sanitization pass |
| **NFR-006** | **Cross-Platform Support** | Core runtime, state storage, and CLI MUST run cleanly on Windows (PowerShell), macOS, and Linux. | Clean unit test runs on supported OS |

---

## 8. Security, Privacy & Compliance Requirements

1. **Authentication:** Outbound TLS 1.3 WebSocket tunnel with mutual 256-bit bearer token authorization.
2. **Replay Protection:** Unique UUIDv4 frame IDs, 60-second millisecond timestamp freshness window, single-use permission IDs.
3. **Privilege Boundary:** Local runtime runs as an unprivileged standard user; escalation commands (`sudo`, `runas`, `Set-ExecutionPolicy`) are explicitly blacklisted.
4. **Secret Storage:** Tokens stored via OS Keyring (`keyring` package) or encrypted in `~/.sutra/state.sqlite3` protected by operating system user ACLs.

---

## 9. Failure Handling & Degraded State Expectations

- **Control Plane Offline:** Local runtime daemon continues to operate locally; CLI commands (`sutra workspace`, `sutra status`) remain fully functional.
- **Telegram Disconnected:** Operator alerts are queued in memory; if unacknowledged, operations remain blocked in default-deny state.
- **Connector Outage:** If an MCP server (e.g., Supabase or GitHub) fails or returns HTTP 5xx, the error is isolated to that connector; the core runtime and unrelated connectors remain healthy.
- **Database Lock/Busy:** SQLite connections configure WAL mode and busy timeouts ($\ge 5000\text{ ms}$) to prevent operational lockup during concurrent access.

---

## 10. Acceptance Criteria & Verification Matrix

| Requirement | Acceptance Test | Verification Method |
| :--- | :--- | :--- |
| `FR-001` Workspace Confinement | Traversal paths (`../../Windows`) raise `PermissionError`. | Automated test: `tests/test_codebase_explorer.py` |
| `FR-002` Cryptographic Pairing | Valid codes pair successfully; expired or replayed codes fail. | Automated test: `tests/test_control_plane.py` |
| `FR-003` Telegram Allowlist | Messages from non-allowlisted user IDs produce no action. | Automated test: `tests/test_control_plane.py` |
| `FR-004` Policy Risk Tiers | `CRITICAL` operations cannot be persisted to `ALLOW_ALWAYS`. | Automated test: `tests/test_permission_policies.py` |
| `FR-005` Git Secret Filter | Adding `.env` or `id_rsa` to git stage raises validation error. | Automated test: `tests/test_git_integration.py` |
| `FR-006` Terminal Safety | Chained commands (`git status; rm -rf /`) are rejected. | Automated test: `tests/test_terminal_connector.py` |
| `FR-007` MCP Protocol Core | Handshake negotiates protocol version; ping responds. | Automated test: `tests/test_mcp_client.py` |
| `FR-008` GitHub/Supabase MCP | Tool discovery lists tools; OS Keyring stores token. | Automated test: `tests/test_mcp_github.py`, `tests/test_mcp_supabase.py` |
| `FR-009` Activity Store | Events are recorded with correlation IDs and queryable. | Automated test: `tests/test_activity_store.py` |

---

## 11. Known MVP Limitations

1. **Loopback Binding by Default:** Services bind to `127.0.0.1`. Remote mobile access requires a secure reverse proxy (e.g., Tailscale, Cloudflare Tunnel, or self-hosted Nginx with public TLS).
2. **Live Service Credentials:** Automated regression tests run against local test mock servers (`tests/mocks/mcp_stdio_test_server.py`). Live GitHub and Supabase tests require operator-supplied personal access tokens.
3. **Scaffolded Integrations:** Slack and Discord connectors are non-functional scaffolds in the current release.

---

## 12. Document Traceability & Links

- [README.md](README.md) — Product overview, quick start, and feature matrix.
- [TRD.md](TRD.md) — Technical Requirements Document and system architecture.
- [SECURITY.md](SECURITY.md) — Threat model and security architecture.
- [SETUP.md](SETUP.md) — Workstation installation and verification guide.
- [docs/architecture/ai-architecture.md](docs/architecture/ai-architecture.md) — Technical architecture blueprint.
