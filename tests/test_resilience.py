"""Tests for rate limiting, timeout, and retry resilience mechanisms."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from sutra.control_plane import ControlPlane
from sutra.project_index import ProjectIndex
from sutra.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    SlidingWindowRateLimiter,
    _is_breaker_failure,
    _is_transient,
    with_timeout_and_retry,
)
from sutra.telegram_bot import TelegramBot


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def test_basic_allowing_up_to_limit(self):
        limiter = SlidingWindowRateLimiter(limit=3, window_seconds=10.0, global_limit=None)
        self.assertEqual(limiter.is_allowed("user1"), (True, 0.0))
        self.assertEqual(limiter.is_allowed("user1"), (True, 0.0))
        self.assertEqual(limiter.is_allowed("user1"), (True, 0.0))

        # 4th request should be rejected
        allowed, retry_after = limiter.is_allowed("user1")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)
        self.assertLessEqual(retry_after, 10.0)

    def test_per_identity_isolation(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0, global_limit=None)
        self.assertTrue(limiter.is_allowed("user1")[0])
        self.assertTrue(limiter.is_allowed("user1")[0])
        self.assertFalse(limiter.is_allowed("user1")[0])

        # user2 should still be allowed
        self.assertTrue(limiter.is_allowed("user2")[0])
        self.assertTrue(limiter.is_allowed("user2")[0])
        self.assertFalse(limiter.is_allowed("user2")[0])

    def test_window_sliding_expiration(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=2.0, global_limit=None)
        now = 1000.0

        with patch("time.monotonic", side_effect=lambda: now):
            self.assertTrue(limiter.is_allowed("u")[0])
            self.assertTrue(limiter.is_allowed("u")[0])
            self.assertFalse(limiter.is_allowed("u")[0])

        # Advance time by 2.1 seconds
        now = 1002.1
        with patch("time.monotonic", side_effect=lambda: now):
            self.assertTrue(limiter.is_allowed("u")[0])

    def test_global_limit_enforcement(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0, global_limit=3)
        self.assertTrue(limiter.is_allowed("u1")[0])
        self.assertTrue(limiter.is_allowed("u1")[0])
        self.assertTrue(limiter.is_allowed("u2")[0])

        # u3 is within its own limit (0/2) but global limit (3/3) is reached
        allowed, retry_after = limiter.is_allowed("u3")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)

    def test_stale_identity_cleanup(self):
        now = 100.0
        with patch("time.monotonic", side_effect=lambda: now):
            limiter = SlidingWindowRateLimiter(limit=2, window_seconds=2.0, global_limit=None)
            limiter.is_allowed("u1")
            limiter.is_allowed("u2")
            self.assertEqual(limiter.active_identity_count, 2)

            # Advance past cleanup threshold
            now = 105.0
            limiter.is_allowed("u3")
            # u1 and u2 should have been purged during cleanup
            self.assertEqual(limiter.active_identity_count, 1)

    def test_from_environment(self):
        with patch.dict(os.environ, {"RATE_LIMIT": "5", "RATE_LIMIT_WINDOW": "30", "RATE_LIMIT_GLOBAL": "50"}):
            limiter = SlidingWindowRateLimiter.from_environment()
            self.assertEqual(limiter.limit, 5)
            self.assertEqual(limiter.window_seconds, 30.0)
            self.assertEqual(limiter.global_limit, 50)

    def test_reset(self):
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=10.0)
        self.assertTrue(limiter.is_allowed("u")[0])
        self.assertFalse(limiter.is_allowed("u")[0])
        limiter.reset()
        self.assertTrue(limiter.is_allowed("u")[0])


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_backoff_bounds(self):
        policy = RetryPolicy(max_retries=3, base_delay=1.0, max_delay=10.0)
        # Full jitter: backoff is in [0, min(max_delay, base * 2^(attempt-1))]
        for _ in range(50):
            d1 = policy.backoff_seconds(1)
            self.assertGreaterEqual(d1, 0.0)
            self.assertLessEqual(d1, 1.0)

            d2 = policy.backoff_seconds(2)
            self.assertGreaterEqual(d2, 0.0)
            self.assertLessEqual(d2, 2.0)

            d3 = policy.backoff_seconds(3)
            self.assertGreaterEqual(d3, 0.0)
            self.assertLessEqual(d3, 4.0)

            d10 = policy.backoff_seconds(10)
            self.assertGreaterEqual(d10, 0.0)
            self.assertLessEqual(d10, 10.0)

    def test_retry_after_header_respected(self):
        policy = RetryPolicy(max_retries=2, base_delay=1.0, max_delay=10.0)
        for _ in range(20):
            # retry_after is 5.0 -> floor is min(5.0, 10.0) = 5.0
            delay = policy.backoff_seconds(1, retry_after=5.0)
            self.assertGreaterEqual(delay, 5.0)
            self.assertLessEqual(delay, 10.0)

        # retry_after exceeding max_delay is capped
        delay_capped = policy.backoff_seconds(1, retry_after=60.0)
        self.assertLessEqual(delay_capped, 10.0)

    def test_from_environment(self):
        with patch.dict(os.environ, {"MAX_RETRIES": "4", "RETRY_BASE_DELAY": "0.5", "RETRY_MAX_DELAY": "15.0"}):
            policy = RetryPolicy.from_environment()
            self.assertEqual(policy.max_retries, 4)
            self.assertEqual(policy.base_delay, 0.5)
            self.assertEqual(policy.max_delay, 15.0)


class WithTimeoutAndRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_call_no_retry(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            return "success"

        policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.1)
        result = await with_timeout_and_retry(op, timeout_seconds=1.0, policy=policy)
        self.assertEqual(result, "success")
        self.assertEqual(calls, 1)

    async def test_transient_error_retries_and_succeeds(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise HTTPException(503, "temporary service unavailable")
            return "recovered"

        policy = RetryPolicy(max_retries=3, base_delay=0.001, max_delay=0.01)
        result = await with_timeout_and_retry(op, timeout_seconds=1.0, policy=policy)
        self.assertEqual(result, "recovered")
        self.assertEqual(calls, 3)

    async def test_transient_error_exhausts_retries(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            raise HTTPException(504, "gateway timeout")

        policy = RetryPolicy(max_retries=2, base_delay=0.001, max_delay=0.01)
        with self.assertRaises(HTTPException) as ctx:
            await with_timeout_and_retry(op, timeout_seconds=1.0, policy=policy)
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(calls, 3)  # initial + 2 retries

    async def test_non_transient_error_fails_immediately_without_retry(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            raise HTTPException(400, "bad request")

        policy = RetryPolicy(max_retries=3, base_delay=0.001, max_delay=0.01)
        with self.assertRaises(HTTPException) as ctx:
            await with_timeout_and_retry(op, timeout_seconds=1.0, policy=policy)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(calls, 1)  # No retry for 400!

    async def test_timeout_is_retried(self):
        calls = 0

        async def slow_op():
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.5)
            return "done"

        policy = RetryPolicy(max_retries=2, base_delay=0.001, max_delay=0.01)
        result = await with_timeout_and_retry(slow_op, timeout_seconds=0.05, policy=policy)
        self.assertEqual(result, "done")
        self.assertEqual(calls, 2)


class ControlPlaneRateLimitingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "operator-token-long-enough-32-chars")
        self.cp.rate_limiter = SlidingWindowRateLimiter(limit=3, window_seconds=10.0, global_limit=10)

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    def _setup_mock_device(self, device_id="AB-PC-RL001"):
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(
            device_id, "mock", "1.0", "linux",
            self.cp.store.digest("token"), time.time() + 3600, session_id,
        )
        self.cp.store.set_session_status(device_id, session_id, generation, "CONNECTED", connected=True)
        socket = AsyncMock()
        self.cp.sessions[device_id] = socket
        self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
        self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
        from sutra.connectivity import ConnectivityGuard
        self.cp.connectivity[device_id] = ConnectivityGuard(self.cp.policy)
        self.cp.connectivity[device_id].state = "HEALTHY"
        return device_id

    async def test_dispatch_command_rate_limited_on_wss_path(self):
        device_id = self._setup_mock_device()

        # Cache misses consume rate limit
        # Mock _dispatch_to_device so we don't need full WSS roundtrip
        async def mock_dispatch(*args, **kwargs):
            return {"status": "success", "payload": {"healthy": True}}

        self.cp._dispatch_to_device = mock_dispatch

        # 3 calls allowed
        for _ in range(3):
            res = await self.cp.dispatch_command(device_id, "project.refresh", {})
            self.assertEqual(res["status"], "success")

        # 4th call to uncached command should raise HTTPException(429)
        with self.assertRaises(HTTPException) as ctx:
            await self.cp.dispatch_command(device_id, "project.refresh", {})
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("Retry-After", ctx.exception.headers)

    async def test_cached_command_does_not_consume_rate_limit(self):
        device_id = self._setup_mock_device()

        async def mock_dispatch(*args, **kwargs):
            return {"status": "success", "payload": {"items": ["proj1"]}}

        self.cp._dispatch_to_device = mock_dispatch

        # 1st call populates cache
        r1 = await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(r1["status"], "success")
        self.assertFalse(r1.get("cached", False))

        # Even if rate limit is set to 0 remaining, cached responses succeed!
        self.cp.rate_limiter.limit = 1
        for _ in range(5):
            r = await self.cp.dispatch_command(device_id, "project.list", {})
            self.assertTrue(r.get("cached", False))


class TelegramBotResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "operator-token-long-enough-32-chars")
        self.bot = TelegramBot(self.cp, "test-bot-token", {42, 99})
        self.bot.rate_limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0)

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    async def test_telegram_message_flood_triggers_rate_limit(self):
        update = {
            "update_id": 1,
            "message": {
                "message_id": 100,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/help",
            },
        }

        # 1st update allowed
        res1 = await self.bot.process_update(update)
        self.assertIn("navigation", str(res1).lower())

        # 2nd update allowed
        update["update_id"] = 2
        res2 = await self.bot.process_update(update)
        self.assertIn("navigation", str(res2).lower())

        # 3rd update should be rate limited
        update["update_id"] = 3
        res3 = await self.bot.process_update(update)
        self.assertIn("rate limit exceeded", str(res3).lower())

        # Different user_id (99) is NOT rate limited
        update_user2 = {
            "update_id": 4,
            "message": {
                "message_id": 101,
                "from": {"id": 99},
                "chat": {"id": 99, "type": "private"},
                "text": "/help",
            },
        }
        res4 = await self.bot.process_update(update_user2)
        self.assertIn("navigation", str(res4).lower())


class ProjectIndexResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_index_uses_timeout_and_retry(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(temp_dir.name)
            (root / "myproject").mkdir()
            (root / "myproject" / "package.json").write_text("{}", encoding="utf-8")

            policy = RetryPolicy(max_retries=2, base_delay=0.001, max_delay=0.01)
            index = ProjectIndex([root], timeout_seconds=2.0, retry_policy=policy)

            # Schedule and wait
            index.schedule_refresh()
            await index.wait_for_refresh()

            data = index.snapshot()
            self.assertEqual(len(data["items"]), 1)
            self.assertEqual(data["items"][0]["name"], "myproject")
            self.assertIsNone(data["error"])
        finally:
            temp_dir.cleanup()


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_1_closed_state_healthy(self):
        cb = CircuitBreaker("test_dep", failure_threshold=3, cooldown_seconds=10.0)
        self.assertTrue(cb.is_closed)
        self.assertEqual(cb.failure_count, 0)

        async def healthy_op():
            return "ok"

        res = await cb.call(healthy_op)
        self.assertEqual(res, "ok")
        self.assertTrue(cb.is_closed)
        self.assertEqual(cb.failure_count, 0)

    async def test_2_failure_accumulation_and_trip(self):
        cb = CircuitBreaker("test_dep", failure_threshold=3, cooldown_seconds=10.0)

        async def failing_op():
            raise HTTPException(503, "service unavailable")

        # Failure 1
        with self.assertRaises(HTTPException):
            await cb.call(failing_op)
        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.failure_count, 1)

        # Failure 2
        with self.assertRaises(HTTPException):
            await cb.call(failing_op)
        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.failure_count, 2)

        # Failure 3 -> Threshold reached -> trips to OPEN
        with self.assertRaises(HTTPException):
            await cb.call(failing_op)
        self.assertEqual(cb.state, "OPEN")
        self.assertTrue(cb.is_open)
        self.assertEqual(cb.failure_count, 3)
        self.assertGreater(cb.opened_at, 0.0)

    async def test_3_open_state_fail_fast(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=30.0)
        cb.state = "OPEN"
        cb.opened_at = time.monotonic()

        called = False
        async def downstream_op():
            nonlocal called
            called = True
            return "called"

        with self.assertRaises(CircuitOpenError) as ctx:
            await cb.call(downstream_op)

        self.assertFalse(called, "Downstream dependency must NOT be called while circuit is OPEN")
        self.assertEqual(ctx.exception.name, "test_dep")
        self.assertGreater(ctx.exception.retry_after, 0.0)
        self.assertLessEqual(ctx.exception.retry_after, 30.0)

    async def test_4_cooldown_transitions_to_half_open(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=5.0)
        now = 1000.0
        cb.state = "OPEN"
        cb.opened_at = now

        # Before cooldown expires -> remains OPEN, fails fast
        with patch("time.monotonic", side_effect=lambda: now + 3.0):
            with self.assertRaises(CircuitOpenError):
                await cb.call(AsyncMock())
            self.assertEqual(cb.state, "OPEN")

        # After cooldown expires -> transitions to HALF_OPEN when call is made
        executed = False
        async def probe_op():
            nonlocal executed
            executed = True
            return "probe_done"

        with patch("time.monotonic", side_effect=lambda: now + 5.1):
            res = await cb.call(probe_op)
            self.assertTrue(executed)
            self.assertEqual(res, "probe_done")
            # Successful probe immediately closes the breaker
            self.assertEqual(cb.state, "CLOSED")

    async def test_5_half_open_success_closes_circuit(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=5.0)
        cb.state = "OPEN"
        cb.failure_count = 2
        now = 1000.0
        cb.opened_at = now

        async def probe_success():
            return "recovered"

        # Advance past cooldown
        with patch("time.monotonic", side_effect=lambda: now + 6.0):
            res = await cb.call(probe_success)

        self.assertEqual(res, "recovered")
        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.failure_count, 0)
        self.assertTrue(cb.is_closed)

        # Subsequent requests proceed normally
        res2 = await cb.call(probe_success)
        self.assertEqual(res2, "recovered")
        self.assertEqual(cb.state, "CLOSED")

    async def test_6_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=5.0)
        cb.state = "OPEN"
        cb.failure_count = 2
        now = 1000.0
        cb.opened_at = now

        async def probe_fail():
            raise ConnectionResetError("downstream reset connection")

        # Advance past cooldown to enter probe
        reopened_time = 1006.0
        with patch("time.monotonic", side_effect=lambda: reopened_time):
            with self.assertRaises(ConnectionResetError):
                await cb.call(probe_fail)

        # Breaker should re-open with fresh opened_at
        self.assertEqual(cb.state, "OPEN")
        self.assertTrue(cb.is_open)
        self.assertEqual(cb.opened_at, reopened_time)
        self.assertEqual(cb.probe_in_flight, 0)

    async def test_7_only_one_half_open_probe_allowed_concurrently(self):
        cb = CircuitBreaker("test_dep", failure_threshold=1, cooldown_seconds=5.0, half_open_probes=1)
        cb.state = "OPEN"
        now = 1000.0
        cb.opened_at = now

        probe_started = asyncio.Event()
        probe_proceed = asyncio.Event()
        probe_executions = 0

        async def controlled_probe():
            nonlocal probe_executions
            probe_executions += 1
            probe_started.set()
            await probe_proceed.wait()
            return "ok"

        # Advance time so cooldown has expired
        with patch("time.monotonic", side_effect=lambda: now + 10.0):
            # Task 1 starts probe and holds it
            t1 = asyncio.create_task(cb.call(controlled_probe))
            await probe_started.wait()

            # Now breaker is in HALF_OPEN and probe_in_flight == 1
            self.assertEqual(cb.state, "HALF_OPEN")
            self.assertEqual(cb.probe_in_flight, 1)

            # Concurrent tasks 2, 3, 4 attempt while probe is in flight
            async def concurrent_attempt():
                return await cb.call(AsyncMock())

            for _ in range(4):
                with self.assertRaises(CircuitOpenError):
                    await concurrent_attempt()

            # Release the first probe
            probe_proceed.set()
            await t1

        self.assertEqual(probe_executions, 1, "Only exactly 1 probe execution was permitted")
        self.assertEqual(cb.state, "CLOSED")

    async def test_8_retry_interaction_multiple_retries_count_as_single_failure(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=10.0)
        policy = RetryPolicy(max_retries=2, base_delay=0.001, max_delay=0.01)

        calls = 0
        async def failing_op():
            nonlocal calls
            calls += 1
            raise HTTPException(504, "gateway timeout")

        # 1 logical request that retries 2 times (3 calls total)
        with self.assertRaises(HTTPException):
            await with_timeout_and_retry(
                failing_op,
                timeout_seconds=1.0,
                policy=policy,
                circuit_breaker=cb,
            )

        self.assertEqual(calls, 3)
        # CRITICAL: Breaker failure count should only increment by 1, NOT 3
        self.assertEqual(cb.failure_count, 1)
        self.assertEqual(cb.state, "CLOSED")

    async def test_9_successful_retry_counts_as_logical_success(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=10.0)
        policy = RetryPolicy(max_retries=2, base_delay=0.001, max_delay=0.01)

        calls = 0
        async def flaky_op():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPException(502, "bad gateway transient")
            return "success_on_retry"

        result = await with_timeout_and_retry(
            flaky_op,
            timeout_seconds=1.0,
            policy=policy,
            circuit_breaker=cb,
        )

        self.assertEqual(result, "success_on_retry")
        self.assertEqual(calls, 2)
        # Breaker does not count intermediate retries as failure
        self.assertEqual(cb.failure_count, 0)
        self.assertEqual(cb.state, "CLOSED")

    async def test_10_permanent_errors_do_not_trip_circuit_breaker(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=10.0)

        for status_code in (400, 401, 403, 404):
            async def bad_client_request():
                raise HTTPException(status_code, f"client error {status_code}")

            with self.assertRaises(HTTPException):
                await cb.call(bad_client_request)

            self.assertEqual(cb.failure_count, 0)
            self.assertEqual(cb.state, "CLOSED")

        # Also ValueError / application validation error
        async def validation_err():
            raise ValueError("malformed parameter")

        with self.assertRaises(ValueError):
            await cb.call(validation_err)

        self.assertEqual(cb.failure_count, 0)
        self.assertEqual(cb.state, "CLOSED")

    async def test_11_http_429_does_not_trip_circuit_breaker(self):
        cb = CircuitBreaker("test_dep", failure_threshold=2, cooldown_seconds=10.0)
        policy = RetryPolicy(max_retries=1, base_delay=0.001, max_delay=0.01)

        calls = 0
        async def rate_limited_upstream():
            nonlocal calls
            calls += 1
            headers = {"Retry-After": "0.001"}
            raise HTTPException(429, "rate limited by upstream", headers=headers)

        # 429 exhausts retries
        with self.assertRaises(HTTPException):
            await with_timeout_and_retry(
                rate_limited_upstream,
                timeout_seconds=1.0,
                policy=policy,
                circuit_breaker=cb,
            )

        self.assertEqual(calls, 2)
        # HTTP 429 is handled by retry and must NOT count as a breaker failure!
        self.assertEqual(cb.failure_count, 0)
        self.assertEqual(cb.state, "CLOSED")

    async def test_12_dependency_isolation(self):
        telegram_cb = CircuitBreaker("telegram_api", failure_threshold=2, cooldown_seconds=30.0)
        project_cb = CircuitBreaker("project_discovery", failure_threshold=2, cooldown_seconds=30.0)

        async def failing_tg():
            raise HTTPException(500, "telegram server error")

        async def healthy_proj():
            return [{"name": "proj1"}]

        # Trip telegram_cb to OPEN
        for _ in range(2):
            with self.assertRaises(HTTPException):
                await telegram_cb.call(failing_tg)

        self.assertEqual(telegram_cb.state, "OPEN")

        # project_cb must remain healthy and CLOSED
        res = await project_cb.call(healthy_proj)
        self.assertEqual(res, [{"name": "proj1"}])
        self.assertEqual(project_cb.state, "CLOSED")
        self.assertEqual(project_cb.failure_count, 0)

    def test_environment_configuration(self):
        env = {
            "CIRCUIT_BREAKER_FAILURE_THRESHOLD": "7",
            "CIRCUIT_BREAKER_OPEN_TIMEOUT": "45.0",
            "CIRCUIT_BREAKER_HALF_OPEN_PROBES": "2",
        }
        with patch.dict(os.environ, env):
            cb = CircuitBreaker.from_environment("generic_service")
            self.assertEqual(cb.failure_threshold, 7)
            self.assertEqual(cb.cooldown_seconds, 45.0)
            self.assertEqual(cb.half_open_probes, 2)

        # Per-dependency override
        env_override = {
            "CIRCUIT_BREAKER_FAILURE_THRESHOLD": "5",
            "TELEGRAM_API_CIRCUIT_FAILURE_THRESHOLD": "3",
            "TELEGRAM_API_CIRCUIT_OPEN_TIMEOUT": "15.0",
        }
        with patch.dict(os.environ, env_override):
            tg_cb = CircuitBreaker.from_environment("telegram_api")
            self.assertEqual(tg_cb.failure_threshold, 3)
            self.assertEqual(tg_cb.cooldown_seconds, 15.0)


class CircuitBreakerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_bot_circuit_breaker_fails_fast_when_open(self):
        temp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(temp.name) / "cp.sqlite3"
            cp = ControlPlane(db_path, "operator-token-long-enough-32-chars")
            bot = TelegramBot(cp, "test-bot-token", {42})

            # Force breaker open
            bot.circuit_breaker.state = "OPEN"
            bot.circuit_breaker.opened_at = time.monotonic()

            client = AsyncMock()
            # Calling _telegram_request while circuit is OPEN must return None immediately
            # without making any client.post call
            res = await bot._telegram_request(client, "sendMessage", {"chat_id": 42, "text": "hello"})
            self.assertIsNone(res)
            client.post.assert_not_called()
            cp.store.close()
        finally:
            temp.cleanup()

    async def test_project_index_circuit_breaker_sets_error(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(temp_dir.name)
            cb = CircuitBreaker("project_discovery", failure_threshold=2, cooldown_seconds=30.0)
            cb.state = "OPEN"
            cb.opened_at = time.monotonic()

            index = ProjectIndex([root], circuit_breaker=cb)
            index.schedule_refresh()
            await index.wait_for_refresh()

            snap = index.snapshot()
            # Breaker was open, so last_error records CircuitOpenError
            self.assertEqual(snap["error"], "CircuitOpenError")
            self.assertEqual(len(snap["items"]), 0)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

