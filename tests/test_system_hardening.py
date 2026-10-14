"""Comprehensive validation suite for System Design Hardening & Edge-Case Remediation (15 Issues)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from sutra.chunking import chunk_message
from sutra.context_budget import ContextBudgetManager, ContextPriority
from sutra.context_retrieval import BoundedContextRetriever
from sutra.file_access import (
    detect_binary_file,
    get_binary_metadata,
    list_directory_paginated,
    preview_file_bounded,
)
from sutra.loop_detector import AgentLoopDetector, LoopEvidence
from sutra.permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionLevel,
    PermissionRequest,
    PermissionRiskLevel,
)
from sutra.redaction import contains_secrets, is_sensitive_file, redact_secrets
from sutra.task_executor import PerProjectTaskExecutor, TaskResult, TaskState
from sutra.task_verification import TaskVerifier, VerificationStatus
from sutra.workspace_lock import LockMode, WorkspaceConflictError, WorkspaceLockManager
from sutra.agents import (
    AgentEvent,
    AgentEventType,
    AgentManager,
    AgentRegistry,
    AgentSession,
    AgentSessionStatus,
    AgentSupervisor,
    EventSeverity,
    GenericCLIAdapter,
)


class SystemDesignHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # ISSUE 01: Large File Handling / 5K+ Line Files
    # -----------------------------------------------------------------------

    def test_01_empty_and_small_files(self) -> None:
        empty_file = self.root / "empty.py"
        empty_file.write_text("", encoding="utf-8")
        res_0 = preview_file_bounded(empty_file, start_line=1, max_lines=35)
        self.assertEqual(res_0["total_lines"], 0)
        self.assertEqual(res_0["lines_returned"], 0)
        self.assertFalse(res_0["has_more"])

        file_35 = self.root / "file35.py"
        file_35.write_text("\n".join(f"line_{i}" for i in range(1, 36)), encoding="utf-8")
        res_35 = preview_file_bounded(file_35, start_line=1, max_lines=35)
        self.assertEqual(res_35["lines_returned"], 35)
        self.assertFalse(res_35["has_more"])

        file_36 = self.root / "file36.py"
        file_36.write_text("\n".join(f"line_{i}" for i in range(1, 37)), encoding="utf-8")
        res_36 = preview_file_bounded(file_36, start_line=1, max_lines=35)
        self.assertEqual(res_36["lines_returned"], 35)
        self.assertTrue(res_36["has_more"])

    def test_01_5k_line_file_streaming_and_paging(self) -> None:
        large_file = self.root / "large.py"
        # 5,000 lines
        large_file.write_text("\n".join(f"def func_{i}(): return {i}" for i in range(1, 5001)), encoding="utf-8")

        # Page 1: lines 1-35
        p1 = preview_file_bounded(large_file, start_line=1, max_lines=35)
        self.assertEqual(p1["start_line"], 1)
        self.assertEqual(p1["end_line"], 35)
        self.assertEqual(p1["lines_returned"], 35)
        self.assertEqual(p1["total_lines"], 5000)
        self.assertTrue(p1["has_more"])
        self.assertFalse(p1["has_prev"])
        self.assertIn("def func_1()", p1["content"])
        self.assertIn("def func_35()", p1["content"])
        self.assertNotIn("def func_36()", p1["content"])

        # Page 2: lines 36-70
        p2 = preview_file_bounded(large_file, start_line=36, max_lines=35)
        self.assertEqual(p2["start_line"], 36)
        self.assertEqual(p2["end_line"], 70)
        self.assertTrue(p2["has_prev"])
        self.assertTrue(p2["has_more"])

        # Jump to line 4990
        p_end = preview_file_bounded(large_file, start_line=4990, max_lines=35)
        self.assertEqual(p_end["start_line"], 4990)
        self.assertEqual(p_end["end_line"], 5000)
        self.assertEqual(p_end["lines_returned"], 11)
        self.assertFalse(p_end["has_more"])
        self.assertTrue(p_end["has_prev"])

        # Change detection
        v1 = p1["file_version"]
        time.sleep(0.01)
        large_file.write_text(large_file.read_text(encoding="utf-8") + "\n# added", encoding="utf-8")
        p_check = preview_file_bounded(large_file, start_line=1, max_lines=35, expected_version=v1)
        self.assertTrue(p_check["version_changed"])

    # -----------------------------------------------------------------------
    # ISSUE 02: Large Directory / Project Listing
    # -----------------------------------------------------------------------

    def test_02_directory_pagination_and_search(self) -> None:
        sub = self.root / "large_dir"
        sub.mkdir()
        # Create 75 files and 5 subdirectories
        for i in range(5):
            (sub / f"folder_{i:02d}").mkdir()
        for i in range(75):
            (sub / f"file_{i:03d}.py").write_text("# py", encoding="utf-8")

        # Page 0 (size 30)
        p0 = list_directory_paginated(sub, page=0, page_size=30)
        self.assertEqual(p0["total_items"], 80)
        self.assertEqual(len(p0["items"]), 30)
        self.assertEqual(p0["total_pages"], 3)
        self.assertTrue(p0["has_more"])
        # Directories listed before files
        self.assertEqual(p0["items"][0]["type"], "directory")

        # Search filter
        search_res = list_directory_paginated(sub, page=0, page_size=50, search="file_04")
        self.assertEqual(search_res["total_items"], 10)  # file_040 to file_049
        for item in search_res["items"]:
            self.assertIn("file_04", item["name"])

    # -----------------------------------------------------------------------
    # ISSUE 03: Binary / Non-Text File Handling
    # -----------------------------------------------------------------------

    def test_03_binary_detection_and_metadata(self) -> None:
        img = self.root / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 50)
        is_bin, cat, mime = detect_binary_file(img)
        self.assertTrue(is_bin)
        self.assertEqual(cat, "image")

        meta = get_binary_metadata(img)
        self.assertTrue(meta["is_binary"])
        self.assertEqual(meta["category"], "image")
        self.assertTrue(meta["download_available"])

        # Unknown binary with null bytes
        unk = self.root / "unknown.dat"
        unk.write_bytes(b"HELLO\x00WORLD\x00BINARY")
        is_bin_unk, _, _ = detect_binary_file(unk)
        self.assertTrue(is_bin_unk)

    # -----------------------------------------------------------------------
    # ISSUE 04: Telegram Long Message Chunking & Code Fence Preservation
    # -----------------------------------------------------------------------

    def test_04_telegram_chunking_preserves_code_fences(self) -> None:
        # Create a long message inside python code fence
        code_body = "\n".join(f"    x_{i} = {i} * 2" for i in range(300))
        msg = f"Here is the code:\n```python\n{code_body}\n```\nDone!"

        chunks = chunk_message(msg, max_chunk_size=1000)
        self.assertGreater(len(chunks), 1)

        # Every chunk that splits inside code fence should close ``` and reopen ```python
        for idx, chunk in enumerate(chunks):
            # No chunk exceeds max budget
            self.assertLessEqual(len(chunk), 1100)
            # Chunk count indicators present
            self.assertIn(f"[{idx+1}/{len(chunks)}]", chunk)

        # First chunk opens code block
        self.assertIn("```python", chunks[0])
        # Intermediate chunks close and re-open code blocks
        for chunk in chunks[:-1]:
            if "```python" in chunk:
                self.assertTrue(chunk.rstrip().endswith("```"))

    # -----------------------------------------------------------------------
    # ISSUE 05: High-Frequency Output Batching & Priority Flushes
    # -----------------------------------------------------------------------

    async def test_05_high_frequency_output_coalescing(self) -> None:
        manager = AgentManager(AgentRegistry(populate_defaults=False))
        session = AgentSession(session_id="test-sess", agent_id="cli", project_id="proj")
        manager._sessions["test-sess"] = session
        q = manager.subscribe("test-sess")

        # Send 10 rapid output chunks
        for i in range(10):
            event = AgentEvent(
                event_type=AgentEventType.AGENT_OUTPUT,
                agent_id="cli",
                agent_session_id="test-sess",
                project_id="proj",
                payload={"stream": "stdout", "text": f"line {i}"},
            )
            manager._handle_adapter_event(event)

        # Queue should not have 10 separate events immediately because of debouncing
        self.assertLess(q.qsize(), 10)

        # Immediate priority event flushes buffered output immediately
        crit = AgentEvent(
            event_type=AgentEventType.PERMISSION_REQUIRED,
            agent_id="cli",
            agent_session_id="test-sess",
            project_id="proj",
            severity=EventSeverity.CRITICAL,
            payload={"action": "rm -rf"},
        )
        manager._handle_adapter_event(crit)

        events: list[AgentEvent] = []
        while not q.empty():
            events.append(q.get_nowait())

        # Should have flushed combined output before permission required
        types = [e.event_type for e in events]
        self.assertIn(AgentEventType.AGENT_OUTPUT, types)
        self.assertIn(AgentEventType.PERMISSION_REQUIRED, types)
        self.assertEqual(types[-1], AgentEventType.PERMISSION_REQUIRED)

    # -----------------------------------------------------------------------
    # ISSUE 06: Duplicate Command / Task Execution
    # -----------------------------------------------------------------------

    async def test_06_task_executor_request_id_deduplication(self) -> None:
        executor = PerProjectTaskExecutor(max_concurrent_per_project=2)
        execution_count = 0

        async def work():
            nonlocal execution_count
            execution_count += 1
            await asyncio.sleep(0.05)
            return f"done_{execution_count}"

        # Submit twice with identical request_id
        fut1 = executor.submit("p1", work, request_id="req-unique-123")
        fut2 = executor.submit("p1", work, request_id="req-unique-123")

        # fut1 and fut2 should resolve to the exact same result
        res1 = await fut1
        res2 = await fut2
        self.assertEqual(res1.result, res2.result)
        self.assertEqual(execution_count, 1)

        # Subsequent submission after completion returns cached result
        fut3 = executor.submit("p1", work, request_id="req-unique-123")
        res3 = await fut3
        self.assertEqual(res3.result, res1.result)
        self.assertEqual(execution_count, 1)

    # -----------------------------------------------------------------------
    # ISSUE 07: Stale Session Event Protection
    # -----------------------------------------------------------------------

    async def test_07_stale_generation_event_rejected(self) -> None:
        manager = AgentManager(AgentRegistry(populate_defaults=False))
        session = AgentSession(session_id="s1", generation=2, status=AgentSessionStatus.RUNNING)
        manager._sessions["s1"] = session
        q = manager.subscribe("s1")

        # Stale event from generation 1
        stale_event = AgentEvent(
            event_type=AgentEventType.AGENT_OUTPUT,
            agent_session_id="s1",
            payload={"text": "old data", "generation": 1},
        )
        manager._handle_adapter_event(stale_event)

        # Fresh event from generation 2
        fresh_event = AgentEvent(
            event_type=AgentEventType.AGENT_READY,
            agent_session_id="s1",
            payload={"generation": 2},
        )
        manager._handle_adapter_event(fresh_event)

        events = []
        while not q.empty():
            events.append(q.get_nowait())

        # Stale event was rejected; only fresh event was published
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, AgentEventType.AGENT_READY)

    # -----------------------------------------------------------------------
    # ISSUE 08: Process Tree Termination
    # -----------------------------------------------------------------------

    async def test_08_process_tree_termination(self) -> None:
        adapter = GenericCLIAdapter()
        proc = await asyncio.create_subprocess_exec(
            "python", "-c", "import time; time.sleep(10)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.assertIsNone(proc.returncode)
        await adapter._terminate_process(proc)
        self.assertIsNotNone(proc.returncode)

    # -----------------------------------------------------------------------
    # ISSUE 09: Multiple Agents Workspace & File Locking
    # -----------------------------------------------------------------------

    async def test_09_workspace_lock_conflict_detection(self) -> None:
        lock_mgr = WorkspaceLockManager()
        file_a = self.root / "src" / "auth.py"

        # Agent 1 acquires WRITE lock
        ok1 = await lock_mgr.acquire(file_a, session_id="agent-1", mode=LockMode.WRITE)
        self.assertTrue(ok1)

        # Agent 2 attempts WRITE lock on same file -> conflict!
        with self.assertRaises(WorkspaceConflictError):
            await lock_mgr.acquire(file_a, session_id="agent-2", mode=LockMode.WRITE)

        # Agent 2 attempts WRITE lock on parent directory -> conflict!
        with self.assertRaises(WorkspaceConflictError):
            await lock_mgr.acquire(self.root / "src", session_id="agent-2", mode=LockMode.WRITE, is_dir=True)

        # Agent 1 terminating releases locks automatically
        released = await lock_mgr.release_session_locks("agent-1")
        self.assertEqual(released, 1)

        # Agent 2 can now acquire lock
        ok2 = await lock_mgr.acquire(file_a, session_id="agent-2", mode=LockMode.WRITE)
        self.assertTrue(ok2)

    # -----------------------------------------------------------------------
    # ISSUE 10: LLM Context / Token Budgeting
    # -----------------------------------------------------------------------

    def test_10_context_budget_trimming(self) -> None:
        # Budget: 500 total tokens, 100 reserved output -> 400 available input (~1600 chars)
        mgr = ContextBudgetManager(total_token_budget=500, reserved_output_tokens=100)

        # High priority prompt
        mgr.add_item("prompt", "Analyze this error", priority=ContextPriority.USER_PROMPT)
        # Critical error
        mgr.add_item("error", "Error: Database connection failed", priority=ContextPriority.ERRORS_AND_FAILURES)
        # Low priority older logs (large)
        mgr.add_item("old_logs", "verbose debug message\n" * 200, priority=ContextPriority.HISTORICAL_OUTPUT)

        assembled = mgr.assemble_context()
        self.assertLessEqual(assembled["used_input_tokens"], assembled["available_input_tokens"])
        self.assertIn("Analyze this error", assembled["text"])
        self.assertIn("Database connection failed", assembled["text"])
        # Low priority older logs dropped
        self.assertGreater(assembled["items_dropped"], 0)

    # -----------------------------------------------------------------------
    # ISSUE 11: Bounded Context Retrieval
    # -----------------------------------------------------------------------

    def test_11_bounded_retrieval_from_stack_trace_and_symbols(self) -> None:
        src_file = self.root / "calculator.py"
        src_file.write_text(
            "def add(a, b):\n    return a + b\n\ndef divide(a, b):\n    return a / b\n",
            encoding="utf-8",
        )
        retriever = BoundedContextRetriever(self.root)

        # From stack trace
        trace = f'File "{src_file}", line 5, in divide'
        snippets = retriever.extract_from_trace(trace)
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["line"], 5)
        self.assertIn("return a / b", snippets[0]["content"])

        # From keyword / symbol
        syms = retriever.search_symbols_or_keywords(["divide"])
        self.assertEqual(len(syms), 1)
        self.assertIn("def divide", syms[0]["content"])

    # -----------------------------------------------------------------------
    # ISSUE 12: AI Loop & Repeated Failure Detection
    # -----------------------------------------------------------------------

    def test_12_loop_detection_repeated_commands_and_cycles(self) -> None:
        detector = AgentLoopDetector(command_threshold=3, error_threshold=3)

        # 3 identical commands
        self.assertIsNone(detector.record_command("pytest tests/test_foo.py"))
        self.assertIsNone(detector.record_command("pytest tests/test_foo.py"))
        ev = detector.record_command("pytest tests/test_foo.py")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.loop_type, "repeated_command")
        self.assertEqual(ev.count, 3)

        # 3 identical errors
        d2 = AgentLoopDetector(error_threshold=3)
        self.assertIsNone(d2.record_error("ModuleNotFoundError: No module named 'xyz'"))
        self.assertIsNone(d2.record_error("ModuleNotFoundError: No module named 'xyz'"))
        ev_err = d2.record_error("ModuleNotFoundError: No module named 'xyz'")
        self.assertIsNotNone(ev_err)
        self.assertEqual(ev_err.loop_type, "repeated_error")

    # -----------------------------------------------------------------------
    # ISSUE 13: Task Verification vs Actual Filesystem State
    # -----------------------------------------------------------------------

    def test_13_task_verification_outcomes(self) -> None:
        target_file = self.root / "app.py"
        target_file.write_text("v1", encoding="utf-8")

        verifier = TaskVerifier(self.root)
        verifier.capture_snapshot()

        # Claimed file modified, but no disk change -> FAILED_VERIFICATION
        res_fail = verifier.verify_completion(claimed_modified_files=["app.py"])
        self.assertEqual(res_fail.status, VerificationStatus.FAILED_VERIFICATION)

        # Actually modify file
        time.sleep(0.01)
        target_file.write_text("v2", encoding="utf-8")
        res_ok = verifier.verify_completion(claimed_modified_files=["app.py"], test_exit_code=0)
        self.assertEqual(res_ok.status, VerificationStatus.VERIFIED)
        self.assertIn("app.py", res_ok.files_changed)

        # Claimed test passed but exit code was 1 -> FAILED_VERIFICATION
        res_test_fail = verifier.verify_completion(
            claimed_modified_files=["app.py"],
            claimed_tests_passed=True,
            test_exit_code=1,
        )
        self.assertEqual(res_test_fail.status, VerificationStatus.FAILED_VERIFICATION)

    # -----------------------------------------------------------------------
    # ISSUE 14: Centralized Secret Redaction
    # -----------------------------------------------------------------------

    def test_14_secret_redaction_patterns(self) -> None:
        # OpenAI key
        redacted = redact_secrets("API_KEY=sk-abc1234567890def")
        self.assertNotIn("sk-abc1234567890def", redacted)
        self.assertIn("[REDACTED", redacted)

        # GitHub token
        gh = redact_secrets("token: ghp_123456789012345678901234567890")
        self.assertNotIn("ghp_1234567890", gh)

        # Sensitive files
        self.assertTrue(is_sensitive_file(".env"))
        self.assertTrue(is_sensitive_file(".env.production"))
        self.assertTrue(is_sensitive_file("cert.pem"))
        self.assertTrue(is_sensitive_file("id_rsa"))
        self.assertFalse(is_sensitive_file("main.py"))

    # -----------------------------------------------------------------------
    # ISSUE 15: Permission Enforcement
    # -----------------------------------------------------------------------

    def test_15_permission_guardrails(self) -> None:
        engine = PermissionEngine()

        # LOW risk auto-approved
        low_req = PermissionRequest(
            request_id="r1",
            action_type="file_read",
            resource_path="src/app.py",
            command=None,
            agent_id="cli",
            session_id="sess-1",
        )
        allowed, _ = engine.check_permission(low_req)
        self.assertTrue(allowed)

        # CRITICAL risk (force push) CANNOT be auto-approved
        crit_req = PermissionRequest(
            request_id="r2",
            action_type="git_operation",
            resource_path=None,
            command="git push --force origin main",
            agent_id="cli",
            session_id="sess-1",
        )
        allowed_crit, reason = engine.check_permission(crit_req)
        self.assertFalse(allowed_crit)
        self.assertIn("CRITICAL", reason)

        # Apply ALLOW_ONCE decision
        engine.apply_decision("r2", PermissionDecision.ALLOW_ONCE)
        allowed_after, _ = engine.check_permission(crit_req)
        self.assertTrue(allowed_after)


if __name__ == "__main__":
    unittest.main()
