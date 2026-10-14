"""In-memory cache and request-coalescing (single-flight) engine for SŪTRA.

Session-Bound Lifecycle
-----------------------
Cache entries are namespaced by::

    {device_id}:{session_id}:{generation}:{command}:{payload}

The lifecycle API mirrors the session lifecycle:

``create_session_namespace(device_id, session_id, generation)``
    Called when a new authenticated session is established.
    The namespace starts empty; reads populate it lazily.

``invalidate_session(device_id, session_id, generation)``
    Called when a session is explicitly ended (Stop, Re-pair, expiry,
    heartbeat timeout, runtime restart that changes generation).
    Destroys ALL entries for that exact session namespace.

``invalidate_device(device_id)``
    Called on device revocation.
    Destroys ALL entries across all sessions/generations for a device.

TTL is preserved alongside session lifecycle: TTL governs freshness;
session destruction governs security/lifecycle.  Both are always active.

SingleFlight lifecycle mirrors the same boundaries:

- ``cancel_for_session``: cancel flights within one session namespace
- ``cancel_for_device``:  cancel all flights for a device (revocation)

Each flight tracks both the shared Future (for coalesced waiters) AND the
executor Task (the coroutine running the operation).  Cancellation signals
the future so coalesced waiters are notified AND cancels the executor task
so the underlying operation is interrupted promptly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from threading import RLock
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

SAFE_CACHE_COMMAND_TTLS: dict[str, float] = {
    "project.list": 10.0,
    "workspace.list": 10.0,
    "agent.list": 10.0,
    "runtime.status": 5.0,
    "runtime.health": 5.0,
}

COALESCIBLE_COMMANDS: set[str] = {
    "project.list",
    "workspace.list",
    "agent.list",
    "runtime.status",
    "runtime.health",
}


def normalize_payload_key(payload: dict[str, Any] | None) -> str:
    """Deterministically serialize payload to a normalized canonical string."""
    if not payload:
        return ""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(sorted(payload.items()))


def make_cache_key(device_id: str, session_id: str, generation: int, command: str, payload: dict[str, Any] | None = None) -> str:
    """Build a strictly isolated cache key bound to device, session, generation, command, and payload."""
    norm_payload = normalize_payload_key(payload)
    return f"{device_id}:{session_id}:{generation}:{command}:{norm_payload}"


def session_cache_prefix(device_id: str, session_id: str, generation: int) -> str:
    """Return the cache key prefix that scopes all entries to one session namespace."""
    return f"{device_id}:{session_id}:{generation}:"


def device_cache_prefix(device_id: str) -> str:
    """Return the cache key prefix that scopes all entries to a device (all sessions)."""
    return f"{device_id}:"


class CacheStore:
    """Thread-safe in-memory cache with monotonic TTL expiration.

    Entries are namespaced by ``device_id:session_id:generation`` so that
    separate sessions are always isolated.  Session lifecycle methods
    provide explicit creation and destruction of those namespaces.
    """

    def __init__(self, max_entries: int = 1000):
        self._entries: dict[str, tuple[Any, float]] = {}
        self._max_entries = max_entries
        self._lock = RLock()

    # ------------------------------------------------------------------
    # Session-bound lifecycle API
    # ------------------------------------------------------------------

    def create_session_namespace(self, device_id: str, session_id: str, generation: int) -> None:
        """Mark a new session namespace as active.

        The namespace starts empty — entries populate on demand.
        Defensively evicts any stale entries sharing the same prefix
        (should never occur in practice given monotonically increasing
        generation numbers, but provides an extra safety net).
        """
        prefix = session_cache_prefix(device_id, session_id, generation)
        with self._lock:
            stale = [k for k in self._entries if k.startswith(prefix)]
            for k in stale:
                self._entries.pop(k, None)
        log.debug(
            "cache_created device=%s session=%s generation=%d",
            device_id, session_id, generation,
        )

    def invalidate_session(self, device_id: str, session_id: str, generation: int) -> int:
        """Destroy ALL cache entries for one session namespace.

        Called on: Stop Session, Re-pair, session expiry, heartbeat timeout,
        and any runtime restart that creates a new generation.

        Returns the number of entries removed.
        """
        prefix = session_cache_prefix(device_id, session_id, generation)
        count = self.invalidate_prefix(prefix)
        log.debug(
            "cache_destroyed device=%s session=%s generation=%d removed=%d",
            device_id, session_id, generation, count,
        )
        return count

    def invalidate_device(self, device_id: str) -> int:
        """Destroy ALL cache entries for a device across all sessions/generations.

        Called on: device revocation.

        Returns the number of entries removed.
        """
        prefix = device_cache_prefix(device_id)
        count = self.invalidate_prefix(prefix)
        log.debug(
            "cache_invalidated device=%s removed=%d", device_id, count,
        )
        return count

    # ------------------------------------------------------------------
    # Core get / set / delete
    # ------------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if now >= expires_at:
                self._entries.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        expires_at = time.monotonic() + ttl_seconds
        with self._lock:
            if len(self._entries) >= self._max_entries:
                self._evict_expired()
                if len(self._entries) >= self._max_entries:
                    # Remove oldest entry
                    oldest_key = next(iter(self._entries))
                    self._entries.pop(oldest_key, None)
            self._entries[key] = (value, expires_at)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            matching = [k for k in self._entries if k.startswith(prefix)]
            for k in matching:
                self._entries.pop(k, None)
            return len(matching)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
        log.debug("cache_cleared")

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._entries.items() if now >= exp]
        for k in expired:
            self._entries.pop(k, None)


class SingleFlight:
    """Coalesces concurrent identical in-flight async operations into a single execution.

    Flight keys encode ``device_id:session_id:generation`` so that requests from
    different sessions NEVER coalesce.  Cancellation is available at both the
    session and device granularity to mirror the cache lifecycle.

    Each active flight tracks:

    - a shared ``asyncio.Future`` that coalesced waiters await, and
    - the ``asyncio.Task`` executing the operation.

    Cancellation via ``cancel_for_session`` / ``cancel_for_device``:

    1. Sets the exception (or cancels) the shared future so any coalesced
       ``await asyncio.shield(future)`` callers are unblocked immediately.
    2. Cancels the executor task so the underlying operation coroutine
       (e.g. a slow WSS round-trip) is interrupted rather than left running.
    """

    def __init__(self) -> None:
        # flight_key -> (device_id, shared_future)
        self._flights: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        # flight_key -> executor asyncio.Task (the coroutine running operation())
        self._executor_tasks: dict[str, asyncio.Task[Any]] = {}

    async def execute(
        self,
        flight_key: str,
        device_id: str,
        operation: Callable[[], Awaitable[Any]],
        on_coalesced: Callable[[], None] | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        existing = self._flights.get(flight_key)
        if existing is not None:
            _, future = existing
            if on_coalesced:
                on_coalesced()
            log.debug("single_flight_joined key=%s", flight_key)
            return await asyncio.shield(future)

        future: asyncio.Future[Any] = loop.create_future()
        self._flights[flight_key] = (device_id, future)
        # Track the executing task so cancel_for_* can interrupt the operation.
        current_task = asyncio.current_task()
        if current_task is not None:
            self._executor_tasks[flight_key] = current_task

        try:
            result = await operation()
            if not future.done():
                future.set_result(result)
            return result
        except asyncio.CancelledError:
            # If cancel_for_session/cancel_for_device already set an exception on
            # the shared future, re-raise that meaningful exception (e.g. "session
            # stopped") rather than the raw CancelledError, so callers see the
            # reason for cancellation.
            if future.done() and not future.cancelled():
                try:
                    exc = future.exception()
                    if exc is not None:
                        raise exc
                except asyncio.CancelledError:
                    pass
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._flights.pop(flight_key, None)
            self._executor_tasks.pop(flight_key, None)

    def _cancel_flight(
        self,
        key: str,
        future: asyncio.Future[Any],
        exception: Exception | None,
    ) -> None:
        """Cancel one flight entry: signal the shared future AND cancel the executor task.

        Signalling the future notifies any coalesced waiters.
        Cancelling the executor task interrupts the blocking operation itself.
        """
        if not future.done():
            if exception is not None:
                future.set_exception(exception)
            else:
                future.cancel()
        executor_task = self._executor_tasks.pop(key, None)
        if executor_task is not None and not executor_task.done():
            executor_task.cancel()
        self._flights.pop(key, None)

    def cancel_for_session(
        self,
        device_id: str,
        session_id: str,
        generation: int,
        exception: Exception | None = None,
    ) -> int:
        """Cancel all in-flight requests for one specific session.

        Uses key-prefix matching on ``device_id:session_id:generation:`` so
        that only flights for this exact session/generation are affected.
        Flights belonging to a different session or generation on the same
        device are left untouched.
        """
        prefix = session_cache_prefix(device_id, session_id, generation)
        count = 0
        for key, (dev_id, future) in list(self._flights.items()):
            if key.startswith(prefix) and not future.done():
                count += 1
                self._cancel_flight(key, future, exception)
        if count:
            log.debug(
                "single_flight_cancelled device=%s session=%s generation=%d count=%d",
                device_id, session_id, generation, count,
            )
        return count

    def cancel_for_device(self, device_id: str, exception: Exception | None = None) -> int:
        """Cancel or fail all in-flight requests belonging to a device immediately.

        Used for device revocation.  For session-level cancellation prefer
        ``cancel_for_session``.
        """
        count = 0
        for key, (dev_id, future) in list(self._flights.items()):
            if dev_id == device_id and not future.done():
                count += 1
                self._cancel_flight(key, future, exception)
        if count:
            log.debug(
                "single_flight_cancelled device=%s count=%d", device_id, count,
            )
        return count

    def active_count(self) -> int:
        return len(self._flights)
