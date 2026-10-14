# AgentBridge — GitHub MCP Connector & Portable Authentication (Phase 7.8A)

## 1. Architectural Concept & Objectives

The **GitHub MCP Connector** (`connector_id = "github-mcp"`, `provider = "github"`, `type = ConnectorType.MCP`) is the first real remote Model Context Protocol integration in AgentBridge. It allows developers and autonomous coding agents to discover and invoke tools exposed by the **official GitHub Remote MCP Server** using portable, user-owned credentials.

### Primary Goals
1. **User-Owned Authentication**: Users provide their own fine-grained Personal Access Token (PAT). No hardcoded credentials, central proxies, or third-party cloud infrastructure are involved.
2. **Zero Local Server Prerequisite**: Communicates directly over HTTPS with GitHub's official remote MCP server at `https://api.githubcopilot.com/mcp/` using AgentBridge's native Streamable HTTP transport (`HttpMCPTransport`). No Docker, Go binary, or local server process is required.
3. **OS-Native Credential Security**: Employs the system keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service) for persistent interactive credentials, alongside deterministic environment variable overrides for CI/headless environments.
4. **Zero Collisions**: Coexists peacefully alongside AgentBridge's existing GitHub REST client (`sutra.git.github`) without modifying or replacing it.
5. **Strict Permission & Audit Boundaries**: Enforces AgentBridge's centralized `PolicyEngine` invariants. Read-only queries are auto-approved, while mutating operations require explicit authorization. Secrets are never persisted in SQLite or emitted in logs.

---

## 2. End-to-End Architecture

```
AgentBridge CLI / Agent Runtime / Telegram
                  │
                  ▼
          ConnectorRegistry
                  │
                  ▼
         GitHubMCPConnector
                  │
         ┌────────┴────────┐
         ▼                 ▼
   MCPClient         PolicyEngine
         │                 │
         ▼                 ▼
     MCPSession      ActivityStore
         │
         ▼
  HttpMCPTransport ──[ Authorization: Bearer <PAT> ]──> https://api.githubcopilot.com/mcp/
         │
   (Streamable HTTP)
```

### Component Roles

| Component | Module | Responsibility |
|---|---|---|
| `GitHubMCPConnector` | `sutra.connectors.mcp.github` | Connector adapter implementing `Connector` and `MCPConnector` with GitHub-specific endpoint defaults, credential integration, and sanitized analysis descriptors. |
| `GitHubCredentialProvider` | `sutra.credentials` | Manages credential lifecycle across in-memory tokens, environment variables, and OS Keyring backends with masking and audit safety. |
| `MCPClient` | `sutra.connectors.mcp.client` | Protocol client orchestrating `tools/list`, `tools/call`, and bounded pagination. |
| `MCPSession` | `sutra.connectors.mcp.session` | Manages protocol state FSM and protocol version negotiation (`2026-07-28`, `2025-03-26`, `2024-11-05`). |
| `HttpMCPTransport` | `sutra.connectors.mcp.http` | Remote Streamable HTTP transport over HTTPS with bounded response buffering and injected authorization headers. |
| [`PolicyEngine`](../../sutra/policy_engine.py) | `sutra.policy_engine` | Authorizes operations using precedence: CRITICAL safety invariant > session grants > workspace/global policies > risk defaults. |
| [`ActivityStore`](../../sutra/activity_store.py) | `sutra.activity_store` | Persists audit trails for connector lifecycle and tool call invocations correlated by `request_id`. |

---

## 3. Portable Credential Management

### Credential Resolution Precedence
`GitHubCredentialProvider` evaluates credential sources in strict, deterministic order:

1. **In-Memory Token** (`CredentialSource.MEMORY`): Set explicitly in constructor or via runtime session parameters (useful for ephemeral execution and unit testing).
2. **Primary Environment Variable** (`CredentialSource.ENV_VAR`):
   ```bash
   export SUTRA_GITHUB_TOKEN="github_pat_11..."
   ```
3. **Compatibility Fallback Variable** (`CredentialSource.FALLBACK_ENV`):
   ```bash
   export GITHUB_PERSONAL_ACCESS_TOKEN="github_pat_11..."
   ```
4. **Operating System Keyring** (`CredentialSource.KEYRING`):
   - **Windows**: Windows Credential Manager (`sutra:github-mcp`)
   - **macOS**: Apple Keychain
   - **Linux**: Secret Service API / Freedesktop Keyring backend

### Keyring Graceful Degradation
If no secure OS keyring backend is installed on the machine (e.g. headless Docker container without `dbus`), the provider:
- Refuses to store credentials in plaintext files or SQLite.
- Produces a helpful, actionable error directing the user to configure `SUTRA_GITHUB_TOKEN` in the environment.

### Secret Redaction Guarantees
- Tokens are **never** echoed in terminal output (interactive CLI uses masked `getpass`).
- Tokens are **never** written into `connector_configs`, `activity_records`, or any SQLite tables.
- Descriptors, analyzer reports, log statements, exception traces, and metrics scrub credentials using `redact_secrets()`.

---

## 4. User Setup Experience & CLI Commands

AgentBridge provides dedicated CLI subcommands under `sutra connectors`:

### 4.1 Register Connector
```bash
sutra connectors add github-mcp
```
Registers the connector with `ConnectorRegistry` pointing to the official remote endpoint `https://api.githubcopilot.com/mcp/`. Safe non-secret metadata is persisted.

### 4.2 Interactive Authentication Setup
```bash
sutra connectors auth github-mcp
```
Prompts for a GitHub fine-grained PAT using masked input (`getpass`) and securely stores it in the local OS keyring.

### 4.3 Connection Verification Probe
```bash
sutra connectors test github-mcp
```
Performs a bounded connection handshake, verifies the protocol version, discovers remote server capabilities, and reports latency without printing the token.

### 4.4 Status & Diagnostic Inspection
```bash
sutra connectors status github-mcp
```
Displays lifecycle status (`READY`, `DISCONNECTED`, `ERROR`), endpoint URL, credential source (`KEYRING`, `ENV_VAR`, `NONE`), and capability flags.

### 4.5 Discover Tools
```bash
sutra connectors tools github-mcp
```
Lists all tools exposed by the remote MCP server alongside read-only status and risk classification.

---

## 5. Tool Discovery & Execution Semantics

Tools discovered from the official GitHub Remote MCP Server include:
- `list_repositories` (Read-only, `LOW` risk)
- `get_issue` (Read-only, `LOW` risk)
- `search_code` (Read-only, `LOW` risk)
- `create_issue` (Mutating, `HIGH` risk)
- `create_pull_request` (Mutating, `HIGH` risk)
- `delete_repository` (Destructive, `CRITICAL` risk)

### Permission Rules & Safety Guarantees
1. **Read-Only Auto-Approval**: Operations starting with `get_`, `list_`, `read_`, `search_`, `view_`, or `describe_` are classified as `tool_read` (`LOW` risk) and execute automatically.
2. **Mutating Operation Confirmation**: State-changing operations (such as creating issues or opening pull requests) require explicit approval via session grant (`ALLOW_SESSION`) or persistent policy.
3. **CRITICAL Operations**: Destructive operations (such as deleting repositories) are immutable and require single-use approval (`ALLOW_ONCE`); persistent policies cannot override this rule.
4. **Unknown Tool Safety**: Tools not explicitly classified default to `HIGH` risk, preventing unvetted tool execution.

---

## 6. Hackathon Demonstration Workflow

For quick, safe live hackathon demonstrations:

### Step 1: Create a Demo Fine-Grained PAT
1. Visit GitHub Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens.
2. Select **Only select repositories** and pick a dedicated demo repository (e.g. `octocat/hackathon-demo`).
3. Under **Repository permissions**, grant:
   - **Issues**: Read and Write
   - **Pull requests**: Read and Write
   - **Contents**: Read-only
4. Generate the token.

### Step 2: Configure & Test in AgentBridge
```bash
# Option A: Environment variable
export SUTRA_GITHUB_TOKEN="github_pat_11..."

# Option B: Interactive Keyring
sutra connectors auth github-mcp

# Verify connectivity
sutra connectors test github-mcp
```

### Step 3: Run Agent Tools
```bash
# Discover available GitHub MCP tools
sutra connectors tools github-mcp

# Execute read-only inquiry safely
sutra connectors test github-mcp --check
```

---

## 7. Authentication Modes: PAT vs. Future OAuth

| Feature | Phase 7.8A (PAT-First) | Future OAuth (Phase 7.8B+) |
|---|---|---|
| **Setup Complexity** | Zero infrastructure (User creates PAT in 1 minute) | Requires registered GitHub OAuth App & client secrets |
| **Portability** | 100% portable across local developer machines | Requires localhost redirect listener or auth proxy |
| **Token Scopes** | User-defined fine-grained repository scopes | Predefined OAuth scopes requested during authorization |
| **Token Revocation** | User revokes instantly from GitHub Web UI | Revocable from GitHub UI or API |
| **CI / Headless Support** | Seamless via `SUTRA_GITHUB_TOKEN` | Requires headless device code flow |

---

## 8. Troubleshooting Guide

| Issue | Root Cause | Solution |
|---|---|---|
| **`HTTP 401 Unauthorized`** | Expired or incorrect GitHub token. | Run `sutra connectors auth github-mcp` to update token or check `SUTRA_GITHUB_TOKEN`. |
| **`HTTP 403 Forbidden`** | Fine-grained PAT lacks permission for target repository. | Edit PAT scopes in GitHub settings to include required repository access. |
| **`Keyring is unavailable`** | Headless Linux environment without D-Bus/Secret Service. | Export `SUTRA_GITHUB_TOKEN="ghp_..."` in the environment. |
| **`Connection Refused`** | Network or firewall blocks outbound HTTPS traffic. | Verify connectivity to `https://api.githubcopilot.com/mcp/` using standard curl/browser. |
