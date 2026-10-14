"""Tests for CursorAdapter covering descriptor, capabilities, CLI invocation, telemetry, and security."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from sutra.agents import (
    AgentAvailability,
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionStatus,
    AgentUnavailableError,
    CursorAdapter,
    validate_project_scope,
)


class CursorAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Test suite for CursorAdapter."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()
        self.project_path = self.root_path / "cursor_test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.python_exe = sys.executable

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 1. Descriptor and capability compliance
    def test_descriptor_and_supported_capabilities(self) -> None:
        adapter = CursorAdapter()
        desc = adapter.descriptor()
        self.assertEqual(desc.agent_id, "cursor")
        self.assertEqual(desc.provider, "Cursor / Anysphere")
        self.assertEqual(desc.display_name, "Cursor")

        caps = adapter.capabilities()
        self.assertIn(AgentCapability.START, caps)
        self.assertIn(AgentCapability.STOP, caps)
        self.assertNotIn(AgentCapability.SEND_PROMPT, caps)
        self.assertIn(AgentCapability.STREAM_OUTPUT, caps)
        self.assertIn(AgentCapability.GET_STATUS, caps)
        self.assertIn(AgentCapability.HEADLESS, caps)
        self.assertIn(AgentCapability.CLI, caps)

        # Invariant from Section 10: DO NOT declare ACP, TOOL_CALLS, PERMISSION_REQUESTS unless genuinely supported
        self.assertNotIn(AgentCapability.ACP, caps)
        self.assertNotIn(AgentCapability.TOOL_CALLS, caps)
        self.assertNotIn(AgentCapability.PERMISSION_REQUESTS, caps)

    # 2. Availability detection
    def test_availability_detection(self) -> None:
        # Default executable 'agent' is likely unavailable unless installed
        adapter = CursorAdapter(executable="agent_non_existent_binary_xyz")
        self.assertFalse(adapter.is_available())
        self.assertEqual(adapter.descriptor().availability, AgentAvailability.UNAVAILABLE)

        # Mock with python executable
        mock_adapter = CursorAdapter(executable=self.python_exe)
        self.assertTrue(mock_adapter.is_available())
        self.assertEqual(mock_adapter.descriptor().availability, AgentAvailability.INSTALLED)

    # 3. Starting unavailable agent raises AgentUnavailableError
    async def test_start_unavailable_agent_raises(self) -> None:
        adapter = CursorAdapter(executable="agent_non_existent_binary_xyz")
        session = AgentSession(
            agent_id="cursor",
            project_id="cursor_test_project",
            project_root=str(self.project_path),
        )
        with self.assertRaises(AgentUnavailableError) as ctx:
            await adapter.start(session)
        self.assertEqual(ctx.exception.code, "AGENT_UNAVAILABLE")

    # 4. Starting with prompt delivers arguments securely
    async def test_cursor_execution_and_stdout(self) -> None:
        # We use a mock script simulating Cursor CLI response
        adapter = CursorAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="cursor",
            project_id="cursor_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Simulate cursor agent outputting progress text
        script = "import sys; print('Cursor Agent: Working on task...'); sys.stdout.flush()"
        await adapter.start(
            session,
            args=["-u", "-c", script],
            on_event=events.append,
        )
        self.assertEqual(session.status, AgentSessionStatus.RUNNING)

        for _ in range(50):
            st = await adapter.get_status(session.session_id)
            if st in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        self.assertEqual(st, AgentSessionStatus.COMPLETED)
        output_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
        self.assertTrue(any("Cursor Agent: Working on task..." in str(e.payload.get("text")) for e in output_events))

    # 5. Structured JSON telemetry parsing and secret redaction
    async def test_cursor_structured_telemetry_and_secret_redaction(self) -> None:
        adapter = CursorAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="cursor",
            project_id="cursor_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Script emits structured JSON telemetry line containing a secret key
        script = (
            "import sys, json\n"
            "telemetry = {'type': 'step', 'name': 'analyze', 'token': 'sk-1234567890abcdefghij', 'details': {'secret': 'pass123'}}\n"
            "print(json.dumps(telemetry))\n"
            "sys.stdout.flush()\n"
        )
        await adapter.start(
            session,
            args=["-u", "-c", script],
            on_event=events.append,
        )

        for _ in range(50):
            st = await adapter.get_status(session.session_id)
            if st in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        self.assertEqual(st, AgentSessionStatus.COMPLETED)

        structured_events = [
            e for e in events
            if e.event_type == AgentEventType.AGENT_OUTPUT and "structured" in e.payload
        ]
        self.assertGreaterEqual(len(structured_events), 1)
        data = structured_events[0].payload["structured"]
        self.assertEqual(data["type"], "step")
        self.assertEqual(data["name"], "analyze")
        # Ensure secret token was redacted inside JSON payload
        self.assertNotIn("sk-1234567890abcdefghij", str(data))
        self.assertIn("[REDACTED", str(data))

    # 6. Stop and lifecycle
    async def test_cursor_stop_lifecycle(self) -> None:
        adapter = CursorAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="cursor",
            project_id="cursor_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        await adapter.start(
            session,
            args=["-u", "-c", "import time; time.sleep(60)"],
            on_event=events.append,
        )
        self.assertEqual(await adapter.get_status(session.session_id), AgentSessionStatus.RUNNING)

        stopped = await adapter.stop(session.session_id)
        self.assertEqual(stopped.status, AgentSessionStatus.STOPPED)
        self.assertEqual(await adapter.get_status(session.session_id), AgentSessionStatus.STOPPED)

        stopped_events = [e for e in events if e.event_type == AgentEventType.AGENT_STOPPED]
        self.assertGreaterEqual(len(stopped_events), 1)


if __name__ == "__main__":
    unittest.main()
