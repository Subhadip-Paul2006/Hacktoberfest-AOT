"""Tests for CodexAdapter covering descriptor, capabilities, credential protection, and lifecycle."""
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
    CodexAdapter,
)


class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Test suite for CodexAdapter."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()
        self.project_path = self.root_path / "codex_test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.python_exe = sys.executable

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 1. Descriptor and capability compliance
    def test_descriptor_and_capabilities(self) -> None:
        adapter = CodexAdapter(executable="codex_non_existent")
        desc = adapter.descriptor()
        self.assertEqual(desc.agent_id, "codex")
        self.assertEqual(desc.provider, "OpenAI")
        self.assertEqual(desc.display_name, "Codex")

        caps = adapter.capabilities()
        self.assertIn(AgentCapability.START, caps)
        self.assertIn(AgentCapability.STOP, caps)
        self.assertNotIn(AgentCapability.SEND_PROMPT, caps)
        self.assertIn(AgentCapability.STREAM_OUTPUT, caps)
        self.assertIn(AgentCapability.GET_STATUS, caps)
        self.assertIn(AgentCapability.API, caps)
        self.assertIn(AgentCapability.CLI, caps)

    # 2. Availability detection
    def test_availability_detection(self) -> None:
        # Neither executable nor API key
        with patch.dict(os.environ, {}, clear=True):
            adapter_unavail = CodexAdapter(executable="codex_missing_exe_999")
            self.assertFalse(adapter_unavail.is_available())
            self.assertEqual(adapter_unavail.descriptor().availability, AgentAvailability.UNAVAILABLE)

        # API key configured
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-mock-key-12345"}, clear=True):
            adapter_api = CodexAdapter(executable="codex_missing_exe_999")
            self.assertTrue(adapter_api.is_available())
            self.assertEqual(adapter_api.descriptor().availability, AgentAvailability.AVAILABLE)

        # Binary installed
        adapter_inst = CodexAdapter(executable=self.python_exe)
        self.assertTrue(adapter_inst.is_available())
        self.assertEqual(adapter_inst.descriptor().availability, AgentAvailability.INSTALLED)

    # 3. Starting unavailable agent raises AgentUnavailableError
    async def test_start_unavailable_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            adapter = CodexAdapter(executable="codex_missing_exe_999")
            session = AgentSession(
                agent_id="codex",
                project_id="codex_test_project",
                project_root=str(self.project_path),
            )
            with self.assertRaises(AgentUnavailableError) as ctx:
                await adapter.start(session)
            self.assertEqual(ctx.exception.code, "AGENT_UNAVAILABLE")

    # 4. Successful execution and prompt delivery
    async def test_codex_execution_and_prompt_delivery(self) -> None:
        adapter = CodexAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="codex",
            project_id="codex_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        script = (
            "import sys\n"
            "print('Codex Agent: Processing instructions...')\n"
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
        output_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
        self.assertTrue(any("Codex Agent: Processing instructions..." in str(e.payload.get("text")) for e in output_events))

    # 5. Strict credential protection
    async def test_strict_credential_protection(self) -> None:
        adapter = CodexAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="codex",
            project_id="codex_test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Script echoes its API key from environment
        script = (
            "import os, sys\n"
            "key = os.environ.get('OPENAI_API_KEY', '')\n"
            "print(f'Using OPENAI_API_KEY={key}')\n"
            "sys.stdout.flush()\n"
        )
        test_secret_key = "sk-proj-super-secret-key-1234567890"
        with patch.dict(os.environ, {"OPENAI_API_KEY": test_secret_key}):
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
        # Ensure secret key was completely redacted and not present in events
        all_texts = "\n".join(str(e.payload) for e in events)
        self.assertNotIn(test_secret_key, all_texts)
        self.assertIn("[REDACTED", all_texts)

    # 6. Stop and lifecycle
    async def test_codex_stop_lifecycle(self) -> None:
        adapter = CodexAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="codex",
            project_id="codex_test_project",
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


if __name__ == "__main__":
    unittest.main()
