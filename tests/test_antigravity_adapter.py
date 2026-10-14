"""Tests for AntigravityAdapter covering descriptor, capabilities, CLI execution, and lifecycle."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sutra.agents import (
    AgentAvailability,
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionStatus,
    AgentUnavailableError,
    AntigravityAdapter,
)
from sutra.agents.generic_cli import GenericCLIAdapter, _SessionProcessState


class AntigravityAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Test suite for AntigravityAdapter."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()
        self.project_path = self.root_path / "antigravity_test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.python_exe = sys.executable

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 1. Descriptor and capabilities
    def test_descriptor_and_capabilities(self) -> None:
        adapter = AntigravityAdapter(executable="agy_non_existent")
        desc = adapter.descriptor()
        self.assertEqual(desc.agent_id, "antigravity")
        self.assertEqual(desc.provider, "Google")
        self.assertEqual(desc.display_name, "Antigravity")

        caps = adapter.capabilities()
        self.assertIn(AgentCapability.START, caps)
        self.assertIn(AgentCapability.STOP, caps)
        self.assertIn(AgentCapability.SEND_PROMPT, caps)
        self.assertIn(AgentCapability.STREAM_OUTPUT, caps)
        self.assertIn(AgentCapability.GET_STATUS, caps)
        self.assertIn(AgentCapability.PLAN_EVENTS, caps)
        self.assertIn(AgentCapability.CLI, caps)
        self.assertIn(AgentCapability.HEADLESS, caps)

        # Invariant from Section 12: does not claim GUI automation / ACP
        self.assertNotIn(AgentCapability.ACP, caps)
        self.assertFalse(desc.metadata.get("gui_automation", False))

    # 2. Availability detection
    def test_availability_detection(self) -> None:
        adapter_missing = AntigravityAdapter(executable="agy_missing_binary_999")
        self.assertFalse(adapter_missing.is_available())
        self.assertEqual(adapter_missing.descriptor().availability, AgentAvailability.UNAVAILABLE)

        adapter_inst = AntigravityAdapter(executable=self.python_exe)
        self.assertTrue(adapter_inst.is_available())
        self.assertEqual(adapter_inst.descriptor().availability, AgentAvailability.INSTALLED)

    # 3. Starting unavailable raises AgentUnavailableError
    async def test_start_unavailable_raises(self) -> None:
        adapter = AntigravityAdapter(executable="agy_missing_binary_999")
        session = AgentSession(
            agent_id="antigravity",
            project_id="antigravity_test_project",
            project_root=str(self.project_path),
        )
        with self.assertRaises(AgentUnavailableError) as ctx:
            await adapter.start(session)
        self.assertEqual(ctx.exception.code, "AGENT_UNAVAILABLE")

    # 4. Successful execution with prompt instructions
    async def test_antigravity_execution_and_stdout(self) -> None:
        adapter = AntigravityAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="antigravity",
            project_id="antigravity_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        script = (
            "import sys\n"
            "print('Google Antigravity: Session initialized')\n"
            "print('Goal achieved successfully')\n"
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
        for _ in range(20):
            if any(e.event_type == AgentEventType.AGENT_OUTPUT for e in events):
                break
            await asyncio.sleep(0.05)
        output_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
        self.assertTrue(any("Google Antigravity: Session initialized" in str(e.payload.get("text")) for e in output_events))

    # 5. Stop and cleanup
    async def test_antigravity_stop_lifecycle(self) -> None:
        adapter = AntigravityAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="antigravity",
            project_id="antigravity_test_project",
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

    async def test_default_launch_uses_documented_stream_protocol_and_sends_initial_prompt(self) -> None:
        adapter = AntigravityAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="antigravity",
            project_id="antigravity_test_project",
            project_root=str(self.project_path),
        )

        with patch.object(GenericCLIAdapter, "start", new=AsyncMock(return_value=session)) as start_mock:
            with patch.object(adapter, "send_prompt", new=AsyncMock()) as prompt_mock:
                await adapter.start(session, prompt="Summarize this project")

        self.assertEqual(
            start_mock.await_args.kwargs["args"],
            ["--print", "--input-format", "stream-json", "--output-format", "stream-json"],
        )
        prompt_mock.assert_awaited_once_with(session.session_id, "Summarize this project")

    async def test_send_prompt_encodes_jsonl_without_interpreting_user_text(self) -> None:
        class _Writer:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, data: bytes) -> None:
                self.data.extend(data)

            async def drain(self) -> None:
                return None

            def is_closing(self) -> bool:
                return False

        writer = _Writer()
        process = SimpleNamespace(returncode=None, stdin=writer)
        adapter = AntigravityAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="antigravity",
            project_id="antigravity_test_project",
            project_root=str(self.project_path),
            status=AgentSessionStatus.RUNNING,
            metadata={"interaction_protocol": "antigravity-stream-json"},
        )
        state = _SessionProcessState(session, process)  # type: ignore[arg-type]
        adapter._active_sessions[session.session_id] = state

        await adapter.send_prompt(session.session_id, 'print("not a command")\n✅')

        self.assertEqual(
            json.loads(writer.data.decode("utf-8")),
            {"event": "user", "message": {"content": 'print("not a command")\n✅'}},
        )
        self.assertTrue(writer.data.endswith(b"\n"))

    async def test_stream_reader_extracts_response_and_conversation_id_only(self) -> None:
        adapter = AntigravityAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="antigravity",
            project_id="antigravity_test_project",
            project_root=str(self.project_path),
        )
        state = _SessionProcessState(session, SimpleNamespace())  # type: ignore[arg-type]
        events: list[AgentEvent] = []
        state.on_event = events.append
        stream = asyncio.StreamReader()
        stream.feed_data((
            '{"event":"init","conversation_id":"conv-123","init":{"tools":[]}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"hello "}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_info":{"parameters":{"command":"secret"}}}}\n'
            '{"event":"result","result":{"conversation_id":"conv-123","status":"SUCCESS","response":"hello world"}}\n'
        ).encode("utf-8"))
        stream.feed_eof()

        await adapter._stream_reader(state, stream, "stdout")

        self.assertEqual(session.metadata["provider_conversation_id"], "conv-123")
        self.assertEqual(session.metadata["final_response"], "hello world")
        self.assertEqual(session.metadata["last_turn_status"], "SUCCESS")
        self.assertEqual([event.payload.get("text") for event in events if "text" in event.payload], ["hello "])
        self.assertEqual(events[-1].event_type, AgentEventType.AGENT_TURN_COMPLETED)
        self.assertFalse(any(event.event_type == AgentEventType.PERMISSION_REQUIRED for event in events))


if __name__ == "__main__":
    unittest.main()
