# AgentBridge — Connector Abstraction & Contract (Phase 7.2)

## 1. Architectural Concept & Objectives

The **Connector Abstraction** defines a stable, provider-neutral domain contract for integrating external tools, protocols, and developer environments into AgentBridge.

### Core Architectural Principle
A connector is a **domain boundary**, not an implementation layer. It defines **WHAT** an integration can do, completely decoupled from **HOW** a specific provider or protocol transport implements it.

```
Telegram ChatOps / Agent Runtime / Internal Caller
                       │
                       ▼
            Connector Manager (Future)
                       │
                       ▼
               Connector Contract
          (Identity, Capabilities, Async)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
       MCP            REST           CLI
    (Future)        (Future)       (Future)
        │              │              │
        └──────────────┴──────────────┘
                       │
                       ▼
          Shared Platform Boundaries
     (Permissions · Resilience · Activity Store)
```

### Invariants:
1. **Zero UI Coupling:** Connectors have no awareness of Telegram bots, update polling, or screen formatting.
2. **Authority Stays Central:** Connectors do not make authorization decisions; calls route through [`PolicyEngine`](../../sutra/policy_engine.py).
3. **Zero Resilience Duplication:** Connectors do not embed their own retry loops, exponential backoff, rate limiters, or circuit breakers; they leverage [`sutra.resilience`](../../sutra/resilience.py).
4. **Secret-Free Metadata:** Connectors never store API keys, tokens, or private keys in their descriptor or execution metadata.
5. **Universal Causality:** All execution flows correlate via `request_id` into the local [`ActivityStore`](../../sutra/activity_store.py).

---

## 2. Connector Domain Model

The connector domain model is located in `sutra/connectors/models.py`.

### 2.1 Connector Identity: `ConnectorDescriptor`
Every connector declares an immutable descriptor:
```python
@dataclass(frozen=True)
class ConnectorDescriptor:
    connector_id: str                          # Unique identifier (e.g. 'mcp.github', 'rest.weather')
    name: str                                  # Human-readable title
    type: ConnectorType                        # MCP, REST, CLI, CUSTOM
    version: str = "1.0.0"                     # Semantic version
    description: str = ""                      # Overview of the integration
    capabilities: frozenset[ConnectorCapability] # Declared capabilities
    status: ConnectorStatus                    # Current lifecycle state
    metadata: dict[str, Any]                   # Scrubbed configuration attributes
```

*Provider identity is strictly separated from transport type:*
- `"github"` or `"notion"` is the provider.
- `ConnectorType.MCP` or `ConnectorType.REST` is the connector type.

### 2.2 Connector Types
```python
class ConnectorType(str, Enum):
    MCP = "MCP"        # Model Context Protocol (JSON-RPC over stdio / HTTP)
    REST = "REST"      # Standard HTTP REST endpoints
    CLI = "CLI"        # Local command-line tool execution
    CUSTOM = "CUSTOM"  # Specialized in-process or hybrid adapters
```

---

## 3. Capability Model

Capabilities describe features supported by a connector without exposing transport details:

| Capability | Identifier | Purpose |
|---|---|---|
| `DISCOVERY` | `"discovery"` | Enumerating exposed tools, prompts, and resources |
| `TOOL_EXECUTION` | `"tool_execution"` | Invoking parameterized tools |
| `HEALTH_CHECK` | `"health_check"` | Actively pinging or diagnosing endpoint responsiveness |
| `RESOURCES` | `"resources"` | Reading external structured resource URIs |
| `PROMPTS` | `"prompts"` | Providing parameterized prompt templates |
| `READ` | `"read"` | Supporting read-only operations |
| `WRITE` | `"write"` | Performing mutating or state-changing operations |

The base contract enforces capability declarations:
```python
connector.require_capability(ConnectorCapability.TOOL_EXECUTION)
```
If a caller attempts an operation without the required capability, a `ConnectorInvalidRequestError` is raised immediately without touching the downstream provider.

---

## 4. Lifecycle States

Every connector transitions across deterministic lifecycle states:

```
[ REGISTERED ] ──► connect() ──► [ CONNECTING ] ──► [ READY ]
       │                                                │
       │                                                ├──► degraded / timeout ──► [ DEGRADED ]
       │                                                │
       └──────────────── failure ───────────────────────┴──► [ ERROR ]
                                                        │
                                                  disconnect()
                                                        ▼
                                                 [ DISCONNECTED ]
```

- **`REGISTERED`**: Defined in configuration; transport not yet initialized.
- **`CONNECTING`**: Connection handshake or process spawning underway.
- **`READY`**: Fully operational; ready to accept execution requests.
- **`DEGRADED`**: Partially operational (e.g. rate-limited, upstream circuit half-open).
- **`DISCONNECTED`**: Intentionally shut down; resources freed.
- **`ERROR`**: Encountered an unrecoverable handshake or runtime failure.

---

## 5. Normalized Operations: The Connector Interface

Located in `sutra/connectors/interface.py`:

```python
class Connector(abc.ABC):
    @abc.abstractmethod
    def identity(self) -> ConnectorDescriptor: ...

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def health(self) -> ConnectorHealth: ...

    async def discover(self) -> Sequence[ConnectorToolDefinition | ConnectorResource]: ...

    @abc.abstractmethod
    async def execute(self, request: ConnectorRequest) -> ConnectorResult: ...
```

All network, process, or protocol interactions are strictly asynchronous (`async`/`await`).

---

## 6. Execution & Result Normalization

### 6.1 Execution Request: `ConnectorRequest`
```python
@dataclass
class ConnectorRequest:
    connector_id: str                # Target connector ID
    operation: str                   # Tool name, command, or endpoint action
    request_id: str                  # Universal correlation ID (matches ActivityStore)
    parameters: dict[str, Any]       # Operation inputs/arguments
    project_id: Optional[str]        # Project workspace context
    session_id: Optional[str]        # Active agent/user session context
    actor: Optional[str]             # Caller identity (e.g. 'telegram:123', 'agent')
    source: Optional[str]            # Origin ('agent', 'chatops', 'system')
    metadata: dict[str, Any]         # Secret-free execution flags
```

### 6.2 Execution Result: `ConnectorResult`
Provider adapters normalize responses into a uniform structure:
```python
@dataclass
class ConnectorResult:
    success: bool
    content: Optional[str]           # Human/agent readable summary or text
    structured_data: Optional[Any]   # Dict/List parsed output
    metadata: dict[str, Any]         # Bounded metadata (duration, headers, flags)
    error: Optional[str]             # User-facing redacted error message
    error_code: Optional[str]        # Standardized ConnectorErrorCode
    truncated: bool = False          # True if output was clamped for memory safety
```

*Transport isolation guarantee:* Callers never receive raw `httpx.Response`, `subprocess.CompletedProcess`, or protocol SDK objects.

---

## 7. Error Model

Located in `sutra/connectors/errors.py`, standardized under `ConnectorErrorCode`:

| Code | Exception Class | Description |
|---|---|---|
| `NOT_CONNECTED` | `ConnectorNotConnectedError` | Operation attempted before `connect()` |
| `CONNECTION_FAILED` | `ConnectorConnectionError` | Network handshake or process spawn failed |
| `TIMEOUT` | `ConnectorTimeoutError` | Operation exceeded allowed deadline |
| `AUTHENTICATION_ERROR`| `ConnectorAuthError` | Credentials rejected by upstream provider |
| `AUTHORIZATION_ERROR` | `ConnectorPermissionError` | Operation blocked by permissions |
| `INVALID_REQUEST` | `ConnectorInvalidRequestError` | Missing parameters or unsupported capability |
| `NOT_FOUND` | `ConnectorNotFoundError` | Tool, prompt, or resource does not exist |
| `PROVIDER_ERROR` | `ConnectorProviderError` | Upstream system returned an error |
| `UNAVAILABLE` | `ConnectorUnavailableError` | Upstream host down or circuit breaker OPEN |
| `TOOL_ERROR` | `ConnectorToolError` | Tool invocation crashed during execution |
| `UNKNOWN_ERROR` | `ConnectorError` | Unclassified connector domain failure |

Every `ConnectorError` can be mapped directly to a failed `ConnectorResult` using `err.to_result()`.

---

## 8. Security & Platform Boundaries

### 8.1 Security & Redaction Boundary
- **Input Sanitization:** `sanitize_metadata` scrubs keys matching `token`, `secret`, `password`, `key`, `auth`, `bearer`.
- **Output Sanitization:** `ConnectorResult` and `ConnectorHealth` automatically apply [`redact_secrets`](../../sutra/redaction.py) to string content and error messages.
- **Zero Token Persistence:** Connectors do not persist tokens in SQLite or metadata.

### 8.2 Permission Boundary
Before calling `connector.execute(request)`:
1. Caller maps `ConnectorRequest` to [`PermissionRequest`](../../sutra/permissions.py):
   - `action_type = "tool_call"`
   - `resource_path = request.connector_id`
   - `command = f"{request.connector_id}.{request.operation}"`
2. Evaluated by [`PolicyEngine.check_permission(...)`](../../sutra/policy_engine.py).
3. If denied, execution is rejected before invoking the connector.

### 8.3 Resilience Boundary
Connectors do not duplicate resilience logic:
- Upstream calls are wrapped by [`with_timeout_and_retry`](../../sutra/resilience.py) and [`CircuitBreaker`](../../sutra/resilience.py).
- Rate limits are governed by [`SlidingWindowRateLimiter`](../../sutra/resilience.py).

### 8.4 ActivityStore Relationship
`request.request_id` links seamlessly into the local SQLite activity tables:
- `connector_events`: Records connector state changes and execution attempts.
- `tool_calls`: Records operation parameters and redacted output summaries.
- `get_timeline(request_id)` aggregates connector actions alongside Telegram commands and Git operations.

---

## 9. Downstream Architecture Links & Non-Goals

- **Phase 7.3 (Completed)**: [`ConnectorRegistry`](connector-registry.md) implements dynamic lifecycle management, availability guards, and in-process coordination.
- **Phase 7.4 (Completed)**: [`ConnectorAnalyzer`](environment-analyzer.md) implements the 12-stage provider-neutral environment and connector verification pipeline.
- **Phase 7.5 (Completed)**: [`MCPConnector`](mcp-client.md) implements the transport-agnostic Model Context Protocol client core, session lifecycle FSM, and ActivityStore auditing.
- **Phase 7.6 (Completed)**: [`StdioMCPTransport`](mcp-stdio.md) implements the concrete child process stdio transport with isolated stderr, bounded ring buffering, and graceful teardown.
- **Phase 7.7 (Completed)**: [`HttpMCPTransport`](mcp-http.md) implements remote MCP Streamable HTTP and legacy SSE transports with authentication injection and bounded buffering.
- **Phase 7.8A (Completed)**: [`GitHubMCPConnector`](github-mcp-connector.md) implements the first real remote GitHub MCP integration over Streamable HTTP with portable OS Keyring authentication and PolicyEngine risk enforcement.
- **Phase 7.8B (Completed)**: [`TerminalConnector`](terminal-connector.md) implements the secure local terminal command execution connector conforming to `ConnectorType.CLI`, enforcing strict `shell=False`, workspace directory confinement, PolicyEngine authorization, and clean process tree cleanup.
- **Phase 7.8C (Completed)**: [`SupabaseMCPConnector`](supabase-mcp-connector.md) implements the official remote Supabase MCP integration over Streamable HTTP (`https://mcp.supabase.com/mcp`), project scoping, read-only defaults, feature-group filtering, reusable OAuth 2.0 PKCE with local loopback callback, headless PAT credential resolution, and PolicyEngine protection.
- **Phase 7.8D+ (Deferred Non-Goals)**:
  - Additional third-party providers (Stitch, Vercel, Notion)
  - Telegram `/connectors` and `/history` ChatOps UI
