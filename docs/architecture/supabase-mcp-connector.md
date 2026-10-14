# AgentBridge — Supabase Remote MCP Connector Architecture (Phase 7.8C)

## 1. Architectural Concept & Objectives

The **Supabase Remote MCP Connector** (`connector_id = "supabase-mcp"`, `provider = "supabase"`, `type = ConnectorType.MCP`, `transport = "streamable_http"`) enables autonomous coding agents and developers to interact with Supabase projects over the Model Context Protocol (MCP) using the official hosted remote endpoint (`https://mcp.supabase.com/mcp`).

### Core Design Principles
1. **Official Remote Endpoint**: Communicates with the hosted Supabase MCP service at `https://mcp.supabase.com/mcp` over Streamable HTTP. Does not require local Docker containers or Supabase CLI installations for remote database inspection.
2. **Explicit Project Scoping**: Connectors bind to a specific project reference (`project_ref`), validated against strict syntax rules (`^[a-z0-9_-]{2,64}$`).
3. **Read-Only by Default**: Defaults to `read_only=true`, enforcing safety both server-side via endpoint query parameters and client-side via AgentBridge's `PolicyEngine`.
4. **Minimal Feature Group Footprint**: Limits server capabilities to a safe minimum subset (defaulting to `docs,database`), avoiding unintended exposure of account-management or destructive features.
5. **Dual Authentication Modes**:
   - **Interactive Browser OAuth 2.0 with PKCE**: Full RFC 7636 PKCE flow with dynamic local loopback server (`http://127.0.0.1:<port>/callback`), state validation, and automatic token refresh.
   - **Headless Personal Access Token (PAT)**: Deterministic environment variable resolution (`SUTRA_SUPABASE_ACCESS_TOKEN` > `SUPABASE_ACCESS_TOKEN`) and secure OS Keyring persistence.
6. **Zero Secret Persistence**: Raw tokens are never logged, never stored in SQLite/ActivityStore, never included in URLs or descriptors, and never echoed in interactive shells.
7. **Complete Layering Reuse**: Directly reuses AgentBridge's existing generic MCP stack (`MCPClient`, `MCPSession`, `HttpMCPTransport`, `MCPConnector`, `ConnectorRegistry`, `PolicyEngine`, `ActivityStore`).

---

## 2. End-to-End Architecture

```
User CLI / Agent Runtime / Telegram
                 │
                 ▼
         ConnectorRegistry
                 │
                 ▼
       SupabaseMCPConnector
                 │
        ┌────────┼────────────────────────┐
        ▼        ▼                        ▼
    MCPClient  PolicyEngine         ActivityStore
        │        │                        │
        ▼        ▼ (Read-only / Risk)     ▼ (req_id audit)
    MCPSession
        │
        ▼
 HttpMCPTransport ──[ Authorization: Bearer <Token> ]──> https://mcp.supabase.com/mcp
        │
 (Streamable HTTP)
```

### Component Roles

| Component | Module | Responsibility |
|---|---|---|
| `SupabaseMCPConnector` | `sutra.connectors.mcp.supabase` | Domain connector adapter handling project scoping, URL synthesis, read-only gating, and analyzer descriptors. |
| `SupabaseCredentialProvider` | `sutra.credentials` | Resolves tokens across Memory, Environment, and OS Keyring with token refresh support. |
| `MCPOAuthClient` | `sutra.connectors.mcp.oauth` | Reusable OAuth 2.0 PKCE client with loopback redirect listener and state/code verification. |
| `MCPClient` | `sutra.connectors.mcp.client` | Protocol client executing `tools/list` and `tools/call`. |
| `MCPSession` | `sutra.connectors.mcp.session` | Manages MCP protocol lifecycle FSM and protocol version negotiation. |
| `HttpMCPTransport` | `sutra.connectors.mcp.http` | Remote Streamable HTTP transport over HTTPS with authorization header injection. |
| [`PolicyEngine`](../../sutra/policy_engine.py) | `sutra.policy_engine` | Evaluates permission risk levels and guards mutating operations. |
| [`ActivityStore`](../../sutra/activity_store.py) | `sutra.activity_store` | Persists sanitized audit trails for connector lifecycle and tool executions. |

---

## 3. Project Scoping & URL Construction

Supabase MCP endpoints require project and feature scoping encoded into the URL query parameters.

### Standard Endpoint Construction
```python
build_supabase_mcp_url(
    base_endpoint="https://mcp.supabase.com/mcp",
    project_ref="xyzcompanyproject",
    read_only=True,
    features=["docs", "database"]
)
# Result:
# https://mcp.supabase.com/mcp?project_ref=xyzcompanyproject&read_only=true&features=docs%2Cdatabase
```

### Parameter Validation
- **`project_ref` Validation**: Required parameter validated against `^[a-z0-9_-]{2,64}$`. Any invalid or malformed strings raise `ValueError` before URL construction.
- **Safe URL Encoding**: Query parameters are constructed using `urllib.parse.urlencode`, preventing path or query injection attacks.
- **Scoping Defaults**: Defaults to `read_only=true` and `features=['docs', 'database']`. Dangerous features such as `branching`, `functions`, or `account_management` are excluded by default.

---

## 4. Authentication Architecture

AgentBridge implements two complementary authentication paths for Supabase MCP:

### Mode 1: Interactive Browser OAuth 2.0 with PKCE
Used during interactive developer CLI setup:
1. AgentBridge generates a cryptographically secure random `state` and PKCE `code_verifier` (SHA-256 `code_challenge`).
2. AgentBridge starts a transient local loopback HTTP server (`OAuthLoopbackServer`) bound to `127.0.0.1` on an OS-assigned ephemeral port (or configured port).
3. AgentBridge constructs the authorization URL targeting `https://api.supabase.com/v1/oauth/authorize` with redirect URI `http://127.0.0.1:<port>/callback`.
4. AgentBridge opens the system browser (hookable for headless test runners).
5. The user authenticates in Supabase and grants project access.
6. Supabase redirects to the loopback server with `code` and `state`.
7. The loopback server validates `state`, extracts `code`, serves a clean confirmation HTML page, and shuts down immediately.
8. AgentBridge exchanges `code` and `code_verifier` at `https://api.supabase.com/v1/oauth/token` for an access token and refresh token.
9. Tokens are securely persisted to the OS Keyring under `supabase-mcp:<connector_id>`.

```
Browser / Supabase                      AgentBridge CLI                   Loopback Server (127.0.0.1)
        │                                   │                                    │
        │                                   ├── Starts loopback listener ───────►│
        │                                   │                                    │
        │◄── Opens authorization URL ───────┤                                    │
        │    (?code_challenge=...&state=...)│                                    │
        │                                   │                                    │
        │── User Authorizes ───────────────►│                                    │
        │                                   │                                    │
        ├── GET /callback?code=...&state=...────────────────────────────────────►│
        │◄── 200 OK (HTML confirmation) ─────────────────────────────────────────┤
        │                                   │                                    │
        │                                   │◄── Returns auth code & state ──────┤
        │                                   ├── Shuts down loopback server ─────►│
        │                                   │
        ├── POST /v1/oauth/token ──────────►│
        │◄── {access_token, refresh_token} ─┤
        │                                   ├── Stores in OS Keyring
```

### Mode 2: Headless Personal Access Token (PAT)
Used for CI/CD, non-interactive environments, and automated agents:
Deterministic resolution precedence:
1. **In-Memory Token** (`CredentialSource.MEMORY`)
2. **Instance-Specific Environment Variable** (`CredentialSource.ENV_PRIMARY`):
   `SUTRA_SUPABASE_ACCESS_TOKEN_<SANITIZED_ID>` (e.g. `SUTRA_SUPABASE_ACCESS_TOKEN_MY_PROJECT`)
3. **Primary Environment Variable** (`CredentialSource.ENV_PRIMARY`):
   `SUTRA_SUPABASE_ACCESS_TOKEN`
4. **Fallback Environment Variable** (`CredentialSource.ENV_FALLBACK`):
   `SUPABASE_ACCESS_TOKEN`
5. **OS Keyring** (`CredentialSource.KEYRING`):
   Entry `supabase-mcp:<connector_id>`

### Automatic Token Refresh
When OAuth credentials near expiration (`expires_at`), `SupabaseCredentialProvider.refresh_if_needed()` contacts the OAuth token endpoint with the stored `refresh_token`, acquires a fresh access token, and updates the keyring backend.

---

## 5. Security & Permission Boundaries

### 5.1 Dual Read-Only Enforcement
1. **Server-Side Guard**: The URL parameter `read_only=true` instructs the official Supabase MCP server to reject mutating operations at the protocol level.
2. **Client-Side Policy Guard**: Prior to executing any tool call, `SupabaseMCPConnector` evaluates the tool:
   - Read-only tools (`get_*`, `list_*`, `read_*`, `search_*`, `describe_*`, `explain_*`, `sql_select`, or tools annotated with `read_only=True`) are mapped to `PermissionRiskLevel.LOW`.
   - Mutating or unknown tools are mapped to `PermissionRiskLevel.HIGH` or `CRITICAL`.
   - When `read_only=True` is configured on the connector, mutating operations are **hard-rejected** with `ConnectorPermissionError` before any network transmission occurs.

### 5.2 Zero Secret Persistence Guarantee
- No tokens or authorization headers are written to `ActivityStore`, SQLite databases, or log files.
- All HTTP diagnostics, error messages, and descriptors pass through `redact_secrets()`.
- Interactive PAT input is masked via `getpass`.
- Keyring failure gracefully prompts the user to use environment variables instead of falling back to plaintext storage.

---

## 6. CLI Usage & Management

The AgentBridge CLI provides first-class commands for Supabase connector administration:

### 1. Register a Supabase Connector
```bash
# Register with project scoping and default read-only safety
sutra connectors add supabase-mcp --project-ref myprojectref

# Custom configuration with specific feature groups
sutra connectors add supabase-staging --project-ref stagingref --read-only --features docs,database
```

### 2. Authenticate
```bash
# Headless / Interactive PAT prompt (stored in OS Keyring)
sutra connectors auth supabase-mcp

# Browser-based OAuth 2.0 PKCE flow
sutra connectors auth supabase-mcp --oauth
```

### 3. Test Connectivity & Scoping
```bash
sutra connectors test supabase-mcp
```
Verifies:
- Endpoint reachability
- MCP session handshake
- Project access and scope verification
- Tool capability discovery

### 4. Inspect Status & Discovered Tools
```bash
# Status and configuration
sutra connectors status supabase-mcp

# Discovered tools
sutra connectors tools supabase-mcp
```

### 5. Remove Credentials
```bash
sutra connectors remove supabase-mcp
```

---

## 7. Testing Strategy

The Supabase MCP integration includes comprehensive deterministic testing covering groups **A through Z** (`tests/test_mcp_supabase.py`):

| Test Group | Verification Description |
|---|---|
| **A** | Connector registration in `ConnectorRegistry` |
| **B** | Default endpoint and URL query parameter construction |
| **C** | Project-reference syntax and security validation |
| **D** | Read-only default setting verification |
| **E** | Feature-group filtering and encoding |
| **F** | Missing credential handling (`ConnectorAuthError`) |
| **G** | Successful mocked Streamable HTTP authentication |
| **H** | OAuth state and PKCE challenge verification |
| **I** | OAuth denial and callback cancellation |
| **J** | Token refresh and expiration handling |
| **K** | Deterministic environment variable precedence |
| **L** | HTTP 401 (AuthError) and 403 (PermissionError) normalization |
| **M** | Network timeout and connection failure handling |
| **N** | MCP session negotiation (`2026-07-28`, `2025-03-26`, `2024-11-05`) |
| **O** | `tools/list` discovery and annotation handling |
| **P** | `tools/call` execution over Streamable HTTP |
| **Q** | Structured data and error response mapping |
| **R** | Mutating operations rejected under `read_only=True` |
| **S** | Unknown tools treated as high risk |
| **T** | Project-instance credential and endpoint isolation |
| **U** | `ActivityStore` audit event correlation by `request_id` |
| **V** | Zero secret persistence audit (repr, metadata, strings) |
| **W** | Generic `ConnectorAnalyzer` 12-stage pipeline integration |
| **X** | Clean disconnect and reconnection lifecycle |
| **Y** | GitHub MCP and Terminal connector coexistence |
| **Z** | Full end-to-end mocked workflow |

---

## 8. Troubleshooting & Safe Testing Guide

### Testing without Production Data
1. Use the local mock server (`tests.mocks.mcp_supabase_test_server.running_mock_supabase_server`):
   ```python
   async with running_mock_supabase_server() as (base_url, state):
       connector = SupabaseMCPConnector(
           connector_id="local-test",
           endpoint=f"{base_url}/mcp",
           project_ref="test-project",
           credential_provider=SupabaseCredentialProvider("local-test", memory_token="sbp_test_token_123"),
           allow_http=True,
       )
       await connector.connect()
   ```
2. For testing against Supabase cloud without risk:
   - Create a free tier Supabase hobby project.
   - Run in read-only mode (`--read-only`).
   - Limit features to `docs,database`.

### Common Error Resolutions
- **`ConnectorAuthError: Authentication failed`**: Ensure your Personal Access Token starts with `sbp_` and has not expired. If using OAuth, re-run `sutra connectors auth supabase-mcp --oauth`.
- **`ConnectorPermissionError: Permission denied`**: The token lacks permission for the specified project, or a mutating operation was blocked by `read_only=True`.
- **`ConnectorConnectionError: Connection failed`**: Verify network connectivity to `https://mcp.supabase.com`.
- **`KeyringError`**: In headless or containerized environments without an OS keyring service, set `SUTRA_SUPABASE_ACCESS_TOKEN` in the environment.
