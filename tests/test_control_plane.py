from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sutra.control_plane import ControlPlane, PairRequest
from sutra.connectivity import ConnectivityGuard, ConnectivityPolicy
from sutra.dev_certificate import create_certificate
from sutra.discovery import discover_projects
from sutra.file_limits import MAX_PREVIEW_PAGE, PREVIEW_FILE_LIMIT, PREVIEW_PAGE_CHARS
from sutra.runtime import Runtime
from sutra.agents import AgentAdapter, AgentAvailability, AgentCapability, AgentDescriptor, AgentEvent, AgentEventType, AgentSession, AgentSessionStatus, EventSeverity
from sutra.state import StateStore
from sutra.telegram_bot import TelegramBot


class ControlPlaneUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cp = ControlPlane(Path(self.temp.name) / "cp.sqlite3", "operator-token-that-is-long-enough")

    def tearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    def test_pairing_code_is_single_use_and_only_token_hash_is_stored(self):
        poll = "poll-secret"
        request = self.cp.pairing.create(PairRequest(device_name="dev", poll_secret_hash=hashlib.sha256(poll.encode()).hexdigest()),
                                         hashlib.sha256(poll.encode()).hexdigest(), "127.0.0.1")
        self.cp.pairing.approve(request["pairing_code"])
        status, result = self.cp.pairing.complete(request["request_id"], poll)
        self.assertEqual(status, 200)
        self.assertNotEqual(result["device_token"], poll)
        row = self.cp.store._db.execute("SELECT token_hash FROM devices").fetchone()
        self.assertEqual(row[0], self.cp.store.digest(result["device_token"]))
        self.assertEqual(self.cp.pairing.complete(request["request_id"], poll)[0], 404)

    def test_fresh_pairing_rotates_session_generation_and_rejects_old_credential(self):
        poll = "poll-secret"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        first = self.cp.pairing.create(PairRequest(device_id="AB-PC-ABC123", device_name="dev",
            poll_secret_hash=poll_hash), poll_hash, "127.0.0.1")
        self.cp.pairing.approve(first["pairing_code"])
        _, first_result = self.cp.pairing.complete(first["request_id"], poll)
        self.assertTrue(self.cp.store.authenticate_device("AB-PC-ABC123", first_result["device_token"],
            first_result["session_id"], first_result["generation"]))
        second = self.cp.pairing.create(PairRequest(device_id="AB-PC-ABC123", device_name="dev",
            poll_secret_hash=poll_hash), poll_hash, "127.0.0.1")
        self.cp.pairing.approve(second["pairing_code"])
        _, second_result = self.cp.pairing.complete(second["request_id"], poll)
        self.assertEqual(first_result["generation"] + 1, second_result["generation"])
        self.assertNotEqual(first_result["session_id"], second_result["session_id"])
        self.assertFalse(self.cp.store.authenticate_device("AB-PC-ABC123", first_result["device_token"],
            first_result["session_id"], first_result["generation"]))

    def test_control_plane_restart_invalidates_persisted_session_credentials(self):
        database = Path(self.temp.name) / "reopen.sqlite3"
        first = ControlPlane(database, "operator-token-that-is-long-enough")
        token = "old-session-token"
        session_id = str(uuid.uuid4())
        generation = first.store.register_device("AB-PC-ABC123", "dev", "0.1", "win",
            first.store.digest(token), time.time() + 60, session_id)
        first.store.close()
        restarted = ControlPlane(database, "operator-token-that-is-long-enough")
        try:
            self.assertFalse(restarted.store.authenticate_device("AB-PC-ABC123", token, session_id, generation))
            self.assertEqual("WAITING_FOR_PAIRING", restarted.store.list_devices(set())[0]["connection_status"])
        finally:
            restarted.store.close()

    def test_new_runtime_process_discards_persisted_remote_session_credential(self):
        with tempfile.TemporaryDirectory() as home:
            store = StateStore(Path(home) / "state.sqlite3")
            store.set_device("device_id", "AB-PC-ABC123")
            store.set_device("gateway_token", "old-session-token")
            store.set_device("session_id", str(uuid.uuid4()))
            store.set_device("session_generation", "9")
            store.close()
            runtime = Runtime(home, "wss://control.example/v1/devices/connect")
            try:
                self.assertIsNone(runtime.connection.token)
                self.assertFalse(runtime.connection.session_id)
                self.assertEqual("", runtime.store.get_device("gateway_token"))
                self.assertEqual("", runtime.store.get_device("session_id"))
            finally:
                runtime.store.close()

    def test_control_request_deadline_and_retry_use_distinct_request_ids(self):
        class NeverResponds:
            def __init__(self):
                self.frames = []
            async def send_json(self, frame):
                self.frames.append(frame)

        async def check():
            device_id, token = "AB-PC-ABC123", "device-secret"
            session_id = str(uuid.uuid4())
            generation = self.cp.store.register_device(device_id, "dev", "0.1", "win",
                self.cp.store.digest(token), time.time() + 60, session_id)
            self.cp.store.set_session_status(device_id, session_id, generation, "CONNECTED", connected=True)
            socket = NeverResponds()
            self.cp.sessions[device_id] = socket
            self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
            self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
            self.cp.connectivity[device_id] = ConnectivityGuard(self.cp.policy)
            self.cp.connectivity[device_id].state = "HEALTHY"
            self.cp.policy = ConnectivityPolicy(800, 1500, 10, 0.01, 0.05, 0.01, 3, 3)
            warnings = []
            for _ in range(2):
                self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
                with self.assertRaises(Exception) as failure:
                    await self.cp.dispatch_command(device_id, "runtime.status", {},
                        warning_callback=lambda request_id: warnings.append(request_id))
                self.assertEqual(504, failure.exception.status_code)
            self.assertEqual(2, len(socket.frames))
            self.assertNotEqual(socket.frames[0]["request_id"], socket.frames[1]["request_id"])
            self.assertEqual("TIMED_OUT", self.cp.request_history[socket.frames[0]["request_id"]]["state"])
            self.assertNotIn(socket.frames[0]["request_id"], self.cp.pending_responses)
            self.assertTrue(warnings)

        asyncio.run(check())

    def test_pairing_rejects_wrong_poll_secret_and_expired_code(self):
        poll = "poll-secret"
        request = self.cp.pairing.create(PairRequest(device_name="dev", poll_secret_hash=hashlib.sha256(poll.encode()).hexdigest()),
                                         hashlib.sha256(poll.encode()).hexdigest(), "127.0.0.1")
        self.assertEqual(self.cp.pairing.complete(request["request_id"], "wrong")[0], 404)
        self.cp.pairing.pending[request["request_id"]]["expires_at"] = time.time() - 1
        with self.assertRaises(Exception):
            self.cp.pairing.approve(request["pairing_code"])

    def test_pairing_code_is_invalid_and_cannot_be_reused(self):
        poll = "poll-secret"
        request = self.cp.pairing.create(PairRequest(device_name="dev", poll_secret_hash=hashlib.sha256(poll.encode()).hexdigest()),
                                         hashlib.sha256(poll.encode()).hexdigest(), "127.0.0.1")
        with self.assertRaises(Exception):
            self.cp.pairing.approve("AAAA-AAAA")
        self.cp.pairing.approve(request["pairing_code"])
        with self.assertRaises(Exception):
            self.cp.pairing.approve(request["pairing_code"])

    def test_device_token_expiry_and_revocation(self):
        token = "device-secret"
        device_id = "AB-PC-ABC123"
        self.cp.store.register_device(device_id, "dev", "0.1", "win", self.cp.store.digest(token), time.time() + 60)
        self.assertTrue(self.cp.store.authenticate_device(device_id, token))
        self.assertFalse(self.cp.store.authenticate_device(device_id, "wrong"))
        self.assertFalse(self.cp.store.authenticate_device("AB-PC-ABC124", token))
        self.cp.store.register_device("AB-PC-ABC124", "other", "0.1", "win", self.cp.store.digest(token), time.time() - 1)
        self.assertFalse(self.cp.store.authenticate_device("AB-PC-ABC124", token))
        self.cp.store.revoke(device_id)
        self.assertFalse(self.cp.store.authenticate_device(device_id, token))

    def test_malformed_websocket_frame_closes_session(self):
        token, device_id = "device-secret", "AB-PC-ABC123"
        self.cp.store.register_device(device_id, "dev", "0.1", "win", self.cp.store.digest(token), time.time() + 60)
        session = self.cp.store.current_session(device_id)
        with TestClient(self.cp.create_app()) as client:
            with client.websocket_connect("/v1/devices/connect", headers={
                    "Authorization": f"Bearer {token}", "X-AgentBridge-Device-ID": device_id,
                    "X-AgentBridge-Session-ID": session["session_id"],
                    "X-AgentBridge-Generation": str(session["session_generation"])}) as websocket:
                websocket.send_text("not-json")
                with self.assertRaises(WebSocketDisconnect):
                    websocket.receive_text()
        self.assertNotIn(device_id, self.cp.sessions)

    def test_heartbeat_timeout_marks_device_offline(self):
        token, device_id = "device-secret", "AB-PC-ABC123"
        self.cp.store.register_device(device_id, "dev", "0.1", "win", self.cp.store.digest(token), time.time() + 60)
        session = self.cp.store.current_session(device_id)
        with patch("sutra.control_plane.HEARTBEAT_TIMEOUT_SECONDS", 0.05):
            with TestClient(self.cp.create_app()) as client:
                with client.websocket_connect("/v1/devices/connect", headers={
                        "Authorization": f"Bearer {token}", "X-AgentBridge-Device-ID": device_id,
                        "X-AgentBridge-Session-ID": session["session_id"],
                        "X-AgentBridge-Generation": str(session["session_generation"])}) as websocket:
                    with self.assertRaises(WebSocketDisconnect):
                        websocket.receive_text()
        self.assertNotIn(device_id, self.cp.sessions)
        self.assertEqual(self.cp.store.list_devices(set())[0]["connection_status"], "WAITING_FOR_PAIRING")
        current = self.cp.store.current_session(device_id)
        self.assertFalse(self.cp.store.authenticate_device(device_id, token,
            current["session_id"], current["session_generation"]))

    def test_home_menu_allowlist_and_secret_safe_output(self):
        secret_marker = "telegram-token-secret-marker"
        bot = TelegramBot(self.cp, secret_marker, {42})
        async def run_check():
            rejected = await bot.process_update({"update_id": 1, "message": {"from": {"id": 7},
                "chat": {"id": 7, "type": "private"}, "text": "/start"}})
            self.assertIsNone(rejected)
            home = await bot.process_update({"update_id": 2, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/start"}})
            self.assertTrue(home.show_navigation)
            self.assertEqual(len(home.inline_keyboard), 4)
            self.assertNotIn(secret_marker, repr(home))
            help_text = await bot.process_update({"update_id": 4, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/help"}})
            self.assertIn("navigation buttons", help_text)
            self.assertNotIn("/status", help_text.split("Compatibility commands:")[0])
            invalid = await bot.process_update({"update_id": 3, "callback_query": {
                "id": "forged", "from": {"id": 42}, "data": "target:forged",
                "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
            self.assertIn("invalid or expired", invalid.text.lower())
        asyncio.run(run_check())

    def test_telegram_and_bearer_credentials_are_redacted_from_logs(self):
        telegram_token = "telegram-secret-marker"
        bot = TelegramBot(self.cp, telegram_token, {42})
        with self.assertLogs("sutra.telegram_bot", level="WARNING") as captured:
            logging.getLogger("sutra.telegram_bot").warning(
                "request https://api.telegram.org/bot%s/sendMessage Authorization: Bearer %s",
                telegram_token, "control-plane-secret-marker")
        output = "\n".join(captured.output)
        self.assertNotIn(telegram_token, output)
        self.assertNotIn("control-plane-secret-marker", output)
        self.assertIn("[REDACTED]", output)

    def test_telegram_webhook_requires_https_and_valid_secret_token(self):
        for url, secret in (("http://example.test/hook", "Valid_Secret-1"),
                            ("https://example.test/hook", "contains spaces"),
                            ("https://example.test/hook", "x" * 257)):
            with self.subTest(url=url, secret_length=len(secret)):
                with self.assertRaises(ValueError):
                    TelegramBot(self.cp, "token", {42}, url, secret)
        bot = TelegramBot(self.cp, "token", {42}, "https://example.test/hook", "x" * 256)
        self.assertEqual("https://example.test/hook", bot.webhook_url)

    def test_file_navigation_stays_in_approved_root_and_redacts_common_secrets(self):
        root = Path(self.temp.name) / "workspace"
        root.mkdir()
        (root / "src").mkdir()
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        source = root / "src" / "app.py"
        source.write_text('api_key = "private-example-value"\nprint("ok")\n', encoding="utf-8")
        (root / ".env").write_text("BOT_TOKEN=private", encoding="utf-8")
        (root / "credentials.json").write_text("private", encoding="utf-8")
        runtime = Runtime(str(Path(self.temp.name) / "runtime"))
        runtime.store.add_workspace(str(root))
        runtime.project_index.set_roots(runtime.store.workspaces())
        async def run_check():
            runtime.project_index.schedule_refresh()
            await runtime.project_index.wait_for_refresh()
            tree = await runtime.handle_remote_command("file.list", {"project_root": str(root), "path": str(root)})
            names = {entry["name"] for entry in tree["items"]}
            self.assertIn("src", names)
            self.assertNotIn(".env", names)
            self.assertNotIn("credentials.json", names)
            params = {"project_root": str(root), "path": str(source)}
            preview = await runtime.handle_remote_command("file.preview", params)
            self.assertIn("[REDACTED]", preview["text"])
            self.assertNotIn("private-example-value", preview["text"])
            download = await runtime.handle_remote_command("file.download", params)
            content = base64.b64decode(download["content_base64"]).decode()
            self.assertNotIn("private-example-value", content)
            protected_paths = [root / ".env"] + [root / "src" / ("protected" + suffix)
                for suffix in (".pkcs12", ".p12", ".pem", ".key")]
            for protected_path in protected_paths[1:]:
                protected_path.write_text("harmless protected-file fixture", encoding="utf-8")
            for protected_path in protected_paths:
                for command in ("file.preview", "file.download"):
                    with self.subTest(path=protected_path.name, command=command):
                        with self.assertRaises(ValueError):
                            await runtime.handle_remote_command(command, {
                                "project_root": str(root), "path": str(protected_path)})
            long_source = root / "src" / "long.py"
            long_source.write_text("x\n" * 6000, encoding="utf-8")
            first = await runtime.handle_remote_command("file.preview", {**params, "path": str(long_source), "page": 0})
            second = await runtime.handle_remote_command("file.preview", {**params, "path": str(long_source), "page": 1})
            self.assertEqual(0, first["page"])
            self.assertEqual(1, second["page"])
            self.assertGreater(first["pages"], 1)
            self.assertNotEqual(first["text"], second["text"])
            binary_source = root / "src" / "picture.png"
            binary_source.write_bytes(b"\x89PNG\x00binary")
            with self.assertRaises(ValueError):
                await runtime.handle_remote_command("file.preview", {**params, "path": str(binary_source)})
            other_project = Path(self.temp.name) / "other-project"
            other_project.mkdir()
            (other_project / "package.json").write_text("{}", encoding="utf-8")
            (other_project / "outside.py").write_text("print('no')", encoding="utf-8")
            with self.assertRaises(PermissionError):
                await runtime.handle_remote_command("file.preview", {"project_root": str(root), "path": str(other_project / "outside.py")})
            with self.assertRaises(PermissionError):
                await runtime.handle_remote_command("file.preview", {"project_root": str(root), "path": str(Path(self.temp.name) / "outside.txt")})
        try:
            asyncio.run(run_check())
        finally:
            runtime.store.close()


class ControlPlaneTlsE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cert, self.key = create_certificate(root / "certs")
        self.cp = ControlPlane(root / "cp.sqlite3", "operator-token-that-is-long-enough")
        self.app = self.cp.create_app()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="critical", ssl_certfile=str(self.cert), ssl_keyfile=str(self.key), ws_max_size=32768)
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve(sockets=[sock]))
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.server.started)
        self.base = f"https://127.0.0.1:{self.port}"
        self.verify = ssl.create_default_context(cafile=str(self.cert))
        self.runtime = Runtime(str(root / "device"), f"wss://127.0.0.1:{self.port}/v1/devices/connect",
                               ca_file=str(self.cert))
        self.runtime.store.add_workspace(str(root))
        self.project = root / "SampleApp"
        (self.project / "src").mkdir(parents=True)
        (self.project / "package.json").write_text('{"name":"sample-app"}', encoding="utf-8")
        (self.project / "src" / "main.py").write_text('api_key = "private-example-value"\nprint("hello")\n', encoding="utf-8")
        self.runtime.project_index.set_roots(self.runtime.store.workspaces())
        self.runtime.project_index.schedule_refresh()
        await self.runtime.project_index.wait_for_refresh()
        self.runtime.store.set_device("device_id", "AB-PC-ABC123")
        self.runtime.connection.device_id = "AB-PC-ABC123"

    async def asyncTearDown(self):
        if hasattr(self, "connection_task"):
            await self.runtime.connection.stop()
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)
        self.cp.store.close()
        self.runtime.store.close()
        self.temp.cleanup()

    async def test_tls_pairing_telegram_approval_command_routing_and_revocation(self):
        poll_secret = "fresh-poll-secret"
        poll_hash = hashlib.sha256(poll_secret.encode()).hexdigest()
        async with httpx.AsyncClient(verify=self.verify, timeout=3) as client:
            created = await client.post(self.base + "/v1/pairing/requests", json={
                "device_id": "AB-PC-ABC123", "device_name": "test workstation",
                "poll_secret_hash": poll_hash})
            self.assertEqual(created.status_code, 200, created.text)
            request = created.json()
            bot = TelegramBot(self.cp, "unused-token", {42})
            result = await bot.process_update({"update_id": 1, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/pair " + request["pairing_code"]}})
            self.assertIn("approved", result)
            complete = await client.get(self.base + f"/v1/pairing/requests/{request['request_id']}/complete",
                headers={"Authorization": f"Bearer {poll_secret}"})
            self.assertEqual(complete.status_code, 200, complete.text)
            health = await client.get(self.base + "/health")
            self.assertEqual(health.status_code, 200)
            self.runtime.connection.token = complete.json()["device_token"]
            self.runtime.connection.session_id = complete.json()["session_id"]
            self.runtime.connection.generation = complete.json()["generation"]
            import websockets
            with self.assertRaises(Exception):
                async with websockets.connect(self.runtime.connection.gateway_url,
                        additional_headers={"Authorization": "Bearer invalid-device-token",
                                            "X-AgentBridge-Device-ID": "AB-PC-ABC123",
                                            "X-AgentBridge-Session-ID": complete.json()["session_id"],
                                            "X-AgentBridge-Generation": str(complete.json()["generation"])},
                        ssl=self.verify, open_timeout=2):
                    pass
            self.connection_task = asyncio.create_task(self.runtime.connection.run())
            for _ in range(100):
                if self.runtime.connection.state == "CONNECTED":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(self.runtime.connection.state, "CONNECTED")
            auth = {"Authorization": "Bearer operator-token-that-is-long-enough"}
            for _ in range(30):
                devices = await client.get(self.base + "/v1/devices", headers=auth)
                if devices.status_code == 200 and devices.json()["devices"][0]["connection_status"] == "CONNECTED":
                    break
                await asyncio.sleep(0.05)
            status = await client.post(self.base + "/v1/devices/AB-PC-ABC123/commands", headers=auth,
                                       json={"command": "runtime.status", "payload": {}})
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["payload"]["device"], "AB-PC-ABC123")
            workspaces = await client.post(self.base + "/v1/devices/AB-PC-ABC123/commands", headers=auth,
                                           json={"command": "workspace.list", "payload": {}})
            self.assertEqual(workspaces.status_code, 200, workspaces.text)
            self.assertEqual(workspaces.json()["payload"]["items"], [str(Path(self.temp.name))])
            telegram_status = await bot.process_update({"update_id": 2, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/status"}})
            self.assertIn("AB-PC-ABC123", telegram_status)
            telegram_projects = await bot.process_update({"update_id": 3, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/projects"}})
            self.assertTrue(telegram_projects)
            self.assertNotIn("unavailable", telegram_projects.lower())
            home = await bot.process_update({"update_id": 4, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/start"}})
            self.assertIn("Projects: 1", home.text)
            self.assertTrue(home.show_navigation)

            async def click(update_id: int, data: str, user_id: int = 42):
                return await bot.process_update({"update_id": update_id, "callback_query": {
                    "id": f"cb-{update_id}", "from": {"id": user_id}, "data": data,
                    "message": {"message_id": 100, "chat": {"id": user_id, "type": "private"}}}})

            projects_screen = await click(5, "nav:projects")
            project_button = projects_screen.inline_keyboard[0][0]["callback_data"]
            self.assertTrue(project_button.startswith("target:"))
            rejected = await click(6, project_button, user_id=99)
            self.assertIsNone(rejected)
            project_screen = await click(7, project_button)
            self.assertIn("Current agent: None", project_screen.text)
            files_button = next(button for row in project_screen.inline_keyboard for button in row if button["text"] == "📂 Files")
            files_screen = await click(8, files_button["callback_data"])
            folder_button = next(button for row in files_screen.inline_keyboard for button in row if button["text"] == "📁 src")
            nested_screen = await click(9, folder_button["callback_data"])
            back_button = next(button for row in nested_screen.inline_keyboard for button in row if button["text"] == "⬅️ Back")
            back_to_root = await click(22, back_button["callback_data"])
            self.assertIn("· .", back_to_root.text)
            file_button = next(button for row in nested_screen.inline_keyboard for button in row if button["text"] == "📄 main.py")
            file_screen = await click(10, file_button["callback_data"])
            preview_button = next(button for row in file_screen.inline_keyboard for button in row if button["text"] == "👁 Preview")
            preview = await click(11, preview_button["callback_data"])
            self.assertIn("[REDACTED]", preview.text)
            self.assertNotIn("private-example-value", preview.text)
            download_button = next(button for row in file_screen.inline_keyboard for button in row if button["text"] == "📥 Download")
            download = await click(12, download_button["callback_data"])
            self.assertTrue(download.document_name.endswith(".redacted.txt"))
            self.assertNotIn(b"private-example-value", download.document_bytes)
            forged = await click(13, "target:forged")
            self.assertIn("invalid or expired", forged.text.lower())
            home_again = await click(14, "nav:home")
            self.assertIn("SŪTRA", home_again.text)
            permissions = await click(15, "nav:permissions")
            self.assertIn("Not Implemented", permissions.text)
            bot.pending_permissions["perm-42"] = {"id": "perm-42", "user_id": 42,
                "device_id": "AB-PC-ABC123", "agent": "Placeholder", "project": "SampleApp",
                "action": "Run command", "summary": "npm test"}
            pending_screen = await click(18, "nav:permissions")
            allow_button = next(button for row in pending_screen.inline_keyboard for button in row if button["text"] == "✅ Allow Once")
            bot.pending_permissions.pop("perm-42")
            stale_permission = await click(19, allow_button["callback_data"])
            self.assertIn("no longer pending", stale_permission.text)
            self.assertFalse(bot.pending_permissions)
            maximum_preview = self.project / "src" / "maximum-preview.txt"
            maximum_preview.write_text("x" * PREVIEW_FILE_LIMIT, encoding="utf-8")
            self.assertEqual(PREVIEW_FILE_LIMIT, maximum_preview.stat().st_size)
            expected_pages = (PREVIEW_FILE_LIMIT + PREVIEW_PAGE_CHARS - 1) // PREVIEW_PAGE_CHARS
            self.assertGreaterEqual(expected_pages, 75)
            self.assertEqual(expected_pages - 1, MAX_PREVIEW_PAGE)
            for page in (0, 65, MAX_PREVIEW_PAGE):
                response = await client.post(self.base + "/v1/devices/AB-PC-ABC123/commands", headers=auth,
                    json={"command": "file.preview", "payload": {
                        "project_root": str(self.project), "path": str(maximum_preview), "page": page}})
                self.assertEqual(200, response.status_code, response.text)
                payload = response.json()["payload"]
                self.assertEqual(page, payload["page"])
                self.assertEqual(expected_pages, payload["pages"])
            final_response = await client.post(self.base + "/v1/devices/AB-PC-ABC123/commands", headers=auth,
                json={"command": "file.preview", "payload": {
                    "project_root": str(self.project), "path": str(maximum_preview), "page": MAX_PREVIEW_PAGE}})
            self.assertEqual(PREVIEW_FILE_LIMIT - MAX_PREVIEW_PAGE * PREVIEW_PAGE_CHARS,
                             len(final_response.json()["payload"]["text"]))
            beyond_final = await client.post(self.base + "/v1/devices/AB-PC-ABC123/commands", headers=auth,
                json={"command": "file.preview", "payload": {
                    "project_root": str(self.project), "path": str(maximum_preview),
                    "page": MAX_PREVIEW_PAGE + 1}})
            self.assertEqual(400, beyond_final.status_code)
            device_screen = await click(16, "nav:devices")
            device_button = device_screen.inline_keyboard[0][0]["callback_data"]
            device_detail = await click(17, device_button)
            self.assertIn("AB-PC-ABC123", device_detail.text)
            self.assertNotIn(complete.json()["device_token"], device_detail.text)
            revoke_button = next(button for row in device_detail.inline_keyboard for button in row if button["text"] == "⛔ Revoke Device")
            confirm_screen = await click(20, revoke_button["callback_data"])
            self.assertIn("Revoke test workstation", confirm_screen.text)
            confirm_button = next(button for row in confirm_screen.inline_keyboard for button in row if button["text"] == "Confirm Revoke")
            revoke_result = await click(21, confirm_button["callback_data"])
            self.assertIn("credential revoked", revoke_result.text)
            denied = await client.post(self.base + "/v1/devices/AB-PC-ABC123/commands", headers=auth,
                                       json={"command": "shell.exec", "payload": {}})
            self.assertEqual(400, denied.status_code)
            for _ in range(30):
                if self.runtime.connection.state == "REVOKED":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(self.runtime.connection.state, "WAITING_FOR_PAIRING")


class AccessRootApprovalWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cert, self.key = create_certificate(root / "certs")
        self.cp = ControlPlane(root / "cp.sqlite3", "operator-token-that-is-long-enough")
        self.app = self.cp.create_app()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="critical", ssl_certfile=str(self.cert), ssl_keyfile=str(self.key), ws_max_size=32768)
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve(sockets=[sock]))
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.server.started)
        self.base = f"https://127.0.0.1:{self.port}"
        self.verify = ssl.create_default_context(cafile=str(self.cert))
        self.runtime = Runtime(str(root / "device"), f"wss://127.0.0.1:{self.port}/v1/devices/connect",
                               ca_file=str(self.cert))
        self.initial_root = root / "InitialRoot"
        self.initial_root.mkdir()
        self.runtime.store.add_workspace(str(self.initial_root))
        self.runtime.store.set_device("device_id", "AB-PC-ABC123")
        self.runtime.connection.device_id = "AB-PC-ABC123"

        poll_secret = "fresh-poll-secret"
        poll_hash = hashlib.sha256(poll_secret.encode()).hexdigest()
        async with httpx.AsyncClient(verify=self.verify, timeout=3) as client:
            created = await client.post(self.base + "/v1/pairing/requests", json={
                "device_id": "AB-PC-ABC123", "device_name": "test workstation",
                "poll_secret_hash": poll_hash})
            request = created.json()
            self.bot = TelegramBot(self.cp, "test-bot-token", {42, 43})
            await self.bot.process_update({"update_id": 1, "message": {"from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "text": "/pair " + request["pairing_code"]}})
            complete = await client.get(self.base + f"/v1/pairing/requests/{request['request_id']}/complete",
                headers={"Authorization": f"Bearer {poll_secret}"})
            self.runtime.connection.token = complete.json()["device_token"]
            self.runtime.connection.session_id = complete.json()["session_id"]
            self.runtime.connection.generation = complete.json()["generation"]
            self.connection_task = asyncio.create_task(self.runtime.connection.run())
            for _ in range(100):
                if self.runtime.connection.state == "CONNECTED":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(self.runtime.connection.state, "CONNECTED")

    async def asyncTearDown(self):
        if hasattr(self, "connection_task"):
            await self.runtime.connection.stop()
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)
        self.cp.store.close()
        self.runtime.store.close()
        self.temp.cleanup()

    async def test_unregistered_root_cannot_be_used_for_discovery(self):
        unregistered = Path(self.temp.name) / "UnregisteredStorage"
        unregistered_proj = unregistered / "HiddenProj"
        unregistered_proj.mkdir(parents=True)
        (unregistered_proj / "pyproject.toml").write_text("[project]\nname='hidden'\n", encoding="utf-8")
        self.assertNotIn(str(unregistered), self.runtime.store.workspaces())
        projects = discover_projects(self.runtime.store.workspaces())
        self.assertNotIn("HiddenProj", [p["name"] for p in projects])
        with self.assertRaises(PermissionError):
            await self.runtime.handle_remote_command("file.list", {
                "project_root": str(unregistered_proj), "path": str(unregistered_proj)})

    async def test_telegram_agent_prompt_and_runtime_output_are_end_to_end(self):
        project = self.initial_root / "AgentBridgeDemo"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname='agentbridge-demo'\n", encoding="utf-8")
        self.runtime.project_index.set_roots([str(self.initial_root)])
        self.runtime.project_index.schedule_refresh()
        if self.runtime.project_index._refresh_task:
            await self.runtime.project_index._refresh_task

        class EventAdapter(AgentAdapter):
            def __init__(self):
                self.received = []
                self._descriptor = AgentDescriptor(
                    agent_id="telegram_test", provider="Test", display_name="Telegram Test",
                    availability=AgentAvailability.AVAILABLE,
                    capabilities=(AgentCapability.START.value, AgentCapability.STOP.value),
                    implementation="Integration Test Adapter",
                )

            def descriptor(self):
                return self._descriptor

            async def start(self, session: AgentSession, **kwargs):
                self.received.append(kwargs.get("prompt"))
                session.metadata["final_response"] = "Actual test response ✓"
                on_event = kwargs["on_event"]
                on_event(AgentEvent(event_type=AgentEventType.AGENT_OUTPUT, agent_id=session.agent_id,
                    agent_session_id=session.session_id, project_id=session.project_id,
                    severity=EventSeverity.INFO, payload={"stream": "stdout", "text": "Actual streamed output ✓"}))
                on_event(AgentEvent(event_type=AgentEventType.AGENT_COMPLETED, agent_id=session.agent_id,
                    agent_session_id=session.session_id, project_id=session.project_id,
                    severity=EventSeverity.INFO, payload={"exit_code": 0}))
                session.status = AgentSessionStatus.RUNNING
                return session

            async def stop(self, session_id: str):
                return AgentSession(session_id=session_id, status=AgentSessionStatus.STOPPED)

        adapter = EventAdapter()
        self.runtime.agent_registry.register(adapter, overwrite=True)
        sent_text = []
        sent_replies = []

        async def capture_text(_client, _chat_id, text):
            sent_text.append(text)
            return {"ok": True}

        async def capture_reply(_client, reply):
            sent_replies.append(reply)

        self.bot._send_text = capture_text
        self.bot._send_reply = capture_reply
        context = {"device_id": "AB-PC-ABC123", "project": {
            "name": "AgentBridgeDemo", "absolute_path": str(project)}, "agent_id": "telegram_test"}
        self.bot.selected_devices[42] = "AB-PC-ABC123"
        agent_screen = await self.bot._screen("project_agent", context, 42, 42)
        self.assertIn("Telegram Test: available", agent_screen.text)
        self.assertIn("Jules: simulator · not live", agent_screen.text)
        self.assertFalse(any("Jules" in button["text"] and button["text"].startswith("▶ Start")
                             for row in agent_screen.inline_keyboard for button in row))
        start_button = next(button for row in agent_screen.inline_keyboard for button in row
                            if button["text"] == "▶ Start · Telegram Test")
        selected = await self.bot.process_update({"update_id": 70, "callback_query": {
            "id": "agent-start", "from": {"id": 42}, "data": start_button["callback_data"],
            "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}},
        }})
        self.assertIn("Send the task", selected.text)
        prompt = "Implement this: café 東京\nKeep the second line"
        message_result = await self.bot.process_update({"update_id": 71, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": prompt,
        }})
        self.assertEqual(adapter.received, [prompt], msg=str(message_result))
        for _ in range(100):
            if sent_replies:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(any("Actual streamed output ✓" in text for text in sent_text))
        self.assertTrue(any("Actual test response ✓" in reply.text for reply in sent_replies))
        self.assertTrue(any(any(button["text"] == "➕ Enter next prompt" for row in reply.inline_keyboard for button in row)
                            for reply in sent_replies))
        next_button = next(button for row in sent_replies[-1].inline_keyboard for button in row
                           if button["text"] == "➕ Enter next prompt")
        next_selected = await self.bot.process_update({"update_id": 72, "callback_query": {
            "id": "agent-next", "from": {"id": 42}, "data": next_button["callback_data"],
            "message": {"message_id": 8, "chat": {"id": 42, "type": "private"}},
        }})
        self.assertIn("Send the task", next_selected.text)
        second_prompt = "Continue the implementation\nUse the existing context"
        await self.bot.process_update({"update_id": 73, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": second_prompt,
        }})
        self.assertEqual(adapter.received, [prompt, second_prompt])
        for _ in range(100):
            if len(sent_replies) >= 2:
                break
            await asyncio.sleep(0.02)
        self.assertGreaterEqual(len(sent_replies), 2)
        for task in list(self.bot.agent_delivery_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.bot.agent_delivery_tasks.values()), return_exceptions=True)


    async def test_user_can_request_authorization_for_root(self):
        reply = await self.bot.process_update({"update_id": 2, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/access_root D:\\"}})
        self.assertIsNotNone(reply)
        self.assertIn("⚠️ Access Root Authorization", reply.text)
        self.assertIn("You are about to allow SŪTRA to discover projects under:", reply.text)
        self.assertIn("D:\\", reply.text)
        self.assertIn("This does NOT automatically grant an agent unrestricted access to the entire drive.", reply.text)
        button_labels = [btn["text"] for row in reply.inline_keyboard for btn in row]
        self.assertTrue(any("✅ Allow D:\\" in label for label in button_labels))
        self.assertIn("❌ Reject", button_labels)

    async def test_authorized_user_can_approve_and_registers_canonical_root_and_discovery_starts(self):
        candidate_root = Path(self.temp.name) / "CandidateStorage"
        candidate_proj = candidate_root / "DiscoveredApp"
        candidate_proj.mkdir(parents=True)
        (candidate_proj / "package.json").write_text('{"name":"discovered"}', encoding="utf-8")
        before_projects = discover_projects(self.runtime.store.workspaces())
        self.assertNotIn("DiscoveredApp", [p["name"] for p in before_projects])
        req_reply = await self.bot.process_update({"update_id": 3, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": f"/access_root {candidate_root}"}})
        allow_btn = next(b["callback_data"] for row in req_reply.inline_keyboard for b in row if "✅ Allow" in b["text"])
        allow_reply = await self.bot.process_update({"update_id": 4, "callback_query": {
            "id": "q_allow", "from": {"id": 42}, "data": allow_btn,
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("✅ Authorized", allow_reply.text)
        self.assertTrue(any(Path(w).resolve() == candidate_root.resolve() for w in self.runtime.store.workspaces()))
        after_projects = discover_projects(self.runtime.store.workspaces())
        self.assertIn("DiscoveredApp", [p["name"] for p in after_projects])

    async def test_reject_does_not_register_root_and_does_not_trigger_discovery(self):
        rejected_root = Path(self.temp.name) / "RejectedStorage"
        rejected_proj = rejected_root / "UnseenApp"
        rejected_proj.mkdir(parents=True)
        (rejected_proj / "package.json").write_text('{"name":"unseen"}', encoding="utf-8")
        req_reply = await self.bot.process_update({"update_id": 5, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": f"/access_root {rejected_root}"}})
        reject_btn = next(b["callback_data"] for row in req_reply.inline_keyboard for b in row if "❌ Reject" in b["text"])
        reject_reply = await self.bot.process_update({"update_id": 6, "callback_query": {
            "id": "q_reject", "from": {"id": 42}, "data": reject_btn,
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn(f"Access to {rejected_root} was not granted.", reject_reply.text)
        self.assertIn("❌ Not Authorized", reject_reply.text)
        self.assertFalse(any(Path(w).resolve() == rejected_root.resolve() for w in self.runtime.store.workspaces()))
        projects = discover_projects(self.runtime.store.workspaces())
        self.assertNotIn("UnseenApp", [p["name"] for p in projects])

    async def test_registered_root_is_hidden_from_picker_and_direct_request(self):
        if os.name != "nt":
            self.skipTest("drive-root picker is Windows-specific")

        original_call_device = self.bot._call_device

        async def roots_with_drive(user_id, command, payload=None):
            if command == "workspace.list":
                return "AB-PC-ABC123", {"items": ["d:\\"]}
            return await original_call_device(user_id, command, payload)

        self.bot._call_device = roots_with_drive
        try:
            picker = await self.bot._screen("add_access_root", {}, 42, 42)
            labels = [button["text"] for row in picker.inline_keyboard for button in row]
            self.assertFalse(any(label.startswith("📁 D:\\") for label in labels))
            self.assertNotIn("already authorized", picker.text.lower())
        finally:
            self.bot._call_device = original_call_device

        already_authorized = await self.bot.process_update({"update_id": 17, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
            "text": f"/access_root {self.initial_root}"}})
        self.assertIn("Access Roots", already_authorized.text)
        self.assertIn("already authorized", already_authorized.text.lower())
        self.assertNotIn("Access Root Authorization", already_authorized.text)

    async def test_stale_reject_callback_does_not_claim_authorized_root_was_rejected(self):
        reject_button = self.bot._target_button("❌ Reject", 42, 42, "access_root_reject", {
            "device_id": "AB-PC-ABC123", "root": str(self.initial_root)})
        reject_reply = await self.bot.process_update({"update_id": 18, "callback_query": {
            "id": "q_stale_reject", "from": {"id": 42}, "data": reject_button["callback_data"],
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("already authorized", reject_reply.text.lower())
        self.assertNotIn("was not granted", reject_reply.text)
        self.assertTrue(any(Path(path).resolve() == self.initial_root.resolve()
                            for path in self.runtime.store.workspaces()))

    async def test_telegram_stop_session_invalidates_credential_and_requires_fresh_pairing(self):
        old_token = self.runtime.connection.token
        device_screen = await self.bot._screen("device", {"device_id": "AB-PC-ABC123"}, 42, 42)
        stop_button = next(button for row in device_screen.inline_keyboard for button in row
                           if button["text"] == "⛔ Stop Session")
        confirm = await self.bot.process_update({"update_id": 30, "callback_query": {
            "id": "q_stop_confirm", "from": {"id": 42}, "data": stop_button["callback_data"],
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("This does not stop a coding-agent process", confirm.text)
        confirm_button = next(button for row in confirm.inline_keyboard for button in row
                              if button["text"] == "⛔ Stop Session")
        stopped = await self.bot.process_update({"update_id": 31, "callback_query": {
            "id": "q_stop_execute", "from": {"id": 42}, "data": confirm_button["callback_data"],
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("session stopped", stopped.text.lower())
        current = self.cp.store.current_session("AB-PC-ABC123")
        self.assertEqual("STOPPED", current["session_status"])
        self.assertFalse(self.cp.store.authenticate_device("AB-PC-ABC123",
            old_token or "", current["session_id"], current["session_generation"]))
        for _ in range(50):
            if self.runtime.connection.state == "WAITING_FOR_PAIRING":
                break
            await asyncio.sleep(0.02)
        self.assertEqual("WAITING_FOR_PAIRING", self.runtime.connection.state)

    async def test_unauthorized_telegram_user_cannot_approve_root(self):
        unauth_root = Path(self.temp.name) / "UnauthStorage"
        unauth_root.mkdir()
        result = await self.bot.process_update({"update_id": 7, "message": {
            "from": {"id": 9999}, "chat": {"id": 9999, "type": "private"}, "text": f"/access_root {unauth_root}"}})
        self.assertIsNone(result)
        self.assertFalse(any(Path(w).resolve() == unauth_root.resolve() for w in self.runtime.store.workspaces()))

    async def test_callback_from_another_user_cannot_approve_root(self):
        another_root = Path(self.temp.name) / "AnotherStorage"
        another_root.mkdir()
        req_reply = await self.bot.process_update({"update_id": 8, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": f"/access_root {another_root}"}})
        allow_btn = next(b["callback_data"] for row in req_reply.inline_keyboard for b in row if "✅ Allow" in b["text"])
        other_user_reply = await self.bot.process_update({"update_id": 9, "callback_query": {
            "id": "q_other", "from": {"id": 43}, "data": allow_btn,
            "message": {"message_id": 1, "chat": {"id": 43, "type": "private"}}}})
        self.assertIn("invalid or expired", other_user_reply.text.lower())
        self.assertFalse(any(Path(w).resolve() == another_root.resolve() for w in self.runtime.store.workspaces()))

    async def test_project_and_file_operations_remain_project_scoped_after_root_approval(self):
        approved_root = Path(self.temp.name) / "ScopedStorage"
        project_dir = approved_root / "ScopedProject"
        project_dir.mkdir(parents=True)
        (project_dir / "pyproject.toml").write_text("[project]\nname='scoped'\n", encoding="utf-8")
        safe_file = project_dir / "safe.py"
        safe_file.write_text("print('safe')\n", encoding="utf-8")
        env_file = project_dir / ".env"
        env_file.write_text("SECRET=bad\n", encoding="utf-8")
        outside_file = approved_root / "outside.txt"
        outside_file.write_text("outside\n", encoding="utf-8")
        self.runtime.store.add_workspace(str(approved_root))
        self.runtime.project_index.set_roots(self.runtime.store.workspaces())
        self.runtime.project_index.schedule_refresh()
        await self.runtime.project_index.wait_for_refresh()
        valid_preview = await self.runtime.handle_remote_command("file.preview", {
            "project_root": str(project_dir), "path": str(safe_file)})
        self.assertIn("print('safe')", valid_preview["text"])
        with self.assertRaises(ValueError):
            await self.runtime.handle_remote_command("file.preview", {
                "project_root": str(project_dir), "path": str(env_file)})
        with self.assertRaises(ValueError):
            await self.runtime.handle_remote_command("file.preview", {
                "project_root": str(project_dir), "path": str(outside_file)})
        with self.assertRaises(PermissionError):
            await self.runtime.handle_remote_command("file.preview", {
                "project_root": str(approved_root), "path": str(outside_file)})

    async def test_access_root_approval_does_not_create_agent_permission(self):
        self.assertEqual(len(self.bot.pending_permissions), 0)
        approved = Path(self.temp.name) / "PermCheckStorage"
        approved.mkdir()
        req_reply = await self.bot.process_update({"update_id": 11, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": f"/access_root {approved}"}})
        allow_btn = next(b["callback_data"] for row in req_reply.inline_keyboard for b in row if "✅ Allow" in b["text"])
        await self.bot.process_update({"update_id": 12, "callback_query": {
            "id": "q_pcheck", "from": {"id": 42}, "data": allow_btn,
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertEqual(len(self.bot.pending_permissions), 0)
        agents = self.bot.agents.list()
        for agent in agents:
            self.assertEqual("Not Connected", agent.status)

    async def test_full_telegram_menu_navigation_flow(self):
        home_reply = await self.bot.process_update({"update_id": 13, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "🏠 Home"}})
        self.assertIn("SŪTRA", home_reply.text)
        settings_reply = await self.bot.process_update({"update_id": 14, "callback_query": {
            "id": "q_nav_set", "from": {"id": 42}, "data": "nav:settings",
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("Settings", settings_reply.text)
        roots_reply = await self.bot.process_update({"update_id": 15, "callback_query": {
            "id": "q_set0", "from": {"id": 42}, "data": "setting:0",
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("Access Roots", roots_reply.text)
        add_root_btn = next(b["callback_data"] for row in roots_reply.inline_keyboard for b in row if "➕ Add Access Root" in b["text"])
        add_reply = await self.bot.process_update({"update_id": 16, "callback_query": {
            "id": "q_add", "from": {"id": 42}, "data": add_root_btn,
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}}})
        self.assertIn("Select an access root below", add_reply.text)


class PairingLifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.cp = ControlPlane(self.home / "cp.sqlite3", "operator-token-that-is-long-enough")
        self.bot = TelegramBot(self.cp, "test-bot-token", {42})

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    async def test_connection_manager_does_not_hang_on_transport_failure(self):
        from sutra.connection import ConnectionManager
        from types import SimpleNamespace
        conn = ConnectionManager("wss://127.0.0.1:9443/ws", "test-token", "AB-PC-123456", max_backoff=1)
        calls = 0

        def connect(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise OSError("network unreachable")

        with patch.dict("sys.modules", {"websockets": SimpleNamespace(connect=connect)}):
            await asyncio.wait_for(conn.run(), timeout=2.0)

        self.assertEqual(1, calls)
        self.assertIsNone(conn.token)
        self.assertEqual("WAITING_FOR_PAIRING", conn.state)

    def test_new_runtime_starts_with_empty_remote_session_credentials(self):
        store = StateStore(self.home / "state.sqlite3")
        store.set_device("device_id", "AB-PC-123456")
        store.set_device("gateway_token", "stale-secret-token")
        store.set_device("session_id", "old-session-uuid")
        store.close()

        runtime = Runtime(str(self.home), "wss://127.0.0.1:9443/v1/devices/connect")
        try:
            self.assertIsNone(runtime.connection.token)
            self.assertFalse(runtime.connection.session_id)
            self.assertEqual("", runtime.store.get_device("gateway_token"))
            self.assertEqual("", runtime.store.get_device("session_id"))
        finally:
            runtime.store.close()

    def test_pairing_completion_single_use_and_rejection(self):
        poll = "poll-secret-123"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        req = self.cp.pairing.create(PairRequest(device_name="workstation", poll_secret_hash=poll_hash),
                                     poll_hash, "127.0.0.1")
        self.cp.pairing.approve(req["pairing_code"])
        status, result = self.cp.pairing.complete(req["request_id"], poll)
        self.assertEqual(status, 200)
        self.assertIn("device_token", result)
        # Second completion attempt must be rejected (404)
        status2, _ = self.cp.pairing.complete(req["request_id"], poll)
        self.assertEqual(status2, 404)

    def test_duplicate_pairing_request_with_same_session_id_reuses_pending_session(self):
        poll = "poll-secret-dup"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        sid = str(uuid.uuid4())
        first = self.cp.pairing.create(PairRequest(device_name="workstation", poll_secret_hash=poll_hash, session_id=sid),
                                       poll_hash, "127.0.0.1")
        second = self.cp.pairing.create(PairRequest(device_name="workstation", poll_secret_hash=poll_hash, session_id=sid),
                                        poll_hash, "127.0.0.1")
        self.assertEqual(first["pairing_code"], second["pairing_code"])
        self.assertEqual(first["request_id"], second["request_id"])

    async def test_telegram_pair_approval_ux_and_state(self):
        poll = "poll-secret-tg"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        req = self.cp.pairing.create(PairRequest(device_name="workstation", poll_secret_hash=poll_hash),
                                     poll_hash, "127.0.0.1")
        reply = await self.bot.process_update({"update_id": 99, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
            "text": f"/pair {req['pairing_code']}"}})
        self.assertIn("Pairing approved", reply)
        self.assertIn("Waiting for runtime authentication", reply)

    async def test_telegram_stop_session_invalidates_current_session(self):
        device_id = "AB-PC-STOP01"
        token = "secret-token-active"
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(device_id, "dev", "0.1", "win",
            self.cp.store.digest(token), time.time() + 3600, session_id)
        self.cp.store.set_session_status(device_id, session_id, generation, "CONNECTED", connected=True)

        class DummyWs:
            async def send_json(self, frame): pass
            async def close(self, code=1000, reason=""): pass

        ws = DummyWs()
        self.cp.sessions[device_id] = ws
        self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}

        stopped, reachable = await self.cp.stop_session(device_id)
        self.assertTrue(stopped)
        self.assertFalse(self.cp.store.authenticate_device(device_id, token, session_id, generation))


class Phase25PairingAndSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "operator-token-that-is-long-enough")
        self.bot = TelegramBot(self.cp, "test-bot-token", {42})

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    def _setup_paired_device(self, device_id="AB-PC-ABC123", connected=True):
        token = "test-device-token-12345"
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(
            device_id, "Test PC", "0.1.0", "win",
            self.cp.store.digest(token), time.time() + 3600, session_id
        )
        status = "CONNECTED" if connected else "DISCONNECTED"
        self.cp.store.set_session_status(device_id, session_id, generation, status, connected=connected)

        class MockWebSocket:
            def __init__(self):
                self.sent = []
                self.closed = False
                self.close_code = None
            async def send_json(self, frame):
                self.sent.append(frame)
            async def close(self, code=1000, reason=""):
                self.closed = True
                self.close_code = code

        ws = MockWebSocket()
        if connected:
            self.cp.sessions[device_id] = ws
            self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
            self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
            self.cp.connectivity[device_id] = ConnectivityGuard(self.cp.policy)
            self.cp.connectivity[device_id].state = "HEALTHY"
        return device_id, token, session_id, generation, ws

    async def test_1_successful_pairing_reaches_connected(self):
        poll = "poll-secret-1"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        session_id = str(uuid.uuid4())
        req = self.cp.pairing.create(
            PairRequest(device_name="PC1", poll_secret_hash=poll_hash, session_id=session_id),
            poll_hash, "127.0.0.1"
        )
        self.cp.pairing.approve(req["pairing_code"])
        status, result = self.cp.pairing.complete(req["request_id"], poll)
        self.assertEqual(status, 200)
        device_id = result["device_id"]
        token = result["device_token"]
        generation = result["generation"]
        self.assertTrue(self.cp.store.authenticate_device(device_id, token, session_id, generation))
        self.cp.store.set_session_status(device_id, session_id, generation, "CONNECTED", connected=True)
        devices = self.cp.store.list_devices({device_id})
        self.assertEqual(devices[0]["connection_status"], "CONNECTED")

    async def test_2_successful_pairing_creates_exactly_one_pairing_code(self):
        poll = "poll-secret-2"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        session_id = str(uuid.uuid4())
        req1 = self.cp.pairing.create(
            PairRequest(device_name="PC2", poll_secret_hash=poll_hash, session_id=session_id),
            poll_hash, "127.0.0.1"
        )
        req2 = self.cp.pairing.create(
            PairRequest(device_name="PC2", poll_secret_hash=poll_hash, session_id=session_id),
            poll_hash, "127.0.0.1"
        )
        self.assertEqual(req1["pairing_code"], req2["pairing_code"])
        self.assertEqual(req1["request_id"], req2["request_id"])
        self.assertEqual(len(self.cp.pairing.pending), 1)

    async def test_3_home_after_pairing_does_not_create_new_pairing(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        self.assertEqual(len(self.cp.pairing.pending), 0)
        home = await self.bot.process_update({"update_id": 1, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "🏠 Home"
        }})
        self.assertIn("CONNECTED", home.text)
        self.assertEqual(len(self.cp.pairing.pending), 0)
        self.assertIsNone(self.cp.pairing.get_pending())

    async def test_4_projects_after_pairing_does_not_create_new_pairing(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        self.bot.selected_devices[42] = device_id

        async def mock_call(user_id, cmd, *args, **kwargs):
            return device_id, {"items": [{"name": "P1", "absolute_path": "/p1", "discovery": "workspace"}]}

        with patch.object(self.bot, "_call_device", side_effect=mock_call):
            projects = await self.bot.process_update({"update_id": 2, "message": {
                "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "📁 Projects"
            }})
            self.assertIn("1 discovered project", projects.text)
        self.assertEqual(len(self.cp.pairing.pending), 0)

    async def test_5_devices_after_pairing_does_not_create_new_pairing(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        devices = await self.bot.process_update({"update_id": 3, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "🖥️ Devices"
        }})
        self.assertIn(device_id, devices.text)
        self.assertEqual(len(self.cp.pairing.pending), 0)

    async def test_6_settings_after_pairing_does_not_create_new_pairing(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        settings = await self.bot.process_update({"update_id": 4, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "⚙️ Settings"
        }})
        self.assertIn("Settings", settings.text)
        self.assertEqual(len(self.cp.pairing.pending), 0)

    async def test_7_all_normal_commands_reuse_active_session(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        self.assertTrue(self.cp.session_matches(device_id, session_id, generation))
        task = asyncio.create_task(self.cp.dispatch_command(device_id, "runtime.status", {}))
        await asyncio.sleep(0.01)
        req_id = list(self.cp.pending_responses.keys())[0]
        _, future = self.cp.pending_responses[req_id]
        context = self.cp.pending_context[req_id]
        self.assertEqual(context["session_id"], session_id)
        self.assertEqual(context["generation"], generation)
        future.set_result({"status": "success", "payload": {"device": device_id}})
        result = await task
        self.assertEqual(result["status"], "success")
        self.assertTrue(self.cp.session_matches(device_id, session_id, generation))

    async def test_8_explicit_pair_runtime_creates_pairing_only_when_needed(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        reply_connected = await self.bot.process_update({"update_id": 5, "callback_query": {
            "id": "cb-5", "from": {"id": 42}, "data": "action:pair_runtime",
            "message": {"message_id": 10, "chat": {"id": 42, "type": "private"}}
        }})
        self.assertIn("already connected", reply_connected.text)
        self.assertEqual(len(self.cp.pairing.pending), 0)

        self.cp.store.invalidate_session(device_id, session_id, generation, "STOPPED")
        self.cp.active_sessions.pop(device_id, None)
        self.cp.sessions.pop(device_id, None)
        poll = "poll-secret-8"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        req = self.cp.pairing.create(PairRequest(device_name="PC8", poll_secret_hash=poll_hash),
                                     poll_hash, "127.0.0.1")
        reply_pending = await self.bot.process_update({"update_id": 6, "callback_query": {
            "id": "cb-6", "from": {"id": 42}, "data": "action:pair_runtime",
            "message": {"message_id": 11, "chat": {"id": 42, "type": "private"}}
        }})
        self.assertIn(req["pairing_code"], reply_pending.text)
        self.assertIn("WAITING FOR PAIRING", reply_pending.text)

    async def test_9_explicit_repair_invalidates_current_session_first(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        target_btn = self.bot._target_button("🔄 Re-pair", 42, 42, "repair_execute", {"device_id": device_id})
        target_cb = target_btn["callback_data"]
        reply = await self.bot.process_update({"update_id": 7, "callback_query": {
            "id": "cb-7", "from": {"id": 42}, "data": target_cb,
            "message": {"message_id": 12, "chat": {"id": 42, "type": "private"}}
        }})
        self.assertIn("invalidated", reply.text.lower())
        self.assertFalse(self.cp.store.authenticate_device(device_id, token, session_id, generation))
        self.assertNotIn(device_id, self.cp.active_sessions)

    async def test_10_stop_session_invalidates_current_credential(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        stopped, _ = await self.cp.stop_session(device_id)
        self.assertTrue(stopped)
        row = self.cp.store._db.execute("SELECT token_hash, session_status FROM devices WHERE device_id=?", (device_id,)).fetchone()
        self.assertIsNone(row["token_hash"])
        self.assertEqual(row["session_status"], "STOPPED")

    async def test_11_stop_session_prevents_old_credential_reuse(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        await self.cp.stop_session(device_id)
        self.assertFalse(self.cp.store.authenticate_device(device_id, token, session_id, generation))

    async def test_12_fresh_pairing_required_after_stop_session(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        await self.cp.stop_session(device_id)
        self.assertFalse(self.cp.store.authenticate_device(device_id, token, session_id, generation))
        poll = "poll-secret-12"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        new_session_id = str(uuid.uuid4())
        req = self.cp.pairing.create(PairRequest(device_id=device_id, device_name="PC", poll_secret_hash=poll_hash, session_id=new_session_id),
                                     poll_hash, "127.0.0.1")
        self.cp.pairing.approve(req["pairing_code"])
        _, result = self.cp.pairing.complete(req["request_id"], poll)
        new_token = result["device_token"]
        new_gen = result["generation"]
        self.assertNotEqual(token, new_token)
        self.assertTrue(self.cp.store.authenticate_device(device_id, new_token, new_session_id, new_gen))

    async def test_13_duplicate_pair_request_reuses_current_pending_pairing(self):
        poll = "poll-secret-13"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        session_id = str(uuid.uuid4())
        r1 = self.cp.pairing.create(PairRequest(device_name="D1", poll_secret_hash=poll_hash, session_id=session_id), poll_hash, "127.0.0.1")
        r2 = self.cp.pairing.create(PairRequest(device_name="D1", poll_secret_hash=poll_hash, session_id=session_id), poll_hash, "127.0.0.1")
        self.assertEqual(r1["request_id"], r2["request_id"])
        self.assertEqual(r1["pairing_code"], r2["pairing_code"])

    async def test_14_old_telegram_callback_cannot_control_new_session(self):
        device_id, token, session_id_1, gen_1, ws1 = self._setup_paired_device()
        btn = self.bot._target_button("⛔ Stop Session", 42, 42, "stop_session_execute", {"device_id": device_id})
        await self.cp.stop_session(device_id)
        session_id_2 = str(uuid.uuid4())
        gen_2 = self.cp.store.register_device(device_id, "PC", "0.1", "win", self.cp.store.digest("token2"), time.time() + 3600, session_id_2)
        self.cp.store.set_session_status(device_id, session_id_2, gen_2, "CONNECTED", connected=True)
        self.cp.active_sessions[device_id] = {"session_id": session_id_2, "generation": gen_2}

        reply = await self.bot.process_update({"update_id": 8, "callback_query": {
            "id": "cb-8", "from": {"id": 42}, "data": btn["callback_data"],
            "message": {"message_id": 15, "chat": {"id": 42, "type": "private"}}
        }})
        self.assertIn("no longer active", reply.text.lower())
        self.assertTrue(self.cp.session_matches(device_id, session_id_2, gen_2))

    async def test_15_temporary_network_failure_does_not_automatically_create_new_pairing_session(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        self.cp.store.set_session_status(device_id, session_id, generation, "DISCONNECTED", connected=False)
        self.cp.sessions.pop(device_id, None)
        self.cp.active_sessions.pop(device_id, None)

        self.assertTrue(self.cp.store.authenticate_device(device_id, token, session_id, generation))
        self.assertEqual(len(self.cp.pairing.pending), 0)
        self.assertIsNone(self.cp.pairing.get_pending())

    async def test_16_session_expiry_requires_fresh_pairing(self):
        device_id = "AB-PC-EXP01"
        token = "expired-token"
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(device_id, "PC", "0.1", "win", self.cp.store.digest(token), time.time() - 10, session_id)
        self.assertFalse(self.cp.store.authenticate_device(device_id, token, session_id, generation))

    async def test_17_control_plane_restart_invalidates_according_to_policy(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        self.cp.store.close()
        restarted_cp = ControlPlane(self.db_path, "operator-token-that-is-long-enough")
        try:
            self.assertFalse(restarted_cp.store.authenticate_device(device_id, token, session_id, generation))
            row = restarted_cp.store._db.execute("SELECT token_hash FROM devices WHERE device_id=?", (device_id,)).fetchone()
            self.assertIsNone(row["token_hash"])
        finally:
            restarted_cp.store.close()
            self.cp = ControlPlane(self.db_path, "operator-token-that-is-long-enough")

    async def test_18_runtime_restart_creates_fresh_pairing(self):
        with tempfile.TemporaryDirectory() as rt_dir:
            store = StateStore(Path(rt_dir) / "state.sqlite3")
            store.set_device("gateway_token", "leftover-token")
            store.set_device("session_id", "leftover-session-id")
            store.close()
            rt = Runtime(rt_dir, "wss://127.0.0.1:9443/v1/devices/connect")
            try:
                self.assertEqual(rt.store.get_device("gateway_token"), "")
                self.assertEqual(rt.store.get_device("session_id"), "")
                self.assertIsNone(rt.connection.token)
                self.assertFalse(rt.connection.session_id)
            finally:
                rt.store.close()

    async def test_19_pairing_approval_credential_delivery_wss_connected_works(self):
        poll = "poll-secret-19"
        poll_hash = hashlib.sha256(poll.encode()).hexdigest()
        session_id = str(uuid.uuid4())
        req = self.cp.pairing.create(PairRequest(device_name="PC19", poll_secret_hash=poll_hash, session_id=session_id),
                                     poll_hash, "127.0.0.1")
        reply = await self.bot.process_update({"update_id": 9, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": f"/pair {req['pairing_code']}"
        }})
        self.assertIn("Pairing approved", reply)
        status, result = self.cp.pairing.complete(req["request_id"], poll)
        self.assertEqual(status, 200)
        token = result["device_token"]
        generation = result["generation"]
        device_id = result["device_id"]
        self.assertTrue(self.cp.store.authenticate_device(device_id, token, session_id, generation))
        self.cp.store.set_session_status(device_id, session_id, generation, "CONNECTED", connected=True)
        dev = self.cp.store.list_devices({device_id})[0]
        self.assertEqual(dev["connection_status"], "CONNECTED")

    async def test_20_no_generic_command_handler_can_invoke_pairing_creation_implicitly(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device(connected=False)
        self.bot.selected_devices[42] = device_id
        projects_reply = await self.bot.process_update({"update_id": 10, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "📁 Projects"
        }})
        self.assertIn("disconnected", projects_reply.text.lower())
        home_reply = await self.bot.process_update({"update_id": 11, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "🏠 Home"
        }})
        self.assertIn("NOT CONNECTED", home_reply.text)
        self.assertEqual(len(self.cp.pairing.pending), 0)
        self.assertIsNone(self.cp.pairing.get_pending())


class Phase25ProjectListTimeoutRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "operator-token-that-is-long-enough")
        self.bot = TelegramBot(self.cp, "test-bot-token", {42})

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    def _setup_paired_device(self, device_id="AB-PC-ABC123", connected=True):
        token = "test-device-token-12345"
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(
            device_id, "Test PC", "0.1.0", "win",
            self.cp.store.digest(token), time.time() + 3600, session_id
        )
        status = "CONNECTED" if connected else "DISCONNECTED"
        self.cp.store.set_session_status(device_id, session_id, generation, status, connected=connected)

        class MockWebSocket:
            def __init__(self):
                self.sent = []
                self.closed = False
                self.close_code = None
            async def send_json(self, frame):
                self.sent.append(frame)
            async def close(self, code=1000, reason=""):
                self.closed = True
                self.close_code = code

        ws = MockWebSocket()
        if connected:
            self.cp.sessions[device_id] = ws
            self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
            self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
            self.cp.connectivity[device_id] = ConnectivityGuard(self.cp.policy)
            self.cp.connectivity[device_id].state = "HEALTHY"
        return device_id, token, session_id, generation, ws

    async def test_over_120_projects_produce_response_under_32kb_with_total_count(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            runtime = Runtime(temp_dir.name)
            mock_projects = [
                {
                    "name": f"App_{i}",
                    "absolute_path": f"D:/workspace/nested/app_{i}",
                    "relative_path": f"nested/app_{i}",
                    "is_git_repository": True,
                    "has_package_json": True,
                    "has_requirements": False,
                    "has_pyproject": False,
                    "has_docker": False,
                    "discovery": "project",
                    "discovery_score": 10,
                }
                for i in range(130)
            ]
            runtime.project_index._items = list(mock_projects)
            runtime.project_index.last_refresh_at = time.time()

            response = await runtime.handle_remote_command("project.list", {})
            self.assertEqual(response["total_count"], 130)
            self.assertEqual(len(response["items"]), 50)
            self.assertEqual(len(runtime.project_index.snapshot()["items"]), 130)

            serialized = json.dumps(response).encode("utf-8")
            self.assertLess(len(serialized), 32768)

            refresh_resp = await runtime.handle_remote_command("project.refresh", {})
            self.assertEqual(refresh_resp["total_count"], 130)
            self.assertEqual(len(refresh_resp["items"]), 50)
            serialized_refresh = json.dumps(refresh_resp).encode("utf-8")
            self.assertLess(len(serialized_refresh), 32768)
        finally:
            runtime.store.close()
            temp_dir.cleanup()

    async def test_pending_project_list_fails_immediately_on_device_disconnect(self):
        device_id = "AB-PC-FAIL01"
        token = "test-token-fail01"
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(
            device_id, "Disc PC", "0.1.0", "win",
            self.cp.store.digest(token), time.time() + 3600, session_id
        )
        disconnect_event = asyncio.Event()

        class MockSocket:
            def __init__(self):
                self.headers = {
                    "x-agentbridge-device-id": device_id,
                    "authorization": f"Bearer {token}",
                    "x-agentbridge-session-id": session_id,
                    "x-agentbridge-generation": str(generation),
                }
                self.accepted = False
                self.closed = False
            async def accept(self):
                self.accepted = True
            async def receive_text(self):
                await disconnect_event.wait()
                raise WebSocketDisconnect(code=1001)
            async def send_json(self, frame):
                pass
            async def close(self, code=1000, reason=""):
                self.closed = True

        mock_ws = MockSocket()
        handler_task = asyncio.create_task(self.cp.handle_device_socket(mock_ws))
        await asyncio.sleep(0.02)
        self.assertTrue(mock_ws.accepted)
        self.assertIs(self.cp.sessions.get(device_id), mock_ws)

        dispatch_task = asyncio.create_task(self.cp.dispatch_command(device_id, "project.list", {}))
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.cp.pending_responses), 1)

        t0 = time.perf_counter()
        disconnect_event.set()
        await handler_task

        with self.assertRaises(HTTPException) as cm:
            await dispatch_task
        elapsed = time.perf_counter() - t0
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("device disconnected", cm.exception.detail)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(self.cp.pending_responses), 0)

    async def test_projects_telegram_flow_succeeds_with_large_d_root(self):
        device_id, token, session_id, generation, ws = self._setup_paired_device()
        self.bot.selected_devices[42] = device_id

        mock_items = [
            {
                "name": f"Project_{i:03d}",
                "absolute_path": f"D:/workspace/proj_{i:03d}",
                "relative_path": f"proj_{i:03d}",
                "is_git_repository": True,
                "discovery": "project" if i < 54 else "candidate",
                "discovery_score": 10 if i < 54 else 3,
            }
            for i in range(113)
        ]
        bounded_response = {
            "items": mock_items[:50],
            "total_count": len(mock_items),
            "refreshing": False,
            "last_refresh_at": time.time(),
            "last_refresh_duration_ms": 15.0,
            "error": None,
        }

        async def mock_call_device(user_id, command, payload=None, **kwargs):
            if command == "project.list":
                return device_id, bounded_response
            raise ValueError(f"unexpected command: {command}")

        self.bot._call_device = mock_call_device
        reply = await self.bot.process_update({"update_id": 99, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "📁 Projects"
        }})

        self.assertIn("113 discovered project(s).", reply.text)
        self.assertNotIn("Request timed out", reply.text)
        self.assertNotIn("Runtime is currently disconnected", reply.text)
        buttons = [b for row in reply.inline_keyboard for b in row if "Project_" in b["text"]]
        self.assertEqual(len(buttons), 30)

    def test_discovery_safe_candidate_cap(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(temp_dir.name) / "root"
            root.mkdir()
            for i in range(115):
                f = root / f"cand_{i:03d}"
                f.mkdir()
                (f / "x.py").write_text("a=1", encoding="utf-8")
                (f / "y.py").write_text("b=2", encoding="utf-8")
            for i in range(5):
                p = root / f"real_{i:03d}"
                p.mkdir()
                (p / ".git").mkdir()
                (p / "package.json").write_text("{}", encoding="utf-8")

            results = discover_projects([root])
            candidates = [r for r in results if r["discovery"] == "candidate"]
            projects = [r for r in results if r["discovery"] == "project"]
            self.assertEqual(len(projects), 5)
            self.assertEqual(len(candidates), 100)
            self.assertEqual(len(results), 105)
        finally:
            temp_dir.cleanup()


class Phase26PerformanceCachingAndHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "operator-token-that-is-long-enough")
        self.bot = TelegramBot(self.cp, "test-bot-token", {42})

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    def _setup_mock_device(self, device_id="AB-PC-P26001", connected=True):
        token = "test-device-token-p26"
        session_id = str(uuid.uuid4())
        generation = self.cp.store.register_device(
            device_id, "P26 PC", "0.1.0", "win",
            self.cp.store.digest(token), time.time() + 3600, session_id
        )
        status = "CONNECTED" if connected else "DISCONNECTED"
        self.cp.store.set_session_status(device_id, session_id, generation, status, connected=connected)

        class MockWebSocket:
            def __init__(self, cp):
                self.cp = cp
                self.sent = []
                self.closed = False
                self.close_code = None

            async def send_json(self, frame):
                self.sent.append(frame)
                req_id = frame.get("request_id")
                cmd = frame.get("command")
                if req_id and req_id in self.cp.pending_responses:
                    if cmd == "project.list":
                        payload = {"items": [{"name": f"MockApp_{device_id}", "absolute_path": f"D:/{device_id}/MockApp", "discovery": "project"}], "total_count": 1}
                    elif cmd == "runtime.status":
                        payload = {"device": device_id, "connection": "CONNECTED", "runtime_version": "0.1.0", "workspaces": 1}
                    elif cmd == "workspace.list":
                        payload = {"items": ["D:/MockApp"]}
                    elif cmd == "workspace.add":
                        payload = {"items": ["D:/MockApp", "D:/NewApp"], "added": "D:/NewApp"}
                    elif cmd == "project.refresh":
                        payload = {"refreshing": False, "items": [{"name": "MockApp", "absolute_path": "D:/MockApp"}], "total_count": 1}
                    else:
                        payload = {}
                    
                    asyncio.get_running_loop().call_soon(
                        self._resolve_pending, req_id, frame.get("device_id"), frame.get("session_id"),
                        frame.get("generation"), frame.get("trace_id"), payload
                    )

            def _resolve_pending(self, req_id, dev_id, sess_id, gen, trace_id, payload):
                if req_id in self.cp.pending_responses:
                    _, fut = self.cp.pending_responses[req_id]
                    if not fut.done():
                        fut.set_result({
                            "type": "response", "request_id": req_id, "device_id": dev_id,
                            "session_id": sess_id, "generation": gen, "trace_id": trace_id,
                            "status": "success", "payload": payload, "runtime_processing_ms": 1.2
                        })


            async def close(self, code=1000, reason=""):
                self.closed = True
                self.close_code = code

        ws = MockWebSocket(self.cp)
        if connected:
            self.cp.sessions[device_id] = ws
            self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": generation}
            self.cp.last_heartbeat_monotonic[device_id] = time.monotonic()
            self.cp.connectivity[device_id] = ConnectivityGuard(self.cp.policy)
            self.cp.connectivity[device_id].state = "HEALTHY"
        return device_id, token, session_id, generation, ws


    async def test_1_cache_hit_and_miss(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device()
        # First call is a cache miss
        r1 = await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)
        self.assertNotIn("cached", r1)
        self.assertEqual(r1["payload"]["items"][0]["name"], f"MockApp_{device_id}")

        # Second call is a cache hit
        r2 = await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)  # No new WSS frame sent
        self.assertTrue(r2.get("cached"))
        self.assertEqual(r1["payload"], r2["payload"])

        summary = self.cp.metrics.summary()
        self.assertIn("cache_hit", summary)
        self.assertIn("cache_miss", summary)
        self.assertIn("cache_lookup_ms", summary)
        self.assertIn("control_plane_dispatch_ms", summary)

    async def test_2_cache_ttl_expiration(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device()
        # Use short TTL on cache
        from sutra.cache import make_cache_key
        cache_key = make_cache_key(device_id, session_id, generation, "project.list", {})
        self.cp.cache.set(cache_key, {"status": "success", "payload": {"items": []}}, ttl_seconds=0.04)
        
        # Immediate read: hit
        self.assertIsNotNone(self.cp.cache.get(cache_key))
        await asyncio.sleep(0.06)
        # Post-TTL read: expired / miss
        self.assertIsNone(self.cp.cache.get(cache_key))

    async def test_3_invalidation_on_workspace_add_and_project_refresh(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device()
        # Populate project.list cache
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)

        # Confirm it's cached
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)

        # project.refresh must invalidate project.list
        await self.cp.dispatch_command(device_id, "project.refresh", {})
        self.assertEqual(len(ws.sent), 2)

        # Next project.list must be a cache miss and send a new frame
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 3)

        # workspace.add must invalidate project.list and workspace.list
        await self.cp.dispatch_command(device_id, "workspace.add", {"path": "D:/NewApp"})
        self.assertEqual(len(ws.sent), 4)

        # project.list is again a miss and sends frame 5
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 5)

    async def test_4_concurrent_identical_requests_share_one_operation(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device()
        # 5 identical concurrent project.list requests
        results = await asyncio.gather(*(self.cp.dispatch_command(device_id, "project.list", {}) for _ in range(5)))
        # Only 1 WSS frame sent!
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(len(results), 5)
        for res in results:
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["payload"]["total_count"], 1)

        summary = self.cp.metrics.summary()
        self.assertIn("single_flight_coalesced", summary)
        self.assertEqual(summary["single_flight_coalesced"]["count"], 4)

    async def test_5_different_devices_do_not_share_results(self):
        dev_a, _, _, _, ws_a = self._setup_mock_device("AB-PC-DEVAAA")
        dev_b, _, _, _, ws_b = self._setup_mock_device("AB-PC-DEVBBB")

        res_a = await self.cp.dispatch_command(dev_a, "project.list", {})
        res_b = await self.cp.dispatch_command(dev_b, "project.list", {})

        self.assertEqual(len(ws_a.sent), 1)
        self.assertEqual(len(ws_b.sent), 1)
        self.assertIn("DEVAAA", res_a["payload"]["items"][0]["name"])
        self.assertIn("DEVBBB", res_b["payload"]["items"][0]["name"])

    async def test_6_different_sessions_do_not_share_results(self):
        device_id, token, session_id_1, gen_1, ws1 = self._setup_mock_device("AB-PC-SESS01")
        res1 = await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws1.sent), 1)

        # Connect a new session for the same device
        session_id_2 = str(uuid.uuid4())
        gen_2 = self.cp.store.register_device(
            device_id, "P26 PC", "0.1.0", "win",
            self.cp.store.digest("new-token"), time.time() + 3600, session_id_2
        )
        self.cp.store.set_session_status(device_id, session_id_2, gen_2, "CONNECTED", connected=True)
        self.cp.active_sessions[device_id] = {"session_id": session_id_2, "generation": gen_2}

        # Query under session 2: must NOT hit session 1 cache!
        res2 = await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws1.sent), 2)
        self.assertNotIn("cached", res2)

    async def test_7_generation_changes_invalidate_cache_and_in_flight(self):
        device_id, token, session_id, gen_1, ws = self._setup_mock_device("AB-PC-GEN001")
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)

        # Increment generation
        gen_2 = gen_1 + 1
        self.cp.store.set_session_status(device_id, session_id, gen_2, "CONNECTED", connected=True)
        self.cp.active_sessions[device_id] = {"session_id": session_id, "generation": gen_2}

        # Must send new frame because generation changed
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 2)

    async def test_8_stop_session_invalidates_cache_and_in_flight_state(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device("AB-PC-STOP01")
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)

        # Stop session
        await self.cp.stop_session(device_id)
        # Cache must be wiped
        self.assertEqual(self.cp.cache.get(f"{device_id}:{session_id}:{generation}:project.list:"), None)
        # Further dispatch must raise 409
        with self.assertRaises(HTTPException) as cm:
            await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(cm.exception.status_code, 409)

    async def test_9_repair_invalidates_relevant_state(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device("AB-PC-REPR01")
        await self.cp.dispatch_command(device_id, "project.list", {})
        self.assertEqual(len(ws.sent), 1)

        btn = self.bot._target_button("🔄 Re-pair", 42, 42, "repair_execute", {"device_id": device_id})
        await self.bot.process_update({"update_id": 88, "callback_query": {
            "id": "cb_rep", "from": {"id": 42}, "data": btn["callback_data"],
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}
        }})
        # Cache cleared and session stopped
        self.assertEqual(len(self.cp.cache._entries), 0)

    async def test_10_failed_in_flight_operation_cleans_up_correctly(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device("AB-PC-FAILSF")

        # Override send_json to not respond so it times out or disconnects
        async def failing_send(frame):
            ws.sent.append(frame)
            req_id = frame.get("request_id")
            # Fail the future with an exception
            if req_id in self.cp.pending_responses:
                _, fut = self.cp.pending_responses[req_id]
                fut.set_exception(HTTPException(409, "device disconnected"))
        ws.send_json = failing_send

        tasks = [asyncio.create_task(self.cp.dispatch_command(device_id, "project.list", {})) for _ in range(3)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # All callers receive the 409 exception
        for r in results:
            self.assertIsInstance(r, HTTPException)
            self.assertEqual(r.status_code, 409)
        # Single-flight table is empty
        self.assertEqual(self.cp.single_flight.active_count(), 0)

    async def test_11_home_and_projects_flow_avoids_duplicate_calls(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device("AB-PC-FLOW01")
        self.bot.selected_devices[42] = device_id

        # Visit Home screen (calls project.list over WSS)
        home = await self.bot.process_update({"update_id": 1, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "🏠 Home"
        }})
        self.assertIn("Projects: 1", home.text)
        self.assertEqual(len(ws.sent), 1)

        # Tap "Projects" button (now hits cache!)
        btn = self.bot._button("📁 Projects", "nav:projects")
        proj = await self.bot.process_update({"update_id": 2, "callback_query": {
            "id": "cb_p", "from": {"id": 42}, "data": btn["callback_data"],
            "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}}
        }})
        self.assertIn("1 discovered project(s).", proj.text)
        # Frame count remains 1! No duplicate WSS round-trip!
        self.assertEqual(len(ws.sent), 1)

    async def test_12_deterministic_performance_comparison(self):
        device_id, token, session_id, generation, ws = self._setup_mock_device("AB-PC-PERF01")
        N = 10
        t0 = time.perf_counter()
        results = await asyncio.gather(*(self.cp.dispatch_command(device_id, "project.list", {}) for _ in range(N)))
        elapsed_s = time.perf_counter() - t0

        self.assertEqual(len(results), N)
        # Exactly 1 WSS frame sent to the runtime for all N callers!
        self.assertEqual(len(ws.sent), 1)
        self.assertLess(elapsed_s, 0.5)


if __name__ == "__main__":
    unittest.main()

