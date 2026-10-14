"""SŪTRA health-check helpers.

Provides two standard checks:

* **Liveness** — is the SŪTRA process alive?
  ``/health/live``
* **Readiness** — is the service initialized enough to accept traffic?
  ``/health/ready``

These checks are intentionally lightweight: they never trigger expensive
operations (no ProjectIndex rebuild, no LLM calls, no retries), and any
I/O they perform is bounded with a short timeout.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class CheckStatus(str, Enum):
    """Per-dependency check outcome."""
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    """Outcome of a single readiness probe."""
    name: str
    status: CheckStatus
    detail: str = ""
    # Optional millisecond timing for observability (not exposed to clients).
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        if self.status == CheckStatus.OK:
            return {"status": "ok"}
        return {"status": "error", "detail": self.detail}


@dataclass
class HealthReport:
    """Aggregated health response."""
    status: str  # "ok" or "error"
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": self.checks}


# ---------------------------------------------------------------------------
# Check function type
# ---------------------------------------------------------------------------

CheckFunc = Callable[[], Awaitable[CheckResult]]


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------

async def check_control_plane(get_cp: Callable[[], Any]) -> CheckResult:
    """Check whether the Control Plane is initialized.

    ``get_cp`` is a zero-argument callable that returns the current
    Control Plane instance (or ``None`` if not yet set).
    """
    t0 = time.perf_counter()
    try:
        cp = get_cp()
        if cp is None or not getattr(cp, "running", False):
            return CheckResult(
                name="control_plane", status=CheckStatus.ERROR,
                detail="not initialized or not running",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        return CheckResult(
            name="control_plane", status=CheckStatus.OK,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        return CheckResult(
            name="control_plane", status=CheckStatus.ERROR,
            detail=str(exc),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )


async def check_runtime(get_runtime: Callable[[], Any]) -> CheckResult:
    """Check whether the async Runtime is available."""
    t0 = time.perf_counter()
    try:
        rt = get_runtime()
        if rt is None:
            return CheckResult(
                name="runtime", status=CheckStatus.ERROR,
                detail="not initialized",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        return CheckResult(
            name="runtime", status=CheckStatus.OK,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        return CheckResult(
            name="runtime", status=CheckStatus.ERROR,
            detail=str(exc),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )


async def check_project_index(get_pi: Callable[[], Any]) -> CheckResult:
    """Check whether ProjectIndex is initialized and has been built at least once."""
    t0 = time.perf_counter()
    try:
        pi = get_pi()
        if pi is None:
            return CheckResult(
                name="project_index", status=CheckStatus.ERROR,
                detail="not initialized",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        if not getattr(pi, "_ready", False):
            return CheckResult(
                name="project_index", status=CheckStatus.ERROR,
                detail="not yet built",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        return CheckResult(
            name="project_index", status=CheckStatus.OK,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        return CheckResult(
            name="project_index", status=CheckStatus.ERROR,
            detail=str(exc),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )


async def check_task_executor(get_executor: Callable[[], Any]) -> CheckResult:
    """Check whether the task executor is available."""
    t0 = time.perf_counter()
    try:
        te = get_executor()
        if te is None:
            return CheckResult(
                name="task_executor", status=CheckStatus.ERROR,
                detail="not initialized",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        return CheckResult(
            name="task_executor", status=CheckStatus.OK,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        return CheckResult(
            name="task_executor", status=CheckStatus.ERROR,
            detail=str(exc),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )


async def check_wss(get_wss: Callable[[], Any]) -> CheckResult:
    """Check whether the WSS manager is available.

    WSS is optional — if the getter raises ``NotImplementedError`` or
    returns ``None``, this check is SKIPPED rather than FAILED.
    """
    t0 = time.perf_counter()
    try:
        wss = get_wss()
        if wss is None:
            return CheckResult(
                name="wss", status=CheckStatus.SKIPPED,
                detail="not configured",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        if not getattr(wss, "running", False):
            return CheckResult(
                name="wss", status=CheckStatus.SKIPPED,
                detail="not running",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        return CheckResult(
            name="wss", status=CheckStatus.OK,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    except NotImplementedError:
        return CheckResult(
            name="wss", status=CheckStatus.SKIPPED,
            detail="not implemented",
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        return CheckResult(
            name="wss", status=CheckStatus.ERROR,
            detail=str(exc),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
