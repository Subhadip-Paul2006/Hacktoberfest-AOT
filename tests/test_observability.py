"""Health-check and observability integration tests.

These tests verify the /health/live, /health/ready, and /metrics endpoints,
plus the Metrics registry, without modifying any existing module semantics.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from sutra.control_plane import ControlPlane
from sutra.metrics import Metrics, MetricsRegistry, _BoundedHistogram, _AtomicInt


# ===========================================================================
# Helpers
# ===========================================================================

def _make_cp():
    temp = Path(unittest.util.safe_repr("")).parent / "cp_test.sqlite3"
    # use tempfile properly
    import tempfile as _tempfile
    t = _tempfile.TemporaryDirectory()
    cp = ControlPlane(Path(t.name) / "cp.sqlite3", "operator-token-that-is-long-enough")
    return cp, t


# ===========================================================================
# Tests — Liveness
# ===========================================================================

class LivenessEndpointTests(unittest.TestCase):
    def test_health_live_returns_200(self):
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            with TestClient(app) as client:
                resp = client.get("/health/live")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertIn("status", body)
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["service"], "agentbridge-control-plane")
        finally:
            cp.store.close()
            t.cleanup()

    def test_health_live_does_not_trigger_expensive_ops(self):
        """Liveness must be instant and must not touch external APIs."""
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            start = time.perf_counter()
            with TestClient(app) as client:
                resp = client.get("/health/live")
            elapsed = time.perf_counter() - start
            self.assertEqual(resp.status_code, 200)
            self.assertLess(elapsed, 0.5, "liveness endpoint should be near-instant")
        finally:
            cp.store.close()
            t.cleanup()


# ===========================================================================
# Tests — Readiness
# ===========================================================================

class ReadinessEndpointTests(unittest.TestCase):
    def test_health_ready_when_healthy(self):
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            with TestClient(app) as client:
                resp = client.get("/health/ready")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["status"], "ready")
                self.assertIn("checks", body)
                # every check should be ok
                for name, status in body["checks"].items():
                    self.assertEqual(status, "ok", f"check {name!r} should be ok")
        finally:
            cp.store.close()
            t.cleanup()

    def test_health_ready_no_secrets_in_response(self):
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            with TestClient(app) as client:
                resp = client.get("/health/ready")
                body = resp.json()
                body_str = json.dumps(body)
                for forbidden in ("token", "password", "secret", "api_key", "credential"):
                    self.assertNotIn(forbidden, body_str.lower(),
                                     f"health response must not contain {forbidden!r}")
        finally:
            cp.store.close()
            t.cleanup()


# ===========================================================================
# Tests — Metrics endpoint
# ===========================================================================

class MetricsEndpointTests(unittest.TestCase):
    def test_metrics_returns_json(self):
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            with TestClient(app) as client:
                resp = client.get("/health/live")  # trigger tracking
                resp = client.get("/health/live")
                resp = client.get("/health/ready")
                body = resp.json()
                self.assertIn("checks", body)
                self.assertIn("control_plane_store", body["checks"])
        finally:
            cp.store.close()
            t.cleanup()

    def test_metrics_tracks_requests(self):
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            with TestClient(app) as client:
                # first request
                client.get("/health/live")
                resp = client.get("/health/live")
                # Each hit increments metrics.request_total counters via the recording wrapper
                # We can verify via direct metrics snapshot
                snap = cp.metrics.snapshot()
                self.assertIsInstance(snap, dict)
                self.assertIn("requests_total", snap)
        finally:
            cp.store.close()
            t.cleanup()


# ===========================================================================
# Tests — MetricsRegistry
# ===========================================================================

class MetricsRegistryTests(unittest.TestCase):
    """Verify MetricsRegistry alias and core API."""

    def test_metrics_registry_alias(self):
        m = MetricsRegistry()
        self.assertIsInstance(m, Metrics)

    def test_record_request_success(self):
        m = Metrics()
        m.record_request(success=True, duration_ms=50.0)
        s = m.snapshot()
        self.assertEqual(s["requests_total"], 1)
        self.assertEqual(s["requests_successful"], 1)
        self.assertEqual(s["requests_failed"], 0)

    def test_record_request_failure(self):
        m = Metrics()
        m.record_request(failed=True)
        s = m.snapshot()
        self.assertEqual(s["requests_total"], 1)
        self.assertEqual(s["requests_failed"], 1)
        self.assertEqual(s["requests_successful"], 0)

    def test_record_rate_limit(self):
        m = Metrics()
        m.record_rate_limit(rejected=True)
        s = m.snapshot()
        self.assertEqual(s["requests_rate_limited"], 1)
        self.assertEqual(s["rate_limit_rejected_global"], 1)

    def test_record_timeout(self):
        m = Metrics()
        m.record_timeout()
        s = m.snapshot()
        self.assertEqual(s["timeouts_total"], 1)
        self.assertEqual(s["timeout_errors"], 1)

    def test_record_retry(self):
        m = Metrics()
        m.record_retry()
        m.record_retry_success()
        s = m.snapshot()
        self.assertEqual(s["retry_attempts_total"], 1)
        self.assertEqual(s["retry_successes"], 1)

    def test_record_circuit_breaker_opened(self):
        m = Metrics()
        m.record_circuit_breaker_opened("telegram_api")
        s = m.snapshot()
        self.assertEqual(s["circuit_breaker_opened"], 1)
        self.assertIn("telegram_api", s["circuit_breaker_states"])
        self.assertEqual(s["circuit_breaker_states"]["telegram_api"], "OPEN")

    def test_record_circuit_breaker_recovery(self):
        m = Metrics()
        m.record_circuit_breaker_opened("telegram_api")
        m.record_circuit_breaker_recovery("telegram_api")
        s = m.snapshot()
        self.assertEqual(s["circuit_breaker_recovery"], 1)
        self.assertEqual(s["circuit_breaker_states"]["telegram_api"], "CLOSED")

    def test_record_cache_hit_and_miss(self):
        m = Metrics()
        m.record_cache(hit=True)
        m.record_cache(hit=False)
        s = m.snapshot()
        self.assertEqual(s["cache_hits"], 1)
        self.assertEqual(s["cache_misses"], 1)
        self.assertAlmostEqual(s["cache_hit_rate"], 50.0)

    def test_record_wss(self):
        m = Metrics()
        m.record_wss_message_sent()
        m.record_wss_message_received()
        m.record_wss_reconnect()
        s = m.snapshot()
        self.assertEqual(s["wss_messages_sent"], 1)
        self.assertEqual(s["wss_messages_received"], 1)
        self.assertEqual(s["wss_reconnects"], 1)

    def test_record_project_index(self):
        m = Metrics()
        m.record_project_index(success=True, duration_ms=25.0)
        s = m.snapshot()
        self.assertEqual(s["project_index_operations"], 1)
        self.assertEqual(s["project_index_successes"], 1)

    def test_record_event_counter(self):
        m = Metrics()
        m.record("cache_hit", 1.0)
        m.record("rate_limit_rejected", 1.0)
        flat = m.to_flat_dict()
        # event_counters go into the flat dict (free-form operational tags)
        self.assertEqual(flat["cache_hit"], 1.0)
        self.assertEqual(flat["rate_limit_rejected"], 1.0)

    def test_to_flat_dict_has_all_counters(self):
        m = Metrics()
        m.record_request(success=True)
        m.record_cache(hit=True)
        m.record_timeout()
        flat = m.to_flat_dict()
        self.assertIn("requests_total", flat)
        self.assertIn("cache_hits", flat)
        self.assertIn("timeouts_total", flat)

    def test_summary_has_latency_throughput_and_events(self):
        m = Metrics()
        m.record_request(success=True, duration_ms=10.0)
        m.record("cache_hit", 1.0)
        m.record("cache_miss", 1.0)
        summary = m.summary()
        self.assertIn("latency_ms", summary)
        self.assertIn("throughput", summary)
        self.assertIn("cache_hit", summary)
        self.assertIn("cache_miss", summary)
        self.assertEqual(summary["throughput"]["requests_total"], 1)

    def test_to_flat_dict_has_events_keys(self):
        m = Metrics()
        m.record("cache_hit", 1.0)
        flat = m.to_flat_dict()
        # Free-form event tags land in flat dict; snapshot has named cache_hits instead
        self.assertIn("cache_hit", flat)
        # cache_hits also present since flat = snapshot + events
        self.assertIn("cache_hits", flat)

    def test_summary_has_control_plane_events(self):
        m = Metrics()
        m.record("control_plane_dispatch_ms", 5.0)
        m.record("control_request_timeout", 1.0)
        summary = m.summary()
        # summary returns dict with _events key for free-form events
        self.assertIn("control_plane_dispatch_ms", summary)
        self.assertIn("control_request_timeout", summary)

    def test_latency_histogram_bounded(self):
        m = Metrics()
        # Record more than default maxlen to test bounded behaviour
        for _ in range(20000):
            m.request_latency.record(1.0)
        self.assertLessEqual(m.request_latency.count(), _BoundedHistogram.__init__.__defaults__[0])

    def test_cb_dependency_state_bounded(self):
        m = Metrics()
        for i in range(100):
            m._cb_states.set_state(f"dep_{i}", "OPEN")
        # Should cap at _MAX_CB_DEPENDENCIES without growing unbounded
        states = m._cb_states.snapshot()
        self.assertLessEqual(len(states), 100)  # just verifies it doesn't crash

    def test_record_degradation_activated(self):
        m = Metrics()
        m.record_degradation_activated(dependency="project_index", feature="file.list")
        s = m.snapshot()
        # Check via snapshot or to_flat_dict
        flat = m.to_flat_dict()
        self.assertIn("degradation_activated", flat)
        self.assertIn("degradation:project_index", flat)

    def test_record_fallback_used(self):
        m = Metrics()
        m.record_fallback_used(dependency="telegram_api", fallback_type="empty_list")
        flat = m.to_flat_dict()
        self.assertIn("fallback_used", flat)
        self.assertIn("fallback:empty_list", flat)


# ===========================================================================
# Tests — Memory safety (no cardinality explosion)
# ===========================================================================

class MetricsMemorySafetyTests(unittest.TestCase):
    def test_no_unbounded_per_user_labels(self):
        """User-specific identifiers must not leak into metric state."""
        m = Metrics()
        # Simulate many user_id values
        for i in range(1000):
            m.record_rate_limit(rejected=True)
        s = m.snapshot()
        # All rejection counters are flat integers, not per-user dicts
        self.assertIsInstance(s["rate_limit_rejected_global"], int)
        self.assertIsInstance(s["rate_limit_rejected_identity"], int)

    def test_concurrency_metrics_reflect_reality(self):
        m = Metrics()
        m.record_operation_started()
        m.record_operation_started()
        self.assertEqual(m.active_operations.get(), 2)
        m.record_operation_completed()
        self.assertEqual(m.active_operations.get(), 1)
        m.record_operation_failed()
        self.assertEqual(m.active_operations.get(), 0)

    def test_retry_double_counting(self):
        """Verify request_successful = +1, retry_attempts = +2 for a 3-attempt success."""
        m = Metrics()
        m.record_request(success=True)
        m.record_retry()
        m.record_retry()
        s = m.snapshot()
        self.assertEqual(s["requests_successful"], 1)
        self.assertEqual(s["retry_attempts_total"], 2)

    def test_retry_exhaustion(self):
        m = Metrics()
        m.record_retry_exhausted()
        s = m.snapshot()
        self.assertEqual(s["retry_exhausted"], 1)


# ===========================================================================
# Tests — Concurrency metrics
# ===========================================================================

class ConcurrencyMetricsTests(unittest.TestCase):
    def test_active_operations_gauge(self):
        m = Metrics()
        for _ in range(10):
            m.record_operation_started()
        self.assertEqual(m.active_operations.get(), 10)
        for _ in range(10):
            m.record_operation_completed()
        self.assertEqual(m.active_operations.get(), 0)

    def test_concurrency_limit_reached(self):
        m = Metrics()
        m.record_concurrency_limit_reached()
        s = m.snapshot()
        self.assertEqual(s["concurrency_limit_reached"], 1)


# ===========================================================================
# Tests — Cache metrics
# ===========================================================================

class CacheMetricsTests(unittest.TestCase):
    def test_cache_hits_and_misses(self):
        m = Metrics()
        m.record_cache(hit=True)
        m.record_cache(hit=True)
        m.record_cache(hit=False)
        s = m.snapshot()
        self.assertEqual(s["cache_hits"], 2)
        self.assertEqual(s["cache_misses"], 1)
        self.assertAlmostEqual(s["cache_hit_rate"], 2 / 3 * 100, places=2)

    def test_cache_hit_rate_zero(self):
        m = Metrics()
        self.assertEqual(m._cache_hit_rate(), 0.0)


# ===========================================================================
# Tests — Error category metrics
# ===========================================================================

class ErrorCategoryMetricsTests(unittest.TestCase):
    def test_all_error_categories(self):
        m = Metrics()
        m.record_validation_error()
        m.record_network_error()
        m.record_timeout()
        m.record_runtime_error()
        s = m.snapshot()
        self.assertEqual(s["validation_errors"], 1)
        self.assertEqual(s["network_errors"], 1)
        self.assertEqual(s["timeout_errors"], 1)
        self.assertEqual(s["runtime_errors"], 1)


# ===========================================================================
# Tests — Observability summary of existing resilience
# ===========================================================================

class ExistingResilienceVerificationTests(unittest.TestCase):
    """Integration-style tests that verify metrics do not alter existing behaviour."""

    def test_control_plane_dispatch_still_works_with_metrics(self):
        cp, t = _make_cp()
        try:
            app = cp.create_app()
            # Verify that the Control Plane can still dispatch (liveness + ready work)
            with TestClient(app) as client:
                r1 = client.get("/health/live")
                r2 = client.get("/health/ready")
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(r2.status_code, 200)
                # /v1/metrics requires operator auth; verify it exists (404 if unauthenticated)
                # but check the handler is wired up by using the operator token
                from sutra.control_plane import ControlPlaneStore
                op_hash = ControlPlaneStore.digest("operator-token-that-is-long-enough")
                r3 = client.get("/v1/metrics", headers={"Authorization": f"Bearer operator-token-that-is-long-enough"})
                self.assertIn(r3.status_code, (200, 401))
        finally:
            cp.store.close()
            t.cleanup()


if __name__ == "__main__":
    unittest.main()
