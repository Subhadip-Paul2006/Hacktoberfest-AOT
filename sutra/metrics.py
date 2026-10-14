"""In-process metrics collection for SŪTRA.

All metrics are aggregate counters or bounded statistical accumulators —
no unbounded per-request state, no external dependencies, and no
cardinality explosion.

Example
-------
::

    from sutra.metrics import get_metrics

    m = get_metrics()
    m.record_request(success=True, latency_ms=42.0)
    m.record_rate_limit(global_=True)
    m.record_timeout()
    m.record_retry()
    m.record_circuit_breaker_opened("telegram_api")
    m.record_cache(hit=True)
    m.record_wss_message_sent()
    m.record_wss_message_received()

    # Read snapshot (safe JSON)
    data = m.to_dict()
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

# ---- Configuration constants (bounded memory) --------------------------------

#: Maximum number of latency samples retained for p50/p95/p99.
_MAX_LATENCY_SAMPLES: int = 10_000
#: Maximum number of per-dependency circuit-breaker states tracked.
_MAX_CB_DEPENDENCIES: int = 64

#: Stable set of well-known event counter names (imported by control_plane).
_EVENT_COUNTER_NAMES: frozenset[str] = frozenset({
    "cache_hit", "cache_miss", "cache_lookup_ms", "control_plane_dispatch_ms",
    "rate_limit_rejected", "single_flight_coalesced", "control_request_timeout",
    "control_request_warning", "wss_roundtrip", "runtime_processing",
    "control_plane_store_ok", "control_plane_store_error",
    "project_index_ok", "project_index_error",
    "activity_store_write_ok", "activity_store_write_error", "activity_store_purge",
    "connector_registered", "connector_unregistered", "connector_connect_success",
    "connector_connect_error", "connector_disconnect", "connector_health_check",
    "connector_enable", "connector_disable",
    "analyzer_run_total", "analyzer_run_success", "analyzer_run_error",
    "mcp_stdio_start_total", "mcp_stdio_start_success_total", "mcp_stdio_start_error_total",
    "mcp_stdio_crash_total", "mcp_stdio_timeout_total", "mcp_stdio_shutdown_total",
    "mcp_http_connect_total", "mcp_http_connect_success_total", "mcp_http_connect_error_total",
    "mcp_http_request_total", "mcp_http_error_total", "mcp_http_timeout_total", "mcp_http_disconnect_total",
})



# ---- Internal helpers --------------------------------------------------------

class _AtomicInt:
    """Thread-safe integer counter."""

    def __init__(self, value: int = 0) -> None:
        self._v = value
        self._lock = Lock()

    def inc(self, n: int = 1) -> int:
        with self._lock:
            self._v += n
            return self._v

    def get(self) -> int:
        with self._lock:
            return self._v

    def to_dict(self) -> int:
        return self.get()


class _BoundedHistogram:
    """Thread-safe bounded histogram for latency samples.

    Retains at most ``_MAX_LATENCY_SAMPLES`` samples in insertion order.
    """

    def __init__(self, maxlen: int = _MAX_LATENCY_SAMPLES) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)
        self._lock = Lock()

    def record(self, value: float) -> None:
        with self._lock:
            self._samples.append(value)

    def percentiles(self, ps: tuple[float, ...] = (50, 95, 99)) -> dict[str, float]:
        """Return ``{"p50": …, "p95": …, "p99": …}``."""
        with self._lock:
            if not self._samples:
                return {f"p{int(p)}": 0.0 for p in ps}
            sorted_s = sorted(self._samples)
            n = len(sorted_s)
            result: dict[str, float] = {}
            for p in ps:
                idx = int(n * p / 100)
                idx = max(0, min(idx, n - 1))
                result[f"p{int(p)}"] = round(sorted_s[idx], 2)
            return result

    def avg(self) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            return round(sum(self._samples) / len(self._samples), 2)

    def count(self) -> int:
        with self._lock:
            return len(self._samples)

    def to_dict(self) -> dict[str, Any]:
        p = self.percentiles()
        p["avg_ms"] = self.avg()
        p["count"] = self.count()
        return p


class _BoundedDependencyState:
    """Thread-safe mapping from dependency name → circuit-breaker state string."""

    def __init__(self, maxlen: int = _MAX_CB_DEPENDENCIES) -> None:
        self._states: dict[str, str] = {}
        self._lock = Lock()
        self._maxlen = maxlen

    def set_state(self, name: str, state: str) -> None:
        with self._lock:
            if name not in self._states and len(self._states) >= self._maxlen:
                return  # silently drop new names — keeps memory bounded
            self._states[name] = state

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._states)


# ---- Public metrics class ----------------------------------------------------

@dataclass
class Metrics:
    """Thread-safe, in-process metrics collector.

    All counters are atomic.  Latency samples are bounded histograms.
    Circuit-breaker dependency states are bounded dicts.
    """

    # Traffic
    requests_total: _AtomicInt = field(default_factory=_AtomicInt)
    requests_successful: _AtomicInt = field(default_factory=_AtomicInt)
    requests_failed: _AtomicInt = field(default_factory=_AtomicInt)
    requests_rate_limited: _AtomicInt = field(default_factory=_AtomicInt)

    # Latency (bounded histogram)
    request_latency: _BoundedHistogram = field(default_factory=_BoundedHistogram)

    # Concurrency
    active_operations: _AtomicInt = field(default_factory=_AtomicInt)
    concurrency_limit_reached: _AtomicInt = field(default_factory=_AtomicInt)
    operation_started: _AtomicInt = field(default_factory=_AtomicInt)
    operation_completed: _AtomicInt = field(default_factory=_AtomicInt)
    operation_failed: _AtomicInt = field(default_factory=_AtomicInt)

    # Rate limiting
    rate_limit_allowed: _AtomicInt = field(default_factory=_AtomicInt)
    rate_limit_rejected_global: _AtomicInt = field(default_factory=_AtomicInt)
    rate_limit_rejected_identity: _AtomicInt = field(default_factory=_AtomicInt)

    # Timeout / retry
    timeouts_total: _AtomicInt = field(default_factory=_AtomicInt)
    retry_attempts_total: _AtomicInt = field(default_factory=_AtomicInt)
    retry_successes: _AtomicInt = field(default_factory=_AtomicInt)
    retry_exhausted: _AtomicInt = field(default_factory=_AtomicInt)
    retryable_failures: _AtomicInt = field(default_factory=_AtomicInt)
    non_retryable_failures: _AtomicInt = field(default_factory=_AtomicInt)

    # Circuit breaker
    circuit_breaker_opened: _AtomicInt = field(default_factory=_AtomicInt)
    circuit_breaker_rejected: _AtomicInt = field(default_factory=_AtomicInt)
    circuit_breaker_half_open: _AtomicInt = field(default_factory=_AtomicInt)
    circuit_breaker_recovery: _AtomicInt = field(default_factory=_AtomicInt)
    _cb_states: _BoundedDependencyState = field(default_factory=_BoundedDependencyState)

    # Cache
    cache_hits: _AtomicInt = field(default_factory=_AtomicInt)
    cache_misses: _AtomicInt = field(default_factory=_AtomicInt)

    # WSS
    wss_active_connections: _AtomicInt = field(default_factory=_AtomicInt)
    wss_messages_sent: _AtomicInt = field(default_factory=_AtomicInt)
    wss_messages_received: _AtomicInt = field(default_factory=_AtomicInt)
    wss_send_errors: _AtomicInt = field(default_factory=_AtomicInt)
    wss_reconnects: _AtomicInt = field(default_factory=_AtomicInt)

    # ProjectIndex
    project_index_operations: _AtomicInt = field(default_factory=_AtomicInt)
    project_index_successes: _AtomicInt = field(default_factory=_AtomicInt)
    project_index_failures: _AtomicInt = field(default_factory=_AtomicInt)
    project_index_duration: _BoundedHistogram = field(default_factory=_BoundedHistogram)

    # Errors (by category)
    validation_errors: _AtomicInt = field(default_factory=_AtomicInt)
    network_errors: _AtomicInt = field(default_factory=_AtomicInt)
    timeout_errors: _AtomicInt = field(default_factory=_AtomicInt)
    rate_limit_errors: _AtomicInt = field(default_factory=_AtomicInt)
    circuit_open_errors: _AtomicInt = field(default_factory=_AtomicInt)
    runtime_errors: _AtomicInt = field(default_factory=_AtomicInt)

    # ------------------------------------------------------------------
    # Generic record() — used by Control Plane for ad-hoc named events
    # ------------------------------------------------------------------

    #: Stable set of well-known event counter names (imported by control_plane).
    _EVENT_COUNTER_NAMES: frozenset[str] = frozenset({
        "cache_hit", "cache_miss", "cache_lookup_ms", "control_plane_dispatch_ms",
        "rate_limit_rejected", "single_flight_coalesced", "control_request_timeout",
        "control_request_warning", "wss_roundtrip", "runtime_processing",
        "control_plane_store_ok", "control_plane_store_error",
        "project_index_ok", "project_index_error",
        "activity_store_write_ok", "activity_store_write_error", "activity_store_purge",
        "connector_registered", "connector_unregistered", "connector_connect_success",
        "connector_connect_error", "connector_disconnect", "connector_health_check",
        "connector_enable", "connector_disable",
        "analyzer_run_total", "analyzer_run_success", "analyzer_run_error",
        "mcp_stdio_start_total", "mcp_stdio_start_success_total", "mcp_stdio_start_error_total",
        "mcp_stdio_crash_total", "mcp_stdio_timeout_total", "mcp_stdio_shutdown_total",
        "mcp_http_connect_total", "mcp_http_connect_success_total", "mcp_http_connect_error_total",
        "mcp_http_request_total", "mcp_http_error_total", "mcp_http_timeout_total", "mcp_http_disconnect_total",
    })

    #: Well-known event counters (all names, values tracked here).
    _event_counters: dict[str, float] = field(default_factory=dict, repr=False, compare=False)

    #: Names of latency-histogram–backed events.
    _HISTOGRAM_EVENTS: frozenset[str] = frozenset({
        "cache_lookup_ms", "control_plane_dispatch_ms", "control_request_timeout",
        "control_request_warning", "wss_roundtrip", "runtime_processing",
    })

    def record(self, name: str, value: float) -> None:
        """Record an ad-hoc named event/metric value.

        Histogram-backed names (ms timings) are also appended to the
        ``request_latency`` bounded histogram so percentile stats are
        available via ``snapshot()`` / ``to_dict()``.
        """
        self._event_counters[name] = self._event_counters.get(name, 0.0) + value
        if name in self._HISTOGRAM_EVENTS:
            self.request_latency.record(value)

    def record_degradation_activated(self, *, dependency: str, feature: str) -> None:
        """Record that graceful degradation was activated for a dependency."""
        self._event_counters["degradation_activated"] = self._event_counters.get("degradation_activated", 0.0) + 1
        self._event_counters[f"degradation:{dependency}"] = (
            self._event_counters.get(f"degradation:{dependency}", 0.0) + 1
        )

    def record_fallback_used(self, *, dependency: str, fallback_type: str) -> None:
        """Record that a fallback response was used."""
        self._event_counters["fallback_used"] = self._event_counters.get("fallback_used", 0.0) + 1
        self._event_counters[f"fallback:{fallback_type}"] = (
            self._event_counters.get(f"fallback:{fallback_type}", 0.0) + 1
        )

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def to_flat_dict(self) -> dict[str, Any]:
        """Return a flat ``{name: value}`` dict of all counters for JSON serialization."""
        flat: dict[str, Any] = {}
        # aggregate counters (flat ints)
        for key, val in self.snapshot().items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                flat[key] = val
        # merge ad-hoc event counters
        for name, val in sorted(self._event_counters.items()):
            flat[name] = round(val, 4)
        return flat

    def summary(self) -> dict[str, Any]:
        """Return a summary dict with latency, throughput, and event counters.

        Events are exposed at the top level as ``{name: {"count": value}}``
        so callers can do ``summary["cache_hit"]`` or
        ``summary["single_flight_coalesced"]["count"]``.
        """
        s = self.snapshot()
        result: dict[str, Any] = {
            "latency_ms": s.pop("request_latency_ms", {}),
            "throughput": {
                "requests_total": s.get("requests_total", 0),
                "requests_successful": s.get("requests_successful", 0),
                "requests_failed": s.get("requests_failed", 0),
            },
        }
        # Flatten event counters at top level
        for name, val in sorted(self._event_counters.items()):
            result[name] = {"count": round(val, 4)}
        return result

    # ------------------------------------------------------------------
    # Traffic
    # ------------------------------------------------------------------

    def record_request(self, *, success: bool | None = None, duration_ms: float = 0.0,
                       latency_ms: float | None = None,
                       failed: bool | None = None, rate_limited: bool = False) -> None:
        self.requests_total.inc()
        if failed is True:
            self.requests_failed.inc()
        elif success is True:
            self.requests_successful.inc()
        elif success is False:
            self.requests_failed.inc()
        if rate_limited:
            self.requests_rate_limited.inc()
        # Accept both names for caller convenience; duration_ms wins if both supplied.
        actual_duration = duration_ms if duration_ms and duration_ms > 0 else (latency_ms or 0.0)
        if actual_duration > 0:
            self.request_latency.record(float(actual_duration))

    # ------------------------------------------------------------------
    # Concurrency
    # ------------------------------------------------------------------

    def record_operation_started(self) -> None:
        self.operation_started.inc()
        self.active_operations.inc()

    def record_operation_completed(self) -> None:
        self.operation_completed.inc()
        self.active_operations.inc(-1)

    def record_operation_failed(self) -> None:
        self.operation_failed.inc()
        self.active_operations.inc(-1)

    def record_concurrency_limit_reached(self) -> None:
        self.concurrency_limit_reached.inc()

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def record_rate_limit(self, *, global_: bool = False, identity: bool = False,
                          rejected: bool = False) -> None:
        self.requests_rate_limited.inc()
        if global_:
            self.rate_limit_rejected_global.inc()
        if identity:
            self.rate_limit_rejected_identity.inc()
        if rejected:
            self.rate_limit_rejected_global.inc()

    # ------------------------------------------------------------------
    # Timeout / retry
    # ------------------------------------------------------------------

    def record_timeout(self) -> None:
        self.timeouts_total.inc()
        self.timeout_errors.inc()

    def record_retry(self, *, success: bool | None = None) -> None:
        self.retry_attempts_total.inc()
        if success is True:
            self.retry_successes.inc()
        elif success is False:
            self.retryable_failures.inc()

    def record_retry_success(self) -> None:
        self.retry_successes.inc()

    def record_retry_exhausted(self) -> None:
        self.retry_exhausted.inc()

    def record_retryable_failure(self) -> None:
        self.retryable_failures.inc()

    def record_non_retryable_failure(self) -> None:
        self.non_retryable_failures.inc()

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def record_circuit_breaker_opened(self, dependency: str) -> None:
        self.circuit_breaker_opened.inc()
        self.circuit_open_errors.inc()
        self._cb_states.set_state(dependency, "OPEN")

    def record_circuit_breaker_rejected(self, dependency: str) -> None:
        self.circuit_breaker_rejected.inc()
        self.circuit_open_errors.inc()

    def record_circuit_breaker_half_open(self, dependency: str) -> None:
        self.circuit_breaker_half_open.inc()
        self._cb_states.set_state(dependency, "HALF_OPEN")

    def record_circuit_breaker_recovery(self, dependency: str) -> None:
        self.circuit_breaker_recovery.inc()
        self._cb_states.set_state(dependency, "CLOSED")

    def record_circuit_breaker(
        self,
        *,
        opened: bool = False,
        rejected: bool = False,
        half_open: bool = False,
        recovered: bool = False,
        dependency: str = "default",
    ) -> None:
        """Unified helper compatible with sutra.resilience.CircuitBreaker."""
        if opened:
            self.record_circuit_breaker_opened(dependency)
        if rejected:
            self.record_circuit_breaker_rejected(dependency)
        if half_open:
            self.record_circuit_breaker_half_open(dependency)
        if recovered:
            self.record_circuit_breaker_recovery(dependency)


    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def record_cache(self, *, hit: bool) -> None:
        if hit:
            self.cache_hits.inc()
        else:
            self.cache_misses.inc()

    # ------------------------------------------------------------------
    # WSS
    # ------------------------------------------------------------------

    def record_wss_message_sent(self) -> None:
        self.wss_messages_sent.inc()

    def record_wss_message_received(self) -> None:
        self.wss_messages_received.inc()

    def record_wss_send_error(self) -> None:
        self.wss_send_errors.inc()

    def record_wss_reconnect(self) -> None:
        self.wss_reconnects.inc()

    # ------------------------------------------------------------------
    # ProjectIndex
    # ------------------------------------------------------------------

    def record_project_index(self, *, success: bool, duration_ms: float = 0.0) -> None:
        self.project_index_operations.inc()
        if success:
            self.project_index_successes.inc()
        else:
            self.project_index_failures.inc()
        if duration_ms > 0:
            self.project_index_duration.record(duration_ms)

    # ------------------------------------------------------------------
    # Errors (by category)
    # ------------------------------------------------------------------

    def record_validation_error(self) -> None:
        self.validation_errors.inc()

    def record_network_error(self) -> None:
        self.network_errors.inc()

    def record_runtime_error(self) -> None:
        self.runtime_errors.inc()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe dict of all current metric values."""
        cb_states = self._cb_states.snapshot()
        return {
            # Traffic
            "requests_total": self.requests_total.get(),
            "requests_successful": self.requests_successful.get(),
            "requests_failed": self.requests_failed.get(),
            "requests_rate_limited": self.requests_rate_limited.get(),
            # Latency
            "request_latency_ms": self.request_latency.to_dict(),
            # Concurrency
            "active_operations": self.active_operations.get(),
            "concurrency_limit_reached": self.concurrency_limit_reached.get(),
            "operation_started": self.operation_started.get(),
            "operation_completed": self.operation_completed.get(),
            "operation_failed": self.operation_failed.get(),
            # Rate limiting
            "rate_limit_allowed": self.rate_limit_allowed.get(),
            "rate_limit_rejected_global": self.rate_limit_rejected_global.get(),
            "rate_limit_rejected_identity": self.rate_limit_rejected_identity.get(),
            # Timeout / retry
            "timeouts_total": self.timeouts_total.get(),
            "retry_attempts_total": self.retry_attempts_total.get(),
            "retry_successes": self.retry_successes.get(),
            "retry_exhausted": self.retry_exhausted.get(),
            "retryable_failures": self.retryable_failures.get(),
            "non_retryable_failures": self.non_retryable_failures.get(),
            # Circuit breaker
            "circuit_breaker_opened": self.circuit_breaker_opened.get(),
            "circuit_breaker_rejected": self.circuit_breaker_rejected.get(),
            "circuit_breaker_half_open": self.circuit_breaker_half_open.get(),
            "circuit_breaker_recovery": self.circuit_breaker_recovery.get(),
            "circuit_breaker_states": cb_states,
            # Cache
            "cache_hits": self.cache_hits.get(),
            "cache_misses": self.cache_misses.get(),
            "cache_hit_rate": self._cache_hit_rate(),
            # WSS
            "wss_active_connections": self.wss_active_connections.get(),
            "wss_messages_sent": self.wss_messages_sent.get(),
            "wss_messages_received": self.wss_messages_received.get(),
            "wss_send_errors": self.wss_send_errors.get(),
            "wss_reconnects": self.wss_reconnects.get(),
            # ProjectIndex
            "project_index_operations": self.project_index_operations.get(),
            "project_index_successes": self.project_index_successes.get(),
            "project_index_failures": self.project_index_failures.get(),
            "project_index_duration_ms": self.project_index_duration.to_dict(),
            # Errors
            "validation_errors": self.validation_errors.get(),
            "network_errors": self.network_errors.get(),
            "timeout_errors": self.timeout_errors.get(),
            "rate_limit_errors": self.rate_limit_errors.get(),
            "circuit_open_errors": self.circuit_open_errors.get(),
            "runtime_errors": self.runtime_errors.get(),
            **{k: v for k, v in self._event_counters.items()},
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for snapshot() — returns a JSON-safe dict."""
        return self.snapshot()

    def _cache_hit_rate(self) -> float:
        hits = self.cache_hits.get()
        total = hits + self.cache_misses.get()
        if total == 0:
            return 0.0
        return round(hits / total * 100, 2)

    @classmethod
    def get_instance(cls) -> "Metrics":
        """Return the global Metrics instance."""
        return get_metrics()


# ---- Backward-compatible alias -------------------------------------------------

#: ``MetricsRegistry`` is an alias for :class:`Metrics` so existing imports
#: such as ``from sutra.metrics import MetricsRegistry`` continue to
#: work without modification.
MetricsRegistry = Metrics


# ---- Singleton access --------------------------------------------------------

_metrics: Metrics | None = None
_metrics_lock = threading.Lock()


def get_metrics() -> Metrics:
    """Return the global Metrics instance (lazy-initialized, thread-safe)."""
    global _metrics
    if _metrics is None:
        with _metrics_lock:
            if _metrics is None:
                _metrics = Metrics()
    return _metrics


def reset_metrics() -> None:
    """Reset the global Metrics instance (useful in tests)."""
    global _metrics
    with _metrics_lock:
        _metrics = Metrics()
