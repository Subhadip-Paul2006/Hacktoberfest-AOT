# AgentBridge Observability — Implementation Report

## 1. Files Created/Modified

### Created
| File | Purpose |
|------|---------|
| `sutra/metrics.py` | Core lightweight in-process metrics system |
| `sutra/degradation.py` | Graceful degradation framework |
| `docs/OBSERVABILITY_REPORT.md` | Detailed documentation of all metrics and endpoints |
| `tests/test_observability.py` | Comprehensive observability tests (99 tests) |
| `AgentBridge.spec` | API specification |

### Modified
| File | Changes |
|------|---------|
| `sutra/runtime.py` | Added `_health_live()`, `_health_ready()`, `/health/live`, `/health/ready`, `/metrics`, `/status` HTTP endpoints; integrated `MetricsRegistry`; graceful degradation wrapper for `handle_remote_command` |
| `sutra/control_plane.py` | Added `_do_dispatch_command` wrapper with graceful degradation; integrated metrics recording for rate limits, retries, circuit breakers, cache hits/misses, latency histograms; added `/health/live`, `/health/ready`, `/v1/metrics` endpoints |
| `sutra/connection.py` | Added WSS metrics recording (messages sent/received, reconnects) |
| `sutra/pairing.py` | Added pairing metrics |
| `sutra/resilience.py` | Integrated timeout/retry metrics into existing decorators |
| `sutra/cache.py` | Added cache hit/miss metrics to existing instrumentation |
| `sutra/degradation.py` | Degradation helpers: `degraded_response`, `can_degrade`, `can_use_cache_fallback`, `is_read_only`, `should_degrade_on_dependency_failure`, `is_non_critical_failure` |
| `sutra/metrics.py` | Core metrics: `Counter`, `BoundedHistogram`, `MetricsRegistry` with `summary()` and `to_flat_dict()` |

---

## 2. Health Checks

### `/health/live`
- **Semantics**: Is the AgentBridge process alive and able to respond?
- **Implementation**: Returns `{"status": "ok", "service": "agentbridge-control-plane", "version": "<version>"}` (Control Plane) or `{"status": "ok", "service": "agentbridge-runtime", "version": "<version>"}` (Runtime).
- **Cost**: Zero — no I/O, no rebuild, no external calls, no retries.
- **Failure**: Always returns HTTP 200 (process must be alive to even return).

### `/health/ready`
- **Semantics**: Is AgentBridge initialized and ready to serve?
- **Dependencies checked** (bounded, fast checks only):
  - State store accessible (in-memory, no I/O)
  - ProjectIndex initialized and responsive
  - Runtime active
  - Control Plane sessions available
  - Configuration valid
  - WSS connectivity state (for runtime)
- **Timeout behavior**: Each check has its own bounded timeout; a failed check marks that dependency `"error"`. Transient outages (e.g., Telegram) do NOT force `not_ready` because they are not required dependencies.
- **Failure behavior**: If any required dependency is `"error"`, returns HTTP 503 with `"status": "not_ready"`. If all are `"ok"`, returns HTTP 200 with `"status": "ready"`.
- **Response format**:
  ```json
  {
    "status": "ready",
    "service": "agentbridge-control-plane",
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
JSON payload at `GET /v1/metrics` (Control Plane) or `GET /metrics` (Runtime). No external infrastructure required.

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
| `request_latency_ms` | bounded histogram | Request durations (p50, p95, p99) |
| `control_plane_dispatch_ms` | bounded histogram | Control plane dispatch latency |
| `cache_lookup_ms` | bounded histogram | Cache lookup latency |

#### Concurrency
| Metric | Type | Description |
|--------|------|-------------|
| `concurrency_operations_started` | counter | Operations started |
| `concurrency_operations_completed` | counter | Operations completed |
| `concurrency_operations_failed` | counter | Operations failed |
| `concurrency_limit_reached` | counter | Concurrency limit reached |

#### Rate Limiting
| Metric | Type | Description |
|--------|------|-------------|
| `rate_limit_allowed` | counter | Rate limit allows |
| `rate_limit_rejected` | counter | Rate limit rejections |

#### Timeout / Retry
| Metric | Type | Description |
|--------|------|-------------|
| `timeouts_total` | counter | Total timeouts |
| `retry_attempts_total` | counter | Total retry attempts |
| `retry_successes` | counter | Retries that succeeded |
| `retry_exhausted` | counter | Retry exhaustion events |
| `retryable_failures` | counter | Failures that triggered retry |
| `non_retryable_failures` | counter | Failures that did not retry |

#### Circuit Breaker
| Metric | Type | Description |
|--------|------|-------------|
| `circuit_breaker_opened` | counter | Circuit breaker OPEN events |
| `circuit_breaker_rejected` | counter | Requests rejected by circuit breaker |
| `circuit_breaker_half_open` | counter | Circuit breaker HALF-OPEN events |
| `circuit_breaker_recovery` | counter | Circuit breaker recovery events |

#### Cache
| Metric | Type | Description |
|--------|------|-------------|
| `cache_hits` | counter | Cache hits |
| `cache_misses` | counter | Cache misses |
| `cache_hit_rate` | derived | Cache hit ratio (derived from hits / (hits + misses)) |

#### WSS
| Metric | Type | Description |
|--------|------|-------------|
| `wss_messages_sent` | counter | WSS messages sent |
| `wss_messages_received` | counter | WSS messages received |
| `wss_send_errors` | counter | WSS send errors |
| `wss_reconnects` | counter | WSS reconnection events |

#### ProjectIndex
| Metric | Type | Description |
|--------|------|-------------|
| `project_index_operations` | counter | ProjectIndex operations |
| `project_index_successes` | counter | Successful operations |
| `project_index_failures` | counter | Failed operations |
| `project_index_duration_ms` | bounded histogram | Operation duration |

#### Errors
| Metric | Type | Description |
|--------|------|-------------|
| `error_validation_errors` | counter | Validation errors |
| `error_network_errors` | counter | Network errors |
| `error_timeout_errors` | counter | Timeout errors |
| `error_rate_limit_errors` | counter | Rate limit errors |
| `error_circuit_open_errors` | counter | Circuit open errors |
| `error_runtime_errors` | counter | Runtime errors |

#### Graceful Degradation
| Metric | Type | Description |
|--------|------|-------------|
| `degradation_activated_total` | counter | Graceful degradation activations |
| `fallback_used_total` | counter | Fallback responses served |
| `fallback_success_total` | counter | Successful fallbacks |
| `fallback_failed_total` | counter | Failed fallbacks |
| `partial_response_total` | counter | Partial responses returned |
| `stale_cache_response_total` | counter | Stale cache responses |
| `feature_unavailable_total` | counter | Features unavailable due to dependency failure |

---

## 4. Memory / Cardinality Safety

### Bounded State
- **Counters**: Simple integer values — O(1) memory per counter.
- **Histograms**: Fixed-size `deque` with `maxlen=1024` samples — bounded by sample limit.
- **No unbounded per-request labels**: Metrics use fixed metric names, never user-specific identifiers, request IDs, file paths, or arbitrary exception messages.
- **No dynamic label creation**: All metric names are predefined strings; the registry does not create new metric series per request.

### Cardinality Guarantees
- No `user_id`, `request_id`, `file_path`, or `exception_message` labels.
- `identity` is aggregated to `"device:<hash>"` — not stored as a metric label.
- Circuit breaker state is exposed as aggregate dependency names (e.g., `telegram_api`, `project_index`), not per-connection state.

---

## 5. Tests

### Results
```
PASS: 237 tests passed
SKIP: 1 test skipped
FAIL: 2 tests failed (intermittent timing-related failures in test_generic_cli_adapter.py when run in full suite — pass individually; pre-existing test flakiness unrelated to observability changes)
```

### Coverage
- **Test 1 (Liveness)**: PASS — `/health/live` returns HTTP 200
- **Test 2 (Readiness healthy)**: PASS — returns HTTP 200 with all checks "ok"
- **Test 3 (Readiness unhealthy)**: PASS — returns HTTP 503 when dependency is unhealthy
- **Test 4 (Health lightweight)**: PASS — no expensive operations triggered
- **Test 5 (Request metrics)**: PASS — `requests_total`, `requests_successful`, `requests_failed` increase correctly
- **Test 6 (Rate-limit metrics)**: PASS — `rate_limit_rejected` increases
- **Test 7 (Timeout metrics)**: PASS — `timeouts_total` increases
- **Test 8 (Retry metrics)**: PASS — `retry_attempts_total` increases correctly
- **Test 9 (Circuit Breaker metrics)**: PASS — `circuit_breaker_opened`, `circuit_breaker_rejected` reflect transitions
- **Test 10 (Cache metrics)**: PASS — cache hit/miss counters increase correctly
- **Test 11 (Concurrency metrics)**: PASS — active-operation metrics reflect concurrency
- **Test 12 (Memory safety)**: PASS — metric state remains bounded

---

## 6. Benchmark

No benchmark changes were made to existing benchmark methodology. The observability layer is designed to be transparent — it observes existing behavior without altering it. Future benchmarks can leverage the new metrics to collect:
- `request_latency_ms` (p50/p95/p99)
- `concurrency_operations_started/completed`
- `timeouts_total`, `retry_attempts_total`
- `cache_hit_rate`

No performance improvements are claimed — the goal was observability, not optimization.

---

## 7. Existing Resilience Verification

| Mechanism | Status | Notes |
|-----------|--------|-------|
| **Rate Limiting** | PRESERVED | Metrics recorded, no behavior change |
| **Async I/O** | PRESERVED | All async operations unchanged |
| **Bounded Concurrency** | PRESERVED | Metrics added, concurrency limit unchanged |
| **Timeout/Retry** | PRESERVED | Metrics added to existing decorators |
| **Circuit Breaker** | PRESERVED | Metrics added, state transitions unchanged |
| **Caching** | PRESERVED | Metrics added, cache semantics unchanged |
| **ProjectIndex** | PRESERVED | Graceful degradation added for non-critical failures |
| **WSS** | PRESERVED | Metrics added, connection lifecycle unchanged |
| **Telegram** | PRESERVED | Not a readiness dependency; resilience unchanged |

---

## 8. Graceful Degradation Summary

### New Framework: `sutra/degradation.py`

| Function | Purpose |
|----------|---------|
| `degraded_response(command, dependency, reason)` | Returns a safe fallback response |
| `can_degrade(command)` | Whether a command supports graceful degradation |
| `can_use_cache_fallback(command)` | Whether cached data can be used |
| `is_read_only(command)` | Whether a command is read-only |
| `should_degrade_on_dependency_failure(command, dependency)` | Whether to degrade on specific dependency failure |
| `is_non_critical_failure(exc)` | Whether an exception is non-critical (ConnectionError, OSError) |

### When Degradation Activates
- **ProjectIndex unavailable**: Read-only commands like `project.list` return empty list with `"degraded": true` flag
- **Control Plane issues**: Non-critical failures return partial data
- **WSS connection lost**: Graceful handling with reconnection

### What Never Degrades
- Authentication/authorization errors
- Validation errors
- Side-effecting operations (workspace.add, agent.start, etc.)
- Timeouts on critical operations

---

## 9. Architecture Diagram

```
                    Telegram
                       ↓
                 Rate Limiter
                       ↓
                 Control Plane
                       ↓
                      WSS
                       ↓
                Async Runtime
                       ↓
              Bounded Concurrency
                       ↓
                 ProjectIndex
                       ↓
                    Cache
                       ↓
               Circuit Breaker
                       ↓
                Timeout + Retry
                       ↓
                External Service


        ┌─────────────────────────────────┐
        │         Observability            │
        │                                 │
        │ MetricsRegistry (in-process)    │
        │   - Counters                    │
        │   - BoundedHistograms           │
        │   - summary() / to_flat_dict()  │
        │                                 │
        │ Graceful Degradation            │
        │   - degraded_response()         │
        │   - should_degrade_on_failure() │
        └─────────────────────────────────┘
                       │
                       ▼
        ┌─────────────────────────────────┐
        │          Health Checks           │
        │                                 │
        │ /health/live → 200 (always)      │
        │ /health/ready → 200/503          │
        │ /v1/metrics → JSON (operator)    │
        │ /metrics → JSON (runtime)        │
        │ /status → diagnostic (runtime)   │
        └─────────────────────────────────┘
```

---

## 10. Key Implementation Details

### MetricsRegistry
- **Thread-safe**: Uses `threading.Lock()` for all counter/histogram access.
- **Async-safe**: Provides `ainc()` and `arecord_ms()` for async contexts.
- **Bounded**: Histograms use `deque(maxlen=1024)` — no unbounded growth.
- **Low-overhead**: Counter increments are O(1), histogram appends are O(1).
- **Easy to test**: All public methods are simple and deterministic.

### Health Endpoints
- **Liveness**: Zero-cost, always returns 200.
- **Readiness**: Bounded checks with fast failure. No external API calls.
- **No retries**: Health checks do not trigger retry logic.
- **No secrets**: Responses contain only service name, version, and check status.

### Graceful Degradation
- **Read-only commands only**: `project.list`, `runtime.status`, `runtime.health` can degrade.
- **Non-critical failures only**: ConnectionError, OSError trigger degradation.
- **Side-effecting commands never degrade**: `workspace.add`, `agent.start`, etc. propagate errors.
- **Transparent to callers**: Response includes `"degraded": true` flag when applicable.
