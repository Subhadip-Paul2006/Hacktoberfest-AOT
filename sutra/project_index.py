"""In-memory project cache with bounded discovery outside the event loop."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .discovery import discover_projects
from .resilience import RetryPolicy, with_timeout_and_retry, CircuitBreaker, CircuitOpenError


class ProjectIndex:
    def __init__(self, roots: list[str | Path] | None = None,
                 timeout_seconds: float = 10.0,
                 retry_policy: RetryPolicy | None = None,
                 circuit_breaker: CircuitBreaker | None = None):
        self._roots = list(roots or [])
        self.timeout_seconds = timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy.from_environment()
        self.circuit_breaker = circuit_breaker or CircuitBreaker.from_environment("project_discovery")
        self._items: list[dict[str, Any]] = []
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_again = False
        self.last_refresh_at: float | None = None
        self.last_refresh_duration_ms: float | None = None
        self.last_error: str | None = None

    @property
    def refreshing(self) -> bool:
        return self._refresh_task is not None and not self._refresh_task.done()

    def snapshot(self) -> dict[str, Any]:
        return {"items": list(self._items), "refreshing": self.refreshing,
                "last_refresh_at": self.last_refresh_at,
                "last_refresh_duration_ms": self.last_refresh_duration_ms,
                "error": self.last_error}

    def set_roots(self, roots: list[str | Path]) -> None:
        self._roots = list(roots)

    def schedule_refresh(self) -> bool:
        if self.refreshing:
            self._refresh_again = True
            return False
        self._refresh_task = asyncio.create_task(self._refresh())
        return True

    async def _refresh(self) -> None:
        while True:
            started = time.perf_counter_ns()
            self._refresh_again = False
            try:
                async def _scan() -> list[dict[str, Any]]:
                    return await asyncio.to_thread(discover_projects, list(self._roots))

                items = await with_timeout_and_retry(
                    _scan,
                    timeout_seconds=self.timeout_seconds,
                    policy=self.retry_policy,
                    circuit_breaker=self.circuit_breaker,
                    operation_name="project_discovery",
                )
                self._items = items
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
            finally:
                self.last_refresh_at = time.time()
                self.last_refresh_duration_ms = (time.perf_counter_ns() - started) / 1_000_000
            if not self._refresh_again:
                return

    async def wait_for_refresh(self) -> None:
        task = self._refresh_task
        if task:
            await task
