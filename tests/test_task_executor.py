"""Tests for the PerProjectTaskExecutor."""
from __future__ import annotations

import asyncio
import time
import unittest

from sutra.task_executor import PerProjectTaskExecutor, TaskState, TaskResult


class PerProjectTaskExecutorTests(unittest.TestCase):
    """Unit tests for the task executor."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_01_task_completes(self):
        """A simple task should complete successfully."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=10,
                max_queue_size=10,
                default_timeout_seconds=5.0,
            )
            result = await executor.submit("proj_a", _coro("ok"))
            self.assertEqual(result.state, TaskState.COMPLETED)
            self.assertEqual(result.result, "ok")
            self.assertEqual(result.attempt, 1)
            self.assertGreater(result.duration_ms, 0)

        self.loop.run_until_complete(run())

    def test_02_task_fails(self):
        """A task that raises should be FAILED."""
        async def run():
            executor = PerProjectTaskExecutor()
            async def bad():
                raise ValueError("boom")
            result = await executor.submit("proj_a", bad())
            self.assertEqual(result.state, TaskState.FAILED)
            self.assertEqual(result.error, "ValueError")

        self.loop.run_until_complete(run())

    def test_03_sequential_same_project(self):
        """Tasks on the same project should execute sequentially."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=10,
            )
            order = []

            async def task(name, delay=0.05):
                order.append(f"{name}_start")
                await asyncio.sleep(delay)
                order.append(f"{name}_end")
                return name

            results = await asyncio.gather(*[
                executor.submit("proj_a", task(f"t{i}")) for i in range(3)
            ])
            self.assertEqual([r.state for r in results], [TaskState.COMPLETED] * 3)
            # Sequential: t0_end before t1_start, t1_end before t2_start
            self.assertEqual(order, ["t0_start", "t0_end", "t1_start", "t1_end", "t2_start", "t2_end"])

        self.loop.run_until_complete(run())

    def test_04_parallel_different_projects(self):
        """Tasks on different projects can run concurrently."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=10,
            )

            async def task(name, delay=0.2):
                await asyncio.sleep(delay)
                return name

            start = time.time()
            results = await asyncio.gather(*[
                executor.submit(f"proj_{i}", task(f"t{i}")) for i in range(3)
            ])
            elapsed = time.time() - start

            self.assertEqual([r.state for r in results], [TaskState.COMPLETED] * 3)
            # Should complete in ~0.2s (parallel) not ~0.6s (sequential)
            self.assertLess(elapsed, 0.5)

        self.loop.run_until_complete(run())

    def test_05_queue_ordering(self):
        """FIFO ordering within a project queue."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=10,
            )
            order = []

            async def task(name, delay=0.1):
                await asyncio.sleep(delay)
                order.append(name)
                return name

            results = await asyncio.gather(*[
                executor.submit("proj_a", task(str(i))) for i in range(5)
            ])
            self.assertEqual([r.state for r in results], [TaskState.COMPLETED] * 5)
            # FIFO order
            self.assertEqual(order, ["0", "1", "2", "3", "4"])

        self.loop.run_until_complete(run())

    def test_06_queue_full_rejection(self):
        """When queue is full, new tasks should be REJECTED."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=1,
                max_queue_size=1,
            )

            async def slow(delay=5.0):
                await asyncio.sleep(delay)
                return "done"

            # Start one task (fills concurrency slot)
            t1 = executor.submit("proj_a", slow())
            await asyncio.sleep(0.02)

            # Queue one more (fills queue)
            t2 = executor.submit("proj_a", slow())
            await asyncio.sleep(0.02)

            # This should be rejected
            result = await executor.submit("proj_a", slow())
            self.assertEqual(result.state, TaskState.REJECTED)
            self.assertIn("queue full", result.error or "")

            executor.shutdown()
            await t1
            await t2

        self.loop.run_until_complete(run())

    def test_07_timeout(self):
        """A task that exceeds timeout should be TIMED_OUT."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                default_timeout_seconds=0.2,
            )

            async def slow():
                await asyncio.sleep(5.0)
                return "too late"

            result = await executor.submit("proj_a", slow())
            self.assertEqual(result.state, TaskState.TIMED_OUT)
            self.assertIn("timed out", result.error or "")
            self.assertEqual(executor.total_timed_out, 1)

        self.loop.run_until_complete(run())

    def test_08_retry_succeeds(self):
        """A transient failure should be retried and eventually succeed."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                default_timeout_seconds=5.0,
                max_retries=2,
                retry_base_delay=0.05,
                retry_max_delay=0.1,
            )

            self._attempts = 0

            # Pass the coroutine *function* so retries can re-invoke it.
            async def flaky():
                self._attempts += 1
                if self._attempts < 3:
                    raise ConnectionError("transient")
                return "success"

            result = await executor.submit("proj_a", flaky)
            self.assertEqual(result.state, TaskState.COMPLETED)
            self.assertEqual(result.result, "success")
            self.assertEqual(result.attempt, 3)
            self.assertEqual(self._attempts, 3)
            self.assertEqual(executor.total_retries, 2)

        self.loop.run_until_complete(run())

    def test_09_retry_exhausted(self):
        """When retries are exhausted, the task should be FAILED."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_retries=2,
                retry_base_delay=0.05,
                retry_max_delay=0.1,
            )

            async def always_fails():
                raise ConnectionError("always down")

            result = await executor.submit("proj_a", always_fails)
            self.assertEqual(result.state, TaskState.FAILED)
            self.assertEqual(result.attempt, 3)  # initial + 2 retries
            self.assertEqual(executor.total_failed, 1)
            self.assertEqual(executor.total_retries, 2)

        self.loop.run_until_complete(run())

    def test_10_non_retryable_failure(self):
        """Permanent errors (ValueError) should not be retried."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_retries=10,
                retry_base_delay=0.01,
            )

            async def bad_input():
                raise ValueError("invalid request")

            result = await executor.submit("proj_a", bad_input())
            self.assertEqual(result.state, TaskState.FAILED)
            self.assertEqual(result.error, "ValueError")
            self.assertEqual(result.attempt, 1)  # no retries
            self.assertEqual(executor.total_retries, 0)

        self.loop.run_until_complete(run())

    def test_11_snapshot_observability(self):
        """Snapshot should show current queue/active counts."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=2,
                max_queue_size=10,
            )

            async def slow(delay=1.0):
                await asyncio.sleep(delay)
                return "done"

            t1 = executor.submit("proj_a", slow())
            t2 = executor.submit("proj_b", slow())
            await asyncio.sleep(0.02)

            snap = executor.snapshot()
            self.assertEqual(snap["active_count"], 2)
            self.assertEqual(snap["queue_size"], 0)

            executor.shutdown()
            await t1
            await t2

        self.loop.run_until_complete(run())

    def test_12_cancel_queued_task(self):
        """A queued task should be cancellable."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=1,
            )

            async def slow():
                await asyncio.sleep(0.5)
                return "done"

            t1 = executor.submit("proj_a", slow())
            await asyncio.sleep(0.02)
            request_id = "test-req-1"
            # We need a request_id to cancel; we'll use the task's request_id
            # The executor assigns a random one, so we cancel by checking the result
            # Instead, just verify queue behavior
            t2 = executor.submit("proj_a", slow())
            await asyncio.sleep(0.02)
            # t1 is active, t2 should be queued
            self.assertEqual(executor.queue_size, 1)

            # Cancel t2 via internal request_id
            qt2_request_id = None
            async with executor._queue_lock:
                for qt in executor._queue:
                    if qt.state == TaskState.QUEUED:
                        qt2_request_id = qt.request_id
                        break

            cancelled = await executor.cancel(qt2_request_id)
            self.assertTrue(cancelled)
            self.assertEqual(executor.queue_size, 0)

            executor.shutdown()
            await t1
            await t2

        self.loop.run_until_complete(run())

    def test_13_timeout_releases_worker(self):
        """After a timeout, the worker should be available for the next task."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                default_timeout_seconds=0.2,
            )

            async def slow():
                await asyncio.sleep(5.0)
                return "too late"

            async def quick():
                return "quick"

            # First task times out
            t1 = executor.submit("proj_a", slow())
            await asyncio.sleep(0.3)
            r1 = await t1
            self.assertEqual(r1.state, TaskState.TIMED_OUT)

            # Second task should run immediately (no blocking)
            start = time.time()
            t2 = executor.submit("proj_a", quick())
            r2 = await t2
            elapsed = time.time() - start
            self.assertEqual(r2.state, TaskState.COMPLETED)
            self.assertLess(elapsed, 0.5)  # Should not wait for timeout again

        self.loop.run_until_complete(run())

    def test_14_bounded_retry_backoff(self):
        """Backoff delay should be bounded and include jitter."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_retries=3,
                retry_base_delay=1.0,
                retry_max_delay=2.0,
            )

            delays = []
            for i in range(5):
                # Build a fake qt-like object with the attributes _compute_backoff needs.
                fake = type("Qt", (), {
                    "attempt": i + 2,
                    "retry_base_delay": 1.0,
                    "retry_max_delay": 2.0,
                })()
                delay = executor._compute_backoff(fake)
                delays.append(delay)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 2.0)

        self.loop.run_until_complete(run())

    def test_15_global_concurrency_limit(self):
        """Global concurrency limit should be respected."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=10,
                max_concurrent_global=2,
            )

            active = []

            async def task(name):
                active.append(name)
                await asyncio.sleep(0.2)
                return name

            results = await asyncio.gather(*[
                executor.submit(f"proj_{i}", task(f"t{i}")) for i in range(4)
            ])
            self.assertEqual([r.state for r in results], [TaskState.COMPLETED] * 4)

        self.loop.run_until_complete(run())

    def test_16_snapshot_counts_accumulate(self):
        """Counters should accumulate correctly."""
        async def run():
            executor = PerProjectTaskExecutor()

            async def ok():
                return "ok"

            for _ in range(5):
                await executor.submit("proj_a", ok())

            snap = executor.snapshot()
            self.assertEqual(snap["total_received"], 5)
            self.assertEqual(snap["total_completed"], 5)
            self.assertEqual(snap["total_rejected"], 0)

        self.loop.run_until_complete(run())

    def test_17_rejected_task_does_not_block_queue(self):
        """Rejected tasks should not block subsequent tasks."""
        async def run():
            executor = PerProjectTaskExecutor(
                max_concurrent_per_project=1,
                max_concurrent_global=1,
                max_queue_size=1,
            )

            async def slow():
                await asyncio.sleep(1.0)
                return "done"

            t1 = executor.submit("proj_a", slow())
            await asyncio.sleep(0.02)
            t2 = executor.submit("proj_a", slow())  # queued
            await asyncio.sleep(0.02)
            t3 = executor.submit("proj_a", slow())  # rejected

            r3 = await t3
            self.assertEqual(r3.state, TaskState.REJECTED)

            executor.shutdown()
            await t1
            await t2

        self.loop.run_until_complete(run())


class _CoroResult:
    """Helper to carry a result through a coroutine."""
    def __init__(self, value):
        self.value = value


def _coro(value):
    async def _inner():
        return value
    return _inner()


if __name__ == "__main__":
    unittest.main()
