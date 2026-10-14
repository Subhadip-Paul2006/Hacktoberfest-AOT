"""Tests for GenericCLIAdapter covering all lifecycle, security, streaming, and concurrency requirements."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import time
import unittest

from sutra.agents import (
    AgentAvailability,
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentManager,
    AgentProcessFailedError,
    AgentRegistry,
    AgentSession,
    AgentSessionStatus,
    AgentStartFailedError,
    AgentUnavailableError,
    CapabilityNotSupportedError,
    GenericCLIAdapter,
    InvalidProjectScopeError,
    SafeProcessConfig,
    validate_project_scope,
)


class GenericCLIAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Thorough tests for GenericCLIAdapter."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()
        self.project_path = self.root_path / "test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.python_exe = sys.executable

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 1. Descriptor correctness and capability reporting
    def test_descriptor_and_capabilities(self) -> None:
        adapter = GenericCLIAdapter()
        desc = adapter.descriptor()
        self.assertEqual(desc.agent_id, "generic_cli")
        self.assertEqual(desc.provider, "Generic")
        self.assertTrue(adapter.is_available())
        caps = adapter.capabilities()
        self.assertIn(AgentCapability.START, caps)
        self.assertIn(AgentCapability.STOP, caps)
        self.assertIn(AgentCapability.SEND_PROMPT, caps)
        self.assertIn(AgentCapability.STREAM_OUTPUT, caps)
        self.assertIn(AgentCapability.GET_STATUS, caps)
        self.assertIn(AgentCapability.CLI, caps)
        self.assertNotIn(AgentCapability.ACP, caps)

    # 2. Availability detection
    def test_availability_detection(self) -> None:
        adapter_with_valid = GenericCLIAdapter(executable=self.python_exe)
        self.assertTrue(adapter_with_valid.is_available())
        desc = adapter_with_valid.descriptor()
        self.assertEqual(desc.availability, AgentAvailability.INSTALLED)

        adapter_with_missing = GenericCLIAdapter(executable="non_existent_binary_xyz_123")
        self.assertFalse(adapter_with_missing.is_available())
        desc_missing = adapter_with_missing.descriptor()
        self.assertEqual(desc_missing.availability, AgentAvailability.UNAVAILABLE)

    # 3. Project scope validation: valid vs invalid
    def test_project_scope_enforcement(self) -> None:
        authorized = [str(self.root_path)]
        valid = validate_project_scope(self.project_path, authorized)
        self.assertEqual(valid, self.project_path)

        outside = tempfile.mkdtemp()
        try:
            with self.assertRaises(InvalidProjectScopeError):
                validate_project_scope(outside, authorized)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    # 4. Malicious argument rejection
    async def test_malicious_argument_rejection(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        # Semicolon injection
        with self.assertRaises(AgentStartFailedError) as ctx:
            await adapter.start(session, args=["-c", "print(1)", ";", "rm", "-rf", "/"])
        self.assertIn("forbidden shell chaining", str(ctx.exception).lower())

        # Pipe injection
        with self.assertRaises(AgentStartFailedError) as ctx:
            await adapter.start(session, args=["-c", "print(1)", "|", "cat"])
        self.assertIn("forbidden shell chaining", str(ctx.exception).lower())

    # 5. Process startup and successful execution with stdout and exit code capture
    async def test_process_startup_stdout_and_completion(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        started = await adapter.start(
            session,
            args=["-u", "-c", "print('Antigravity test output 123')"],
            on_event=events.append,
        )
        self.assertEqual(started.status, AgentSessionStatus.RUNNING)

        # Wait for watchdog to complete the process
        for _ in range(50):
            status = await adapter.get_status(session.session_id)
            if status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        self.assertEqual(status, AgentSessionStatus.COMPLETED)
        output_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
        self.assertTrue(any("Antigravity test output 123" in str(e.payload.get("text")) for e in output_events))
        for _ in range(20):
            completed_events = [e for e in events if e.event_type == AgentEventType.AGENT_COMPLETED]
            if completed_events:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(completed_events), 1)
        self.assertEqual(completed_events[0].payload.get("exit_code"), 0)

    # 6. Stderr capture and process failure with non-zero exit code
    async def test_process_failure_and_stderr(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        await adapter.start(
            session,
            args=["-u", "-c", "import sys; sys.stderr.write('Fatal custom error\\n'); sys.stderr.flush(); sys.exit(7)"],
            on_event=events.append,
        )

        for _ in range(50):
            status = await adapter.get_status(session.session_id)
            if status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        self.assertEqual(status, AgentSessionStatus.FAILED)
        stderr_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT and e.payload.get("stream") == "stderr"]
        self.assertTrue(any("Fatal custom error" in str(e.payload.get("text")) for e in stderr_events))
        for _ in range(20):
            failed_events = [e for e in events if e.event_type == AgentEventType.AGENT_FAILED]
            if failed_events:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0].payload.get("exit_code"), 7)

    # 7. Prompt delivery via stdin
    async def test_prompt_delivery_via_stdin(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Script reads one line from stdin and prints it with prefix
        script = "import sys; line = sys.stdin.readline(); print('ECHOED:' + line.strip()); sys.stdout.flush()"
        await adapter.start(
            session,
            args=["-u", "-c", script],
            on_event=events.append,
        )

        # Deliver prompt
        await adapter.send_prompt(session.session_id, "hello from SŪTRA test")

        for _ in range(50):
            status = await adapter.get_status(session.session_id)
            if status == AgentSessionStatus.COMPLETED:
                break
            await asyncio.sleep(0.1)

        output_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
        self.assertTrue(any("ECHOED:hello from SŪTRA test" in str(e.payload.get("text")) for e in output_events))

    # 8. Clean stop / termination
    async def test_clean_stop_and_cleanup(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Long running sleep
        await adapter.start(
            session,
            args=["-c", "import time; time.sleep(60)"],
            on_event=events.append,
        )
        self.assertEqual(await adapter.get_status(session.session_id), AgentSessionStatus.RUNNING)

        # Stop session
        stopped = await adapter.stop(session.session_id)
        self.assertEqual(stopped.status, AgentSessionStatus.STOPPED)
        self.assertEqual(await adapter.get_status(session.session_id), AgentSessionStatus.STOPPED)

        stopped_events = [e for e in events if e.event_type == AgentEventType.AGENT_STOPPED]
        self.assertGreaterEqual(len(stopped_events), 1)

    # 9. Timeout handling
    async def test_timeout_handling(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe, timeout_seconds=0.5)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Sleep for 10 seconds, but timeout is 0.5s
        await adapter.start(
            session,
            args=["-c", "import time; time.sleep(10)"],
            timeout_seconds=0.5,
            on_event=events.append,
        )

        # Wait for timeout watchdog
        for _ in range(30):
            status = await adapter.get_status(session.session_id)
            if status == AgentSessionStatus.FAILED:
                break
            await asyncio.sleep(0.1)

        self.assertEqual(status, AgentSessionStatus.FAILED)
        failed_events = [e for e in events if e.event_type == AgentEventType.AGENT_FAILED]
        self.assertEqual(len(failed_events), 1)
        self.assertIn("timed out", str(failed_events[0].payload.get("error")).lower())

    # 10. Secret redaction in output streams
    async def test_secret_redaction_in_output(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        script = (
            "import sys\n"
            "print('API_KEY=super_secret_token_12345')\n"
            "print('token sk-proj-1234567890abcdefghij is ready')\n"
            "print('-----BEGIN RSA PRIVATE KEY-----\\nABCDEF123456\\n-----END RSA PRIVATE KEY-----')\n"
        )
        await adapter.start(session, args=["-u", "-c", script], on_event=events.append)

        for _ in range(50):
            status = await adapter.get_status(session.session_id)
            if status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        for _ in range(20):
            output_texts = [str(e.payload.get("text", "")) for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
            if output_texts:
                break
            await asyncio.sleep(0.05)
        all_output = "\n".join(output_texts)
        self.assertNotIn("super_secret_token_12345", all_output)
        self.assertNotIn("sk-proj-1234567890abcdefghij", all_output)
        self.assertNotIn("ABCDEF123456", all_output)
        self.assertIn("[REDACTED", all_output)

    # 11. Oversized output handling / truncation
    async def test_oversized_output_handling(self) -> None:
        adapter = GenericCLIAdapter(executable=self.python_exe)
        session = AgentSession(
            agent_id="generic_cli",
            project_id="test_project",
            project_root=str(self.project_path),
        )
        events: list[AgentEvent] = []
        # Print a line with 10,000 characters
        script = "print('Z' * 10000)"
        await adapter.start(session, args=["-c", script], on_event=events.append)

        for _ in range(50):
            status = await adapter.get_status(session.session_id)
            if status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        output_events = [e for e in events if e.event_type == AgentEventType.AGENT_OUTPUT]
        self.assertGreaterEqual(len(output_events), 1)
        for oe in output_events:
            self.assertLessEqual(len(str(oe.payload.get("text", ""))), 4500)
            self.assertIn("[TRUNCATED]", str(oe.payload.get("text", "")))

    # 12. Multi-agent concurrency and session isolation on the same project
    async def test_concurrent_sessions_on_same_project(self) -> None:
        registry = AgentRegistry(populate_defaults=False)
        cli_adapter = GenericCLIAdapter(agent_id="generic_cli", executable=self.python_exe)
        registry.register(cli_adapter)

        manager = AgentManager(registry)
        sess1 = manager.create_session("generic_cli", self.project_path, [str(self.root_path)])
        sess2 = manager.create_session("generic_cli", self.project_path, [str(self.root_path)])

        self.assertNotEqual(sess1.session_id, sess2.session_id)

        q1 = manager.subscribe(sess1.session_id)
        q2 = manager.subscribe(sess2.session_id)

        # Start session 1: prints message 1
        await manager.start_session(
            sess1.session_id,
            args=["-u", "-c", "import time; print('MESSAGE FROM SESS 1'); time.sleep(1.0)"],
        )
        # Start session 2: prints message 2 concurrently
        await manager.start_session(
            sess2.session_id,
            args=["-u", "-c", "import time; print('MESSAGE FROM SESS 2'); time.sleep(0.3)"],
        )

        self.assertEqual(sess1.status, AgentSessionStatus.RUNNING)
        self.assertEqual(sess2.status, AgentSessionStatus.RUNNING)

        # Allow output to be received by stream reader before stopping sess1
        for _ in range(50):
            if any(e.event_type == AgentEventType.AGENT_OUTPUT for e in list(q1._queue)):
                break
            await asyncio.sleep(0.05)

        # Stop sess1 early; sess2 should keep running
        await manager.stop_session(sess1.session_id)
        self.assertEqual(sess1.status, AgentSessionStatus.STOPPED)

        # Wait for sess2 to complete naturally
        for _ in range(30):
            st = await manager.get_session_status(sess2.session_id)
            if st in (AgentSessionStatus.COMPLETED, AgentSessionStatus.FAILED):
                break
            await asyncio.sleep(0.1)

        self.assertEqual(st, AgentSessionStatus.COMPLETED)

        # Allow output for sess2 to be received
        for _ in range(50):
            if any(e.event_type == AgentEventType.AGENT_OUTPUT for e in list(q2._queue)):
                break
            await asyncio.sleep(0.05)

        # Verify event isolation
        events_1: list[AgentEvent] = []
        while not q1.empty():
            events_1.append(q1.get_nowait())

        events_2: list[AgentEvent] = []
        while not q2.empty():
            events_2.append(q2.get_nowait())

        sess1_texts = [str(e.payload.get("text", "")) for e in events_1 if e.event_type == AgentEventType.AGENT_OUTPUT]
        sess2_texts = [str(e.payload.get("text", "")) for e in events_2 if e.event_type == AgentEventType.AGENT_OUTPUT]

        self.assertTrue(any("MESSAGE FROM SESS 1" in t for t in sess1_texts))
        self.assertFalse(any("MESSAGE FROM SESS 2" in t for t in sess1_texts))

        self.assertTrue(any("MESSAGE FROM SESS 2" in t for t in sess2_texts))
        self.assertFalse(any("MESSAGE FROM SESS 1" in t for t in sess2_texts))

        # Check sequence numbers monotonic per session
        seqs_1 = [e.sequence for e in events_1]
        seqs_2 = [e.sequence for e in events_2]
        self.assertEqual(seqs_1, sorted(seqs_1))
        self.assertEqual(seqs_2, sorted(seqs_2))


if __name__ == "__main__":
    unittest.main()
