"""Rate limiting, timeout, and retry-with-backoff utilities for SŪTRA.

This module is the ONLY new source of rate limiting and retry logic.
It intentionally avoids external dependencies (Redis, Celery, etc.):
everything runs in the existing single-process asyncio event loop.

Rate Limiting
-------------
:class:`SlidingWindowRateLimiter` implements a per-identity sliding-window
counter.  State is bounded: each identity's deque holds at most
``limit`` timestamps, and identities that have not sent a request within
``window_seconds`` are removed on the next cleanup pass.

Identity is normally a Telegram ``user_id`` (int) so that one user cannot
flood the whole system.  An optional **global** cap provides a second-line
defence: if the total across all identities exceeds ``global_limit``, the
request is rejected even if the individual user is within their budget.

Policy summary
~~~~~~~~~~~~~~
- **Scope**: per-user (Telegram user_id); optional global safety cap
- **Limit**: ``RATE_LIMIT`` requests per ``RATE_LIMIT_WINDOW`` seconds
  (defaults: 20 / 60)
- **Global cap**: ``RATE_LIMIT_GLOBAL`` requests per same window
  (default: 200; None = disabled)
- **Behaviour on excess**: immediate rejection — no queueing
- **Memory**: O(limit × active_identities); stale entries cleaned on access

Timeout / Retry
---------------
:func:`with_timeout_and_retry` wraps an arbitrary async callable with:

* A per-attempt ``timeout_seconds`` (raises :class:`asyncio.TimeoutError`).
* Bounded exponential backoff with full-jitter between retries.
* A ``Retry-After`` hint from HTTP 429 responses is respected when it falls
  within ``retry_max_delay``.
* Only *transient* errors are retried.  Authentication / validation errors
  are surfaced immediately.
* Retries do NOT bypass the rate limiter — callers must re-check before
  each retry if the limiter is in the call path.

Config (read from environment via ``RetryPolicy.from_environment``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``MAX_RETRIES``         maximum additional attempts after the first (default 2)
``RETRY_BASE_DELAY``    seconds for the first retry wait (default 1.0)
``RETRY_MAX_DELAY``     cap on retry wait (default 30.0)
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Awaitable, Callable, TypeVar

from fastapi import HTTPException

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

log = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Transient-error classification
# ---------------------------------------------------------------------------

_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

_TRANSIENT_EXCEPTION_TYPES = (
    asyncio.TimeoutError,
    ConnectionResetError,
    ConnectionRefusedError,
    TimeoutError,
    OSError,
)


def _is_transient(exc: BaseException) -> bool:
    """Return True when *exc* represents a failure that is worth retrying.

    Never retries:
    * HTTP 4xx (except 429 which IS transient — rate-limited by upstream)
    * Validation / authentication / authorisation errors
    * Deterministic application errors
    """
    if isinstance(exc, HTTPException):
        return exc.status_code in _TRANSIENT_HTTP_CODES
    if _HTTPX_AVAILABLE:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _TRANSIENT_HTTP_CODES
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
    return isinstance(exc, _TRANSIENT_EXCEPTION_TYPES)


# ---------------------------------------------------------------------------
# Circuit Breaker Failure Classification
# ---------------------------------------------------------------------------

_BREAKER_HTTP_CODES = frozenset({500, 502, 503, 504})

_BREAKER_EXCEPTION_TYPES = (
    asyncio.TimeoutError,
    ConnectionResetError,
    ConnectionRefusedError,
    TimeoutError,
    OSError,
)


def _is_breaker_failure(exc: BaseException) -> bool:
    """Return True if *exc* represents a downstream dependency health failure.

    Triggers circuit breaker on:
    * timeouts (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)
    * connection errors (ConnectionResetError, ConnectionRefusedError, httpx.NetworkError)
    * server errors (HTTP 500, 502, 503, 504)
    * I/O errors (OSError)

    Does NOT trigger circuit breaker on:
    * HTTP 4xx (including HTTP 429, which is handled via Retry-After retry)
    * Client / validation / authorization errors (HTTP 400, 401, 403, 404, ValueError)
    """
    if isinstance(exc, HTTPException):
        return exc.status_code in _BREAKER_HTTP_CODES
    if _HTTPX_AVAILABLE:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _BREAKER_HTTP_CODES
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
    return isinstance(exc, _BREAKER_EXCEPTION_TYPES)


class CircuitOpenError(Exception):
    """Raised when an operation is rejected because a CircuitBreaker is OPEN."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"Circuit breaker '{name}' is OPEN; retry after {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Rate Limiter — Sliding Window
# ---------------------------------------------------------------------------

class SlidingWindowRateLimiter:
    """In-process, per-identity sliding-window rate limiter.

    State is kept entirely in memory.  No threads, no external services.
    Each identity tracks only the most recent ``limit`` request timestamps so
    memory is bounded even under sustained load.

    Parameters
    ----------
    limit:
        Maximum number of requests allowed per identity within *window_seconds*.
    window_seconds:
        Length of the sliding window in seconds.
    global_limit:
        Optional hard cap across *all* identities combined.
        When ``None``, no global cap is applied.
    """

    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60.0,
        global_limit: int | None = 600,
    ) -> None:
        if limit < 1:
            raise ValueError("rate limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if global_limit is not None and global_limit < limit:
            raise ValueError("global_limit must be >= per-identity limit")

        self.limit = limit
        self.window_seconds = window_seconds
        self.global_limit = global_limit

        # identity -> deque of timestamps (monotonic clock)
        self._windows: dict[Any, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.limit)
        )
        self._last_cleanup = time.monotonic()

    @classmethod
    def from_environment(cls) -> "SlidingWindowRateLimiter":
        """Build a limiter from environment variables.

        Variables
        ---------
        ``RATE_LIMIT``          per-identity requests per window (default 60)
        ``RATE_LIMIT_WINDOW``   window in seconds (default 60)
        ``RATE_LIMIT_GLOBAL``   global cap across all identities (default 600; 0 = disabled)
        """
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return default if raw is None else int(raw)

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return default if raw is None else float(raw)

        limit = _int("RATE_LIMIT", 60)
        window = _float("RATE_LIMIT_WINDOW", 60.0)
        raw_global = _int("RATE_LIMIT_GLOBAL", 600)
        global_limit: int | None = None if raw_global == 0 else raw_global

        inst = cls(limit=limit, window_seconds=window, global_limit=global_limit)
        log.info(
            "rate_limiter_init limit=%d window=%.1fs global_limit=%s",
            limit, window, str(global_limit),
        )
        return inst

    def is_allowed(self, identity: Any) -> tuple[bool, float]:
        """Check whether *identity* may proceed right now.

        Returns
        -------
        allowed : bool
            ``True`` if the request is permitted.
        retry_after : float
            Seconds until the earliest slot opens (0.0 when allowed).

        Side-effects
        ------------
        Records the timestamp of an *allowed* request.
        Periodically evicts identities whose window has fully expired.
        """
        now = time.monotonic()
        window = self.window_seconds
        cutoff = now - window

        # Periodic cleanup of stale identities (every window_seconds)
        if now - self._last_cleanup >= window:
            self._cleanup(cutoff)
            self._last_cleanup = now

        timestamps = self._windows[identity]

        # Drop timestamps outside the current window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        # --- Per-identity check ---
        if len(timestamps) >= self.limit:
            # Oldest timestamp + window is when the next slot opens
            retry_after = max(0.0, timestamps[0] + window - now)
            log.info(
                "rate_limited identity=%s count=%d limit=%d retry_after=%.1fs",
                identity, len(timestamps), self.limit, retry_after,
            )
            return False, retry_after

        # --- Global cap check ---
        if self.global_limit is not None:
            total = 0
            for ts in self._windows.values():
                while ts and ts[0] <= cutoff:
                    ts.popleft()
                total += len(ts)
            if total >= self.global_limit:
                log.info(
                    "rate_limited_global identity=%s total=%d global_limit=%d",
                    identity, total, self.global_limit,
                )
                return False, 1.0  # generic hint; no precise slot available

        # Allowed — record now
        timestamps.append(now)
        log.debug("rate_allowed identity=%s count=%d", identity, len(timestamps))
        return True, 0.0

    def reset(self) -> None:
        """Clear all rate limit state."""
        self._windows.clear()
        self._last_cleanup = time.monotonic()

    def _cleanup(self, cutoff: float) -> None:
        """Remove identities with no timestamps in the current window."""
        stale = [
            ident for ident, ts in self._windows.items()
            if not ts or ts[-1] <= cutoff
        ]
        for ident in stale:
            del self._windows[ident]
        if stale:
            log.debug("rate_limiter_cleanup evicted=%d", len(stale))

    @property
    def active_identity_count(self) -> int:
        return len(self._windows)


# ---------------------------------------------------------------------------
# Retry Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for timeout and exponential-backoff retry.

    Attributes
    ----------
    max_retries:
        Maximum number of *additional* attempts after the first.
        0 means no retry; 2 means up to 3 total attempts.
    base_delay:
        Initial wait between retries in seconds.
    max_delay:
        Upper cap on wait time (before jitter) in seconds.
    """

    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 30.0

    @classmethod
    def from_environment(cls) -> "RetryPolicy":
        """Build a :class:`RetryPolicy` from environment variables.

        Variables
        ---------
        ``MAX_RETRIES``       default 2
        ``RETRY_BASE_DELAY``  default 1.0 s
        ``RETRY_MAX_DELAY``   default 30.0 s
        """
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return default if raw is None else int(raw)

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return default if raw is None else float(raw)

        return cls(
            max_retries=max(0, _int("MAX_RETRIES", 2)),
            base_delay=max(0.0, _float("RETRY_BASE_DELAY", 1.0)),
            max_delay=max(0.0, _float("RETRY_MAX_DELAY", 30.0)),
        )

    def backoff_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        """Compute the delay before *attempt* (1-indexed, first retry = 1).

        Uses **full jitter**: delay = random(0, min(max_delay, base * 2^attempt)).

        If ``retry_after`` is provided (from a ``Retry-After`` header) and
        falls within ``max_delay``, that value is used as the wait floor.
        The result is still capped at ``max_delay``.

        Parameters
        ----------
        attempt:
            Retry attempt number (starts at 1 for the first retry).
        retry_after:
            Optional server-provided hint in seconds.
        """
        # Exponential cap: base_delay * 2^(attempt-1), capped at max_delay
        exp_cap = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        # Full jitter within [0, exp_cap]
        jitter_delay = random.uniform(0.0, exp_cap)

        if retry_after is not None and math.isfinite(retry_after):
            # Respect Retry-After but never exceed max_delay
            floor = min(retry_after, self.max_delay)
            return max(jitter_delay, floor)

        return jitter_delay


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """In-process, state-machine Circuit Breaker protecting downstream services.

    Three logical states:
    * CLOSED: Normal operation. Successes reset failure count.
              Breaker-triggering failures increment failure counter.
              When ``failure_count >= failure_threshold``, transitions to OPEN.
    * OPEN:   Fails fast immediately with ``CircuitOpenError``. No downstream
              calls, no timeouts, no retries.
              After ``cooldown_seconds``, transitions to HALF_OPEN on next call.
    * HALF_OPEN: Allows exactly ``half_open_probes`` (default 1) probe request.
                 If probe succeeds -> transitions to CLOSED.
                 If probe fails -> transitions to OPEN (resets cooldown).

    Parameters
    ----------
    name:
        Identifier for this breaker (e.g. 'telegram_api', 'project_discovery').
    failure_threshold:
        Consecutive failures before tripping from CLOSED to OPEN (default 5).
    cooldown_seconds:
        Seconds to remain in OPEN before allowing a probe in HALF_OPEN (default 30.0).
    half_open_probes:
        Number of concurrent probe requests permitted in HALF_OPEN (default 1).
    is_failure_func:
        Optional custom callable (exc) -> bool to classify breaker failures.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        half_open_probes: int = 1,
        is_failure_func: Callable[[BaseException], bool] | None = None,
        metrics: Any = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        if half_open_probes < 1:
            raise ValueError("half_open_probes must be >= 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_probes = half_open_probes
        self.is_failure_func = is_failure_func

        self.state: str = "CLOSED"
        self.failure_count: int = 0
        self.opened_at: float = 0.0
        self.probe_in_flight: int = 0
        self._lock = asyncio.Lock()
        self._metrics = metrics

        log.info(
            "circuit_created dependency=%s threshold=%d cooldown=%.1fs half_open_probes=%d",
            self.name, self.failure_threshold, self.cooldown_seconds, self.half_open_probes,
        )

    @classmethod
    def from_environment(
        cls,
        name: str,
        default_threshold: int = 5,
        default_cooldown: float = 30.0,
        default_probes: int = 1,
    ) -> "CircuitBreaker":
        """Build a CircuitBreaker from environment variables with optional per-dependency override.

        Settings:
        * ``CIRCUIT_BREAKER_FAILURE_THRESHOLD`` (default 5)
        * ``CIRCUIT_BREAKER_OPEN_TIMEOUT`` (default 30.0 s)
        * ``CIRCUIT_BREAKER_HALF_OPEN_PROBES`` (default 1)
        """
        def _int(var: str, default: int) -> int:
            raw = os.environ.get(var)
            return default if raw is None else int(raw)

        def _float(var: str, default: float) -> float:
            raw = os.environ.get(var)
            return default if raw is None else float(raw)

        prefix = name.upper().replace("-", "_").replace(".", "_") + "_"
        threshold = _int(prefix + "CIRCUIT_FAILURE_THRESHOLD", _int("CIRCUIT_BREAKER_FAILURE_THRESHOLD", default_threshold))
        cooldown = _float(prefix + "CIRCUIT_OPEN_TIMEOUT", _float("CIRCUIT_BREAKER_OPEN_TIMEOUT", default_cooldown))
        probes = _int(prefix + "CIRCUIT_HALF_OPEN_PROBES", _int("CIRCUIT_BREAKER_HALF_OPEN_PROBES", default_probes))

        return cls(name=name, failure_threshold=threshold, cooldown_seconds=cooldown, half_open_probes=probes)

    @property
    def is_closed(self) -> bool:
        return self.state == "CLOSED"

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @property
    def is_half_open(self) -> bool:
        return self.state == "HALF_OPEN"

    def reset(self) -> None:
        """Reset the breaker to initial CLOSED state."""
        self.state = "CLOSED"
        self.failure_count = 0
        self.opened_at = 0.0
        self.probe_in_flight = 0

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Execute *operation* protected by the circuit breaker.

        Raises
        ------
        CircuitOpenError:
            When circuit is OPEN or probe capacity in HALF_OPEN is reached.
        """
        async with self._lock:
            now = time.monotonic()
            if self.state == "OPEN":
                if now - self.opened_at >= self.cooldown_seconds:
                    self.state = "HALF_OPEN"
                    self.probe_in_flight = 0
                    log.info(
                        "circuit_half_open dependency=%s state=HALF_OPEN probes_allowed=%d",
                        self.name, self.half_open_probes,
                    )
                    if self._metrics is not None:
                        self._metrics.record_circuit_breaker(
                            opened=False, rejected=False, half_open=True, recovered=False
                        )
                else:
                    retry_after = max(0.0, self.opened_at + self.cooldown_seconds - now)
                    log.info(
                        "circuit_rejected dependency=%s state=OPEN retry_after=%.1fs",
                        self.name, retry_after,
                    )
                    if self._metrics is not None:
                        self._metrics.record_circuit_breaker(
                            opened=False, rejected=True, half_open=False, recovered=False
                        )
                    raise CircuitOpenError(self.name, retry_after)

            if self.state == "HALF_OPEN":
                if self.probe_in_flight < self.half_open_probes:
                    self.probe_in_flight += 1
                else:
                    retry_after = max(0.0, self.opened_at + self.cooldown_seconds - now)
                    log.info(
                        "circuit_rejected dependency=%s state=HALF_OPEN retry_after=%.1fs",
                        self.name, retry_after,
                    )
                    if self._metrics is not None:
                        self._metrics.record_circuit_breaker(
                            opened=False, rejected=True, half_open=False, recovered=False
                        )
                    raise CircuitOpenError(self.name, retry_after)

        try:
            result = await operation()
        except BaseException as exc:
            async with self._lock:
                if isinstance(exc, asyncio.CancelledError):
                    if self.state == "HALF_OPEN":
                        self.probe_in_flight = max(0, self.probe_in_flight - 1)
                    raise

                classify = self.is_failure_func or _is_breaker_failure
                if classify(exc):
                    if self.state == "HALF_OPEN":
                        self.state = "OPEN"
                        self.opened_at = time.monotonic()
                        self.probe_in_flight = 0
                        log.warning(
                            "circuit_probe_failure dependency=%s state=HALF_OPEN error=%s",
                            self.name, type(exc).__name__,
                        )
                        log.warning(
                            "circuit_opened dependency=%s state=OPEN failure_count=%d cooldown=%.1fs",
                            self.name, self.failure_count, self.cooldown_seconds,
                        )
                        if self._metrics is not None:
                            self._metrics.record_circuit_breaker(
                                opened=True, rejected=False, half_open=False, recovered=False
                            )
                    elif self.state == "CLOSED":
                        self.failure_count += 1
                        log.warning(
                            "circuit_failure dependency=%s state=CLOSED failure_count=%d/%d error=%s",
                            self.name, self.failure_count, self.failure_threshold, type(exc).__name__,
                        )
                        if self.failure_count >= self.failure_threshold:
                            self.state = "OPEN"
                            self.opened_at = time.monotonic()
                            log.warning(
                                "circuit_opened dependency=%s state=OPEN failure_count=%d cooldown=%.1fs",
                                self.name, self.failure_count, self.cooldown_seconds,
                            )
                            if self._metrics is not None:
                                self._metrics.record_circuit_breaker(
                                    opened=True, rejected=False, half_open=False, recovered=False
                                )
                else:
                    # Non-breaker failure (e.g. 400, 401, 403, 404, validation error)
                    if self.state == "HALF_OPEN":
                        self.state = "CLOSED"
                        self.failure_count = 0
                        self.probe_in_flight = 0
                        log.info("circuit_probe_success dependency=%s state=HALF_OPEN", self.name)
                        log.info("circuit_closed dependency=%s state=CLOSED", self.name)
                        if self._metrics is not None:
                            self._metrics.record_circuit_breaker(
                                opened=False, rejected=False, half_open=False, recovered=True
                            )
            raise
        else:
            async with self._lock:
                if self.state == "HALF_OPEN":
                    self.state = "CLOSED"
                    self.failure_count = 0
                    self.probe_in_flight = 0
                    log.info("circuit_probe_success dependency=%s state=HALF_OPEN", self.name)
                    log.info("circuit_closed dependency=%s state=CLOSED", self.name)
                    if self._metrics is not None:
                        self._metrics.record_circuit_breaker(
                            opened=False, rejected=False, half_open=False, recovered=True
                        )
                elif self.state == "CLOSED":
                    self.failure_count = 0
            return result


# ---------------------------------------------------------------------------
# Idempotency & Deduplication
# ---------------------------------------------------------------------------

class IdempotencyState(str, Enum):
    NEW = "NEW"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a conflicting payload."""


@dataclass
class IdempotencyRecord:
    key: str
    state: IdempotencyState
    created_at: float
    updated_at: float
    payload_hash: str | None = None
    result: Any = None
    error: str | None = None
    future: asyncio.Future[Any] | None = None


class IdempotencyStore:
    """Bounded in-memory idempotency and deduplication store with TTL eviction.

    Protects side-effecting operations against duplicate delivery, concurrent race
    conditions, and retry storms.
    Tracks state transitions: NEW -> PROCESSING -> COMPLETED (or FAILED).
    Concurrent requests with identical keys wait on the in-flight future rather than
    executing duplicate side effects.
    """

    def __init__(
        self,
        max_entries: int = 2048,
        ttl_seconds: float = 300.0,
        allow_retry_on_failure: bool = True,
    ) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.allow_retry_on_failure = allow_retry_on_failure
        self._records: dict[str, IdempotencyRecord] = {}
        self._order: deque[str] = deque()
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.conflicts = 0
        self.evictions = 0

    @classmethod
    def from_environment(cls, prefix: str = "IDEMPOTENCY_") -> IdempotencyStore:
        max_entries = int(os.environ.get(f"{prefix}MAX_ENTRIES", "2048"))
        ttl_seconds = float(os.environ.get(f"{prefix}TTL_SECONDS", "300.0"))
        return cls(max_entries=max_entries, ttl_seconds=ttl_seconds)

    @staticmethod
    def compute_payload_hash(payload: Any) -> str | None:
        if payload is None:
            return None
        try:
            serialized = json.dumps(payload, sort_keys=True, default=str)
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        except Exception:
            return hashlib.sha256(str(payload).encode("utf-8")).hexdigest()

    def _purge_expired_locked(self, now: float) -> None:
        while self._order:
            oldest_key = self._order[0]
            rec = self._records.get(oldest_key)
            if rec is None:
                self._order.popleft()
            elif now - rec.updated_at > self.ttl_seconds:
                self._order.popleft()
                self._records.pop(oldest_key, None)
                self.evictions += 1
            else:
                break

    async def get_state(self, key: str) -> IdempotencyState:
        now = time.monotonic()
        async with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return IdempotencyState.NEW
            if now - rec.updated_at > self.ttl_seconds:
                self._records.pop(key, None)
                return IdempotencyState.NEW
            return rec.state

    async def check_and_start(
        self,
        key: str,
        payload: Any = None,
    ) -> tuple[str, Any]:
        """Atomically check idempotency status.

        Returns:
            ("COMPLETED", result) if already completed successfully.
            ("PROCESSING", future) if another task is executing this key right now.
            ("FAILED", error) if previously failed and retries are disallowed.
            ("EXECUTE", None) if caller should proceed with execution.
        """
        now = time.monotonic()
        payload_hash = self.compute_payload_hash(payload)

        async with self._lock:
            self._purge_expired_locked(now)

            rec = self._records.get(key)
            if rec is not None:
                if now - rec.updated_at > self.ttl_seconds:
                    self._records.pop(key, None)
                    rec = None
                else:
                    if (
                        rec.payload_hash is not None
                        and payload_hash is not None
                        and rec.payload_hash != payload_hash
                    ):
                        self.conflicts += 1
                        log.warning(
                            "idempotency_payload_conflict key=%s expected=%s got=%s",
                            key, rec.payload_hash, payload_hash,
                        )
                        raise IdempotencyConflictError(
                            f"Idempotency key {key!r} already used with different payload"
                        )

                    if rec.state == IdempotencyState.COMPLETED:
                        self.hits += 1
                        log.debug("idempotency_hit_completed key=%s", key)
                        return ("COMPLETED", rec.result)
                    elif rec.state == IdempotencyState.PROCESSING:
                        self.hits += 1
                        log.debug("idempotency_hit_processing key=%s", key)
                        return ("PROCESSING", rec.future)
                    elif rec.state == IdempotencyState.FAILED:
                        if not self.allow_retry_on_failure:
                            log.debug("idempotency_hit_failed_no_retry key=%s", key)
                            return ("FAILED", rec.error)
                        log.info("idempotency_retry_reopen key=%s", key)
                        loop = asyncio.get_running_loop()
                        rec.state = IdempotencyState.PROCESSING
                        rec.updated_at = now
                        rec.future = loop.create_future()
                        rec.error = None
                        return ("EXECUTE", None)

            self.misses += 1
            while len(self._records) >= self.max_entries and self._order:
                oldest = self._order.popleft()
                if self._records.pop(oldest, None) is not None:
                    self.evictions += 1

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            new_rec = IdempotencyRecord(
                key=key,
                state=IdempotencyState.PROCESSING,
                created_at=now,
                updated_at=now,
                payload_hash=payload_hash,
                future=fut,
            )
            self._records[key] = new_rec
            self._order.append(key)
            return ("EXECUTE", None)

    async def complete(self, key: str, result: Any) -> None:
        now = time.monotonic()
        async with self._lock:
            rec = self._records.get(key)
            if rec is not None and rec.state == IdempotencyState.PROCESSING:
                rec.state = IdempotencyState.COMPLETED
                rec.result = result
                rec.updated_at = now
                if rec.future is not None and not rec.future.done():
                    rec.future.set_result(result)
                log.debug("idempotency_completed key=%s", key)

    async def fail(self, key: str, error: str | None = None, exc: BaseException | None = None) -> None:
        now = time.monotonic()
        async with self._lock:
            rec = self._records.get(key)
            if rec is not None and rec.state == IdempotencyState.PROCESSING:
                rec.state = IdempotencyState.FAILED
                rec.error = error or (type(exc).__name__ if exc else "Failed")
                rec.updated_at = now
                if rec.future is not None and not rec.future.done():
                    if exc is not None:
                        rec.future.set_exception(exc)
                    else:
                        rec.future.set_exception(RuntimeError(rec.error))
                log.debug("idempotency_failed key=%s error=%s", key, rec.error)

    async def execute(
        self,
        key: str,
        operation: Callable[[], Awaitable[T]],
        payload: Any = None,
        timeout: float | None = None,
    ) -> T:
        action, value = await self.check_and_start(key, payload)
        if action == "COMPLETED":
            return value
        if action == "PROCESSING":
            if isinstance(value, asyncio.Future):
                if timeout is not None and timeout > 0:
                    return await asyncio.wait_for(asyncio.shield(value), timeout=timeout)
                return await asyncio.shield(value)
            return value
        if action == "FAILED":
            raise RuntimeError(f"Idempotent operation {key} failed previously: {value}")

        # action == "EXECUTE"
        try:
            if timeout is not None and timeout > 0:
                res = await asyncio.wait_for(operation(), timeout=timeout)
            else:
                res = await operation()
            await self.complete(key, res)
            return res
        except BaseException as exc:
            await self.fail(key, exc=exc)
            raise

    def clear(self) -> None:
        self._records.clear()
        self._order.clear()


# ---------------------------------------------------------------------------
# Combined timeout + retry helper
# ---------------------------------------------------------------------------

async def with_timeout_and_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    policy: RetryPolicy,
    circuit_breaker: CircuitBreaker | None = None,
    idempotency_store: IdempotencyStore | None = None,
    idempotency_key: str | None = None,
    payload: Any = None,
    operation_name: str = "operation",
    request_id: str | None = None,
    metrics: Any = None,
) -> T:
    """Await *operation()* with a per-attempt timeout, bounded retry, and optional CircuitBreaker / IdempotencyStore.

    When *idempotency_store* and *idempotency_key* are provided, the logical operation
    is guarded by the store. Completed results are returned directly without touching
    the circuit breaker or retrying. Concurrent attempts wait on the in-flight future.
    When *circuit_breaker* is provided, the entire logical request (including all retries)
    is guarded by the circuit breaker. If the breaker is OPEN, it fails fast immediately
    without starting timeouts or retrying.
    """
    if idempotency_store is not None and idempotency_key is not None:
        async def _idempotent_op() -> T:
            if circuit_breaker is not None:
                async def _breaker_run() -> T:
                    return await _execute_with_timeout_and_retry(
                        operation,
                        timeout_seconds=timeout_seconds,
                        policy=policy,
                        operation_name=operation_name,
                        request_id=request_id or idempotency_key,
                        metrics=metrics,
                    )
                return await circuit_breaker.call(_breaker_run)
            return await _execute_with_timeout_and_retry(
                operation,
                timeout_seconds=timeout_seconds,
                policy=policy,
                operation_name=operation_name,
                request_id=request_id or idempotency_key,
                metrics=metrics,
            )

        return await idempotency_store.execute(
            idempotency_key,
            _idempotent_op,
            payload=payload,
        )

    if circuit_breaker is not None:
        async def _run() -> T:
            return await _execute_with_timeout_and_retry(
                operation,
                timeout_seconds=timeout_seconds,
                policy=policy,
                operation_name=operation_name,
                request_id=request_id,
                metrics=metrics,
            )
        return await circuit_breaker.call(_run)

    return await _execute_with_timeout_and_retry(
        operation,
        timeout_seconds=timeout_seconds,
        policy=policy,
        operation_name=operation_name,
        request_id=request_id,
        metrics=metrics,
    )


async def _execute_with_timeout_and_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    policy: RetryPolicy,
    operation_name: str = "operation",
    request_id: str | None = None,
    metrics: Any = None,
) -> T:
    met = metrics
    rid = request_id or "-"
    max_attempts = 1 + max(0, policy.max_retries)

    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        log.debug(
            "op_attempt op=%s request_id=%s attempt=%d/%d",
            operation_name, rid, attempt, max_attempts,
        )
        started_ns = time.perf_counter_ns()
        try:
            if timeout_seconds > 0:
                result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
            else:
                result = await operation()
            duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            log.debug(
                "op_success op=%s request_id=%s attempt=%d duration_ms=%.1f",
                operation_name, rid, attempt, duration_ms,
            )
            return result  # type: ignore[return-value]

        except BaseException as exc:
            duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000

            if met is not None:
                met.record_retry_attempts(1.0)

            if isinstance(exc, asyncio.CancelledError):
                raise

            if not _is_transient(exc):
                log.info(
                    "op_failed_permanent op=%s request_id=%s attempt=%d "
                    "error_type=%s duration_ms=%.1f",
                    operation_name, rid, attempt, type(exc).__name__, duration_ms,
                )
                raise

            last_exc = exc
            if attempt == max_attempts:
                log.warning(
                    "op_retry_exhausted op=%s request_id=%s attempt=%d "
                    "error_type=%s duration_ms=%.1f",
                    operation_name, rid, attempt, type(exc).__name__, duration_ms,
                )
                break

            # Extract Retry-After from HTTP 429 responses
            retry_after: float | None = None
            if isinstance(exc, HTTPException) and exc.status_code == 429:
                # HTTPException.headers is set when callers provide it;
                # handle both present and absent cases safely.
                headers = getattr(exc, "headers", None) or {}
                raw_ra = headers.get("Retry-After") or headers.get("retry-after")
                if raw_ra is not None:
                    try:
                        retry_after = float(raw_ra)
                    except (ValueError, TypeError):
                        pass
            elif _HTTPX_AVAILABLE and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                raw_ra = exc.response.headers.get("Retry-After") or exc.response.headers.get("retry-after")
                if raw_ra is not None:
                    try:
                        retry_after = float(raw_ra)
                    except (ValueError, TypeError):
                        pass

            delay = policy.backoff_seconds(attempt, retry_after)
            log.info(
                "op_retry_scheduled op=%s request_id=%s attempt=%d "
                "error_type=%s delay_s=%.2f duration_ms=%.1f",
                operation_name, rid, attempt, type(exc).__name__, delay, duration_ms,
            )
            await asyncio.sleep(delay)

    assert last_exc is not None  # loop always sets it before break
    raise last_exc
