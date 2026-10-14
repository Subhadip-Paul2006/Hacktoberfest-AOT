from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from sutra.connection import ConnectionManager
from sutra.connectivity import ConnectivityGuard, ConnectivityPolicy
from sutra.discovery import discover_projects
from sutra.latency import LatencyMetrics
from sutra.pairing import PairingManager
from sutra.project_index import ProjectIndex
from sutra.runtime import Runtime
from sutra.security import is_path_allowed, require_allowed, validate_workspace_root
from sutra.state import StateStore

class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = StateStore(Path(self.temp.name) / "state.sqlite3"); self.pairing = PairingManager(self.store)
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def test_valid_pair_creates_separate_persistent_identity_and_token(self):
        code = self.pairing.display_code; device_id = self.pairing.pair(code, "devbox")
        self.assertRegex(device_id, r"^AB-PC-[A-F0-9]{6}$"); self.assertEqual(device_id, self.store.get_device("device_id"))
        self.assertTrue(self.store.get_device("device_token")); self.assertNotEqual(code.replace("-", ""), self.store.get_device("device_token")); self.assertEqual("", self.pairing._code)
    def test_invalid_and_reused_pair_fail(self):
        with self.assertRaises(PermissionError): self.pairing.pair("XXXXXXXX")
        code = self.pairing.display_code; self.pairing.pair(code)
        with self.assertRaises(PermissionError): self.pairing.pair(code)
    def test_expired_pair_fails(self):
        code = self.pairing.display_code
        with patch("sutra.pairing.time.monotonic", return_value=self.pairing._expires_at + 1):
            with self.assertRaises(PermissionError): self.pairing.pair(code)

class WorkspaceTests(unittest.TestCase):
    def setUp(self): self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name) / "Projects"; self.root.mkdir()
    def tearDown(self): self.temp.cleanup()
    def test_registered_root_and_children_allowed_outside_and_traversal_denied(self):
        project = self.root / "NETRA"; project.mkdir()
        self.assertTrue(is_path_allowed(self.root, [self.root])); self.assertTrue(is_path_allowed(project / "src", [self.root]))
        self.assertFalse(is_path_allowed(self.root.parent / "other", [self.root])); self.assertFalse(is_path_allowed(self.root / ".." / "other", [self.root]))
        self.assertFalse(is_path_allowed(self.root / "%252e%252e" / "other", [self.root]))
        deeply_encoded = "%252525252525252e%252525252525252e"
        self.assertFalse(is_path_allowed(str(self.root) + "\\" + deeply_encoded + "\\outside", [self.root]))
        with self.assertRaises(PermissionError): require_allowed(self.root.parent, [self.root])
        self.assertEqual(self.root.resolve(), validate_workspace_root(self.root))
        with self.assertRaises(ValueError): validate_workspace_root(Path(self.root.anchor))
        if os.name == "nt":
            self.assertTrue(is_path_allowed(str(project / "src").upper(), [self.root]))
            self.assertTrue(is_path_allowed(str(project) + ".", [self.root]))
    def test_symlink_escape_denied(self):
        outside = Path(self.temp.name) / "outside"; outside.mkdir(); link = self.root / "escape"
        try: link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError): self.skipTest("symlink creation is unavailable")
        self.assertFalse(is_path_allowed(link / "secret.txt", [self.root]))
    def test_only_registered_workspace_scanned_and_git_detected(self):
        git = self.root / "GitProject"; git.mkdir(); (git / ".git").mkdir()
        plain = self.root / "Plain"; plain.mkdir(); (plain / "requirements.txt").write_text("")
        outside = Path(self.temp.name) / "Outside"; outside.mkdir(); (outside / ".git").mkdir()
        results = discover_projects([self.root]); self.assertEqual({"GitProject", "Plain"}, {p["name"] for p in results})
        self.assertTrue(next(p for p in results if p["name"] == "GitProject")["is_git_repository"])
        self.assertFalse(next(p for p in results if p["name"] == "Plain")["is_git_repository"])

    def test_house_price_project_hides_internal_directories_and_marks_ambiguous_candidates(self):
        project = self.root / "HousePricePredictor"
        project.mkdir()
        (project / ".git").mkdir()
        for name in ("data", "models", "notebooks", "screenshots", "test_dataset01", "test_dataset02", "venv"):
            internal = project / name
            internal.mkdir()
            (internal / "requirements.txt").write_text("", encoding="utf-8")
        ambiguous = self.root / "PossibleTool"
        ambiguous.mkdir()
        (ambiguous / "README.md").write_text("notes", encoding="utf-8")
        (ambiguous / "src").mkdir()
        deep_parent = self.root
        for index in range(5):
            deep_parent = deep_parent / f"nested{index}"
            deep_parent.mkdir()
        deep_project = deep_parent / "HiddenDeepProject"
        deep_project.mkdir()
        (deep_project / "requirements.txt").write_text("", encoding="utf-8")
        results = discover_projects([self.root])
        by_name = {item["name"]: item for item in results}
        self.assertIn("HousePricePredictor", by_name)
        self.assertEqual("project", by_name["HousePricePredictor"]["discovery"])
        self.assertEqual("candidate", by_name["PossibleTool"]["discovery"])
        self.assertEqual({"HousePricePredictor", "PossibleTool"}, set(by_name))

class RuntimeTests(unittest.TestCase):
    def test_health_and_loopback_only(self):
        with tempfile.TemporaryDirectory() as home:
            runtime = Runtime(home); status = runtime.status(); self.assertEqual("healthy", status["status"]); self.assertEqual("disconnected", status["connection"])
            runtime.store.close()
            with self.assertRaises(ValueError): Runtime(home, host="0.0.0.0")
    def test_connection_disconnected_shutdown_and_secure_url_validation(self):
        async def run_checks():
            conn = ConnectionManager("", None, None); await conn.run(); self.assertEqual("DISCONNECTED", conn.state)
            await conn.stop(); self.assertEqual("SHUTTING_DOWN", conn.state)
            bad = ConnectionManager("ws://example.test", "t", "d")
            with self.assertRaises(ValueError): await bad.run()
        asyncio.run(run_checks())

    def test_heartbeat_sent_when_connected_and_stops_cleanly(self):
        async def run_check():
            conn = ConnectionManager("wss://gateway.example/ws", "secret", "AB-PC-123456")
            sent = []
            class FakeSocket:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return False
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration
                async def send(self, message):
                    sent.append(json.loads(message)); await conn.stop()
            fake_module = SimpleNamespace(connect=lambda *args, **kwargs: FakeSocket())
            with patch.dict(sys.modules, {"websockets": fake_module}): await conn.run()
            self.assertEqual("SHUTTING_DOWN", conn.state)
            self.assertEqual("heartbeat", sent[0]["type"])
            self.assertIsNotNone(conn.last_heartbeat)
        asyncio.run(run_check())

class ConnectivityAndIndexTests(unittest.TestCase):
    def test_connectivity_guard_uses_hysteresis_and_reports_unknown_throughput(self):
        policy = ConnectivityPolicy(100, 200, 10, 1, 2, 1, 3, 3)
        guard = ConnectivityGuard(policy)
        self.assertEqual("DEGRADED", guard.record_success(250, 1.0))
        self.assertEqual("DEGRADED", guard.record_success(250, 2.0))
        self.assertEqual("UNUSABLE", guard.record_success(250, 3.0))
        self.assertFalse(guard.allows_request())
        self.assertEqual("RECOVERING", guard.record_success(50, 4.0))
        self.assertEqual("RECOVERING", guard.record_success(50, 5.0))
        self.assertEqual("HEALTHY", guard.record_success(50, 6.0))
        self.assertEqual("UNKNOWN", guard.snapshot()["throughput"])
        self.assertEqual(50.0, guard.snapshot()["rtt_ms"])

    def test_project_refresh_runs_in_background_and_keeps_cached_snapshot_available(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_discovery(roots):
            entered.set()
            release.wait(2)
            return [{"name": "CachedProject", "absolute_path": str(roots[0])}]

        async def check():
            index = ProjectIndex([Path("C:/fixture")])
            with patch("sutra.project_index.discover_projects", side_effect=slow_discovery):
                started = time.perf_counter()
                self.assertTrue(index.schedule_refresh())
                snapshot = index.snapshot()
                self.assertLess(time.perf_counter() - started, 0.1)
                self.assertTrue(snapshot["refreshing"])
                self.assertEqual([], snapshot["items"])
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertEqual([], index.snapshot()["items"])
                release.set()
                await index.wait_for_refresh()
            self.assertEqual("CachedProject", index.snapshot()["items"][0]["name"])
            self.assertGreaterEqual(index.snapshot()["last_refresh_duration_ms"], 0)

        asyncio.run(check())

    def test_latency_metrics_reports_observed_percentiles(self):
        metrics = LatencyMetrics()
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            metrics.record("wss", value)
        self.assertEqual({"count": 5, "p50_ms": 3.0, "p95_ms": 5.0, "p99_ms": 5.0},
                         metrics.summary()["wss"])

class RuntimeSessionTests(unittest.TestCase):
    def test_transport_failure_requires_fresh_pairing_instead_of_reusing_credential(self):
        async def run_check():
            conn = ConnectionManager("wss://gateway.example/ws", "secret", "AB-PC-123456", max_backoff=1)
            calls = 0
            class FakeSocket:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return False
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration
                async def send(self, message): await conn.stop()
            def connect(*args, **kwargs):
                nonlocal calls
                calls += 1
                raise OSError("temporary disconnect")
            with patch.dict(sys.modules, {"websockets": SimpleNamespace(connect=connect)}): await conn.run()
            self.assertEqual(1, calls)
            self.assertIsNone(conn.token)
            self.assertEqual("WAITING_FOR_PAIRING", conn.state)
        asyncio.run(run_check())

    def test_loopback_http_health_and_one_time_pairing(self):
        with tempfile.TemporaryDirectory() as home:
            runtime = Runtime(home, port=0)
            runtime.start_http()
            assert runtime.server
            base = f"http://127.0.0.1:{runtime.server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/health") as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual("healthy", json.load(response)["status"])
                request = urllib.request.Request(base + "/pair", data=json.dumps({"code": runtime.pairing.display_code}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
                    self.assertTrue(result["paired"])
                self.assertIsNone(runtime.pairing)
                self.assertTrue(runtime.store.get_device("device_token"))
            finally:
                runtime.server.shutdown(); runtime.server.server_close(); runtime.store.close()

if __name__ == "__main__": unittest.main()
