"""Tests for JulesAdapter covering asynchronous cloud execution, descriptor, capabilities, and lifecycle."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sutra.agents import (
    AgentAvailability,
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionStatus,
    AgentUnavailableError,
    JulesAdapter,
)


class JulesAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Test suite for JulesAdapter."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()
        self.project_path = self.root_path / "jules_test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.python_exe = sys.executable

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 1. Descriptor and capabilities
    def test_descriptor_and_capabilities(self) -> None:
        adapter = JulesAdapter(executable="jules_missing_bin")
        desc = adapter.descriptor()
        self.assertEqual(desc.agent_id, "jules")
        self.assertEqual(desc.provider, "Google")
        self.assertEqual(desc.display_name, "Jules")

        caps = adapter.capabilities()
        self.assertIn(AgentCapability.START, caps)
        self.assertIn(AgentCapability.STOP, caps)
        self.assertIn(AgentCapability.SEND_PROMPT, caps)
        self.assertIn(AgentCapability.STREAM_OUTPUT, caps)
        self.assertIn(AgentCapability.GET_STATUS, caps)
        self.assertIn(AgentCapability.GIT_AWARE, caps)
        self.assertIn(AgentCapability.CLI, caps)
        self.assertIn(AgentCapability.API, caps)

        # Invariant from Section 13: Jules is marked as cloud remote execution
        self.assertTrue(desc.metadata.get("cloud_agent"))
        self.assertTrue(desc.metadata.get("remote_execution"))

    # 2. Availability detection
    def test_availability_detection(self) -> None:
        # Neither CLI nor API token
        with patch.dict(os.environ, {}, clear=True):
            adapter_unavail = JulesAdapter(executable="jules_missing_bin")
            self.assertFalse(adapter_unavail.is_available())
            self.assertEqual(adapter_unavail.descriptor().availability, AgentAvailability.UNAVAILABLE)

        # Cloud token configured
        with patch.dict(os.environ, {"JULES_API_KEY": "test-jules-key-123"}, clear=True):
            adapter_token = JulesAdapter(executable="jules_missing_bin")
            self.assertTrue(adapter_token.is_available())
            self.assertEqual(adapter_token.descriptor().availability, AgentAvailability.AVAILABLE)

        # CLI installed
        adapter_inst = JulesAdapter(executable=self.python_exe)
        self.assertTrue(adapter_inst.is_available())
        self.assertEqual(adapter_inst.descriptor().availability, AgentAvailability.INSTALLED)

    # 3. Starting unavailable raises AgentUnavailableError
    async def test_start_unavailable_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            adapter = JulesAdapter(executable="jules_missing_bin")
            session = AgentSession(
                agent_id="jules",
                project_id="jules_test_project",
                project_root=str(self.project_path),
            )
            with self.assertRaises(AgentUnavailableError) as ctx:
                await adapter.start(session)
            self.assertEqual(ctx.exception.code, "AGENT_UNAVAILABLE")

    # 4. Asynchronous remote cloud execution and event streaming
    async def test_jules_async_cloud_lifecycle_and_events(self) -> None:
        adapter = JulesAdapter(executable=self.python_exe, poll_interval_seconds=0.05)
        session = AgentSession(
            agent_id="jules",
            project_id="jules_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        simulation_steps = [
            {"type": "AGENT_PROGRESS", "payload": {"phase": "cloning_repo", "progress": 25}},
            {"type": "AGENT_PROGRESS", "payload": {"phase": "running_tests", "progress": 75}},
            {"type": "AGENT_OUTPUT", "payload": {"text": "All remote cloud checks passed"}},
        ]
        started = await adapter.start(
            session,
            task="Refactor async session management",
            simulation_steps=simulation_steps,
            on_event=events.append,
        )
        self.assertEqual(started.status, AgentSessionStatus.RUNNING)
        self.assertIn("remote_task_id", started.metadata)

        # Wait for simulation to complete
        for _ in range(30):
            st = await adapter.get_status(session.session_id)
            if st in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.05)

        self.assertEqual(st, AgentSessionStatus.COMPLETED)
        completed_events = [e for e in events if e.event_type == AgentEventType.AGENT_COMPLETED]
        self.assertEqual(len(completed_events), 1)

        progress_events = [e for e in events if e.event_type == AgentEventType.AGENT_PROGRESS]
        self.assertGreaterEqual(len(progress_events), 2)

    # 5. Stop cancels remote task cleanly
    async def test_jules_stop_cancels_remote_task(self) -> None:
        adapter = JulesAdapter(executable=self.python_exe, poll_interval_seconds=10.0)
        session = AgentSession(
            agent_id="jules",
            project_id="jules_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        await adapter.start(
            session,
            task="Long running task",
            simulation_steps=[{"type": "AGENT_PROGRESS", "payload": {"step": 1}}],
            on_event=events.append,
        )
        self.assertEqual(await adapter.get_status(session.session_id), AgentSessionStatus.RUNNING)

        stopped = await adapter.stop(session.session_id)
        self.assertEqual(stopped.status, AgentSessionStatus.STOPPED)
        self.assertEqual(await adapter.get_status(session.session_id), AgentSessionStatus.STOPPED)

        stopped_events = [e for e in events if e.event_type == AgentEventType.AGENT_STOPPED]
        self.assertGreaterEqual(len(stopped_events), 1)

    # 6. Send prompt to remote task
    async def test_jules_send_prompt(self) -> None:
        adapter = JulesAdapter(executable=self.python_exe, poll_interval_seconds=1.0)
        session = AgentSession(
            agent_id="jules",
            project_id="jules_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        await adapter.start(
            session,
            task="Initial task",
            on_event=events.append,
        )
        await adapter.send_prompt(session.session_id, "Additional instructions for Jules")

        prompt_events = [
            e for e in events
            if e.event_type == AgentEventType.AGENT_PROGRESS and e.payload.get("action") == "send_prompt"
        ]
        self.assertEqual(len(prompt_events), 1)

        await adapter.stop(session.session_id)


if __name__ == "__main__":
    unittest.main()
