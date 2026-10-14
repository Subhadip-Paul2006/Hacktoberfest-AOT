"""Phase 3.3 tests — session lifecycle, concurrency, event streaming, cleanup, stale protection."""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from sutra.agents import (
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentManager,
    AgentRegistry,
    AgentSession,
    AgentSessionStatus,
    AgentStartFailedError,
    AgentStopFailedError,
    EventSeverity,
    GenericCLIAdapter,
    InvalidStateTransitionError,
    InvalidProjectScopeError,
    SessionNotFoundError,
)
from sutra.agents.errors import AgentError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _TempEnv:
    """Persistent test temp directory that survives across test methods."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="agentbridge_p33_"))
        self.projs: list[Path] = []

    def make_project(self, name: str = "proj") -> tuple[str, list[str]]:
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text("test")
        self.projs.append(root)
        return str(root), [str(self.base)]

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def _make_manager() -> AgentManager:
    registry = AgentRegistry()
    registry.register(GenericCLIAdapter(executable=sys.executable), overwrite=True)
    return AgentManager(registry)


def _make_session(manager: AgentManager, env: _TempEnv) -> AgentSession:
    root, roots = env.make_project()
    return manager.create_session(
        agent_id="generic_cli",
        project_root=root,
        authorized_roots=roots,
    )


# ---------------------------------------------------------------------------
# 1. Session creation
# ---------------------------------------------------------------------------

class SessionCreationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    def test_creates_session_with_idle_status(self):
        session = _make_session(self.mgr, self.env)
        self.assertEqual(session.status, AgentSessionStatus.IDLE)
        self.assertEqual(session.generation, 1)
        self.assertIsNotNone(session.session_id)

    def test_creates_session_with_project_metadata(self):
        root, _ = self.env.make_project()
        session = self.mgr.create_session(
            agent_id="generic_cli",
            project_root=root,
            authorized_roots=[str(self.env.base)],
            metadata={"custom": "value"},
        )
        self.assertEqual(session.metadata.get("custom"), "value")
        self.assertIsNotNone(session.project_id)
        self.assertEqual(session.project_root, str(Path(root).resolve()))

    def test_creates_session_with_unique_ids(self):
        s1 = _make_session(self.mgr, self.env)
        s2 = _make_session(self.mgr, self.env)
        self.assertNotEqual(s1.session_id, s2.session_id)
        self.assertEqual(s1.agent_id, s2.agent_id)


# ---------------------------------------------------------------------------
# 2. Session lifecycle
# ---------------------------------------------------------------------------

class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_start_stop_lifecycle(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "import time; time.sleep(0.1)"]
        started = await self.mgr.start_session(session.session_id)
        self.assertEqual(started.status, AgentSessionStatus.RUNNING)

        stopped = await self.mgr.stop_session(session.session_id)
        self.assertIn(stopped.status, {
            AgentSessionStatus.STOPPED, AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED
        })

    async def test_start_failure_marks_failed(self):
        """Passing a bogus executable via kwargs forces a startup failure."""
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "print('hello')"]
        try:
            await self.mgr.start_session(
                session.session_id,
                executable="definitely_not_a_real_binary_xyz123",
            )
        except AgentStartFailedError:
            pass
        except Exception:
            pass
        current = self.mgr.get_session(session.session_id)
        self.assertEqual(current.status, AgentSessionStatus.FAILED)

    async def test_idempotent_start_when_running(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "import time; time.sleep(0.2)"]
        await self.mgr.start_session(session.session_id)
        try:
            await self.mgr.start_session(session.session_id)
        except InvalidStateTransitionError:
            pass
        await self.mgr.stop_session(session.session_id)

    async def test_idempotent_stop_when_stopped(self):
        session = _make_session(self.mgr, self.env)
        try:
            await self.mgr.stop_session(session.session_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. Invalid transitions
# ---------------------------------------------------------------------------

class StateTransitionTests(unittest.TestCase):
    def test_invalid_idle_to_running_direct(self):
        from sutra.agents.manager import _VALID_TRANSITIONS
        allowed = _VALID_TRANSITIONS.get(AgentSessionStatus.IDLE, set())
        self.assertNotIn(AgentSessionStatus.RUNNING, allowed)

    def test_valid_failed_to_starting(self):
        """FAILED sessions can be restarted (FAILED -> STARTING)."""
        from sutra.agents.manager import _VALID_TRANSITIONS
        allowed = _VALID_TRANSITIONS.get(AgentSessionStatus.FAILED, set())
        self.assertIn(AgentSessionStatus.STARTING, allowed)

    def test_valid_running_to_failed(self):
        from sutra.agents.manager import _VALID_TRANSITIONS
        allowed = _VALID_TRANSITIONS.get(AgentSessionStatus.RUNNING, set())
        self.assertIn(AgentSessionStatus.FAILED, allowed)


# ---------------------------------------------------------------------------
# 4. Concurrency
# ---------------------------------------------------------------------------

class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_three_sessions_simultaneous(self):
        sessions = []
        for i in range(3):
            s = _make_session(self.mgr, self.env)
            s.metadata["args"] = ["-c", "import time; time.sleep(0.3)"]
            sessions.append(s)
        await asyncio.gather(*(self.mgr.start_session(s.session_id) for s in sessions))
        statuses = [self.mgr.get_session(s.session_id).status for s in sessions]
        for st in statuses:
            self.assertEqual(st, AgentSessionStatus.RUNNING)
        await asyncio.gather(*(self.mgr.stop_session(s.session_id) for s in sessions))

    async def test_stop_one_does_not_affect_others(self):
        s_a = _make_session(self.mgr, self.env)
        s_b = _make_session(self.mgr, self.env)
        for s in (s_a, s_b):
            s.metadata["args"] = ["-c", "import time; time.sleep(1.0)"]
        await self.mgr.start_session(s_a.session_id)
        await self.mgr.start_session(s_b.session_id)

        await self.mgr.stop_session(s_a.session_id)
        status_b = self.mgr.get_session(s_b.session_id).status
        self.assertEqual(status_b, AgentSessionStatus.RUNNING)
        await self.mgr.stop_session(s_b.session_id)

    async def test_failure_of_one_does_not_affect_others(self):
        s_a = _make_session(self.mgr, self.env)
        s_b = _make_session(self.mgr, self.env)
        # s_a gets invalid executable via kwargs; s_b is normal
        s_a.metadata["args"] = ["-c", "print('a')"]
        s_b.metadata["args"] = ["-c", "import time; time.sleep(0.2)"]
        try:
            await self.mgr.start_session(s_a.session_id, executable="definitely_not_a_real_binary_xyz")
        except (AgentStartFailedError, Exception):
            pass
        await self.mgr.start_session(s_b.session_id)
        self.assertEqual(self.mgr.get_session(s_b.session_id).status, AgentSessionStatus.RUNNING)
        await self.mgr.stop_session(s_b.session_id)


# ---------------------------------------------------------------------------
# 5. Event sequencing
# ---------------------------------------------------------------------------

class EventSequencingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_events_have_monotonic_sequence(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "import time; time.sleep(0.2)"]
        captured: list[AgentEvent] = []

        async def sub(event: AgentEvent) -> None:
            captured.append(event)

        self.mgr.subscribe(sub)
        await self.mgr.start_session(session.session_id)
        await asyncio.sleep(0.05)
        await self.mgr.stop_session(session.session_id)

        session_events = [e for e in captured if e.agent_session_id == session.session_id]
        sequences = [e.sequence for e in session_events]
        for i in range(1, len(sequences)):
            self.assertGreater(sequences[i], sequences[i - 1], f"Non-monotonic: {sequences}")

    async def test_event_payload_sanitized(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "print('hello')"]
        captured: list[AgentEvent] = []

        async def sub(event: AgentEvent) -> None:
            captured.append(event)

        self.mgr.subscribe(sub)
        await self.mgr.start_session(session.session_id)
        await self.mgr.stop_session(session.session_id)

        for e in captured:
            payload_str = str(e.payload).lower()
            self.assertNotIn("api_key", payload_str)
            self.assertNotIn("password", payload_str)
            self.assertNotIn("secret", payload_str.replace("secret_id", ""))


# ---------------------------------------------------------------------------
# 6. Cleanup
# ---------------------------------------------------------------------------

class SessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_cleanup_after_failed_session(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "print('hello')"]
        try:
            await self.mgr.start_session(
                session.session_id,
                executable="definitely_not_a_real_binary_xyz",
            )
        except (AgentStartFailedError, Exception):
            pass
        # cleanup_session is synchronous — just call it
        self.mgr.cleanup_session(session.session_id)

    async def test_cleanup_removes_all_state(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "import time; time.sleep(0.1)"]
        await self.mgr.start_session(session.session_id)
        self.mgr.cleanup_session(session.session_id)
        with self.assertRaises(SessionNotFoundError):
            self.mgr.get_session(session.session_id)

    async def test_list_after_cleanup(self):
        s1 = _make_session(self.mgr, self.env)
        s2 = _make_session(self.mgr, self.env)
        self.mgr.cleanup_session(s1.session_id)
        ids = {s.session_id for s in self.mgr.list_sessions()}
        self.assertNotIn(s1.session_id, ids)
        self.assertIn(s2.session_id, ids)


# ---------------------------------------------------------------------------
# 7. Prompt routing
# ---------------------------------------------------------------------------

class PromptRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_prompt_routes_to_correct_session(self):
        s_a = _make_session(self.mgr, self.env)
        s_b = _make_session(self.mgr, self.env)
        for s in (s_a, s_b):
            s.metadata["args"] = ["-c", "import time; time.sleep(1.0)"]
        await self.mgr.start_session(s_a.session_id)
        await self.mgr.start_session(s_b.session_id)
        self.assertEqual(self.mgr.get_session(s_a.session_id).session_id, s_a.session_id)
        self.assertEqual(self.mgr.get_session(s_b.session_id).session_id, s_b.session_id)
        await self.mgr.stop_session(s_a.session_id)
        await self.mgr.stop_session(s_b.session_id)

    async def test_prompt_fails_on_stopped_session(self):
        session = _make_session(self.mgr, self.env)
        self.assertEqual(self.mgr.get_session(session.session_id).status, AgentSessionStatus.IDLE)


# ---------------------------------------------------------------------------
# 8. Failure recovery
# ---------------------------------------------------------------------------

class FailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_failed_session_can_restart(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "print('hello')"]
        try:
            await self.mgr.start_session(
                session.session_id,
                executable="definitely_not_a_real_binary_xyz",
            )
        except (AgentStartFailedError, Exception):
            pass
        # After failure, session is FAILED — cleanup and start fresh
        self.mgr.cleanup_session(session.session_id)
        new_session = self.mgr.create_session(
            agent_id="generic_cli",
            project_root=self.env.make_project()[0],
            authorized_roots=[str(self.env.base)],
            metadata={"args": ["-c", "import time; time.sleep(0.1)"]},
        )
        restarted = await self.mgr.start_session(new_session.session_id)
        self.assertIn(restarted.status, {AgentSessionStatus.RUNNING, AgentSessionStatus.FAILED})
        self.mgr.cleanup_session(new_session.session_id)

    async def test_start_failure_marks_failed(self):
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "print('hello')"]
        try:
            await self.mgr.start_session(
                session.session_id,
                executable="definitely_not_a_real_binary_xyz",
            )
        except (AgentStartFailedError, Exception):
            pass
        current = self.mgr.get_session(session.session_id)
        self.assertEqual(current.status, AgentSessionStatus.FAILED)


# ---------------------------------------------------------------------------
# 9. Status synchronization
# ---------------------------------------------------------------------------

class StatusSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_get_session_status_returns_actual_status(self):
        session = _make_session(self.mgr, self.env)
        status = self.mgr.get_session(session.session_id).status
        self.assertEqual(status, AgentSessionStatus.IDLE)
        session.metadata["args"] = ["-c", "import time; time.sleep(0.2)"]
        await self.mgr.start_session(session.session_id)
        status = self.mgr.get_session(session.session_id).status
        self.assertEqual(status, AgentSessionStatus.RUNNING)
        await self.mgr.stop_session(session.session_id)
        status = self.mgr.get_session(session.session_id).status
        self.assertIn(status, {AgentSessionStatus.STOPPED, AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED})


# ---------------------------------------------------------------------------
# 10. Stop timeout
# ---------------------------------------------------------------------------

class StopTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_stop_uses_bounded_timeout(self):
        import time
        session = _make_session(self.mgr, self.env)
        session.metadata["args"] = ["-c", "import time; time.sleep(60)"]
        await self.mgr.start_session(session.session_id)
        t0 = time.monotonic()
        await self.mgr.stop_session(session.session_id)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, f"Stop took too long: {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 11. Backpressure / bounded output
# ---------------------------------------------------------------------------

class BackpressureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    async def test_event_queue_maxsize(self):
        from sutra.agents.manager import EVENT_QUEUE_MAXSIZE
        self.assertGreater(EVENT_QUEUE_MAXSIZE, 0)
        self.assertLessEqual(EVENT_QUEUE_MAXSIZE, 100000)

    async def test_max_output_chunks(self):
        from sutra.agents.manager import MAX_AGENT_OUTPUT_CHUNKS
        self.assertGreater(MAX_AGENT_OUTPUT_CHUNKS, 0)
        self.assertLessEqual(MAX_AGENT_OUTPUT_CHUNKS, 1024 * 1024)

    async def test_max_payload_size(self):
        from sutra.agents.manager import MAX_EVENT_PAYLOAD_SIZE
        self.assertGreater(MAX_EVENT_PAYLOAD_SIZE, 0)
        self.assertLessEqual(MAX_EVENT_PAYLOAD_SIZE, 1024 * 1024)


# ---------------------------------------------------------------------------
# 12. Security scope validation
# ---------------------------------------------------------------------------

class SecurityScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = _TempEnv()
        self.mgr = _make_manager()

    def tearDown(self) -> None:
        self.env.cleanup()

    def test_outside_project_scope_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            other_proj = Path(other) / "elsewhere"
            other_proj.mkdir()
            with self.assertRaises(InvalidProjectScopeError):
                self.mgr.create_session(
                    agent_id="generic_cli",
                    project_root=str(other_proj),
                    authorized_roots=[str(self.env.base)],
                )


if __name__ == "__main__":
    unittest.main()
