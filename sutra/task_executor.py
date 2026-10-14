"""Task execution control for SŪTRA.

Per-project concurrency control, bounded queueing, backpressure, timeouts,
and retries — all in-process, no external dependencies.

Task lifecycle
--------------
    RECEIVED -> QUEUED -> RUNNING -> COMPLETED
                               -> FAILED
                               -> TIMED_OUT
                                 (retry) -> RETRY_WAIT -> QUEUEDED -> ...
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

log = logging.getLogger(__name__)


class TaskState(str, Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    RETRY_WAIT = "retry_wait"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TaskResult:
    """Outcome returned by PerProjectTaskExecutor.submit()."""
    __slots__ = ("task_id", "state", "result", "error", "project_id",
                 "request_id", "attempt", "duration_ms", "queued_ms")

    def __init__(self, **kwargs: Any) -> None:
        self.task_id: str = kwargs.get("task_id", "")
        self.state: TaskState = kwargs.get("state", TaskState.RECEIVED)
        self.result: Any = kwargs.get("result")
        self.error: str | None = kwargs.get("error")
        self.project_id: str = kwargs.get("project_id", "")
        self.request_id: str = kwargs.get("request_id", "")
        self.attempt: int = kwargs.get("attempt", 1)
        self.duration_ms: float = kwargs.get("duration_ms", 0.0)
        self.queued_ms: float = kwargs.get("queued_ms", 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "duration_ms": self.duration_ms,
            "queued_ms": self.queued_ms,
        }


# --- Retry helper types ------------------------------------------------------

#: Exceptions whose failure is considered transient and worth retrying.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)
_RETRYABLE_ERROR_NAMES: frozenset[str] = frozenset(
    {e.__name__ for e in _RETRYABLE_EXCEPTIONS}
)
_NON_RETRYABLE_HTTP_CODES: frozenset[int] = frozenset({400, 401, 403, 404, 422})


def _is_retryable(exc: BaseException) -> bool:
    """Return True if the failure looks transient and worth retrying."""
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status not in _NON_RETRYABLE_HTTP_CODES:
        return True
    return False


def _is_retryable_error(error_name: str | None) -> bool:
    """Return True if a failure identified by error string name is retryable."""
    if not error_name:
        return False
    return error_name in _RETRYABLE_ERROR_NAMES


# --- Internal queue item -----------------------------------------------------

@dataclass
class _QueuedTask:
    """One task inside the executor's queue."""
    task_id: str
    project_id: str
    request_id: str
    # Stored as a *factory* (zero-arg callable returning a coroutine) so
    # retries can create a fresh coroutine for each attempt.
    _coro_factory: Callable[[], Awaitable[Any]]
    _unstarted_coroutine: Any = None
    state: TaskState = TaskState.RECEIVED
    attempt: int = 1
    created_at: float = field(default_factory=time.time)
    queued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    queued_ms: float = 0.0
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    _result_future: asyncio.Future[TaskResult] = field(default_factory=asyncio.Future)

    def make_coroutine(self) -> Awaitable[Any]:
        """Build a fresh coroutine from the factory."""
        self._unstarted_coroutine = None
        return self._coro_factory()


# --- Main executor -----------------------------------------------------------

class PerProjectTaskExecutor:
    """Concurrency-controlled, queued task executor.

    Per-project serialization with a global concurrency cap, bounded queue,
    per-task timeout, and exponential-backoff retries for transient failures.

    Architecture
    ------------
    * submit() is **synchronous** — it validates, enqueues or schedules, and
      returns an ``asyncio.Future`` immediately.  The caller ``await``s that
      future to receive the ``TaskResult``.
    * Active-tracking sets hold task_id strings (hashable, memory-bounded).
    * A single drain loop (Condition-driven) promotes queued tasks into
      execution slots as slots free up — no busy polling.
    """

    def __init__(
        self,
        max_concurrent_per_project: int = 1,
        max_concurrent_global: int = 20,
        max_queue_size: int = 100,
        default_timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
    ) -> None:
        self._max_per_project = max(1, max_concurrent_per_project)
        self._max_global = max(1, max_concurrent_global)
        self._max_queue = max(0, max_queue_size)

        self._default_timeout = default_timeout_seconds
        self._max_retries = max(0, max_retries)
        self._retry_base = max(0.1, retry_base_delay)
        self._retry_max = max(self._retry_base, retry_max_delay)

        # Per-project semaphores (created lazily)
        self._project_semaphores: dict[str, asyncio.Semaphore] = {}

        # Queue and active tracking (task_id strings — hashable)
        self._queue: deque[_QueuedTask] = deque()
        self._queue_lock = asyncio.Lock()
        self._active_per_project: dict[str, set[str]] = {}
        self._active_global: set[str] = set()
        self._tasks: dict[str, _QueuedTask] = {}

        # Drain loop coordination
        self._drain_condition = asyncio.Condition()
        self._drain_task: asyncio.Task[None] | None = None

        # Observability counters (bounded, in-process)
        self._total_received = 0
        self._total_queued = 0
        self._total_rejected = 0
        self._total_completed = 0
        self._total_failed = 0
        self._total_timed_out = 0
        self._total_retries = 0
        self._max_queue_seen = 0

        # Request ID deduplication (Issue 06)
        self._request_id_map: dict[str, asyncio.Future[TaskResult]] = {}
        self._completed_requests: dict[str, TaskResult] = {}
        self._max_completed_cache = 1000

        self._running = True

    # -- Public API -----------------------------------------------------------

    @staticmethod
    def _close_unstarted_coroutine(coroutine: Any) -> None:
        if asyncio.iscoroutine(coroutine):
            coroutine.close()

    def submit(
        self,
        project_id: str,
        # Accept either a coroutine factory (preferred, enables retries)
        # or a bare coroutine (legacy convenience, retries disabled).
        coroutine_or_factory: Callable[[], Awaitable[Any]] | Awaitable[Any],
        *,
        request_id: str = "",
        timeout_seconds: float = 0,
        max_retries: int = -1,
    ) -> asyncio.Future[TaskResult]:
        """Submit a task. Returns a Future that resolves to a TaskResult.

        This method is synchronous — it schedules or enqueues and returns
        the result future.  The caller ``await``s that future.

        Parameters
        ----------
        project_id:
            Logical project to group the task under.
        coroutine_or_factory:
            Either a coroutine object (e.g. ``my_coroutine()``) or a
            zero-argument callable that returns a fresh coroutine
            (e.g. ``my_coroutine``).  Passing a callable allows retries
            because a new coroutine object is created for each attempt.
        """
        unstarted_coroutine = (
            coroutine_or_factory if asyncio.iscoroutine(coroutine_or_factory) else None
        )
        if not self._running:
            self._close_unstarted_coroutine(unstarted_coroutine)
            fut: asyncio.Future[TaskResult] = asyncio.Future()
            fut.set_result(TaskResult(
                state=TaskState.REJECTED, project_id=project_id,
                request_id=request_id, error="executor shutting down",
            ))
            return fut

        # Normalise to a factory (zero-arg callable returning a fresh coroutine)
        # so retries can re-invoke it.
        if asyncio.iscoroutinefunction(coroutine_or_factory):
            # e.g. the user passed ``my_coroutine`` (the function itself)
            factory = coroutine_or_factory  # type: ignore[assignment]
        elif asyncio.iscoroutine(coroutine_or_factory):
            # e.g. the user passed ``my_coroutine()`` (already invoked)
            _coro = coroutine_or_factory  # type: ignore[assignment]
            factory = lambda: _coro  # type: ignore[assignment]
        else:
            # Any other callable — treat as a factory
            factory = coroutine_or_factory  # type: ignore[assignment]

        # Check for duplicate in-flight or completed task by request_id (Issue 06)
        if request_id:
            if request_id in self._request_id_map:
                existing_fut = self._request_id_map[request_id]
                if not existing_fut.done():
                    self._close_unstarted_coroutine(unstarted_coroutine)
                    log.debug("task_dedup_inflight request_id=%s project=%s", request_id, project_id)
                    return existing_fut
            if request_id in self._completed_requests:
                self._close_unstarted_coroutine(unstarted_coroutine)
                cached_res = self._completed_requests[request_id]
                log.debug("task_dedup_completed request_id=%s project=%s", request_id, project_id)
                fut: asyncio.Future[TaskResult] = asyncio.Future()
                fut.set_result(cached_res)
                return fut

        task_id = str(uuid.uuid4())
        effective_timeout = timeout_seconds if timeout_seconds > 0 else self._default_timeout
        effective_retries = max_retries if max_retries >= 0 else self._max_retries

        qt = _QueuedTask(
            task_id=task_id,
            project_id=project_id,
            request_id=request_id or str(uuid.uuid4()),
            _coro_factory=factory,
            _unstarted_coroutine=unstarted_coroutine,
            timeout_seconds=effective_timeout,
            max_retries=effective_retries,
            retry_base_delay=self._retry_base,
            retry_max_delay=self._retry_max,
        )
        result_future = qt._result_future
        self._tasks[task_id] = qt
        self._total_received += 1

        if request_id:
            self._request_id_map[request_id] = result_future

            def _on_done(f: asyncio.Future[TaskResult]) -> None:
                if not f.cancelled():
                    try:
                        res = f.result()
                        if len(self._completed_requests) >= self._max_completed_cache:
                            self._completed_requests.pop(next(iter(self._completed_requests)), None)
                        self._completed_requests[request_id] = res
                    except Exception:
                        pass
                self._request_id_map.pop(request_id, None)

            result_future.add_done_callback(_on_done)

        # Try to schedule directly (bypasses the queue entirely)
        if self._try_schedule(qt):
            asyncio.ensure_future(self._wrap_and_run(qt))
            return result_future

        # Backpressure: enqueue
        if self._max_queue > 0 and len(self._queue) >= self._max_queue:
            self._total_rejected += 1
            log.warning("task_rejected queue_full task_id=%s project=%s", task_id, project_id)
            self._close_unstarted_coroutine(qt._unstarted_coroutine)
            qt._unstarted_coroutine = None
            result_future.set_result(TaskResult(
                task_id=task_id, state=TaskState.REJECTED,
                project_id=project_id, request_id=qt.request_id,
                error="queue full",
            ))
            return result_future

        qt.state = TaskState.QUEUED
        self._queue.append(qt)
        self._total_queued += 1
        if len(self._queue) > self._max_queue_seen:
            self._max_queue_seen = len(self._queue)
        log.debug("task_queued task_id=%s project=%s depth=%d", task_id, project_id, len(self._queue))

        self._ensure_drain()
        return result_future

    def submit_nowait(
        self,
        project_id: str,
        coroutine_or_factory: Callable[[], Awaitable[Any]] | Awaitable[Any],
        *,
        request_id: str = "",
        timeout_seconds: float = 0,
        max_retries: int = -1,
    ) -> "TaskResult | None":
        """Submit only if queue has capacity; return None if full."""
        if not self._running:
            self._close_unstarted_coroutine(coroutine_or_factory)
            return TaskResult(state=TaskState.REJECTED, project_id=project_id,
                              request_id=request_id, error="executor shutting down")
        if self._max_queue > 0 and len(self._queue) >= self._max_queue:
            self._total_rejected += 1
            self._close_unstarted_coroutine(coroutine_or_factory)
            return None
        fut = self.submit(project_id, coroutine_or_factory, request_id=request_id,
                          timeout_seconds=timeout_seconds, max_retries=max_retries)
        return TaskResult(task_id="pending", state=TaskState.QUEUED, project_id=project_id)

    async def cancel(self, request_id: str) -> bool:
        """Cancel a queued task by request_id."""
        async with self._queue_lock:
            for i, qt in enumerate(self._queue):
                if qt.request_id == request_id and qt.state in (TaskState.QUEUED, TaskState.RECEIVED):
                    qt.state = TaskState.CANCELLED
                    del self._queue[i]
                    self._close_unstarted_coroutine(qt._unstarted_coroutine)
                    qt._unstarted_coroutine = None
                    if not qt._result_future.done():
                        qt._result_future.set_result(TaskResult(
                            task_id=qt.task_id, state=TaskState.CANCELLED,
                            project_id=qt.project_id, request_id=qt.request_id,
                        ))
                    log.info("task_cancelled task_id=%s request_id=%s", qt.task_id, request_id)
                    self._wake_drain()
                    return True
        return False

    def shutdown(self) -> None:
        """Stop accepting new tasks; cancel everything queued."""
        self._running = False

        def _do() -> None:
            for qt in list(self._queue):
                if qt.state in (TaskState.QUEUED, TaskState.RECEIVED, TaskState.RETRY_WAIT):
                    qt.state = TaskState.CANCELLED
                    self._close_unstarted_coroutine(qt._unstarted_coroutine)
                    qt._unstarted_coroutine = None
                    if not qt._result_future.done():
                        try:
                            loop = asyncio.get_event_loop()
                            loop.call_soon_threadsafe(
                                qt._result_future.set_result,
                                TaskResult(task_id=qt.task_id, state=TaskState.CANCELLED,
                                           project_id=qt.project_id, request_id=qt.request_id),
                            )
                        except RuntimeError:
                            pass
            self._queue.clear()

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(_do)
            else:
                _do()
        except RuntimeError:
            pass

    # -- Observability --------------------------------------------------------

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def active_count(self) -> int:
        return len(self._active_global)

    @property
    def active_per_project(self) -> dict[str, int]:
        return {pid: len(ids) for pid, ids in self._active_per_project.items()}

    @property
    def total_received(self) -> int:
        return self._total_received

    @property
    def total_queued(self) -> int:
        return self._total_queued

    @property
    def total_rejected(self) -> int:
        return self._total_rejected

    @property
    def total_completed(self) -> int:
        return self._total_completed

    @property
    def total_failed(self) -> int:
        return self._total_failed

    @property
    def total_timed_out(self) -> int:
        return self._total_timed_out

    @property
    def total_retries(self) -> int:
        return self._total_retries

    def snapshot(self) -> dict[str, Any]:
        return {
            "queue_size": len(self._queue),
            "active_count": len(self._active_global),
            "active_per_project": self.active_per_project,
            "max_queue": self._max_queue,
            "max_concurrent_per_project": self._max_per_project,
            "max_concurrent_global": self._max_global,
            "total_received": self._total_received,
            "total_queued": self._total_queued,
            "total_rejected": self._total_rejected,
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "total_timed_out": self._total_timed_out,
            "total_retries": self._total_retries,
        }

    # -- Internal machinery ---------------------------------------------------

    def _try_schedule(self, qt: _QueuedTask) -> bool:
        """Try to promote a task to running. Returns True if scheduled."""
        proj_active = self._active_per_project.get(qt.project_id, set())
        if len(proj_active) >= self._max_per_project:
            return False
        if len(self._active_global) >= self._max_global:
            return False
        qt.state = TaskState.RUNNING
        qt.started_at = time.time()
        qt.queued_ms = (qt.started_at - qt.queued_at) * 1000
        self._active_global.add(qt.task_id)
        self._active_per_project.setdefault(qt.project_id, set()).add(qt.task_id)
        return True

    def _wake_drain(self) -> None:
        """Wake or restart the drain loop."""
        if self._drain_task is not None and not self._drain_task.done():
            try:
                self._drain_task.cancel()
            except Exception:
                pass
        self._drain_task = asyncio.ensure_future(self._drain_loop())

    def _ensure_drain(self) -> None:
        """Ensure the drain loop is running."""
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.ensure_future(self._drain_loop())

    async def _drain_loop(self) -> None:
        """Drain queued tasks into execution slots as slots free up."""
        try:
            while self._running and self._queue:
                if self.active_count < self._max_global:
                    qt = self._queue[0]
                    proj_active = self._active_per_project.get(qt.project_id, set())
                    if len(proj_active) < self._max_per_project:
                        self._queue.popleft()
                        asyncio.ensure_future(self._wrap_and_run(qt))
                        continue
                async with self._drain_condition:
                    try:
                        await asyncio.wait_for(
                            self._drain_condition.wait(), timeout=0.05
                        )
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            return

    async def _wrap_and_run(self, qt: _QueuedTask) -> None:
        """Wrapper that runs the task and handles completion."""
        try:
            await self._run_task(qt)
        finally:
            self._finish_task(qt)

    def _get_semaphore(self, project_id: str) -> asyncio.Semaphore:
        """Get or create a semaphore for the project."""
        if project_id not in self._project_semaphores:
            self._project_semaphores[project_id] = asyncio.Semaphore(self._max_per_project)
        return self._project_semaphores[project_id]

    async def _run_task(self, qt: _QueuedTask) -> None:
        """Execute a single task with timeout."""
        sem = self._get_semaphore(qt.project_id)
        async with sem:
            if qt.state == TaskState.CANCELLED:
                return
            try:
                result = await asyncio.wait_for(
                    qt.make_coroutine(),
                    timeout=qt.timeout_seconds,
                )
                qt.state = TaskState.COMPLETED
                qt.result = result
                qt.error = None
                qt.completed_at = time.time()
                qt.duration_ms = (qt.completed_at - qt.started_at) * 1000
                self._total_completed += 1
                log.debug(
                    "task_completed task_id=%s project=%s attempt=%d duration_ms=%.1f",
                    qt.task_id, qt.project_id, qt.attempt, qt.duration_ms,
                )
            except asyncio.TimeoutError:
                qt.state = TaskState.TIMED_OUT
                qt.error = f"timed out after {qt.timeout_seconds:.1f}s"
                qt.completed_at = time.time()
                qt.duration_ms = (qt.completed_at - qt.started_at) * 1000
                self._total_timed_out += 1
                log.warning(
                    "task_timed_out task_id=%s project=%s timeout=%.1fs",
                    qt.task_id, qt.project_id, qt.timeout_seconds,
                )
            except Exception as exc:
                qt.state = TaskState.FAILED
                qt.error = type(exc).__name__
                qt.completed_at = time.time()
                qt.duration_ms = (qt.completed_at - qt.started_at) * 1000
                log.warning(
                    "task_failed task_id=%s project=%s error=%s",
                    qt.task_id, qt.project_id, type(exc).__name__,
                )

    def _finish_task(self, qt: _QueuedTask) -> None:
        """Handle task completion — resolve future or schedule retry."""
        self._active_global.discard(qt.task_id)
        proj = self._active_per_project.get(qt.project_id)
        if proj:
            proj.discard(qt.task_id)

        should_retry = (
            qt.state == TaskState.FAILED
            and qt.attempt <= qt.max_retries
            and _is_retryable_error(qt.error)
        )

        if should_retry:
            self._total_retries += 1
            backoff = self._compute_backoff(qt)
            qt.state = TaskState.RETRY_WAIT
            log.info(
                "task_retry task_id=%s attempt=%d backoff=%.2fs",
                qt.task_id, qt.attempt + 1, backoff,
            )
            # Schedule retry after backoff
            def _retry() -> None:
                qt.attempt += 1
                qt.state = TaskState.RECEIVED
                qt.queued_at = time.time()
                qt.started_at = 0.0
                qt.completed_at = 0.0
                qt.duration_ms = 0.0
                qt.result = None
                qt.error = None
                # Re-enqueue and kick drain
                self._queue.append(qt)
                self._total_queued += 1
                self._ensure_drain()

            try:
                loop = asyncio.get_event_loop()
                loop.call_later(backoff, _retry)
            except RuntimeError:
                pass
            return

        # Final outcome — resolve the future
        self._total_failed += 1
        if not qt._result_future.done():
            qt._result_future.set_result(TaskResult(
                task_id=qt.task_id, state=qt.state,
                project_id=qt.project_id, request_id=qt.request_id,
                result=qt.result, error=qt.error, attempt=qt.attempt,
                duration_ms=qt.duration_ms, queued_ms=qt.queued_ms,
            ))

    def _compute_backoff(self, qt: _QueuedTask) -> float:
        """Exponential backoff with jitter, capped at retry_max_delay."""
        raw = qt.retry_base_delay * (2 ** (qt.attempt - 1))
        jitter = random.uniform(0, raw * 0.1)
        return min(raw + jitter, qt.retry_max_delay)
