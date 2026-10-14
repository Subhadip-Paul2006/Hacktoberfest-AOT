# AgentBridge — Reliability & Failure Behavior Guide

## 1. Introduction for Operators & Engineers

This guide details the observable behavior of AgentBridge when running in various operational environments, including downstream GitHub outages, network latency spikes, rate limits, and concurrent user interactions.

---

## 2. Real-World Disruption Scenarios

### 2.1 GitHub Is Down (Circuit Breaker OPEN)
When GitHub REST API experiences a major outage (returning repeated HTTP 500, 502, 503, or 504 errors):
1. **First 5 Requests**: Encounter provider errors and trigger retries with exponential backoff and jitter.
2. **Breaker Trips to OPEN**: After 5 consecutive failures, the dedicated `github_rest` circuit breaker trips to `OPEN`.
3. **Fast-Fail Mode**: All subsequent GitHub requests fail in sub-millisecond time with:
   > *"GitHub service is temporarily unavailable (circuit open). Local Git and Codebase Explorer remain operational. Retry in 28s."*
4. **Degraded Read Cache**: If cached PR or release data is available, AgentBridge returns the cached data in degraded mode rather than throwing errors.
5. **Local Work Remains 100% Intact**:
   - `/git status`, `/git diff`, `/git commit`, `/git branch` continue working normally.
   - Codebase Explorer file navigation, symbol search, and downloads continue uninterrupted.
   - Agent pairing, session heartbeats, and Doctor checks remain fully active.
6. **Automatic Recovery**: After the cooldown period (30 seconds), the circuit enters `HALF_OPEN`. The next valid request executes downstream; on success, the breaker resets to `CLOSED`.

---

### 2.2 GitHub API Rate Limiting (HTTP 429 / 403)
When rate limits are exceeded:
- **Telegram Display**: The user receives a clear, actionable prompt:
  > *"GitHub rate-limited you. Retry after 24 seconds."*
- **No Infinite Hammering**: Requests do not loop aggressively. The background retry policy respects `Retry-After`.
- **Isolation**: One user hitting their rate bucket does not impact other paired users or local Git operations.

---

### 2.3 Double-Clicking Buttons & Duplicate Requests
In high-latency mobile conditions, users may press Telegram action buttons multiple times:
1. **Target Button Allocation**: Action buttons (e.g. `Merge PR #42` or `Post Comment`) are registered with single-use callback IDs (`target:<uuid>`).
2. **First Click**: Pops the callback target, initiates permission verification, and registers an idempotency key with [`IdempotencyStore`](../../sutra/resilience.py).
3. **Second Click**:
   - The target ID is already consumed from callback state, resulting in a silent ignore or *"Action already processing"* prompt.
   - Even if duplicate requests reach the resilience boundary, the `PROCESSING` or `COMPLETED` idempotency status deduplicates the execution and returns the original result without executing a second merge or posting a duplicate comment.

---

### 2.4 Local Git Process Hangs
If a local git command hangs due to a locked index file, corrupted repository, or credential prompt:
1. **Interactive Prompt Prevention**: All commands run with `GIT_TERMINAL_PROMPT = "0"`. Git will never freeze waiting on stdin for username/password.
2. **Bounded Timeout**: After `SUTRA_GIT_TIMEOUT = 30.0s`, the subprocess is forcefully terminated via `proc.kill()`.
3. **Normalized Error**: The command returns code 124 with:
   > *"Local Git command timed out after 30.0s. Workstation remains healthy."*

---

## 3. Observability & Metrics

Operators can monitor system health via Prometheus metrics exposed by [`sutra/metrics.py`](../../sutra/metrics.py):

| Metric Name | Type | Description |
| :--- | :--- | :--- |
| `sutra_requests_total` | Counter | Total requests segmented by success / failure |
| `sutra_request_latency_seconds` | Histogram | Request execution latency distribution |
| `sutra_circuit_breaker_events_total` | Counter | Breaker state transitions (`opened`, `rejected`, `half_open`, `recovered`) |
| `sutra_rate_limit_hits_total` | Counter | Rate limit rejections across user, repo, and global tiers |
| `sutra_retries_total` | Counter | Retry attempts performed by the resilience layer |
| `sutra_cache_hits_total` | Counter | Cache hits vs misses for safe idempotent reads |
| `sutra_degradation_active` | Gauge | Active degraded mode indicators for external dependencies |

### Safe Logging Invariant
Under no circumstances do AgentBridge loggers output GitHub Personal Access Tokens, OAuth credentials, raw Authorization headers, or pairing secrets. All log entries are scrubbed through [`redact_secrets`](../../sutra/redaction.py).
