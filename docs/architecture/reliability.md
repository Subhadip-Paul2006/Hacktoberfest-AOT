# AgentBridge — Reliability & Resilience Architecture

## 1. Executive Summary & Objective

Phase 6C establishes the **Unified Reliability & Resilience Layer** for AgentBridge. The core objective is hardening all Git and GitHub interactions against network anomalies, provider rate limiting, cascading retry storms, hung subprocesses, and upstream outages.

Crucially, AgentBridge establishes an unbreakable boundary between **Authorization** and **Resilience**:
```
                  📱 Telegram
                       │
                       ▼
                Telegram App
                       │
                       ▼
                Control Plane
                       │
                       ▼
                PermissionEngine (Guarded Risk Check)
                       │
              ┌────────┴─────────┐
              │                  │
              ▼                  ▼
         Local Git          GitHub Service
              │                  │
              ▼                  ▼
        git / .git         Resilience Boundary
      (Subprocess          ├─ Rate Limiting (User / Repo / Global)
        Timeout)           ├─ Bounded Timeout (Connect / Read)
              │            ├─ Retry Policy (Exp Backoff + Full Jitter)
              │            ├─ 429 & Retry-After Parser
              │            ├─ Dedicated Circuit Breaker
              │            ├─ Bulkhead Concurrency Semaphore
              │            ├─ Cache & Invalidation Store
              │            ├─ Idempotency & Deduplication
              │            └─ Error Normalization & Redaction
              │                  │
              ▼                  ▼
        Local Machine       GitHub REST API
```

> [!IMPORTANT]
> **Authorization Precedes Resilience**: A retry, cache hit, or circuit-breaker recovery must **never** bypass [`PermissionEngine`](../../sutra/permissions.py). Every mutation or high-risk operation is evaluated and authorized before reaching the resilience boundary.

---

## 2. Core Architectural Pillars

### 2.1 Single Unified Resilience Boundary
All 14 remote GitHub REST operations in [`sutra/git/github.py`](../../sutra/git/github.py) are executed strictly via the [`GitHubResilienceBoundary`](../../sutra/git/resilience.py) singleton:
1. `create_pull_request`
2. `list_pull_requests`
3. `get_pull_request`
4. `get_pull_request_files`
5. `list_pull_request_comments`
6. `add_pull_request_comment`
7. `get_pull_request_reviews`
8. `merge_pull_request`
9. `list_workflow_runs`
10. `get_workflow_run`
11. `get_workflow_run_logs`
12. `cancel_workflow_run`
13. `rerun_workflow_run`
14. `list_releases`

Telegram handlers in [`sutra/apps/telegram/`](../../sutra/apps/telegram) contain **zero** provider retry or rate-limiting logic.

### 2.2 Hierarchical Rate Limiting
Rate limiting operates across three independent sliding-window tiers implemented via [`SlidingWindowRateLimiter`](../../sutra/resilience.py):
- **User Bucket** (`user:<telegram_id>`): Prevents spamming from individual accounts (default: 30 req/min).
- **Repository Bucket** (`repo:<owner>/<repo>`): Shields individual GitHub repositories from secondary rate-limiting (default: 60 req/min).
- **Global Safety Limit** (`global`): Bounded ceiling across the entire node (default: 120 req/min).

When a limit is reached, a normalized `RATE_LIMITED` error is emitted with exact `Retry-After` seconds, avoiding unhandled provider crashes.

### 2.3 Transient Failure Classification & Retries
Retries are managed through [`RetryPolicy`](../../sutra/resilience.py) and [`is_retriable_error`](../../sutra/git/resilience.py):
- **Retriable Errors**: HTTP 429, 500, 502, 503, 504, connection timeouts, socket resets, and transient transport failures.
- **Non-Retriable Errors**: HTTP 400, 401 (bad credentials), 403 (permissions), 404 (not found), 409 (conflict), 422 (validation), stale-state conflicts, and permission denials.
- **Backoff Formula**:
  $$\text{delay} = \text{random}\left(0, \min\left(\text{max\_delay}, \text{base\_delay} \times 2^{\text{attempt} - 1}\right)\right)$$
  Full jitter guarantees that concurrent retries do not thundering-herd GitHub REST servers.
- **Mutations are Non-Retriable by Default**: Write operations (`is_mutation=True`) fail fast unless explicit state reconciliation proves safety.

### 2.4 Dedicated Circuit Breaker
GitHub REST availability is tracked by a dedicated [`CircuitBreaker`](../../sutra/resilience.py) named `github_rest`:
- **CLOSED**: Requests proceed normally. Consecutive failures increment the failure counter.
- **OPEN**: Once 5 consecutive transient failures occur, breaker transitions to `OPEN`. Subsequent calls fail fast in microseconds with a clean `CIRCUIT_OPEN` message without hitting the network.
- **HALF_OPEN**: After a 30-second cooldown, a trial request tests upstream health. Success closes the breaker; failure re-opens it.

### 2.5 Bulkhead Bounded Concurrency
To prevent unbounded connection storms:
- **GitHub REST Bulkhead**: Bounded by `asyncio.Semaphore(10)` (configurable via `SUTRA_GITHUB_MAX_CONCURRENCY`).
- **Local Git Bulkhead**: Bounded by `asyncio.Semaphore(4)` to prevent disk I/O thrashing and lock contention.
- **Zero Heavy Infrastructure**: Implemented purely in in-memory Python primitives without Redis, Celery, RabbitMQ, or Kafka.

### 2.6 Subprocess Timeout Isolation for Local Git
Local Git processes ([`sutra/git/local.py`](../../sutra/git/local.py)) run with:
- Strict execution timeout: `SUTRA_GIT_TIMEOUT = 30.0` seconds.
- Interactive prompt suppression: `GIT_TERMINAL_PROMPT = "0"`.
- Clean timeout recovery: Process terminations raise [`GitSubprocessTimeoutError`](../../sutra/git/local.py) and emit normalized returncode 124 rather than hanging indefinitely.

---

## 3. Graceful Degradation Invariant

One of the foundational acceptance criteria of AgentBridge Phase 6C is **Fault Isolation**:
$$\text{GitHub Outage} \not\equiv \text{AgentBridge Outage}$$

When GitHub REST is completely offline or rate-limited:
- **Local Git**: Status, diff, commit, branch, and log commands continue working locally with zero degradation.
- **Telegram ChatOps**: Bots, pairings, command dispatch, and alerts remain fully operational.
- **Codebase Explorer**: File browsing, previews, search, symbols, and export function with 100% availability.
- **Agent Runtime & Tools**: Agent Bridge, Doctor diagnostics, and workspace tools operate uninterrupted.
