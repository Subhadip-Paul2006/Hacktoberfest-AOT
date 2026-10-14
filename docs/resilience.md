# AgentBridge Resilience Architecture

AgentBridge implements an in-process, multi-layer resilience pipeline designed to safeguard the system against request floods, hanging dependencies, transient network blips, and cascading downstream service outages.

---

## 1. Resilience Architecture Pipeline

```text
Incoming Request (Telegram / Operator / API)
       ↓
[ Rate Limiter ]           ← Per-user sliding-window & global rate capping
       ↓
  Control Plane
       ↓
     [ WSS ]               ← Authenticated WebSocket session routing
       ↓
    Runtime
       ↓
[ Circuit Breaker ]        ← Downstream health protection & fail-fast state machine
       ↓
[ Timeout + Retry ]        ← Per-attempt timeout with exponential backoff & full jitter
       ↓
External Dependency        ← Telegram API / Project Discovery / Downstream I/O
       ↓
    Response
```

---

## 2. Core Resilience Mechanisms

### Rate Limiting
**Protects AgentBridge from excessive incoming traffic.**
- **Implementation**: `SlidingWindowRateLimiter` in `sutra/resilience.py`.
- **Policy**: Per-identity (e.g. Telegram `user_id`) sliding window counter with an optional global safety cap.
- **Behavior**: Rejects excess requests immediately with HTTP 429 and a `Retry-After` hint. Does not queue or create backlog.
- **Memory**: Bounded deques holding at most `limit` timestamps per identity, with periodic eviction of expired identities.

### Timeout
**Prevents downstream operations from hanging indefinitely.**
- **Implementation**: Per-attempt timeout via `asyncio.wait_for` inside `with_timeout_and_retry`.
- **Behavior**: If an external dependency or thread execution does not return within `timeout_seconds`, an `asyncio.TimeoutError` is raised, releasing the event loop.

### Retry
**Attempts recovery from transient failures.**
- **Implementation**: `RetryPolicy` and `with_timeout_and_retry` in `sutra/resilience.py`.
- **Policy**: Bounded exponential backoff with full jitter:
  $$\text{delay} = \text{random}(0, \min(\text{max\_delay}, \text{base\_delay} \times 2^{\text{attempt}-1}))$$
- **Retry-After**: Explicit server-provided hints (such as HTTP 429 headers) are respected as delays up to `max_delay`.
- **Scope**: Retries only transient failures (HTTP 500, 502, 503, 504, 429, timeouts, and network connection errors). Permanent client errors (HTTP 400, 401, 403, 404, validation errors) fail immediately without retry.

### Circuit Breaker
**Temporarily stops calling an unhealthy downstream dependency after repeated failures and allows a controlled probe after cooldown.**
- **Implementation**: `CircuitBreaker` in `sutra/resilience.py`.
- **States**: `CLOSED`, `OPEN`, `HALF_OPEN`.

---

## 3. Circuit Breaker State Machine

```text
              failures >= threshold
       ┌──────────────────────────────┐
       │                              ↓
   ┌────────┐                    ┌────────┐
   │ CLOSED │ ─────────────────→ │  OPEN  │
   └───┬────┘                    └───┬────┘
       ↑                             │
       │                             │ cooldown
       │                             ↓
       │                        ┌───────────┐
       │   probe success        │ HALF_OPEN │
       └────────────────────────│           │
                                └─────┬─────┘
                                      │
                                  probe fail
                                      ↓
                                     OPEN
```

### State Definitions & Transitions

1. **`CLOSED` (Normal Operation)**:
   - Requests pass directly to downstream operations.
   - Failures increment an in-memory `failure_count`.
   - Successful operations reset `failure_count` to `0`.
   - When `failure_count >= failure_threshold`, the circuit transitions to **`OPEN`**, setting `opened_at = time.monotonic()`.

2. **`OPEN` (Fail-Fast)**:
   - Downstream calls are **NOT** made.
   - Timeouts and retries are **NOT** initiated.
   - Calls immediately raise `CircuitOpenError(name, retry_after)`.
   - When the cooldown duration (`cooldown_seconds`) expires, the next incoming call transitions the circuit to **`HALF_OPEN`**.

3. **`HALF_OPEN` (Controlled Probing)**:
   - Exactly `half_open_probes` (default `1`) concurrent probe request is allowed through.
   - Any concurrent requests while the probe is in flight are rejected fast with `CircuitOpenError`.
   - **Probe Success**: Circuit transitions back to **`CLOSED`**, and `failure_count` resets to `0`.
   - **Probe Failure**: Circuit transitions back to **`OPEN`**, restarting the cooldown timer (`opened_at` refreshed).

---

## 4. Retry + Circuit Breaker Interaction

The Circuit Breaker wraps the logical request *outside* the timeout and retry loop:

```text
Circuit Breaker
      ↓
Timeout + Retry (Attempts 1..N)
      ↓
External Service
```

### Key Principles:
1. **Logical Request Failure Accounting**: Multiple retries during a single logical operation count as at most **ONE** Circuit Breaker failure. For example, if an operation fails 3 times before retry exhaustion, the Circuit Breaker failure count increments by 1, not 3.
2. **Intermediate Success Recovery**: If Attempt 1 fails but Attempt 2 succeeds, the logical operation succeeds. The Circuit Breaker does not treat this as an outage; it treats it as a successful operation.
3. **HTTP 429 Handling**: HTTP 429 (Too Many Requests) is a rate-limit indicator, not a service health failure. It is retried using `Retry-After` backoff by the retry policy, and is explicitly excluded from tripping the Circuit Breaker.

---

## 5. Protected Downstream Dependencies

AgentBridge uses independent Circuit Breaker instances for isolated failure domains:

1. **`telegram_api`**:
   - Guards Telegram HTTP Bot API operations (`sendMessage`, `editMessageText`, `answerCallbackQuery`).
   - If Telegram API has an outage, the breaker trips to OPEN. The bot logs `circuit_rejected` and skips outbound network requests without blocking the event loop or hammering Telegram.
2. **`project_discovery`**:
   - Guards disk scanning and filesystem directory discovery in `ProjectIndex`.
   - If slow network shares or disconnected mounts cause I/O timeouts, the breaker trips to OPEN. `ProjectIndex` marks `last_error="CircuitOpenError"` and returns immediately from cached snapshots.
3. **Isolation Guarantee**:
   - If `telegram_api` is OPEN, `project_discovery` remains CLOSED and operational. Failure in one dependency does not cascade into unrelated services.

---

## 6. Failure Classification Matrix

| Error Type / Status Code | Retried by Retry Policy? | Counts as Breaker Failure? | Rationale |
| :--- | :---: | :---: | :--- |
| **asyncio.TimeoutError** | Yes | Yes | Downstream dependency unresponsive |
| **ConnectionResetError / Refused** | Yes | Yes | Network or transport layer failure |
| **HTTP 500, 502, 503, 504** | Yes | Yes | Downstream server outage / gateway failure |
| **OSError / I/O Error** | Yes | Yes | Filesystem mount or hardware I/O failure |
| **HTTP 429 (Too Many Requests)** | Yes (`Retry-After`) | **No** | Normal rate limiting; handled via backoff |
| **HTTP 400 (Bad Request)** | No | **No** | Client error; deterministic invalid syntax |
| **HTTP 401 / 403 (Auth Error)** | No | **No** | Credential issue; retries will not help |
| **HTTP 404 (Not Found)** | No | **No** | Resource missing; deterministic |
| **ValueError / KeyError** | No | **No** | Application logic or data validation error |

---

## 7. Configuration Reference

All settings can be specified via environment variables:

| Variable | Default | Description |
| :--- | :---: | :--- |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures before tripping from CLOSED to OPEN |
| `CIRCUIT_BREAKER_OPEN_TIMEOUT` | `30.0` | Cooldown duration in seconds before allowing a probe in HALF_OPEN |
| `CIRCUIT_BREAKER_HALF_OPEN_PROBES` | `1` | Maximum concurrent probe requests in HALF_OPEN |
| `<DEPENDENCY>_CIRCUIT_FAILURE_THRESHOLD` | *Inherits default* | Per-dependency threshold override (e.g. `TELEGRAM_API_CIRCUIT_FAILURE_THRESHOLD`) |
| `<DEPENDENCY>_CIRCUIT_OPEN_TIMEOUT` | *Inherits default* | Per-dependency cooldown override (e.g. `TELEGRAM_API_CIRCUIT_OPEN_TIMEOUT`) |
| `<DEPENDENCY>_CIRCUIT_HALF_OPEN_PROBES` | *Inherits default* | Per-dependency probe override |
