from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import tracemalloc
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sutra.agents import AgentSession, GenericCLIAdapter
from sutra.apps.telegram.callbacks import handle_callback
from sutra.connectors.terminal import TerminalCommandRequest, TerminalConnector
from sutra.task_executor import PerProjectTaskExecutor


class _PermissionEngine:
    def __init__(self, result: bool | Exception) -> None:
        self.result = result

    def apply_decision(self, request_id: str, decision: object) -> bool:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _TelegramCallbackBot:
    def __init__(self, engine: _PermissionEngine) -> None:
        self.control_plane = SimpleNamespace(permission_engine=engine)
        self.callback_targets = {
            "target": {
                "user_id": 5,
                "chat_id": 5,
                "action": "permission",
                "context": {"id": "permission-1", "device_id": "device-1", "decision": "allow_once"},
                "created_at": time.time(),
            }
        }
        self.pending_permissions = {
            "permission-1": {"user_id": 5, "device_id": "device-1"}
        }

    @staticmethod
    def _reply(text: str, *args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(text=text)

    @staticmethod
    def _nav_buttons(**kwargs: object) -> list[list[dict[str, str]]]:
        return []


class TelegramPermissionCallbackRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_permission_decision_is_not_reported_as_recorded(self) -> None:
        bot = _TelegramCallbackBot(_PermissionEngine(False))
        update = {"callback_query": {"id": "callback-1", "message": {}, "data": "target:target"}}

        reply = await handle_callback(bot, update, user_id=5, chat_id=5)

        self.assertNotIn("decision recorded", reply.text.lower())
        self.assertNotIn("permission-1", bot.pending_permissions)

    async def test_permission_decision_exception_keeps_request_retryable(self) -> None:
        bot = _TelegramCallbackBot(_PermissionEngine(RuntimeError("storage unavailable")))
        update = {"callback_query": {"id": "callback-2", "message": {}, "data": "target:target"}}

        reply = await handle_callback(bot, update, user_id=5, chat_id=5)

        self.assertNotIn("decision recorded", reply.text.lower())
        self.assertIn("permission-1", bot.pending_permissions)


@unittest.skipIf(os.name == "nt", "POSIX process-group behavior")
class GenericCLIProcessGroupRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_process_uses_its_own_group_for_forced_tree_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = GenericCLIAdapter(
                executable=sys.executable,
                graceful_stop_timeout=0.05,
            )
            session = AgentSession(
                agent_id="generic_cli",
                project_id="test-project",
                project_root=str(root),
            )
            await adapter.start(
                session,
                args=["-u", "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
            )
            process = adapter._active_sessions[session.session_id].process
            try:
                await asyncio.sleep(0.1)
                self.assertEqual(os.getpgid(process.pid), process.pid)
                stopped = await adapter.stop(session.session_id)
                self.assertEqual("STOPPED", stopped.status.value)
                self.assertIsNotNone(process.returncode)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


class TerminalOutputMemoryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_output_is_drained_without_retaining_the_full_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connector = TerminalConnector(workspace_roots=[root], max_stdout_bytes=1024)
            request = TerminalCommandRequest(
                executable=sys.executable,
                args=["-c", "import sys; sys.stdout.buffer.write(b'x' * (16 * 1024 * 1024))"],
                cwd=root,
                timeout=10,
            )

            tracemalloc.start()
            try:
                result = await connector._run_subprocess(request, root)
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertTrue(result.truncated)
            self.assertEqual(1024, len(result.stdout.encode("utf-8")))
            self.assertLess(peak_bytes, 4 * 1024 * 1024)


class RejectedTaskCoroutineRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_and_cancelled_queued_coroutines_are_closed(self) -> None:
        executor = PerProjectTaskExecutor(
            max_concurrent_per_project=1,
            max_concurrent_global=1,
            max_queue_size=1,
        )
        release_active = asyncio.Event()

        async def wait_for_release() -> None:
            await release_active.wait()

        active_coroutine = wait_for_release()
        active_future = executor.submit("project", active_coroutine)
        await asyncio.sleep(0)

        queued_coroutine = wait_for_release()
        queued_future = executor.submit("project", queued_coroutine, request_id="queued")
        rejected_coroutine = wait_for_release()
        rejected_future = executor.submit("project", rejected_coroutine)

        self.assertIsNone(rejected_coroutine.cr_frame)
        self.assertEqual("rejected", (await rejected_future).state.value)
        self.assertTrue(await executor.cancel("queued"))
        self.assertIsNone(queued_coroutine.cr_frame)
        self.assertEqual("cancelled", (await queued_future).state.value)

        release_active.set()
        await active_future
        executor.shutdown()
