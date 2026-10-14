# Integrations & Connectors Technical Reference — SAMANVAYA · SŪTRA

> **File:** `docs/reference/integrations.md`  
> **Purpose:** Reference specifications, configuration namespaces, credential resolution, and operational status for all external integrations, messaging bots, and connectors in SŪTRA.  
> **Audience:** Developers, system integrators, and security evaluators.  
> **Status:** Maintained — Verified against repository implementation.  

---

## Table of Contents

- [1. Integration Status Matrix](#1-integration-status-matrix)
- [2. Messaging & ChatOps Integrations](#2-messaging--chatops-integrations)
- [3. Git & GitHub Integrations (REST vs. MCP)](#3-git--github-integrations-rest-vs-mcp)
- [4. Execution Connectors (Terminal & Supabase MCP)](#4-execution-connectors-terminal--supabase-mcp)
- [5. AI Provider & Agent Adapters](#5-ai-provider--agent-adapters)
- [6. Architectural Isolation Principles](#6-architectural-isolation-principles)
- [7. Credential Storage & Security Rules](#7-credential-storage--security-rules)
- [8. Troubleshooting & Diagnostics](#8-troubleshooting--diagnostics)

---

## 1. Integration Status Matrix

| Integration / Connector | Category | Operational Status | Module Path | Configuration Prefix / Source | Key Capabilities |
| :--- | :---: | :---: | :--- | :--- | :--- |
| **Telegram Bot** | Bot / ChatOps | **Implemented (agent workflow partial)** | [`sutra/apps/telegram/`](../../sutra/apps/telegram/) | `SUTRA_TELEGRAM_*`<br>`TELEGRAM_*` | Numeric allowlisting, mobile menus, guarded callbacks. Agent process start/output/CLI approval is not wired into the Telegram UI. |
| **Local Git** | Version Control | **Implemented** | [`sutra/git/local.py`](../../sutra/git/local.py) | Host `.git` directory | Porcelain status, bounded diffs, branch management, pre-staging secret filtering. |
| **GitHub REST API** | Cloud Collaboration | **Implemented** | [`sutra/git/github.py`](../../sutra/git/github.py) | `GITHUB_TOKEN`<br>`GH_TOKEN` | PR inspection/creation, reviews, comments, CI workflow dispatch, guarded merge. |
| **Terminal Connector** | Local Execution | **Implemented** | [`sutra/connectors/terminal/`](../../sutra/connectors/terminal/) | `sutra connectors add` | `shell=False` execution, argument tokenization, workspace confinement, forbidden operator blocking. |
| **GitHub Remote MCP** | MCP Connector | **Implemented** | [`sutra/connectors/mcp/github.py`](../../sutra/connectors/mcp/github.py) | OS Keyring / `GITHUB_PERSONAL_ACCESS_TOKEN` | Streaming HTTP MCP client, dynamic tool discovery on `api.githubcopilot.com/mcp/`. |
| **Supabase Remote MCP** | MCP Connector | **Implemented** | [`sutra/connectors/mcp/supabase.py`](../../sutra/connectors/mcp/supabase.py) | OS Keyring / `SUPABASE_ACCESS_TOKEN` | Project ref validation, OAuth resolution, schema inspection, read-only safety defaults. |
| **Ollama / Local Models** | AI Engine | **Configurable** | `sutra/agents/` | `OLLAMA_BASE_URL`<br>`OLLAMA_MODEL` | Local inference (Gemma, Llama) without sending tokens to cloud APIs. |
| **Agent Adapters** | Agent Runtime | **Implemented** | [`sutra/agents/`](../../sutra/agents/) | Local CLI binaries in `PATH` | Adapters for Google Antigravity, Cursor, OpenAI Codex, Jules, and Generic CLI. |
| **Slack Bot** | Bot / ChatOps | *Scaffolded (Planned)* | [`sutra/apps/slack/`](../../sutra/apps/slack/) | `SUTRA_SLACK_*` | Architectural scaffold for future multi-bot support. Not operational in MVP. |
| **Discord Bot** | Bot / ChatOps | *Scaffolded (Planned)* | [`sutra/apps/discord/`](../../sutra/apps/discord/) | `SUTRA_DISCORD_*` | Architectural scaffold for future multi-bot support. Not operational in MVP. |

---

## 2. Messaging & ChatOps Integrations

### Telegram Bot (`sutra/apps/telegram/`)
- **Transport:** Long-polling (default) or Webhook over HTTPS.
- **Security:** Inbound updates MUST match `TELEGRAM_ALLOWED_USER_IDS`. Unapproved user IDs receive no response.
- **Commands:** `/start`, `/help`, `/status`, `/pair`, `/projects`, `/files`, `/cmd`, `/diff`, `/stop`.
- **Callback Integrity:** Interactive callback queries (`ALLOW_ONCE`, `DENY`) enforce expiration nonces and single-use processing.

### Slack & Discord Scaffolds
- Reside in `sutra/apps/slack/` and `sutra/apps/discord/` as clean API scaffolds.
- **Status:** Non-operational in the current release; do not configure production tokens for these services.

---

## 3. Git & GitHub Integrations (REST vs. MCP)

### Local Git Engine (`sutra/git/local.py`)
- Executes Git CLI directly via `subprocess.run(["git", ...], shell=False, cwd=repo_root)`.
- Pre-staging sanitizer automatically rejects: `.env*`, `*.pem`, `*.key`, `*.pfx`, `id_rsa`, `id_ed25519`.

### GitHub REST API (`sutra/git/github.py`)
- Direct REST integration using `httpx.AsyncClient` targeting `https://api.github.com`.
- Handles pull request creation, review comments, CI check monitoring, and guarded merging.
- **Authentication:** `GITHUB_TOKEN` or `GH_TOKEN`.

### GitHub Remote MCP Connector (`sutra/connectors/mcp/github.py`)
- Connects to GitHub's streamable Model Context Protocol service (`https://api.githubcopilot.com/mcp/`).
- Uses `HttpMCPTransport` (JSON-RPC 2.0).
- Tools are discovered dynamically (`tools/list`) and permissioned individually through SŪTRA's `PolicyEngine`.

---

## 4. Execution Connectors (Terminal & Supabase MCP)

### Guarded Terminal Connector (`sutra/connectors/terminal/`)
- **Execution Mode:** `shell=False` strictly enforced.
- **Command Validation:** Tokenized via `shlex.split`. Chained commands (`;`, `&&`, `||`, `|`, `>`, `<`) are rejected.
- **Path Confinement:** Every path argument must reside inside the registered workspace root.
- **Environment Scrubbing:** Inherited subprocess environment strips sensitive tokens (`GITHUB_TOKEN`, `SUPABASE_KEY`).

### Supabase Remote MCP Connector (`sutra/connectors/mcp/supabase.py`)
- **Project Ref Validation:** Enforces 20-character lowercase alphanumeric identifier (`^[a-z0-9]{20}$`).
- **Credential Storage:** Stored in OS Keyring under service `sutra-connector-supabase`.
- **Safety Defaults:** Read-only operations allowed under `MEDIUM` policy; mutating schema changes require `CRITICAL` confirmation.

---

## 5. AI Provider & Agent Adapters

SŪTRA integrates coding agents via the `AgentRegistry` and `AgentManager` abstractions (`sutra/agents/`):

- **Google Antigravity:** Uses `agy --print --input-format stream-json --output-format stream-json` for the default launch. The structured stdin protocol supports more than one prompt in the same process; structured response deltas and final response metadata are captured. Headless tool permissions remain governed by Antigravity policy; approval-required actions can be soft-denied and are not presented as Telegram approvals.
- **Cursor:** Launches the print-mode `agent` CLI with a prompt. The current adapter does not establish a persistent interactive session or route approval choices.
- **OpenAI Codex:** Launches `codex exec` with a prompt. The current adapter does not establish a persistent interactive session or route approval choices.
- **Google Jules:** The registered adapter currently creates local simulated task IDs and simulated progress; it does not call the Jules service. Treat the provider integration as unimplemented.
- **Generic CLI Adapter:** Uses ordinary subprocess stdin/stdout/stderr pipes, not a PTY. Raw stdin support depends on the child CLI's own protocol and does not provide generic approval detection.
- **Claude Code:** Registered as a placeholder, not a concrete adapter.
- **Ollama:** Supports local inference via HTTP REST (`http://127.0.0.1:11434`) for models like Google Gemma 2 and Meta Llama 3.

Adapter availability means only that a configured executable or credential indicator was found. It does not establish Telegram lifecycle control, approval delivery, or real provider connectivity. Only Antigravity's documented stream-json prompt protocol is implemented here for in-process continuation. No adapter currently supplies a genuine pending-approval event to Telegram.

---

## 6. Architectural Isolation Principles

1. **One Integration = One Namespace:** Each integration resides in its dedicated directory under `sutra/apps/` or `sutra/connectors/`.
2. **Local Workstation Primary:** Local `.git` on disk is always the primary source of truth. Remote services are secondary.
3. **Deterministic Policy Gate:** No integration or connector may bypass the central `PolicyEngine`.
4. **No IDE Required:** SŪTRA operates entirely headlessly via terminal and Telegram; no running VS Code or IDE window is required.
5. **No Monolithic State:** External connectors do not share database tables; audit events are unified through `ActivityStore`.

---

## 7. Credential Storage & Security Rules

- **Precedence:** Explicit CLI flags $\to$ OS Environment Variables $\to$ `.env` file $\to$ OS Keyring $\to$ Defaults.
- **Keyring Backends:** Utilizes native OS credential managers (Windows Credential Manager, Apple Keychain, Linux Secret Service).
- **Zero Logging:** Credentials and bearer headers are automatically masked by `sutra/redaction.py`.

---

## 8. Troubleshooting & Diagnostics

- **Connector Diagnostic:** Run `python -m sutra connectors test <connector_id>`.
- **General Health Check:** Run `python -m sutra doctor`.
- **Detailed Reference:** See [docs/architecture/connectors.md](../architecture/connectors.md) for complete connector architecture.
