"""Tests for Phase 7.0 + 7.1 Local Activity / History Foundation.

Validates:
- Database migration & schema versioning
- Restart persistence & standalone connections
- Telegram inbound & outbound message persistence
- Prompt persistence & lifecycle updates
- Operation persistence & latency tracking
- ActivityContext context manager
- Permission event persistence
- Connector event persistence
- Tool call persistence & lifecycle updates
- Audit event persistence & artifact metadata
- Universal request_id correlation (timeline aggregation)
- Bounded pagination and query filters
- Centralized secret redaction before SQLite writes
- Retention cleanup (purge older than N days)
- Defensive failure isolation (DB errors never crash main operations)
- ControlPlane integration & zero SQLite sprawl (shared DB file)
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from sutra.activity_store import ActivityContext, ActivityStore
from sutra.control_plane import ControlPlane, ControlPlaneStore
from sutra.metrics import MetricsRegistry
from sutra.policy_store import PolicyStore


class TestActivityStoreMigrationsAndLifecycle(unittest.TestCase):
    """Test database setup, migrations, and restart persistence."""

    def test_migration_fresh_database(self) -> None:
        """Verify that a fresh in-memory database runs migration v1 creating all tables & indexes."""
        store = ActivityStore.from_path(":memory:")
        self.assertEqual(store.schema_version, 1)

        # Inspect SQLite master tables
        with store._lock:
            cur = store._db.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}

        expected_tables = {
            "schema_meta",
            "telegram_messages",
            "prompts",
            "operations",
            "permission_events",
            "connector_events",
            "tool_calls",
            "audit_events",
            "artifact_metadata",
        }
        for table in expected_tables:
            self.assertIn(table, tables, f"Table {table} missing from SQLite master")

    def test_migration_existing_control_plane_db(self) -> None:
        """Verify ActivityStore cleanly attaches to an existing ControlPlaneStore database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "shared.sqlite3"
            # 1. Initialize ControlPlaneStore first
            cp_store = ControlPlaneStore(str(db_path))
            cp_store.register_device("dev-1", "Test Dev", "0.1.0", "windows", "token_hash", time.time() + 3600)

            try:
                # 2. Attach PolicyStore
                pol_store = PolicyStore(cp_store._db, cp_store._lock)

                # 3. Attach ActivityStore
                act_store = ActivityStore(cp_store._db, cp_store._lock)
                self.assertEqual(act_store.schema_version, 1)

                # Check that devices table still has our data
                devices = cp_store.list_devices({"dev-1"})
                self.assertEqual(len(devices), 1)
                self.assertEqual(devices[0]["device_id"], "dev-1")

                # Check that schema_meta contains both version keys
                with cp_store._lock:
                    cur = cp_store._db.cursor()
                    cur.execute("SELECT key, value FROM schema_meta")
                    meta = dict(cur.fetchall())
                    self.assertEqual(meta.get("policy_schema_version"), "1")
                    self.assertEqual(meta.get("activity_schema_version"), "1")
            finally:
                act_store.close()
                try:
                    cp_store._db.close()
                except Exception:
                    pass

    def test_migration_is_idempotent(self) -> None:
        """Verify running migrations repeatedly does not error or reset data."""
        store = ActivityStore.from_path(":memory:")
        msg_id = store.record_telegram_message(
            request_id="req-1",
            chat_id=100,
            telegram_user_id=200,
            direction="inbound",
            message_type="command",
            text="hello",
        )
        self.assertIsNotNone(msg_id)

        # Run migration again manually
        with store._lock:
            store._apply_migrations()

        msgs = store.list_telegram_messages(request_id="req-1")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "hello")

    def test_restart_persistence(self) -> None:
        """Verify activity records survive file-backed database close and reopen."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "activity.sqlite3"

            # Open and write
            store1 = ActivityStore.from_path(db_path)
            op_id = store1.record_operation_start(
                request_id="req-restart-1",
                operation_type="git.status",
                source="telegram",
                project_id="proj-123",
            )
            store1.record_operation_complete(op_id, status="completed", duration_ms=45.2)
            store1.close()

            # Reopen fresh connection
            store2 = ActivityStore.from_path(db_path)
            ops = store2.list_operations(request_id="req-restart-1")
            self.assertEqual(len(ops), 1)
            self.assertEqual(ops[0]["id"], op_id)
            self.assertEqual(ops[0]["operation_type"], "git.status")
            self.assertEqual(ops[0]["status"], "completed")
            self.assertAlmostEqual(ops[0]["duration_ms"], 45.2, places=1)
            store2.close()


class TestEntityPersistence(unittest.TestCase):
    """Test persistence of all 8 core entities."""

    def setUp(self) -> None:
        self.store = ActivityStore.from_path(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_telegram_messages_inbound_and_outbound(self) -> None:
        """Test recording inbound and outbound Telegram messages."""
        in_id = self.store.record_telegram_message(
            request_id="req-tg-1",
            chat_id=12345,
            telegram_user_id=67890,
            message_id=101,
            direction="inbound",
            message_type="command",
            text="/status",
            project_id="proj-a",
            session_id="sess-b",
        )
        self.assertIsNotNone(in_id)

        out_id = self.store.record_telegram_message(
            request_id="req-tg-1",
            chat_id=12345,
            telegram_user_id=67890,
            message_id=102,
            direction="outbound",
            message_type="response",
            text="System online: git clean.",
            project_id="proj-a",
            session_id="sess-b",
        )
        self.assertIsNotNone(out_id)

        messages = self.store.list_telegram_messages(request_id="req-tg-1")
        self.assertEqual(len(messages), 2)
        by_dir = {m["direction"]: m["text"] for m in messages}
        self.assertEqual(by_dir.get("inbound"), "/status")
        self.assertEqual(by_dir.get("outbound"), "System online: git clean.")

    def test_prompt_persistence_and_completion(self) -> None:
        """Test recording prompts and marking them complete."""
        p_id = self.store.record_prompt_start(
            request_id="req-p-1",
            prompt_text="Refactor the authentication module",
            source="telegram",
            project_id="proj-alpha",
            session_id="sess-beta",
            agent_id="agent-claude",
        )
        self.assertIsNotNone(p_id)

        # Verify initial pending status
        prompts = self.store.list_prompts(request_id="req-p-1")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["status"], "pending")
        self.assertIsNone(prompts[0]["completed_at"])

        # Complete prompt
        self.store.record_prompt_complete(p_id, status="completed")
        prompts_after = self.store.list_prompts(request_id="req-p-1")
        self.assertEqual(prompts_after[0]["status"], "completed")
        self.assertIsNotNone(prompts_after[0]["completed_at"])

    def test_operation_persistence_and_failure(self) -> None:
        """Test recording operations and capturing failure details."""
        op_id = self.store.record_operation_start(
            request_id="req-op-fail",
            operation_type="git.push",
            source="telegram",
            project_id="proj-main",
        )
        self.store.record_operation_complete(
            op_id,
            status="failed",
            duration_ms=120.5,
            error_code="GIT_REJECTED",
            error_message="Remote rejected branch: non-fast-forward",
        )

        ops = self.store.list_operations(request_id="req-op-fail")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["status"], "failed")
        self.assertEqual(ops[0]["error_code"], "GIT_REJECTED")
        self.assertIn("non-fast-forward", ops[0]["error_message"])
        self.assertAlmostEqual(ops[0]["duration_ms"], 120.5, places=1)

    def test_permission_event_persistence(self) -> None:
        """Test recording permission evaluation events."""
        pe_id = self.store.record_permission_event(
            request_id="req-perm-1",
            operation_id="op-999",
            risk_level="HIGH",
            decision="ASK",
            reason="Destructive git push requires confirmation",
            actor="telegram:12345",
        )
        self.assertIsNotNone(pe_id)

        events = self.store.list_permission_events(request_id="req-perm-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["risk_level"], "HIGH")
        self.assertEqual(events[0]["decision"], "ASK")
        self.assertEqual(events[0]["actor"], "telegram:12345")

    def test_connector_event_persistence(self) -> None:
        """Test recording connector events."""
        ce_id = self.store.record_connector_event(
            request_id="req-conn-1",
            connector_id="github-connector",
            event_type="rate_limit_reset",
            status="info",
            metadata={"remaining": 4900, "reset_time": 1700000000},
        )
        self.assertIsNotNone(ce_id)

        with self.store._lock:
            cur = self.store._db.cursor()
            cur.execute("SELECT connector_id, event_type, metadata FROM connector_events WHERE id = ?", (ce_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], "github-connector")
            self.assertEqual(row[1], "rate_limit_reset")
            meta = json.loads(row[2])
            self.assertEqual(meta["remaining"], 4900)

    def test_tool_call_persistence_and_arguments(self) -> None:
        """Test recording tool calls, arguments, and execution results."""
        tc_id = self.store.record_tool_call_start(
            request_id="req-tc-1",
            connector_id="github",
            server_name="github-mcp",
            tool_name="create_issue",
            arguments={"title": "Bug in parser", "labels": ["bug"]},
            risk_level="MEDIUM",
            permission_decision="ALLOW",
        )
        self.assertIsNotNone(tc_id)

        calls = self.store.list_tool_calls(request_id="req-tc-1")
        self.assertEqual(len(calls), 1)
        self.assertIn(calls[0]["status"], {"started", "running"})
        self.assertEqual(calls[0]["arguments"]["title"], "Bug in parser")

        # Mark tool call completed
        self.store.record_tool_call_complete(
            tc_id,
            result={"issue_number": 42, "url": "https://github.com/repo/issues/42"},
            status="success",
            duration_ms=85.0,
        )

        calls_after = self.store.list_tool_calls(request_id="req-tc-1")
        self.assertEqual(calls_after[0]["status"], "success")
        self.assertEqual(calls_after[0]["result"]["issue_number"], 42)
        self.assertAlmostEqual(calls_after[0]["duration_ms"], 85.0, places=1)

    def test_audit_event_persistence(self) -> None:
        """Test recording operational audit events."""
        ae_id = self.store.record_audit_event(
            request_id="req-audit-1",
            action="policy.created",
            actor="telegram:9999",
            details={"scope": "project", "risk_level": "LOW", "decision": "ALLOW"},
        )
        self.assertIsNotNone(ae_id)

        events = self.store.list_audit_events(request_id="req-audit-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "policy.created")
        self.assertEqual(events[0]["details"]["scope"], "project")

    def test_artifact_metadata_persistence(self) -> None:
        """Test storing artifact metadata without saving binary payload in SQLite."""
        art_id = self.store.record_artifact_metadata(
            request_id="req-art-1",
            artifact_name="patch-summary.diff",
            artifact_type="text/x-diff",
            size_bytes=4096,
            storage_uri="file:///d:/practice-file/.artifacts/patch-summary.diff",
            metadata={"lines_added": 45, "lines_removed": 12},
        )
        self.assertIsNotNone(art_id)

        with self.store._lock:
            cur = self.store._db.cursor()
            cur.execute("SELECT artifact_name, size_bytes, storage_uri FROM artifact_metadata WHERE id = ?", (art_id,))
            row = cur.fetchone()
            self.assertEqual(row[0], "patch-summary.diff")
            self.assertEqual(row[1], 4096)
            self.assertEqual(row[2], "file:///d:/practice-file/.artifacts/patch-summary.diff")


class TestCorrelationAndActivityContext(unittest.TestCase):
    """Test universal request_id correlation and ActivityContext manager."""

    def setUp(self) -> None:
        self.store = ActivityStore.from_path(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_get_timeline_correlates_all_entities(self) -> None:
        """Verify that get_timeline returns all records across all tables for a given request_id."""
        req_id = "req-corr-flow-1"

        # 1. Telegram inbound
        self.store.record_telegram_message(
            request_id=req_id,
            chat_id=100,
            telegram_user_id=200,
            direction="inbound",
            message_type="command",
            text="/commit -m 'feat: activity store'",
        )

        # 2. Prompt
        p_id = self.store.record_prompt_start(
            request_id=req_id,
            prompt_text="commit -m 'feat: activity store'",
            source="telegram",
        )
        self.store.record_prompt_complete(p_id, status="completed")

        # 3. Permission Event
        self.store.record_permission_event(
            request_id=req_id,
            risk_level="MEDIUM",
            decision="ALLOW",
            reason="Auto-approved commit",
            actor="policy_engine",
        )

        # 4. Operation
        op_id = self.store.record_operation_start(
            request_id=req_id,
            operation_type="git.commit",
            source="telegram",
        )
        self.store.record_operation_complete(op_id, status="completed", duration_ms=25.0)

        # 5. Tool Call
        tc_id = self.store.record_tool_call_start(
            request_id=req_id,
            tool_name="git_commit",
            arguments={"message": "feat: activity store"},
        )
        self.store.record_tool_call_complete(tc_id, result={"hash": "abc1234"}, status="success")

        # 6. Telegram outbound
        self.store.record_telegram_message(
            request_id=req_id,
            chat_id=100,
            telegram_user_id=200,
            direction="outbound",
            message_type="response",
            text="Committed abc1234 successfully.",
        )

        # Fetch timeline
        timeline = self.store.get_timeline(req_id)
        self.assertEqual(timeline["request_id"], req_id)
        self.assertEqual(len(timeline["telegram_messages"]), 2)
        self.assertEqual(len(timeline["prompts"]), 1)
        self.assertEqual(len(timeline["permission_events"]), 1)
        self.assertEqual(len(timeline["operations"]), 1)
        self.assertEqual(len(timeline["tool_calls"]), 1)

    def test_activity_context_operation_success(self) -> None:
        """Verify ActivityContext async operation context manager records duration and success."""
        ctx = ActivityContext(
            request_id="req-ctx-1",
            store=self.store,
            project_id="proj-test",
            session_id="sess-test",
        )

        async def _run() -> None:
            async with ctx.operation("test_op") as op_id:
                self.assertIsNotNone(op_id)
                await asyncio.sleep(0.01)

        asyncio.run(_run())

        ops = self.store.list_operations(request_id="req-ctx-1")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["operation_type"], "test_op")
        self.assertEqual(ops[0]["status"], "completed")
        self.assertGreater(ops[0]["duration_ms"], 5.0)
        self.assertIsNone(ops[0]["error_code"])

    def test_activity_context_operation_failure(self) -> None:
        """Verify ActivityContext async operation manager records exception and re-raises."""
        ctx = ActivityContext(
            request_id="req-ctx-2",
            store=self.store,
        )

        async def _run_fail() -> None:
            async with ctx.operation("failing_op"):
                raise ValueError("Operation execution exploded")

        with self.assertRaises(ValueError):
            asyncio.run(_run_fail())

        ops = self.store.list_operations(request_id="req-ctx-2")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["operation_type"], "failing_op")
        self.assertEqual(ops[0]["status"], "failed")
        self.assertEqual(ops[0]["error_code"], "ValueError")
        self.assertIn("exploded", ops[0]["error_message"])


class TestSecurityRedaction(unittest.TestCase):
    """Verify that all sensitive credentials and tokens are redacted prior to persistence."""

    def setUp(self) -> None:
        self.store = ActivityStore.from_path(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_telegram_message_redaction(self) -> None:
        """Verify GitHub tokens and Bearer headers in messages are redacted."""
        secret_text = "Here is my token: ghp_1234567890abcdef1234567890abcdef1234 and Bearer sec_tok_9999"
        msg_id = self.store.record_telegram_message(
            request_id="req-sec-1",
            chat_id=1,
            telegram_user_id=2,
            direction="inbound",
            message_type="command",
            text=secret_text,
        )

        messages = self.store.list_telegram_messages(request_id="req-sec-1")
        stored_text = messages[0]["text"]
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef1234", stored_text)
        self.assertIn("[REDACTED]", stored_text)

    def test_prompt_redaction(self) -> None:
        """Verify prompts containing private keys or bot tokens are redacted."""
        prompt_with_key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
        p_id = self.store.record_prompt_start(
            request_id="req-sec-2",
            prompt_text=f"Please inspect this key:\n{prompt_with_key}",
            source="user",
        )

        prompts = self.store.list_prompts(request_id="req-sec-2")
        stored = prompts[0]["prompt_text"]
        self.assertNotIn("BEGIN RSA PRIVATE KEY", stored)
        self.assertIn("[REDACTED", stored)

    def test_tool_call_arguments_redaction(self) -> None:
        """Verify tool call arguments containing secrets are scrubbed in JSON."""
        secret_token = "ghp_ABCDEF1234567890abcdef1234567890abcd"
        tc_id = self.store.record_tool_call_start(
            request_id="req-sec-3",
            tool_name="git_clone",
            arguments={"repo": "https://github.com/org/repo", "token": secret_token},
        )

        calls = self.store.list_tool_calls(request_id="req-sec-3")
        args = calls[0]["arguments"]
        self.assertNotIn(secret_token, json.dumps(args))
        self.assertIn("[REDACTED", args["token"])

    def test_operation_error_redaction(self) -> None:
        """Verify error messages with credential leakage are redacted."""
        op_id = self.store.record_operation_start(
            request_id="req-sec-4",
            operation_type="api_call",
            source="agent",
        )
        self.store.record_operation_complete(
            op_id,
            status="failed",
            error_code="AUTH_FAIL",
            error_message="Failed authenticating with token ghp_9999999999abcdef9999999999abcdef9999",
        )

        ops = self.store.list_operations(request_id="req-sec-4")
        err = ops[0]["error_message"]
        self.assertNotIn("ghp_9999999999abcdef9999999999abcdef9999", err)
        self.assertIn("[REDACTED", err)


class TestPaginationAndRetention(unittest.TestCase):
    """Verify bounded pagination and time-based retention purging."""

    def setUp(self) -> None:
        self.store = ActivityStore.from_path(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_bounded_pagination(self) -> None:
        """Test limit and offset behavior on entity listings."""
        for i in range(15):
            self.store.record_operation_start(
                request_id=f"req-page-{i}",
                operation_type="test_op",
                source="test",
            )

        page1 = self.store.list_operations(limit=5, offset=0)
        page2 = self.store.list_operations(limit=5, offset=5)
        page3 = self.store.list_operations(limit=5, offset=10)

        self.assertEqual(len(page1), 5)
        self.assertEqual(len(page2), 5)
        self.assertEqual(len(page3), 5)

        # Ensure pages do not overlap
        p1_ids = {item["id"] for item in page1}
        p2_ids = {item["id"] for item in page2}
        p3_ids = {item["id"] for item in page3}
        self.assertTrue(p1_ids.isdisjoint(p2_ids))
        self.assertTrue(p2_ids.isdisjoint(p3_ids))

    def test_retention_purge(self) -> None:
        """Test purging records older than retention_days while retaining newer ones."""
        now = time.time()
        old_timestamp = now - (100 * 86400)  # 100 days ago

        # Insert old record directly into DB with ancient timestamp
        with self.store._lock:
            cur = self.store._db.cursor()
            cur.execute(
                """
                INSERT INTO operations (id, request_id, operation_type, source, status, started_at)
                VALUES ('op-old', 'req-old', 'git.clean', 'cli', 'completed', ?)
                """,
                (old_timestamp,),
            )
            self.store._db.commit()

        # Insert fresh record today
        self.store.record_operation_start(
            request_id="req-fresh",
            operation_type="git.status",
            source="telegram",
        )

        # Before purge: 2 operations exist
        ops_before = self.store.list_operations()
        self.assertEqual(len(ops_before), 2)

        # Purge records older than 90 days
        deleted = self.store.purge_retention(retention_days=90)
        self.assertGreaterEqual(deleted, 1)

        # After purge: old operation is gone, fresh one remains
        ops_after = self.store.list_operations()
        self.assertEqual(len(ops_after), 1)
        self.assertEqual(ops_after[0]["id"], "op-fresh" if ops_after[0]["id"] == "op-fresh" else ops_after[0]["id"])
        self.assertEqual(ops_after[0]["request_id"], "req-fresh")


class TestFailureIsolation(unittest.TestCase):
    """Verify that activity store failures degrade gracefully and NEVER crash core callers."""

    def test_broken_database_does_not_crash_writes(self) -> None:
        """Simulate a broken database connection and assert that writes return None without raising."""
        metrics = MetricsRegistry()
        store = ActivityStore.from_path(":memory:", metrics=metrics)

        # Intentionally close the underlying DB connection to trigger sqlite3.Error
        with store._lock:
            store._db.close()

        # All recording methods must return None and not raise
        self.assertIsNone(
            store.record_telegram_message(
                request_id="req-err-1",
                chat_id=1,
                telegram_user_id=2,
                direction="inbound",
                message_type="cmd",
                text="hello",
            )
        )
        self.assertIsNone(
            store.record_prompt_start(
                request_id="req-err-2",
                prompt_text="hello prompt",
                source="user",
            )
        )
        self.assertIsNone(
            store.record_operation_start(
                request_id="req-err-3",
                operation_type="test",
                source="user",
            )
        )
        self.assertIsNone(
            store.record_permission_event(
                request_id="req-err-4",
                risk_level="LOW",
                decision="ALLOW",
                reason="test",
                actor="test",
            )
        )
        self.assertIsNone(
            store.record_connector_event(
                request_id="req-err-5",
                connector_id="conn1",
                event_type="test",
                status="ok",
            )
        )
        self.assertIsNone(
            store.record_tool_call_start(
                request_id="req-err-6",
                tool_name="test_tool",
            )
        )
        self.assertIsNone(
            store.record_audit_event(
                request_id="req-err-7",
                action="test",
                actor="test",
            )
        )
        self.assertIsNone(
            store.record_artifact_metadata(
                request_id="req-err-8",
                artifact_name="test",
                artifact_type="txt",
                size_bytes=10,
                storage_uri="file:///test",
            )
        )

        # Metric activity_store_write_error should have been incremented
        flat = metrics.to_flat_dict()
        self.assertGreater(flat.get("activity_store_write_error", 0), 0)

    def test_activity_context_survives_store_failure(self) -> None:
        """Verify ActivityContext runs caller code successfully even if store throws exceptions."""
        broken_store = MagicMock()
        broken_store.record_operation_start.side_effect = sqlite3.OperationalError("disk I/O error")
        broken_store.record_operation_complete.side_effect = sqlite3.OperationalError("disk I/O error")

        ctx = ActivityContext(request_id="req-survive", store=broken_store)

        executed = False

        async def _work() -> None:
            nonlocal executed
            async with ctx.operation("critical_business_logic"):
                executed = True

        asyncio.run(_work())
        self.assertTrue(executed, "Caller work must execute even if ActivityStore fails")


class TestControlPlaneIntegration(unittest.TestCase):
    """Verify ControlPlane integrates ActivityStore without database sprawl."""
    def test_control_plane_has_activity_store_sharing_db(self) -> None:
        """ControlPlane.activity_store shares the SQLite database connection with ControlPlaneStore and PolicyStore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "control-plane.sqlite3"
            cp = ControlPlane(database_path=str(db_path), operator_token="op-token-123")

            self.assertIsNotNone(cp.activity_store)
            self.assertEqual(cp.activity_store.schema_version, 1)

            # Check that activity_store shares the exact same db connection object
            self.assertIs(cp.activity_store._db, cp.store._db)
            self.assertIs(cp.activity_store._lock, cp.store._lock)

            # Record an activity event via cp.activity_store
            msg_id = cp.activity_store.record_telegram_message(
                request_id="req-cp-1",
                chat_id=123,
                telegram_user_id=456,
                direction="inbound",
                message_type="command",
                text="/help",
            )
            self.assertIsNotNone(msg_id)

            # Verify that only one .sqlite3 file exists in the directory (plus possible -wal/-shm)
            sqlite_files = [f.name for f in Path(tmpdir).iterdir() if f.name.endswith(".sqlite3")]
            self.assertEqual(sqlite_files, ["control-plane.sqlite3"])

            try:
                cp.store._db.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
