"""Validation tests for Idempotency and Deduplication in SŪTRA."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from sutra.control_plane import ControlPlane
from sutra.resilience import (
    IdempotencyConflictError,
    IdempotencyRecord,
    IdempotencyState,
    IdempotencyStore,
    RetryPolicy,
    with_timeout_and_retry,
)
from sutra.telegram_bot import TelegramBot


class IdempotencyStoreUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_request_execution_lifecycle(self):
        """Initial request: NEW -> PROCESSING -> COMPLETED."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0)
        self.assertEqual(await store.get_state("key-1"), IdempotencyState.NEW)

        action, val = await store.check_and_start("key-1", {"path": "/foo"})
        self.assertEqual(action, "EXECUTE")
        self.assertIsNone(val)
        self.assertEqual(await store.get_state("key-1"), IdempotencyState.PROCESSING)

        await store.complete("key-1", {"status": "success", "id": 123})
        self.assertEqual(await store.get_state("key-1"), IdempotencyState.COMPLETED)

        # Subsequent check returns cached result
        action2, val2 = await store.check_and_start("key-1", {"path": "/foo"})
        self.assertEqual(action2, "COMPLETED")
        self.assertEqual(val2, {"status": "success", "id": 123})

    async def test_duplicate_while_processing_waits_for_completion(self):
        """Concurrent request for same key waits on the existing future."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0)

        action1, _ = await store.check_and_start("key-slow", {"arg": 1})
        self.assertEqual(action1, "EXECUTE")

        # Second caller sees PROCESSING and receives the in-flight Future
        action2, fut = await store.check_and_start("key-slow", {"arg": 1})
        self.assertEqual(action2, "PROCESSING")
        self.assertIsInstance(fut, asyncio.Future)
        self.assertFalse(fut.done())

        # Complete from the first caller
        await store.complete("key-slow", {"data": "done"})

        # The future resolves to the completed result
        result = await asyncio.wait_for(fut, timeout=1.0)
        self.assertEqual(result, {"data": "done"})

    async def test_duplicate_after_completion_returns_cached_result(self):
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0)
        execution_count = 0

        async def _side_effect():
            nonlocal execution_count
            execution_count += 1
            return f"result_{execution_count}"

        res1 = await store.execute("op-100", _side_effect, payload={"a": 1})
        self.assertEqual(res1, "result_1")
        self.assertEqual(execution_count, 1)

        # Second execution with same key and payload returns cached without running _side_effect
        res2 = await store.execute("op-100", _side_effect, payload={"a": 1})
        self.assertEqual(res2, "result_1")
        self.assertEqual(execution_count, 1)

    async def test_payload_mismatch_raises_conflict(self):
        """Reusing the same key with different payload raises IdempotencyConflictError."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0)

        await store.check_and_start("key-conflict", {"command": "add", "target": "A"})
        await store.complete("key-conflict", {"ok": True})

        with self.assertRaises(IdempotencyConflictError):
            await store.check_and_start("key-conflict", {"command": "add", "target": "DIFFERENT_B"})

    async def test_ttl_expiration_purges_and_allows_reexecution(self):
        """Expired keys are evicted and permit fresh execution."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=0.1)

        action1, _ = await store.check_and_start("key-ttl", {"val": "test"})
        self.assertEqual(action1, "EXECUTE")
        await store.complete("key-ttl", {"result": 1})

        self.assertEqual(await store.get_state("key-ttl"), IdempotencyState.COMPLETED)

        # Wait for TTL to pass
        await asyncio.sleep(0.15)

        # Now key should be expired/NEW
        self.assertEqual(await store.get_state("key-ttl"), IdempotencyState.NEW)
        action2, _ = await store.check_and_start("key-ttl", {"val": "test"})
        self.assertEqual(action2, "EXECUTE")

    async def test_concurrent_races_execute_exactly_once(self):
        """10 concurrent tasks racing for the same key execute side effect exactly once."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0)
        executions = 0

        async def _work():
            nonlocal executions
            executions += 1
            await asyncio.sleep(0.05)
            return "shared_value"

        tasks = [store.execute("race-key", _work, payload={"k": "v"}) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(executions, 1)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(r == "shared_value" for r in results))

    async def test_failure_handling_and_retry_reopen(self):
        """When an operation fails, state transitions to FAILED and can be retried."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0, allow_retry_on_failure=True)

        action, _ = await store.check_and_start("fail-key", {"x": 1})
        self.assertEqual(action, "EXECUTE")

        await store.fail("fail-key", error="network timeout", exc=TimeoutError("timeout"))
        self.assertEqual(await store.get_state("fail-key"), IdempotencyState.FAILED)

        # With allow_retry_on_failure=True, next attempt is allowed to execute again
        action2, _ = await store.check_and_start("fail-key", {"x": 1})
        self.assertEqual(action2, "EXECUTE")
        self.assertEqual(await store.get_state("fail-key"), IdempotencyState.PROCESSING)

    async def test_bounded_memory_eviction(self):
        """Store strictly bounds memory by evicting oldest entries when max_entries is exceeded."""
        store = IdempotencyStore(max_entries=5, ttl_seconds=300.0)

        for i in range(10):
            await store.check_and_start(f"key-{i}", {"i": i})
            await store.complete(f"key-{i}", f"val-{i}")

        # Size cannot exceed max_entries (5)
        self.assertLessEqual(len(store._records), 5)
        self.assertGreater(store.evictions, 0)

        # Oldest keys (0 to 4) were evicted
        self.assertEqual(await store.get_state("key-0"), IdempotencyState.NEW)
        self.assertEqual(await store.get_state("key-1"), IdempotencyState.NEW)

        # Most recent keys (5 to 9) remain
        self.assertEqual(await store.get_state("key-9"), IdempotencyState.COMPLETED)

    async def test_retry_policy_preserves_same_idempotency_key(self):
        """Retries within with_timeout_and_retry reuse the exact same idempotency key."""
        store = IdempotencyStore(max_entries=100, ttl_seconds=60.0)
        attempts = 0

        async def _faulty_op():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionResetError("temporary glitch")
            return "recovered"

        policy = RetryPolicy(max_retries=3, base_delay=0.01, max_delay=0.05)
        res = await with_timeout_and_retry(
            _faulty_op,
            timeout_seconds=5.0,
            policy=policy,
            idempotency_store=store,
            idempotency_key="logical-op-42",
            payload={"action": "deploy"},
        )

        self.assertEqual(res, "recovered")
        self.assertEqual(attempts, 3)

        # Result is completed in store under the same logical key
        self.assertEqual(await store.get_state("logical-op-42"), IdempotencyState.COMPLETED)

        # A re-invocation returns immediately from store without executing _faulty_op again
        res_cached = await with_timeout_and_retry(
            _faulty_op,
            timeout_seconds=5.0,
            policy=policy,
            idempotency_store=store,
            idempotency_key="logical-op-42",
            payload={"action": "deploy"},
        )
        self.assertEqual(res_cached, "recovered")
        self.assertEqual(attempts, 3)  # Did not run again!


class TelegramBotIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "test-operator-token-long-enough-32")
        self.bot = TelegramBot(self.cp, "test-bot-token", {42})

    async def asyncTearDown(self):
        await self.bot.aclose()
        self.cp.store.close()
        self.temp.cleanup()

    async def test_duplicate_telegram_update_deduplicated_in_memory(self):
        """Duplicate update_id is returned from in-memory idempotency store."""
        update = {
            "update_id": 8888,
            "message": {
                "message_id": 55,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/help",
            },
        }

        # First delivery processes successfully
        res1 = await self.bot.process_update(update)
        self.assertIsNotNone(res1)
        self.assertIn("navigation", str(res1).lower())

        # Second delivery with the EXACT same update_id is deduplicated
        res2 = await self.bot.process_update(update)
        # Returns the cached result without duplicate processing
        self.assertEqual(res1, res2)

    async def test_concurrent_duplicate_updates_execute_once(self):
        """Two concurrent updates with identical update_id race safely."""
        update = {
            "update_id": 9999,
            "message": {
                "message_id": 56,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/help",
            },
        }

        t1 = asyncio.create_task(self.bot.process_update(update))
        t2 = asyncio.create_task(self.bot.process_update(update))

        r1, r2 = await asyncio.gather(t1, t2)
        self.assertEqual(r1, r2)
        self.assertIn("navigation", str(r1).lower())


if __name__ == "__main__":
    unittest.main()
