"""Unit and integration tests for Phase 3.1 Agent Foundation and Base Model."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException
from sutra.connectivity import ConnectivityGuard
from sutra.control_plane import ControlPlane
from sutra.adapters import AdapterInfo, AgentRegistry as CompatAgentRegistry
from sutra.agents import (
    AgentAdapter,
    AgentAvailability,
    AgentCapability,
    AgentDescriptor,
    AgentError,
    AgentEvent,
    AgentEventType,
    AgentManager,
    AgentNotFoundError,
    AgentProcessFailedError,
    AgentRegistry,
    AgentSession,
    AgentSessionStatus,
    AgentStartFailedError,
    AgentStopFailedError,
    AgentTimeoutError,
    AgentUnavailableError,
    CapabilityNotSupportedError,
    DuplicateAgentError,
    EventSeverity,
    InvalidProjectScopeError,
    PlaceholderAdapter,
    SafeProcessConfig,
    SessionNotFoundError,
    validate_project_scope,
)
from sutra.runtime import Runtime


class MockEchoAdapter(AgentAdapter):
    """Test adapter implementing start, stop, and send_prompt capabilities."""

    def __init__(
        self,
        agent_id: str = "mock_echo",
        display_name: str = "Mock Echo Agent",
        capabilities: set[AgentCapability] | None = None,
        available: bool = True,
    ) -> None:
        self._caps = capabilities if capabilities is not None else {
            AgentCapability.START,
            AgentCapability.STOP,
            AgentCapability.SEND_PROMPT,
            AgentCapability.GET_STATUS,
        }
        self._desc = AgentDescriptor(
            agent_id=agent_id,
            provider="TestProvider",
            display_name=display_name,
            version="1.0.0",
            executable="mock_exe",
            availability=AgentAvailability.AVAILABLE if available else AgentAvailability.UNAVAILABLE,
            capabilities=tuple(sorted(c.value for c in self._caps)),
        )
        self.started_sessions: list[str] = []
        self.stopped_sessions: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def descriptor(self) -> AgentDescriptor:
        return self._desc

    async def start(self, session: AgentSession, **kwargs: object) -> AgentSession:
        self.require_capability(AgentCapability.START)
        self.started_sessions.append(session.session_id)
        session.status = AgentSessionStatus.RUNNING
        return session

    async def stop(self, session_id: str) -> AgentSession:
        self.require_capability(AgentCapability.STOP)
        self.stopped_sessions.append(session_id)
        return AgentSession(session_id=session_id, status=AgentSessionStatus.STOPPED)

    async def send_prompt(self, session_id: str, prompt: str) -> None:
        self.require_capability(AgentCapability.SEND_PROMPT)
        self.prompts.append((session_id, prompt))

    async def get_status(self, session_id: str) -> AgentSessionStatus:
        self.require_capability(AgentCapability.GET_STATUS)
        if session_id in self.stopped_sessions:
            return AgentSessionStatus.STOPPED
        if session_id in self.started_sessions:
            return AgentSessionStatus.RUNNING
        return AgentSessionStatus.IDLE


class Phase31AgentFoundationTests(unittest.TestCase):
    """Test suite covering all Phase 3.1 requirements."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.test_dir.name).resolve()
        self.project_path = self.root_path / "sample_project"
        self.project_path.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        try:
            self.test_dir.cleanup()
        except Exception:
            pass

    # 1. AgentDescriptor validation
    def test_agent_descriptor_validation_and_backward_compatibility(self) -> None:
        desc = AgentDescriptor(
            agent_id="test_agent",
            provider="ACME",
            display_name="Test Agent",
            version="0.1.0",
            executable="acme_cli",
            availability=AgentAvailability.AVAILABLE,
            capabilities=(AgentCapability.START.value, AgentCapability.STOP.value),
            status="Connected",
            implementation="ACME (acme_cli)",
        )
        self.assertEqual(desc.agent_id, "test_agent")
        self.assertEqual(desc.provider, "ACME")
        self.assertEqual(desc.name, "Test Agent")
        self.assertEqual(desc.status, "Connected")
        self.assertIn("ACME", desc.implementation)

        # Verify default compatibility fields
        default_desc = AgentDescriptor(
            agent_id="default_agent",
            provider="ACME",
            display_name="Default Agent",
        )
        self.assertEqual(default_desc.status, "Not Connected")
        self.assertEqual(default_desc.implementation, "Not Implemented")

        data = desc.to_dict()
        self.assertEqual(data["agent_id"], "test_agent")
        self.assertEqual(data["availability"], "AVAILABLE")
        self.assertIn("START", data["capabilities"])
        # Verify JSON serializability of __dict__
        serialized = json.dumps(desc.__dict__)
        self.assertIn("test_agent", serialized)

    # 2. AgentCapabilities
    def test_agent_capabilities(self) -> None:
        desc = AgentDescriptor(
            agent_id="caps_test",
            provider="ACME",
            display_name="Caps Agent",
            capabilities=(AgentCapability.START.value, AgentCapability.SEND_PROMPT.value),
        )
        self.assertTrue(desc.has_capability(AgentCapability.START))
        self.assertTrue(desc.has_capability("START"))
        self.assertTrue(desc.has_capability(AgentCapability.SEND_PROMPT))
        self.assertFalse(desc.has_capability(AgentCapability.GIT_AWARE))
        self.assertFalse(desc.has_capability("NON_EXISTENT_CAPABILITY"))

    # 3. AgentSession lifecycle
    def test_agent_session_lifecycle(self) -> None:
        session = AgentSession(
            agent_id="test_agent",
            project_id="sample_project",
            project_root=str(self.project_path),
            status=AgentSessionStatus.IDLE,
        )
        self.assertEqual(session.status, AgentSessionStatus.IDLE)
        self.assertEqual(session.generation, 1)

        data = session.to_dict()
        self.assertEqual(data["agent_id"], "test_agent")
        self.assertEqual(data["status"], "IDLE")
        self.assertEqual(data["project_root"], str(self.project_path))

    # 4. AgentEvent creation/validation
    def test_agent_event_creation_and_validation(self) -> None:
        event = AgentEvent(
            event_type=AgentEventType.AGENT_STARTED,
            agent_id="test_agent",
            agent_session_id="session-123",
            project_id="sample_project",
            severity=EventSeverity.INFO,
            payload={"phase": "init"},
            trace_id="trace-abc",
        )
        self.assertEqual(event.event_type, AgentEventType.AGENT_STARTED)
        self.assertEqual(event.severity, EventSeverity.INFO)
        data = event.to_dict()
        self.assertEqual(data["event_type"], "AGENT_STARTED")
        self.assertEqual(data["trace_id"], "trace-abc")
        self.assertEqual(data["payload"], {"phase": "init"})

    # 5. Adapter interface behavior
    def test_adapter_interface_behavior(self) -> None:
        adapter = MockEchoAdapter()
        self.assertEqual(adapter.descriptor().agent_id, "mock_echo")
        self.assertTrue(adapter.is_available())
        caps = adapter.capabilities()
        self.assertIn(AgentCapability.START, caps)
        self.assertIn(AgentCapability.STOP, caps)

    # 6. Unsupported capability handling
    def test_unsupported_capability_handling(self) -> None:
        # Adapter with only START capability
        adapter = MockEchoAdapter(capabilities={AgentCapability.START})
        with self.assertRaises(CapabilityNotSupportedError) as ctx:
            asyncio.run(adapter.send_prompt("sess-1", "hello"))
        self.assertEqual(ctx.exception.code, "CAPABILITY_NOT_SUPPORTED")
        self.assertIn("SEND_PROMPT", str(ctx.exception))

    # 7. AgentRegistry registration
    def test_agent_registry_registration_and_listing(self) -> None:
        registry = AgentRegistry(populate_defaults=False)
        self.assertEqual(len(registry.list_adapters()), 0)

        adapter = MockEchoAdapter(agent_id="echo_1", display_name="Echo 1")
        registry.register(adapter)
        self.assertEqual(len(registry.list_adapters()), 1)
        self.assertEqual(registry.list_descriptors()[0].agent_id, "echo_1")

        # Test backward-compatible list()
        items = registry.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "Echo 1")

    # 8. AgentRegistry lookup
    def test_agent_registry_lookup(self) -> None:
        registry = AgentRegistry(populate_defaults=False)
        adapter = MockEchoAdapter(agent_id="my_agent", display_name="My Cool Agent")
        registry.register(adapter)

        # Lookup by exact ID
        self.assertIsNotNone(registry.find("my_agent"))
        # Lookup case-insensitive
        self.assertIsNotNone(registry.find("MY_AGENT"))
        # Lookup by display name
        self.assertIsNotNone(registry.find("My Cool Agent"))
        self.assertIsNotNone(registry.find("my cool agent"))
        # get() succeeds
        self.assertEqual(registry.get("my_agent").descriptor().agent_id, "my_agent")

        # Missing lookup raises AgentNotFoundError
        with self.assertRaises(AgentNotFoundError) as ctx:
            registry.get("non_existent")
        self.assertEqual(ctx.exception.code, "AGENT_NOT_FOUND")

    # 9. Duplicate adapter registration
    def test_duplicate_adapter_registration(self) -> None:
        registry = AgentRegistry(populate_defaults=False)
        adapter1 = MockEchoAdapter(agent_id="dupe_agent")
        adapter2 = MockEchoAdapter(agent_id="dupe_agent")
        registry.register(adapter1)

        with self.assertRaises(DuplicateAgentError) as ctx:
            registry.register(adapter2, overwrite=False)
        self.assertEqual(ctx.exception.code, "DUPLICATE_AGENT")

        # Can overwrite if explicit
        registry.register(adapter2, overwrite=True)
        self.assertEqual(len(registry.list_adapters()), 1)

    # 10. Agent availability handling
    def test_agent_availability_handling(self) -> None:
        registry = AgentRegistry(populate_defaults=True)
        descriptors = registry.list_descriptors()
        self.assertGreaterEqual(len(descriptors), 6)
        ids = {d.agent_id for d in descriptors}
        self.assertTrue({"antigravity", "cursor", "codex", "claude_code", "jules", "generic_cli"}.issubset(ids))

        # Safe discover() does not crash or execute arbitrary commands
        discovered = registry.discover()
        self.assertEqual(len(discovered), len(descriptors))

    # 11. Project scope validation
    def test_project_scope_validation(self) -> None:
        authorized_roots = [str(self.root_path)]

        # Valid project within authorized root
        valid_path = validate_project_scope(self.project_path, authorized_roots)
        self.assertEqual(valid_path, self.project_path)

        # Non-existent path raises InvalidProjectScopeError
        with self.assertRaises(InvalidProjectScopeError):
            validate_project_scope(self.root_path / "does_not_exist", authorized_roots)

        # File instead of directory raises InvalidProjectScopeError
        file_path = self.project_path / "file.txt"
        file_path.write_text("sample")
        with self.assertRaises(InvalidProjectScopeError):
            validate_project_scope(file_path, authorized_roots)

        # Path outside authorized root raises InvalidProjectScopeError
        outside_dir = tempfile.mkdtemp()
        try:
            with self.assertRaises(InvalidProjectScopeError):
                validate_project_scope(outside_dir, authorized_roots)
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

        # Bare drive root rejected
        drive_root = Path(self.root_path.anchor)
        with self.assertRaises(InvalidProjectScopeError):
            validate_project_scope(drive_root, [str(drive_root)])

    # 12. Session isolation
    def test_session_isolation(self) -> None:
        registry = AgentRegistry(populate_defaults=False)
        adapter1 = MockEchoAdapter(agent_id="agent_1")
        adapter2 = MockEchoAdapter(agent_id="agent_2")
        registry.register(adapter1)
        registry.register(adapter2)

        manager = AgentManager(registry)
        sess1 = manager.create_session("agent_1", self.project_path, [str(self.root_path)])
        sess2 = manager.create_session("agent_2", self.project_path, [str(self.root_path)])

        self.assertNotEqual(sess1.session_id, sess2.session_id)
        self.assertEqual(sess1.agent_id, "agent_1")
        self.assertEqual(sess2.agent_id, "agent_2")

        # Filtering by agent_id works
        self.assertEqual(len(manager.list_sessions(agent_id="agent_1")), 1)
        self.assertEqual(len(manager.list_sessions(agent_id="agent_2")), 1)
        self.assertEqual(len(manager.list_sessions()), 2)

    # 13. Structured error handling
    def test_structured_error_handling(self) -> None:
        errs = [
            AgentNotFoundError("agent_x"),
            AgentUnavailableError("agent_y", "binary missing"),
            CapabilityNotSupportedError("agent_z", "STREAM_OUTPUT"),
            SessionNotFoundError("sess_123"),
            InvalidProjectScopeError("out of root"),
            AgentStartFailedError("sess_1", "init crashed"),
            AgentStopFailedError("sess_1", "sigkill timeout"),
            AgentProcessFailedError("agent_p", 137, "OOM"),
            AgentTimeoutError("run_task", 10.0),
            DuplicateAgentError("agent_dupe"),
        ]
        for err in errs:
            d = err.to_dict()
            self.assertIn("error", d)
            self.assertIn("message", d)
            self.assertIn("details", d)
            self.assertEqual(d["error"], err.code)

    # 14. No arbitrary shell execution (SafeProcessConfig validation)
    def test_no_arbitrary_shell_execution(self) -> None:
        # Valid process config
        cfg = SafeProcessConfig(
            args=["git", "status"],
            cwd=self.project_path,
            env={"CUSTOM_KEY": "val"},
            timeout_seconds=15.0,
        )
        self.assertEqual(cfg.args, ["git", "status"])

        # String passed instead of list
        with self.assertRaises(ValueError):
            SafeProcessConfig(args="git status; rm -rf /", cwd=self.project_path)  # type: ignore[arg-type]

        # Shell operators inside argument list rejected
        with self.assertRaises(ValueError):
            SafeProcessConfig(args=["git", "status", ";", "rm", "-rf", "/"], cwd=self.project_path)

        with self.assertRaises(ValueError):
            SafeProcessConfig(args=["cat", "file", "|", "grep", "token"], cwd=self.project_path)

        # Environment sanitization strips secret tokens
        sanitized = cfg.build_sanitized_env(base_env={
            "PATH": "/usr/bin",
            "TELEGRAM_BOT_TOKEN": "secret-token-1234",
            "AWS_SECRET_ACCESS_KEY": "my-secret-key",
            "SAFE_VAR": "hello",
        })
        self.assertIn("SAFE_VAR", sanitized)
        self.assertIn("CUSTOM_KEY", sanitized)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", sanitized)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", sanitized)

    # 15. Event correlation/IDs and subscriptions
    def test_event_correlation_and_subscriptions(self) -> None:
        async def _run() -> None:
            registry = AgentRegistry(populate_defaults=False)
            adapter = MockEchoAdapter(agent_id="echo")
            registry.register(adapter)
            manager = AgentManager(registry)

            session = manager.create_session("echo", self.project_path, [str(self.root_path)])
            queue = manager.subscribe(session.session_id)

            await manager.start_session(session.session_id)
            await manager.send_prompt(session.session_id, "hello agent", trace_id="trace-999")
            await manager.stop_session(session.session_id)

            events: list[AgentEvent] = []
            while not queue.empty():
                events.append(queue.get_nowait())

            # Verify events arrived with monotonic sequence numbers
            self.assertGreaterEqual(len(events), 3)
            sequences = [e.sequence for e in events]
            self.assertEqual(sequences, sorted(sequences))
            self.assertEqual(sequences, list(range(1, len(events) + 1)))

            # Verify prompt event had trace_id preserved
            prompt_events = [e for e in events if e.payload.get("action") == "send_prompt"]
            self.assertEqual(len(prompt_events), 1)
            self.assertEqual(prompt_events[0].trace_id, "trace-999")

            manager.unsubscribe(session.session_id, queue)
            manager.cleanup_session(session.session_id)
            self.assertEqual(len(manager.list_sessions()), 0)

        asyncio.run(_run())

    # 16. Existing Phase 2 / Phase 2.5 behavior remains intact
    def test_backward_compatibility_with_cli_and_telegram(self) -> None:
        # CLI agents command behavior
        compat_registry = CompatAgentRegistry()
        items = compat_registry.list()
        self.assertGreaterEqual(len(items), 6)
        # Each item.__dict__ serializes to JSON cleanly without TypeError
        serialized = json.dumps([item.__dict__ for item in items], indent=2)
        parsed = json.loads(serialized)
        self.assertIsInstance(parsed, list)
        self.assertIn("name", parsed[0])
        self.assertIn("status", parsed[0])
        self.assertIn("implementation", parsed[0])

        # AdapterInfo is still available
        info = AdapterInfo("Antigravity")
        self.assertEqual(info.name, "Antigravity")

    # 17. Runtime remote command routing for agent.list and agent.status
    def test_runtime_agent_remote_command_routing(self) -> None:
        async def _run() -> None:
            runtime = Runtime(self.test_dir.name)
            try:
                # Register a mock adapter on the runtime's agent_registry
                adapter = MockEchoAdapter(agent_id="echo_test")
                runtime.agent_registry.register(adapter, overwrite=True)

                # agent.list command
                list_res = await runtime.handle_remote_command("agent.list", {})
                self.assertIn("items", list_res)
                self.assertIn("total_count", list_res)
                agent_ids = [item["agent_id"] for item in list_res["items"]]
                self.assertIn("echo_test", agent_ids)

                # Non-empty payload on agent.list raises ValueError
                with self.assertRaises(ValueError):
                    await runtime.handle_remote_command("agent.list", {"extra": "invalid"})

                # agent.status with agent_id
                status_res = await runtime.handle_remote_command("agent.status", {"agent_id": "echo_test"})
                self.assertIn("agent", status_res)
                self.assertEqual(status_res["agent"]["agent_id"], "echo_test")

                # agent.status with empty payload
                summary_res = await runtime.handle_remote_command("agent.status", {})
                self.assertIn("agents", summary_res)
                self.assertIn("active_sessions", summary_res)

                # Set up workspace and project index for agent.start
                (self.project_path / "pyproject.toml").write_text("[project]\nname='sample'")
                runtime.store.add_workspace(str(self.root_path))
                runtime.project_index.set_roots([str(self.root_path)])
                runtime.project_index.schedule_refresh()
                if runtime.project_index._refresh_task:
                    await runtime.project_index._refresh_task

                # agent.start command
                start_res = await runtime.handle_remote_command("agent.start", {
                    "agent_id": "echo_test",
                    "project_root": str(self.project_path),
                    "prompt": "Test initial prompt",
                })
                self.assertIn("session", start_res)
                session_id = start_res["session"]["session_id"]
                self.assertEqual(start_res["session"]["status"], "RUNNING")

                # agent.prompt command
                prompt_res = await runtime.handle_remote_command("agent.prompt", {
                    "session_id": session_id,
                    "prompt": "Follow up instructions",
                })
                self.assertEqual(prompt_res["status"], "delivered")

                # agent.stop command
                stop_res = await runtime.handle_remote_command("agent.stop", {"session_id": session_id})
                self.assertIn("session", stop_res)
                self.assertEqual(stop_res["session"]["status"], "STOPPED")

                # The paired runtime exposes the session's bounded AgentManager stream.
                event_res = await runtime.handle_remote_command("agent.events", {
                    "session_id": session_id, "limit": 50,
                })
                self.assertTrue(event_res["finished"])
                self.assertEqual(event_res["status"], "STOPPED")
                self.assertTrue(any(event["event_type"] == "AGENT_STOPPED" for event in event_res["events"]))
                with self.assertRaises(ValueError):
                    await runtime.handle_remote_command("agent.events", {"session_id": session_id, "limit": 0})
            finally:
                runtime.store.close()

        asyncio.run(_run())


class Phase31ControlPlaneAgentDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests verifying control plane dispatching, caching, and single-flight for agent commands."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "cp_test.sqlite3"
        self.cp = ControlPlane(database_path=str(db_path), operator_token="test_op_token")

    def tearDown(self) -> None:
        self.cp.store.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _setup_mock_device(self, device_id: str = "AB-PC-AABB11") -> tuple[str, str, str, int, Any]:
        token = "test_token_256bit_value_for_unit_tests"
        session_id = "11111111-2222-3333-4444-555555555555"
        generation = 1

        class MockWebSocket:
            def __init__(self, cp: ControlPlane) -> None:
                self.cp = cp
                self.sent: list[str] = []

            async def send_text(self, text: str) -> None:
                await self.send_json(json.loads(text))

            async def send_json(self, data: Any) -> None:
                text = json.dumps(data)
                self.sent.append(text)
                frame = data
                req_id = frame.get("request_id")
                cmd = frame.get("command")
                if cmd == "agent.list":
                    payload = {
                        "items": [
                            {"agent_id": "antigravity", "display_name": "Antigravity", "status": "Not Connected"}
                        ],
                        "total_count": 1
                    }
                elif cmd == "agent.status":
                    payload = {"agent": {"agent_id": "antigravity", "status": "Not Connected"}}
                elif cmd == "agent.start":
                    payload = {"session": {"session_id": "sess-123", "status": "RUNNING"}}
                elif cmd == "agent.stop":
                    payload = {"session": {"session_id": "sess-123", "status": "STOPPED"}}
                elif cmd == "agent.prompt":
                    payload = {"status": "delivered", "session_id": "sess-123"}
                else:
                    payload = {}
                asyncio.get_running_loop().call_soon(
                    self._resolve_pending, req_id, frame.get("device_id"), frame.get("session_id"),
                    frame.get("generation"), frame.get("trace_id"), payload
                )

            def _resolve_pending(self, req_id: str, dev_id: str, sess_id: str, gen: int, trace_id: str, payload: dict) -> None:
                if req_id in self.cp.pending_responses:
                    _, fut = self.cp.pending_responses[req_id]
                    if not fut.done():
                        fut.set_result({
                            "type": "response", "request_id": req_id, "device_id": dev_id,
                            "session_id": sess_id, "generation": gen, "trace_id": trace_id,
                            "status": "success", "payload": payload, "runtime_processing_ms": 1.2
                        })

        ws = MockWebSocket(self.cp)
        self.cp.sessions[device_id] = ws
        self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
        self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
        self.cp.connectivity[device_id] = ConnectivityGuard(self.cp.policy)
        self.cp.connectivity[device_id].state = "HEALTHY"
        return device_id, token, session_id, generation, ws

    async def test_control_plane_agent_list_caching_and_coalescing(self) -> None:
        device_id, _, _, _, ws = self._setup_mock_device()

        # First call is a miss, dispatches over WSS
        r1 = await self.cp.dispatch_command(device_id, "agent.list", {})
        self.assertEqual(len(ws.sent), 1)
        self.assertNotIn("cached", r1)
        self.assertEqual(r1["payload"]["items"][0]["agent_id"], "antigravity")

        # Second call is a cache hit
        r2 = await self.cp.dispatch_command(device_id, "agent.list", {})
        self.assertEqual(len(ws.sent), 1)  # No new socket frame sent
        self.assertTrue(r2.get("cached"))
        self.assertEqual(r1["payload"], r2["payload"])

        # Multiple concurrent requests share 1 flight (single-flight deduplication)
        self.cp.cache.clear()
        ws.sent.clear()
        results = await asyncio.gather(*(self.cp.dispatch_command(device_id, "agent.list", {}) for _ in range(5)))
        self.assertEqual(len(results), 5)
        self.assertEqual(len(ws.sent), 1)  # exactly 1 frame sent over WSS for 5 concurrent callers!
        for res in results:
            self.assertEqual(res["payload"]["items"][0]["agent_id"], "antigravity")

    async def test_control_plane_agent_command_validation(self) -> None:
        device_id, _, _, _, _ = self._setup_mock_device()

        # agent.list with non-empty payload rejected with 400
        with self.assertRaises(HTTPException) as ctx:
            await self.cp.dispatch_command(device_id, "agent.list", {"extra": "invalid"})
        self.assertEqual(ctx.exception.status_code, 400)

        # agent.status with valid payload succeeds
        status_res = await self.cp.dispatch_command(device_id, "agent.status", {"agent_id": "antigravity"})
        self.assertEqual(status_res["payload"]["agent"]["agent_id"], "antigravity")

        # agent.status with invalid payload key rejected with 400
        with self.assertRaises(HTTPException) as ctx:
            await self.cp.dispatch_command(device_id, "agent.status", {"unsupported_key": "val"})
        self.assertEqual(ctx.exception.status_code, 400)

        # agent.start with valid payload succeeds
        start_res = await self.cp.dispatch_command(device_id, "agent.start", {
            "agent_id": "antigravity",
            "project_root": "D:/projects/my_project",
            "prompt": "build feature",
        })
        self.assertEqual(start_res["payload"]["session"]["status"], "RUNNING")

        # agent.start missing project_root rejected with 400
        with self.assertRaises(HTTPException) as ctx:
            await self.cp.dispatch_command(device_id, "agent.start", {"agent_id": "antigravity"})
        self.assertEqual(ctx.exception.status_code, 400)

        # agent.prompt with valid payload succeeds
        prompt_res = await self.cp.dispatch_command(device_id, "agent.prompt", {
            "session_id": "sess-123",
            "prompt": "run tests",
        })
        self.assertEqual(prompt_res["payload"]["status"], "delivered")

        # agent.prompt missing prompt rejected with 400
        with self.assertRaises(HTTPException) as ctx:
            await self.cp.dispatch_command(device_id, "agent.prompt", {"session_id": "sess-123"})
        self.assertEqual(ctx.exception.status_code, 400)

        # agent.stop with valid payload succeeds
        stop_res = await self.cp.dispatch_command(device_id, "agent.stop", {"session_id": "sess-123"})
        self.assertEqual(stop_res["payload"]["session"]["status"], "STOPPED")

        # agent.stop with invalid payload rejected with 400
        with self.assertRaises(HTTPException) as ctx:
            await self.cp.dispatch_command(device_id, "agent.stop", {"wrong_key": "sess-123"})
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

