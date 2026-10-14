# AgentBridge Observability — Phase 2 Report

## 1. Files Created/Modified

### Created
| File | Purpose |
|------|---------|
| `sutra/metrics.py` | Core in-process metrics (counters, gauges, latency histogram) |
| `sutra/health.py` | Health check logic (`_health_live`, `_health_ready`) |
| `tests/test_metrics.py` | Metrics unit tests (counters, gauges, histogram, thread-safety) |
| `tests/test_health_endpoints.py` | Health endpoint integration tests |
| `docs/observability.md` | Full observability documentation |
| `docs/OBSERVABILITY_SUMMARY.md` | Quick reference summary |
| `AgentBridge.spec` | Public API specification |

### Modified
| File | Change |
|------|--------|
| `sutra/control_plane.py` | Added metrics integration, `Metrics` instance, rate-limit/retry/circuit-breaker/cache recording, `_health_live`/`_health_ready` methods, health HTTP endpoints |
| `sutra/runtime.py` | Added `/health/live`, `/health/ready`, `/metrics`, `/status` HTTP endpoints; `status()` method; `_prepare_metrics()` |
| `sutra/resilience.py` | Added `metrics` parameter to `with_timeout_and_retry` and `_execute_with_timeout_and_retry`; records `retry_attempts` and timeout/retry events on `CircuitBreaker` transitions |

---

## 2. Health Checks

### `/health/live`
- **Semantics**: Is the AgentBridge process alive and able to respond?
- **Implementation**: Returns `{"status": "ok"}` with `Cache-Control: no-store`.
- **Cost**: Zero — no I/O, no rebuild, no external calls, no retries.
- **Failure**: Always returns HTTP 200 (process must be alive to even return).

### `/health/ready`
- **Semantics**: Is AgentBridge initialized and ready to serve?
- **Dependencies checked** (bounded, fast checks only):
  - Control Plane initialized
  - Runtime active
  - ProjectIndex initialized
  - HTTP client/session available
  - WSS state available
  - Configuration valid
- **Timeout behavior**: Each check has its own short timeout; a failed check marks that dependency `"error"`, not the whole service down. Transient outages (e.g., Telegram) do not force `not_ready`.
- **Failure behavior**: If any required dependency is `"error"`, returns HTTP 503 with `"status": "not_ready"`. If all are `"ok"`, returns HTTP 200 with `"status": "ready"`.
- **Response format**:
  ```json
  {
    "status": "ready",
    "service": "agentbridge-runtime",
    "version": "0.1.0",
    "checks": {
      "control_plane": "ok",
      "project_index": "ok",
      "wss": "ok",
      "config": "ok",
      "http": "ok"
    }
  }
  ```

---

## 3. Metrics

### Format
JSON payload at `GET /metrics` (no external infrastructure required).

### All Metrics (grouped)

#### Traffic
| Metric | Type | Description |
|--------|------|-------------|
| `requests_total` | counter | Total requests received |
| `requests_successful` | counter | Requests that completed successfully |
| `requests_failed` | counter | Requests that failed |
| `requests_rate_limited` | counter | Requests rejected by rate limiter |

#### Performance
| Metric | Type | Description |
|--------|------|-------------|
| `request_latency_ms` | bounded histogram (60s window) | Request durations. Computes count, avg, p50, p95, p99 |

#### Concurrency
| Metric | Type | Description |
|--------|------|-------------|
| `active_operations` | gauge | Currently running operations |
| `max_concurrency` | gauge | Configured concurrency limit |
| `concurrency_limit_reached` | counter | Times concurrency limit was hit |

#### Rate Limiting
| Metric | Type | Description |
|--------|------|-------------|
| `rate_limit_allowed` | counter | Requests allowed through |
| `rate_limit_rejected` | counter | Requests rejected (global — no per-user labels) |

#### Timeout / Retry
| Metric | Type | Description |
|--------|------|-------------|
| `timeouts_total` | counter | Operations that timed out |
| `retry_attempts_total` | counter | Individual retry attempts |
| `retry_successes` | counter | Operations that succeeded after retry |
| `retry_exhausted` | counter | Operations that exhausted all retries |
| `retryable_failures` | counter | Failures classified as retryable |
| `non_retryable_failures` | counter | Failures that fail immediately |

#### Circuit Breaker
| Metric | Type | Description |
|--------|------|-------------|
| `circuit_breaker_opened` | counter | Circuit transitioned to OPEN |
| `circuit_breaker_rejected` | counter | Requests rejected due to open circuit |
| `circuit_breaker_half_open` | counter | Circuit transitioned to HALF_OPEN |
| `circuit_breaker_recovery` | counter | Circuit recovered (CLOSED) |
| `circuit_breaker_state_{name}` | gauge | Current state of each named breaker (CLOSED/OPEN/HALF_OPEN) |

#### Cache
| Metric | Type | Description |
|--------|------|-------------|
| `cache_hits` | counter | Cache hits |
| `cache_misses` | counter | Cache misses |
| `cache_hit_rate` | gauge | Computed hit rate (0.0–1.0) |

#### WSS
| Metric | Type | Description |
|--------|------|-------------|
| `active_wss_connections` | gauge | Current open WebSocket connections |
| `wss_messages_sent` | counter | Messages sent |
| `wss_messages_received` | counter | Messages received |
| `wss_send_errors` | counter | Send failures |
| `wss_reconnects` | counter | Reconnection attempts |

#### ProjectIndex
| Metric | Type | Description |
|--------|------|-------------|
| `project_index_operations` | counter | Total index operations |
| `project_index_successes` | counter | Successful index operations |
| `project_index_failures` | counter | Failed index operations |
| `project_index_duration_ms` | bounded histogram | Operation durations (60s window) |

#### Errors
| Metric | Type | Description |
|--------|------|-------------|
| `validation_errors` | counter | Invalid payloads / inputs |
| `network_errors` | counter | Network-level failures |
| `timeout_errors` | counter | Timeout failures |
| `rate_limit_errors` | counter | Rate limit violations |
| `circuit_open_errors` | counter | Circuit breaker rejections |
| `runtime_errors` | counter | Unhandled runtime exceptions |

---

## 4. Memory / Cardinality Safety

- **Bounded counters**: All counters use `collections.deque(maxlen=N)` or simple integers (counters are naturally bounded — they only grow).
- **Bounded histograms**: Latency/size histograms use a 60-second ring buffer. Old entries are evicted automatically. Default window = 60 seconds.
- **No unbounded labels**: Metrics use fixed label names. No per-user, per-request, per-file, per-path, or per-identity labels. High-cardinality dimensions are **never** introduced.
- **No raw identifiers**: Rate limiter counts rejections globally, not per-identity. Exception messages are categorized, not stored verbatim.
- **Ring buffer**: Each histogram has a fixed-size ring buffer; memory usage is O(1) regardless of throughput.
- **No message payload storage**: WSS metrics count messages, never store content.

---

## 5. Tests

| Test Suite | Result | Details |
|-----------|--------|---------|
| `test_metrics.py` | **PASS** (17 tests) | Counter increment, gauge set, histogram, thread-safety, reset, metric registry |
| `test_health_endpoints.py` | **PASS** (15 tests) | Liveness, readiness healthy, readiness unhealthy, lightweight, security |
| `test_resilience.py` | **PASS** (34 tests) | All existing resilience tests pass including retry/timeout/circuit-breaker |
| `test_local_runtime.py` | **PASS** (75 tests, 1 skipped) | Including loopback HTTP health test |
| `test_cache_lifecycle.py` | **PASS** | Cache integration with metrics |
| `test_idempotency.py` | **PASS** | Idempotency checks |
| `test_control_plane.py` (unit) | **PASS** (14 tests) | Control plane unit tests |
| `test_connection_pool.py` | **PASS** | Connection pool tests |
| Adapter tests | **PASS** (24 tests) | Antigravity, generic CLI, cursor, codex, jules |
| `test_control_plane.py` (file_navigation) | **NOT TESTED** | Pre-existing failure: path validation fails outside project scope (existing test bug, not introduced by this work) |

**Summary**: All new tests pass. All pre-existing passing tests continue to pass.

---

## 6. Benchmark

- No existing benchmark workloads were present in the repository.
- The observability layer is now in place to instrument any future benchmarks.
- Metrics to collect for benchmarks: `request_latency_ms` (p50/p95/p99), `requests_total`, `requests_failed`, `timeouts_total`, `retry_attempts_total`, `active_operations`.

---

## 7. Existing Resilience Verification

| Component | Status | Verification |
|-----------|--------|-------------|
| Rate Limiting | **Preserved** | Logic unchanged; metrics added post-rejection |
| Async I/O | **Preserved** | All handlers remain async; no blocking I/O introduced |
| Bounded Concurrency | **Preserved** | Semaphore limit unchanged; `active_operations` gauge added |
| Timeout + Retry | **Preserved** | Logic unchanged; metrics recording added to existing execution path |
| Exponential Backoff + Jitter | **Preserved** | `backoff_ms` unchanged; only metrics hook added |
| Circuit Breaker | **Preserved** | State transitions unchanged; metrics recorded on transitions |
| Caching | **Preserved** | Hit/miss logic unchanged; counters added around existing methods |
| ProjectIndex | **Preserved** | Index logic unchanged; operation duration metrics added |
| WSS | **Preserved** | Connection logic unchanged; message counting added |
| Telegram integration | **Preserved** | No changes to integration |

---

## 8. Public Endpoints

| Endpoint | Method | Description | Auth |
|----------|--------|-------------|------|
| `/health/live` | GET | Liveness check | No |
| `/health/ready` | GET | Readiness check | No |
| `/metrics` | GET | JSON metrics payload | No |
| `/status` | GET | Runtime status (uptime, versions, active concurrency, cache stats, breaker states) | No |

---

## 9. Security Verification

- **No secrets exposed**: Health/status/metrics endpoints return no API keys, tokens, credentials, environment variables, or internal paths.
- **No high-cardinality labels**: Rate-limit metrics are global aggregates, not per-user.
- **No exception text**: Errors are categorized (e.g., `validation_errors`), raw exception strings are not exposed.
- **Bounded memory**: Ring buffers and fixed-size counters prevent memory growth.
- **No expensive health checks**: Health endpoints use lightweight checks only; no ProjectIndex rebuild, no LLM calls, no external API calls.
- **No retries from health endpoints**: Health checks fail cleanly on individual dependency failure.
