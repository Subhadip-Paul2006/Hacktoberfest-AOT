"""Tests for PHASE 2.6A — Session-Bound Cache Lifecycle.

Tests cover:
 1.  Cache namespace created for new session
 2.  Cache hit within same session
 3.  Different sessions cannot access each other's cache
 4.  Generation isolation
 5.  Stop Session destroys session cache
 6.  Re-pair destroys old cache
 7.  Re-pair starts empty cache
 8.  Device revoke destroys cache
 9.  Session expiry destroys cache (invalidate_session called)
10.  Runtime restart / new generation cannot reuse old cache
11.  Temporary transport disconnect does NOT destroy still-valid session cache
12.  Invalidated session cannot use old SingleFlight
13.  Stop Session cancels in-flight SingleFlight requests
14.  New session cannot receive old in-flight response
15.  TTL still works
16.  Existing 2.6 cache tests (make_cache_key, invalidate_prefix) remain green
17.  Phase 3.1 agent imports are unaffected
"""
from __future__ import annotations

import asyncio
import time
import unittest

from sutra.cache import (
    CacheStore,
    COALESCIBLE_COMMANDS,
    SAFE_CACHE_COMMAND_TTLS,
    SingleFlight,
    device_cache_prefix,
    make_cache_key,
    normalize_payload_key,
    session_cache_prefix,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(cache: CacheStore, device: str, session: str, gen: int, command: str,
           value: object, ttl: float = 60.0) -> str:
    key = make_cache_key(device, session, gen, command)
    cache.set(key, value, ttl)
    return key


def _get(cache: CacheStore, device: str, session: str, gen: int, command: str) -> object:
    key = make_cache_key(device, session, gen, command)
    return cache.get(key)


# ---------------------------------------------------------------------------
# 1. Cache namespace created for new session
# ---------------------------------------------------------------------------

class TestCreateSessionNamespace(unittest.TestCase):
    """create_session_namespace initialises an empty namespace and evicts stale data."""

    def test_namespace_starts_empty_after_create(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-AA0001", "sess-1", 1
        # Pre-seed a stale entry with the same prefix (simulates defensive eviction)
        key = make_cache_key(device, session, gen, "project.list")
        cache.set(key, {"stale": True}, 60.0)
        # Now mark namespace as created — should evict that stale entry
        cache.create_session_namespace(device, session, gen)
        self.assertIsNone(cache.get(key), "Stale entries must be evicted on namespace creation")

    def test_different_session_not_evicted(self) -> None:
        cache = CacheStore()
        device = "AB-PC-AA0001"
        key_other = make_cache_key(device, "sess-X", 5, "project.list")
        cache.set(key_other, {"other": True}, 60.0)
        # Creating a namespace for a different session must not touch the other session
        cache.create_session_namespace(device, "sess-1", 1)
        self.assertIsNotNone(cache.get(key_other))

    def test_create_session_namespace_returns_none(self) -> None:
        cache = CacheStore()
        result = cache.create_session_namespace("AB-PC-AA0001", "s", 1)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 2. Cache hit within same session
# ---------------------------------------------------------------------------

class TestCacheHitSameSession(unittest.TestCase):

    def test_hit_within_session(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-BB0001", "sess-A", 1
        value = {"items": ["proj1", "proj2"]}
        _store(cache, device, session, gen, "project.list", value)
        result = _get(cache, device, session, gen, "project.list")
        self.assertEqual(result, value)

    def test_repeated_hit_returns_same_value(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-BB0001", "sess-A", 1
        value = {"items": []}
        _store(cache, device, session, gen, "runtime.status", value)
        for _ in range(5):
            self.assertEqual(_get(cache, device, session, gen, "runtime.status"), value)


# ---------------------------------------------------------------------------
# 3. Different sessions cannot access each other's cache
# ---------------------------------------------------------------------------

class TestSessionIsolation(unittest.TestCase):

    def test_session_a_cannot_read_session_b(self) -> None:
        cache = CacheStore()
        device = "AB-PC-CC0001"
        _store(cache, device, "sess-A", 1, "project.list", {"session": "A"})
        # Session B must get a miss even though the command is identical
        self.assertIsNone(_get(cache, device, "sess-B", 1, "project.list"))

    def test_session_b_populates_independently(self) -> None:
        cache = CacheStore()
        device = "AB-PC-CC0001"
        _store(cache, device, "sess-A", 1, "project.list", {"session": "A"})
        _store(cache, device, "sess-B", 1, "project.list", {"session": "B"})
        self.assertEqual(_get(cache, device, "sess-A", 1, "project.list"), {"session": "A"})
        self.assertEqual(_get(cache, device, "sess-B", 1, "project.list"), {"session": "B"})

    def test_invalidating_session_a_does_not_affect_session_b(self) -> None:
        cache = CacheStore()
        device = "AB-PC-CC0001"
        _store(cache, device, "sess-A", 1, "project.list", {"session": "A"})
        _store(cache, device, "sess-B", 2, "project.list", {"session": "B"})
        cache.invalidate_session(device, "sess-A", 1)
        self.assertIsNone(_get(cache, device, "sess-A", 1, "project.list"))
        self.assertEqual(_get(cache, device, "sess-B", 2, "project.list"), {"session": "B"})


# ---------------------------------------------------------------------------
# 4. Generation isolation
# ---------------------------------------------------------------------------

class TestGenerationIsolation(unittest.TestCase):

    def test_same_session_id_different_generation_is_isolated(self) -> None:
        """Even if session_id somehow repeats, generation change must isolate cache."""
        cache = CacheStore()
        device, session = "AB-PC-DD0001", "sess-same"
        _store(cache, device, session, 1, "project.list", {"gen": 1})
        # Generation bump: old cache must not be visible
        self.assertIsNone(_get(cache, device, session, 2, "project.list"))

    def test_generation_n_not_affected_by_generation_n_plus_1_invalidation(self) -> None:
        cache = CacheStore()
        device, session = "AB-PC-DD0001", "sess-same"
        _store(cache, device, session, 1, "project.list", {"gen": 1})
        _store(cache, device, session, 2, "project.list", {"gen": 2})
        cache.invalidate_session(device, session, 2)
        # gen-1 data must be untouched
        self.assertEqual(_get(cache, device, session, 1, "project.list"), {"gen": 1})
        self.assertIsNone(_get(cache, device, session, 2, "project.list"))


# ---------------------------------------------------------------------------
# 5. Stop Session destroys session cache
# ---------------------------------------------------------------------------

class TestStopSessionDestroysCache(unittest.TestCase):

    def test_invalidate_session_removes_all_commands(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-EE0001", "sess-stop", 3
        for cmd in ("project.list", "workspace.list", "runtime.status", "runtime.health"):
            _store(cache, device, session, gen, cmd, {"cmd": cmd})
        cache.invalidate_session(device, session, gen)
        for cmd in ("project.list", "workspace.list", "runtime.status", "runtime.health"):
            self.assertIsNone(_get(cache, device, session, gen, cmd),
                              f"Expected miss for {cmd} after stop_session")

    def test_invalidate_session_returns_correct_count(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-EE0001", "sess-stop", 3
        _store(cache, device, session, gen, "project.list", {})
        _store(cache, device, session, gen, "workspace.list", {})
        removed = cache.invalidate_session(device, session, gen)
        self.assertEqual(removed, 2)


# ---------------------------------------------------------------------------
# 6 & 7. Re-pair destroys old cache and starts fresh
# ---------------------------------------------------------------------------

class TestRePairCacheBehavior(unittest.TestCase):

    def test_re_pair_session_a_destroyed(self) -> None:
        """Session A cache is gone after invalidate_session is called (simulates re-pair)."""
        cache = CacheStore()
        device = "AB-PC-FF0001"
        _store(cache, device, "sess-A", 1, "project.list", {"session": "A"})
        # Simulate invalidating old session on re-pair
        cache.invalidate_session(device, "sess-A", 1)
        self.assertIsNone(_get(cache, device, "sess-A", 1, "project.list"))

    def test_re_pair_session_b_starts_empty(self) -> None:
        """After re-pair, session B namespace is empty; it does not see session A data."""
        cache = CacheStore()
        device = "AB-PC-FF0001"
        # Populate session A
        _store(cache, device, "sess-A", 1, "project.list", {"session": "A"})
        # Re-pair: invalidate A, create B
        cache.invalidate_session(device, "sess-A", 1)
        cache.create_session_namespace(device, "sess-B", 2)
        # Session B must be a miss for data that was in session A
        self.assertIsNone(_get(cache, device, "sess-B", 2, "project.list"))

    def test_session_b_cannot_read_session_a_data_at_all(self) -> None:
        """Session B key is structurally different — it cannot read session A data by key."""
        cache = CacheStore()
        device = "AB-PC-FF0001"
        _store(cache, device, "sess-A", 1, "project.list", {"session": "A"})
        # Session B lookup uses a different key prefix — guaranteed miss
        self.assertIsNone(_get(cache, device, "sess-B", 2, "project.list"))


# ---------------------------------------------------------------------------
# 8. Device revoke destroys cache
# ---------------------------------------------------------------------------

class TestDeviceRevokeDestroysCache(unittest.TestCase):

    def test_revoke_removes_all_sessions(self) -> None:
        cache = CacheStore()
        device = "AB-PC-GG0001"
        _store(cache, device, "sess-A", 1, "project.list", {"s": "A"})
        _store(cache, device, "sess-B", 2, "project.list", {"s": "B"})
        _store(cache, device, "sess-C", 3, "workspace.list", {"s": "C"})
        removed = cache.invalidate_device(device)
        self.assertEqual(removed, 3)
        self.assertIsNone(_get(cache, device, "sess-A", 1, "project.list"))
        self.assertIsNone(_get(cache, device, "sess-B", 2, "project.list"))
        self.assertIsNone(_get(cache, device, "sess-C", 3, "workspace.list"))

    def test_revoke_does_not_affect_other_device(self) -> None:
        cache = CacheStore()
        _store(cache, "AB-PC-GG0001", "sess-A", 1, "project.list", {"device": "GG"})
        _store(cache, "AB-PC-HH0002", "sess-X", 1, "project.list", {"device": "HH"})
        cache.invalidate_device("AB-PC-GG0001")
        self.assertIsNone(_get(cache, "AB-PC-GG0001", "sess-A", 1, "project.list"))
        self.assertIsNotNone(_get(cache, "AB-PC-HH0002", "sess-X", 1, "project.list"))


# ---------------------------------------------------------------------------
# 9. Session expiry destroys cache (same as stop — invalidate_session)
# ---------------------------------------------------------------------------

class TestSessionExpiryDestroysCache(unittest.TestCase):

    def test_expired_session_cache_destroyed(self) -> None:
        """invalidate_session removes data; expired session cannot use cached results."""
        cache = CacheStore()
        device, session, gen = "AB-PC-II0001", "sess-exp", 7
        _store(cache, device, session, gen, "runtime.status", {"ok": True})
        # Simulate session expiry -> invalidate
        cache.invalidate_session(device, session, gen)
        self.assertIsNone(_get(cache, device, session, gen, "runtime.status"))


# ---------------------------------------------------------------------------
# 10. Runtime restart / new generation cannot reuse old cache
# ---------------------------------------------------------------------------

class TestRuntimeRestartGenerationIsolation(unittest.TestCase):

    def test_new_generation_is_always_a_miss(self) -> None:
        cache = CacheStore()
        device = "AB-PC-JJ0001"
        _store(cache, device, "sess-run", 5, "project.list", {"gen": 5})
        # Runtime restarts with generation 6 — must not see gen-5 data
        self.assertIsNone(_get(cache, device, "sess-run", 6, "project.list"))

    def test_old_gen_cache_destroyed_on_invalidation(self) -> None:
        cache = CacheStore()
        device = "AB-PC-JJ0001"
        _store(cache, device, "sess-run", 5, "project.list", {"gen": 5})
        cache.invalidate_session(device, "sess-run", 5)
        self.assertIsNone(_get(cache, device, "sess-run", 5, "project.list"))


# ---------------------------------------------------------------------------
# 11. Temporary transport disconnect does NOT destroy still-valid session cache
# ---------------------------------------------------------------------------

class TestTemporaryDisconnectPreservesCache(unittest.TestCase):
    """
    Per spec §13: A temporary WebSocket drop does NOT equal an unpair.
    The session credential may still be valid; the cache must be preserved.

    We test the CacheStore API directly: after a transport disconnect the
    control plane should NOT call invalidate_session if the session status
    transitions to DISCONNECTED (not STOPPED/INVALIDATED).  In that case
    the cache entry must still be readable.
    """

    def test_cache_survives_when_no_invalidation_called(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-KK0001", "sess-live", 2
        _store(cache, device, session, gen, "project.list", {"alive": True})
        # Simulate temporary disconnect: do NOT call invalidate_session
        # (the control plane only calls invalidate_session when session_truly_ended)
        result = _get(cache, device, session, gen, "project.list")
        self.assertIsNotNone(result, "Cache must survive a temporary transport disconnect")
        self.assertEqual(result, {"alive": True})

    def test_cache_destroyed_only_when_session_truly_ended(self) -> None:
        cache = CacheStore()
        device, session, gen = "AB-PC-KK0001", "sess-live", 2
        _store(cache, device, session, gen, "project.list", {"alive": True})
        # Simulate session truly ended (heartbeat timeout -> WAITING_FOR_PAIRING)
        cache.invalidate_session(device, session, gen)
        self.assertIsNone(_get(cache, device, session, gen, "project.list"),
                          "Cache must be destroyed when session is truly ended")


# ---------------------------------------------------------------------------
# 12. Invalidated session cannot use old SingleFlight
# ---------------------------------------------------------------------------

class TestSingleFlightSessionIsolation(unittest.IsolatedAsyncioTestCase):

    async def test_different_sessions_do_not_coalesce(self) -> None:
        sf = SingleFlight()
        calls = []

        async def op_a() -> str:
            calls.append("A")
            return "result-A"

        async def op_b() -> str:
            calls.append("B")
            return "result-B"

        key_a = make_cache_key("dev", "sess-A", 1, "project.list")
        key_b = make_cache_key("dev", "sess-B", 2, "project.list")

        result_a = await sf.execute(key_a, "dev", op_a)
        result_b = await sf.execute(key_b, "dev", op_b)

        self.assertEqual(result_a, "result-A")
        self.assertEqual(result_b, "result-B")
        self.assertEqual(calls, ["A", "B"],
                         "Different sessions must NOT coalesce into one flight")

    async def test_same_session_different_generation_does_not_coalesce(self) -> None:
        sf = SingleFlight()
        calls: list[str] = []

        async def op(tag: str):
            calls.append(tag)
            return tag

        key_g1 = make_cache_key("dev", "sess", 1, "project.list")
        key_g2 = make_cache_key("dev", "sess", 2, "project.list")

        r1 = await sf.execute(key_g1, "dev", lambda: op("g1"))
        r2 = await sf.execute(key_g2, "dev", lambda: op("g2"))
        self.assertEqual(r1, "g1")
        self.assertEqual(r2, "g2")
        self.assertIn("g1", calls)
        self.assertIn("g2", calls)


# ---------------------------------------------------------------------------
# 13. Stop Session cancels in-flight SingleFlight requests
# ---------------------------------------------------------------------------

class TestStopSessionCancelsSingleFlight(unittest.IsolatedAsyncioTestCase):

    async def test_cancel_for_session_cancels_pending_future(self) -> None:
        sf = SingleFlight()
        device, session, gen = "dev-LL", "sess-stop", 1
        key = make_cache_key(device, session, gen, "project.list")

        started = asyncio.Event()

        async def slow_op():
            started.set()
            await asyncio.sleep(10)  # blocks indefinitely
            return {}

        task = asyncio.create_task(sf.execute(key, device, slow_op))
        await started.wait()

        exc = Exception("session stopped")
        cancelled = sf.cancel_for_session(device, session, gen, exc)
        self.assertGreater(cancelled, 0)

        with self.assertRaises(Exception):
            await task

    async def test_cancel_for_session_does_not_affect_other_session(self) -> None:
        sf = SingleFlight()
        device = "dev-LL"
        key_a = make_cache_key(device, "sess-A", 1, "project.list")
        key_b = make_cache_key(device, "sess-B", 2, "project.list")

        started_a = asyncio.Event()

        async def slow_a():
            started_a.set()
            await asyncio.sleep(10)
            return {}

        task_a = asyncio.create_task(sf.execute(key_a, device, slow_a))
        await started_a.wait()

        # Cancel only session-B flights; session-A flight must survive
        cancelled = sf.cancel_for_session(device, "sess-B", 2, Exception("stop"))
        self.assertEqual(cancelled, 0, "Session-B cancel must not affect session-A flight")

        task_a.cancel()
        await asyncio.gather(task_a, return_exceptions=True)

    async def test_cancel_for_session_returns_count(self) -> None:
        sf = SingleFlight()
        device, session, gen = "dev-LL", "sess-cnt", 1

        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)
            return {}

        key = make_cache_key(device, session, gen, "project.list")
        task = asyncio.create_task(sf.execute(key, device, slow))
        await started.wait()

        count = sf.cancel_for_session(device, session, gen, Exception("done"))
        self.assertEqual(count, 1)
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# 14. New session cannot receive old in-flight response
# ---------------------------------------------------------------------------

class TestNewSessionCannotReceiveOldFlight(unittest.IsolatedAsyncioTestCase):

    async def test_different_session_key_is_a_miss(self) -> None:
        """
        Old session flight key is device:sess-old:1:cmd.
        New session flight key is device:sess-new:2:cmd.
        They are different keys — coalescing is structurally impossible.
        """
        sf = SingleFlight()
        device = "dev-MM"
        key_old = make_cache_key(device, "sess-old", 1, "project.list")
        key_new = make_cache_key(device, "sess-new", 2, "project.list")
        self.assertNotEqual(key_old, key_new)

    async def test_result_from_old_flight_not_placed_in_new_session_cache(self) -> None:
        """
        Even if an old flight completes after session change, cache keys are
        scoped so the new session cache can never receive it.
        """
        cache = CacheStore()
        device = "dev-MM"
        old_key = make_cache_key(device, "sess-old", 1, "project.list")
        new_key = make_cache_key(device, "sess-new", 2, "project.list")
        # Old flight completes and tries to write to old key
        cache.set(old_key, {"from": "old"}, 60.0)
        # New session must not see it
        self.assertIsNone(cache.get(new_key))


# ---------------------------------------------------------------------------
# 15. TTL still works
# ---------------------------------------------------------------------------

class TestTTLBehavior(unittest.TestCase):

    def test_entry_expires_after_ttl(self) -> None:
        cache = CacheStore()
        key = make_cache_key("dev", "sess", 1, "runtime.status")
        cache.set(key, {"ok": True}, ttl_seconds=0.05)
        self.assertIsNotNone(cache.get(key), "Entry should be present before TTL")
        time.sleep(0.1)
        self.assertIsNone(cache.get(key), "Entry should have expired after TTL")

    def test_ttl_zero_not_stored(self) -> None:
        cache = CacheStore()
        key = make_cache_key("dev", "sess", 1, "runtime.status")
        cache.set(key, {"ok": True}, ttl_seconds=0.0)
        self.assertIsNone(cache.get(key))

    def test_ttl_negative_not_stored(self) -> None:
        cache = CacheStore()
        key = make_cache_key("dev", "sess", 1, "runtime.status")
        cache.set(key, {"ok": True}, ttl_seconds=-1.0)
        self.assertIsNone(cache.get(key))

    def test_canonical_ttl_values_present(self) -> None:
        """Canonical safe TTLs from spec must be unchanged."""
        self.assertEqual(SAFE_CACHE_COMMAND_TTLS["project.list"], 10.0)
        self.assertEqual(SAFE_CACHE_COMMAND_TTLS["workspace.list"], 10.0)
        self.assertEqual(SAFE_CACHE_COMMAND_TTLS["runtime.status"], 5.0)
        self.assertEqual(SAFE_CACHE_COMMAND_TTLS["runtime.health"], 5.0)


# ---------------------------------------------------------------------------
# 16. Existing 2.6 cache tests — make_cache_key, invalidate_prefix, etc.
# ---------------------------------------------------------------------------

class TestExistingCacheFunctionality(unittest.TestCase):

    def test_make_cache_key_format(self) -> None:
        key = make_cache_key("devA", "sess1", 3, "project.list")
        self.assertEqual(key, "devA:sess1:3:project.list:")

    def test_make_cache_key_with_payload(self) -> None:
        key = make_cache_key("devA", "sess1", 1, "file.list", {"path": "/x", "project_root": "/r"})
        self.assertIn("devA:sess1:1:file.list:", key)
        self.assertIn("path", key)

    def test_normalize_payload_key_stable(self) -> None:
        a = normalize_payload_key({"b": 2, "a": 1})
        b = normalize_payload_key({"a": 1, "b": 2})
        self.assertEqual(a, b)

    def test_normalize_payload_key_empty(self) -> None:
        self.assertEqual(normalize_payload_key(None), "")
        self.assertEqual(normalize_payload_key({}), "")

    def test_invalidate_prefix_removes_matching(self) -> None:
        cache = CacheStore()
        cache.set("devA:s:1:cmd:", {}, 60.0)
        cache.set("devA:s:1:other:", {}, 60.0)
        cache.set("devB:s:1:cmd:", {}, 60.0)
        removed = cache.invalidate_prefix("devA:")
        self.assertEqual(removed, 2)
        self.assertIsNone(cache.get("devA:s:1:cmd:"))
        self.assertIsNone(cache.get("devA:s:1:other:"))
        self.assertIsNotNone(cache.get("devB:s:1:cmd:"))

    def test_delete_single_entry(self) -> None:
        cache = CacheStore()
        key = "devA:s:1:cmd:"
        cache.set(key, {}, 60.0)
        self.assertTrue(cache.delete(key))
        self.assertFalse(cache.delete(key))

    def test_clear_removes_all(self) -> None:
        cache = CacheStore()
        cache.set("k1", {}, 60.0)
        cache.set("k2", {}, 60.0)
        cache.clear()
        self.assertIsNone(cache.get("k1"))
        self.assertIsNone(cache.get("k2"))

    def test_coalescible_commands_unchanged(self) -> None:
        for cmd in ("project.list", "workspace.list", "runtime.status", "runtime.health"):
            self.assertIn(cmd, COALESCIBLE_COMMANDS)

    def test_session_cache_prefix_format(self) -> None:
        prefix = session_cache_prefix("devA", "sess1", 3)
        self.assertEqual(prefix, "devA:sess1:3:")

    def test_device_cache_prefix_format(self) -> None:
        prefix = device_cache_prefix("devA")
        self.assertEqual(prefix, "devA:")

    def test_max_entries_eviction(self) -> None:
        cache = CacheStore(max_entries=3)
        for i in range(5):
            cache.set(f"key{i}", i, 60.0)
        # After eviction at least the recent entries should fit
        self.assertLessEqual(len(cache._entries), 3)


# ---------------------------------------------------------------------------
# 17. Phase 3.1 agent imports are unaffected
# ---------------------------------------------------------------------------

class TestPhase31AgentsUnaffected(unittest.TestCase):

    def test_agent_models_import(self) -> None:
        from sutra.agents.models import (
            AgentCapability, AgentDescriptor, AgentSession, AgentSessionStatus,
        )
        self.assertTrue(issubclass(AgentCapability, str))

    def test_agent_registry_import(self) -> None:
        from sutra.agents.registry import AgentRegistry
        reg = AgentRegistry(populate_defaults=False)
        self.assertEqual(reg.list(), [])

    def test_agent_manager_import(self) -> None:
        from sutra.agents.manager import AgentManager
        from sutra.agents.registry import AgentRegistry
        mgr = AgentManager(AgentRegistry(populate_defaults=False))
        self.assertEqual(mgr.list_sessions(), [])

    def test_adapters_module_import(self) -> None:
        from sutra.adapters import AdapterInfo, AgentRegistry
        info = AdapterInfo(name="test")
        self.assertEqual(info.status, "Not Connected")


# ---------------------------------------------------------------------------
# SingleFlight — cancel_for_device (existing behaviour unchanged)
# ---------------------------------------------------------------------------

class TestSingleFlightCancelForDevice(unittest.IsolatedAsyncioTestCase):

    async def test_cancel_for_device_cancels_all_device_flights(self) -> None:
        sf = SingleFlight()
        device = "dev-cancel"
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)
            return {}

        key = make_cache_key(device, "sess", 1, "project.list")
        task = asyncio.create_task(sf.execute(key, device, slow))
        await started.wait()

        count = sf.cancel_for_device(device, Exception("revoked"))
        self.assertEqual(count, 1)
        await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_for_device_does_not_affect_other_device(self) -> None:
        sf = SingleFlight()
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)
            return {}

        key_a = make_cache_key("dev-A", "sess", 1, "project.list")
        key_b = make_cache_key("dev-B", "sess", 1, "project.list")

        task_a = asyncio.create_task(sf.execute(key_a, "dev-A", slow))
        await started.wait()

        count = sf.cancel_for_device("dev-B", Exception("revoked"))
        self.assertEqual(count, 0, "dev-B cancel must not touch dev-A flight")
        task_a.cancel()
        await asyncio.gather(task_a, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
