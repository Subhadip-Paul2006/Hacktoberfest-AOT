"""Tests for AgentSupervisor covering health, supervision, recovery, and alerting."""
from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from sutra.agents import (
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentHealthState,
    AgentHealthSnapshot,
    AgentManager,
    AgentRegistry,
    AgentSession,
    AgentSessionStatus,
    AgentSupervisor,
    GenericCLIAdapter,
    SupervisionPolicy,
)


class FakeAdapter:
    """Standalone fake adapter for supervisor tests — no GenericCLIAdapter dependency."""

    def __init__(self, agent_id: str = "fake", fail_start: bool = False, **kwargs):
        self._agent_id = agent_id
        self.fail_start = fail_start
        self._simulated_status: dict[str, AgentSessionStatus] = {}
        self._caps = {
            AgentCapability.START,
            AgentCapability.STOP,
            AgentCapability.GET_STATUS,
            AgentCapability.SEND_PROMPT,
        }
        self.metadata: dict[str, Any] = {}

    def descriptor(self):
        from sutra.agents import AgentDescriptor, AgentAvailability
        return AgentDescriptor(
            agent_id=self._agent_id,
            provider="Fake",
            display_name="Fake",
            availability=AgentAvailability.INSTALLED,
            capabilities=tuple(sorted(c.value for c in self._caps)),
        )

    def capabilities(self):
        return self._caps

    def is_available(self):
        return True

    def require_capability(self, cap):
        if cap not in self._caps:
            raise Exception(f"capability {cap} not supported")

    async def start(self, session, **kwargs):
        if self.fail_start:
            raise Exception("simulated start failure")
        self._simulated_status[session.session_id] = AgentSessionStatus.RUNNING
        return session

    async def stop(self, session_id):
        self._simulated_status.pop(session_id, None)
        return AgentSession(session_id=session_id, status=AgentSessionStatus.STOPPED)

    async def get_status(self, session_id):
        return self._simulated_status.get(session_id, AgentSessionStatus.IDLE)

    async def send_prompt(self, session_id, prompt):
        pass

    async def subscribe(self, session_id):
        return iter([])


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    """Tests for AgentSupervisor."""

    def setUp(self) -> None:
        self.registry = AgentRegistry(populate_defaults=False)
        self.manager = AgentManager(self.registry)
        self.supervisor = AgentSupervisor(self.manager, self.registry, SupervisionPolicy(
            watchdog_interval_seconds=0.05,
            no_activity_threshold_seconds=0.3,
            stall_threshold_seconds=0.6,
            unresponsive_retries=2,
            unresponsive_retry_delay_seconds=0.05,
            max_recovery_attempts=2,
            recovery_cooldown_seconds=0.1,
        ))

    async def asyncSetUp(self) -> None:
        await self.supervisor.start()

    async def asyncTearDown(self) -> None:
        await self.supervisor.stop()

    # 1. health model
    def test_health_snapshot_defaults(self) -> None:
        snap = AgentHealthSnapshot(session_id="s1")
        self.assertEqual(snap.health, AgentHealthState.UNKNOWN.value)
        self.assertIsNone(snap.process_alive)
        self.assertEqual(snap.recovery_attempt, 0)

    def test_health_snapshot_to_dict(self) -> None:
        snap = AgentHealthSnapshot(
            session_id="s1",
            agent_id="a1",
            health=AgentHealthState.HEALTHY.value,
            recovery_active=True,
            recovery_attempt=1,
        )
        d = snap.to_dict()
        self.assertEqual(d["session_id"], "s1")
        self.assertEqual(d["health"], AgentHealthState.HEALTHY.value)
        self.assertEqual(d["recovery"]["active"], True)
        self.assertEqual(d["recovery"]["attempt"], 1)

    # 2. health state transitions
    def test_health_states_are_strings(self) -> None:
        for state in AgentHealthState:
            self.assertIsInstance(state.value, str)
            self.assertTrue(len(state.value) > 0)

    # 3. watchdog startup/shutdown
    async def test_watchdog_lifecycle(self) -> None:
        self.assertIsNotNone(self.supervisor._watchdog_task)
        self.assertFalse(self.supervisor._watchdog_task.done())

    # 4. healthy session detection
    async def test_healthy_session_detection(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        # Inject an activity event directly to set health to HEALTHY
        self.supervisor.on_adapter_event(AgentEvent(
            event_type=AgentEventType.AGENT_READY,
            agent_id="fake",
            agent_session_id=session.session_id,
        ))
        snap = self.supervisor.get_health(session.session_id)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.health, AgentHealthState.HEALTHY.value)
        await self.manager.stop_session(session.session_id)

    # 5. explicit adapter failure detection
    async def test_adapter_reports_failure_while_running(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        # Simulate the adapter reporting a terminal failure status
        adapter._simulated_status[session.session_id] = AgentSessionStatus.FAILED
        await asyncio.sleep(0.2)
        snap = self.supervisor.get_health(session.session_id)
        # Supervisor should detect process_alive=False (adapter reports terminal status)
        self.assertEqual(snap.process_alive, False)
        self.assertEqual(snap.health, AgentHealthState.FAILED.value)

    # 6. no-activity detection
    async def test_no_activity_detection(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        # Wait past no_activity_threshold (0.3s)
        await asyncio.sleep(0.5)
        snap = self.supervisor.get_health(session.session_id)
        self.assertIn(snap.health, {
            AgentHealthState.NO_RECENT_ACTIVITY.value,
            AgentHealthState.POSSIBLE_STALL.value,
        })

    # 7. possible-stall detection
    async def test_possible_stall_detection(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        # Wait past stall threshold (0.6s)
        await asyncio.sleep(0.8)
        snap = self.supervisor.get_health(session.session_id)
        self.assertIn(snap.health, {
            AgentHealthState.NO_RECENT_ACTIVITY.value,
            AgentHealthState.POSSIBLE_STALL.value,
        })

    # 8. activity resets health
    async def test_activity_resets_health(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        await asyncio.sleep(0.5)  # trigger NO_RECENT_ACTIVITY
        snap1 = self.supervisor.get_health(session.session_id)
        self.assertIn(snap1.health, {AgentHealthState.NO_RECENT_ACTIVITY.value, AgentHealthState.POSSIBLE_STALL.value})

        # Inject an activity event
        self.supervisor.on_adapter_event(AgentEvent(
            event_type=AgentEventType.AGENT_PROGRESS,
            agent_id="fake",
            agent_session_id=session.session_id,
        ))
        snap2 = self.supervisor.get_health(session.session_id)
        self.assertEqual(snap2.health, AgentHealthState.HEALTHY.value)

    # 9. user-initiated stop does not trigger recovery
    async def test_user_stop_does_not_recover(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        # Stop the session (sets stopped_at)
        await self.manager.stop_session(session.session_id)
        snap = self.supervisor._health.get(session.session_id)
        if snap is None:
            self.supervisor._ensure_health_snapshot(session.session_id)
            snap = self.supervisor._health[session.session_id]
        snap.session_status = AgentSessionStatus.FAILED.value
        snap.failure_info = "test failure"
        # Recovery should be blocked because stopped_at is set
        await self.supervisor._maybe_recover(session.session_id, session, snap)
        self.assertFalse(snap.recovery_active)

    # 10. stale generation blocks recovery
    async def test_stale_generation_blocks_recovery(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        session.status = AgentSessionStatus.FAILED
        await self.supervisor.supervise_session(session.session_id)
        snap = self.supervisor.get_health(session.session_id)
        # Set snapshot generation to stale value
        snap.generation = 0
        await self.supervisor._maybe_recover(session.session_id, session, snap)
        self.assertFalse(snap.recovery_active)

    # 11. supervision cleanup
    async def test_supervision_cleanup(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        self.assertIn(session.session_id, self.supervisor._supervision_tasks)
        await self.supervisor.stop_session_supervision(session.session_id)
        self.assertNotIn(session.session_id, self.supervisor._supervision_tasks)
        self.assertNotIn(session.session_id, self.supervisor._health)

    # 12. independent multi-session supervision
    async def test_independent_multi_session_supervision(self) -> None:
        adapter1 = FakeAdapter(agent_id="fake1")
        adapter2 = FakeAdapter(agent_id="fake2")
        self.registry.register(adapter1)
        self.registry.register(adapter2)
        s1 = self.manager.create_session("fake1", ".", ["."])
        s2 = self.manager.create_session("fake2", ".", ["."])
        await self.manager.start_session(s1.session_id)
        await self.manager.start_session(s2.session_id)
        await self.supervisor.supervise_session(s1.session_id)
        await self.supervisor.supervise_session(s2.session_id)
        await asyncio.sleep(0.15)
        snap1 = self.supervisor.get_health(s1.session_id)
        snap2 = self.supervisor.get_health(s2.session_id)
        self.assertIsNotNone(snap1)
        self.assertIsNotNone(snap2)
        self.assertEqual(snap1.agent_id, "fake1")
        self.assertEqual(snap2.agent_id, "fake2")

    # 13. alert deduplication
    async def test_alert_deduplication(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        session.status = AgentSessionStatus.FAILED
        snap = self.supervisor._health.get(session.session_id)
        if snap is None:
            self.supervisor._ensure_health_snapshot(session.session_id)
            snap = self.supervisor._health[session.session_id]
        snap.session_status = AgentSessionStatus.FAILED.value
        snap.failure_info = "test"
        # First call: should set alert
        await self.supervisor._on_health_change(session.session_id, snap)
        first_alert = self.supervisor._alerted_states.get(session.session_id)
        # Second call with same state: should not change
        await self.supervisor._on_health_change(session.session_id, snap)
        second_alert = self.supervisor._alerted_states.get(session.session_id)
        self.assertEqual(first_alert, second_alert)

    # 14. recovery events emitted
    async def test_recovery_events_emitted(self) -> None:
        captured = []
        orig_publish = self.manager.publish_event
        def patched(e):
            captured.append(e)
            return orig_publish(e)
        self.manager.publish_event = patched
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        session.status = AgentSessionStatus.FAILED
        snap = self.supervisor._health.get(session.session_id)
        if snap is None:
            self.supervisor._ensure_health_snapshot(session.session_id)
            snap = self.supervisor._health[session.session_id]
        snap.session_status = AgentSessionStatus.FAILED.value
        snap.failure_info = "test"
        await self.supervisor._maybe_recover(session.session_id, session, snap)
        # Should have emitted at least one event
        self.assertTrue(len(captured) >= 1)

    # 15. metrics shape
    async def test_metrics_shape(self) -> None:
        metrics = self.supervisor.get_metrics()
        self.assertIn("supervised_sessions", metrics)
        self.assertIn("healthy", metrics)
        self.assertIn("watchdog_running", metrics)

    # 16. get_health returns None for unknown session
    def test_get_health_unknown_session(self) -> None:
        snap = self.supervisor.get_health("nonexistent")
        self.assertIsNone(snap)

    # 17. supervisor callback integration via manager
    async def test_supervisor_callback_from_manager(self) -> None:
        adapter = FakeAdapter(agent_id="fake")
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        await self.manager.start_session(session.session_id)
        await self.supervisor.supervise_session(session.session_id)
        # Set the manager's supervisor callback
        self.manager.set_supervisor_callback(self.supervisor.on_adapter_event)
        # Simulate an adapter event
        event = AgentEvent(
            event_type=AgentEventType.AGENT_PROGRESS,
            agent_id="fake",
            agent_session_id=session.session_id,
            payload={"message": "doing work"},
        )
        self.manager._supervisor_callback(event)
        snap = self.supervisor.get_health(session.session_id)
        self.assertIsNotNone(snap)
        # Activity event should have been recorded
        self.assertIsNotNone(snap.last_activity_at)
        # Health will be updated by background loop; just verify it's a valid state
        self.assertIn(snap.health, {s.value for s in AgentHealthState})

    # 18. recovery cooldown prevents rapid retries
    async def test_recovery_cooldown_blocks_rapid_retries(self) -> None:
        adapter = FakeAdapter(agent_id="fake", fail_start=True)
        self.registry.register(adapter)
        session = self.manager.create_session("fake", ".", ["."])
        session.status = AgentSessionStatus.FAILED
        self.manager._sessions[session.session_id] = session
        self.manager._session_generations[session.session_id] = session.generation
        self.supervisor._ensure_health_snapshot(session.session_id)
        snap = self.supervisor._health[session.session_id]
        snap.session_status = AgentSessionStatus.FAILED.value
        snap.failure_info = "test"
        # First attempt
        await self.supervisor._maybe_recover(session.session_id, session, snap)
        first_attempts = self.supervisor._recovery_attempts.get(session.session_id, 0)
        # Cooldown should block immediate second attempt
        await asyncio.sleep(0.05)
        snap2 = AgentHealthSnapshot(
            session_id=session.session_id,
            agent_id=session.agent_id,
            project_id=session.project_id,
            generation=session.generation,
            session_status=AgentSessionStatus.FAILED.value,
            failure_info="test2",
        )
        await self.supervisor._maybe_recover(session.session_id, session, snap2)
        second_attempts = self.supervisor._recovery_attempts.get(session.session_id, 0)
        self.assertEqual(first_attempts, second_attempts)

    # 19. multiple health snapshots independent
    async def test_independent_health_snapshots(self) -> None:
        adapter1 = FakeAdapter(agent_id="fake1")
        adapter2 = FakeAdapter(agent_id="fake2")
        self.registry.register(adapter1)
        self.registry.register(adapter2)
        s1 = self.manager.create_session("fake1", ".", ["."])
        s2 = self.manager.create_session("fake2", ".", ["."])
        await self.manager.start_session(s1.session_id)
        await self.manager.start_session(s2.session_id)
        await self.supervisor.supervise_session(s1.session_id)
        await self.supervisor.supervise_session(s2.session_id)
        await asyncio.sleep(0.15)
        snap1 = self.supervisor.get_health(s1.session_id)
        snap2 = self.supervisor.get_health(s2.session_id)
        self.assertIsNotNone(snap1)
        self.assertIsNotNone(snap2)
        # Fail s1 — s2 should not be affected
        session1 = self.manager.get_session(s1.session_id)
        session1.status = AgentSessionStatus.FAILED
        snap1.session_status = AgentSessionStatus.FAILED.value
        snap1.failure_info = "s1 failure"
        await self.supervisor._on_health_change(s1.session_id, snap1)
        snap2_after = self.supervisor.get_health(s2.session_id)
        self.assertEqual(snap2_after.health, AgentHealthState.HEALTHY.value)


class HealthModelTests(unittest.TestCase):
    """Unit tests for the health domain model."""

    def test_all_health_states_exist(self) -> None:
        required = {"HEALTHY", "NO_RECENT_ACTIVITY", "POSSIBLE_STALL", "UNRESPONSIVE",
                     "FAILED", "RECOVERING", "STOPPED", "UNKNOWN"}
        actual = {s.value for s in AgentHealthState}
        self.assertTrue(required.issubset(actual))

    def test_snapshot_defaults(self) -> None:
        snap = AgentHealthSnapshot()
        self.assertEqual(snap.health, AgentHealthState.UNKNOWN.value)
        self.assertEqual(snap.recovery_attempt, 0)
        self.assertFalse(snap.recovery_active)

    def test_snapshot_to_dict_has_recovery_key(self) -> None:
        snap = AgentHealthSnapshot(session_id="s1")
        d = snap.to_dict()
        self.assertIn("recovery", d)
        self.assertIn("active", d["recovery"])
        self.assertIn("attempt", d["recovery"])


if __name__ == "__main__":
    unittest.main()
