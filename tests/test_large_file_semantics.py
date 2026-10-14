"""Comprehensive validation suite for Large File Preview & Export Semantics Hardening (Phase 5 Hardening).

Covers all 26 required scenarios:
1. 10 MiB file (single chunk boundary)
2. 11 MiB file (multipart export trigger)
3. 50 MiB file (former stream limit boundary)
4. 51 MiB file (proves >50MB files are previewable)
5. 100 MiB file (crosses GitHub limit, local preview works)
6. 120 MiB local file (lines 17200-17234 bounded preview without whole-file memory load)
7. Small preview from a large file
8. Large-file jump-to-line (fast sparse line index seek)
9. Preview byte limit (MAX_PREVIEW_BYTES truncation)
10. Preview line limit (bounded by MAX_PREVIEW_LINES)
11. Single huge line (pathological line truncation)
12. UTF-8 boundary safety (never bisects multi-byte sequence)
13. Large text export (newline boundary splitting)
14. Binary export (byte-range slicing)
15. Multi-part export ordering (strictly contiguous indices and byte ranges)
16. Part checksum (per-part SHA-256 matches)
17. Whole-file checksum (manifest whole_file_sha256 matches)
18. Manifest correctness (schema_version, original_size, parts array)
19. Reconstruction from parts (exact byte-for-byte fidelity)
20. Missing-part detection (verify_export_integrity reports missing part)
21. Corrupted-part detection (verify_export_integrity reports checksum failure)
22. File modified during export (version token mismatch detection)
23. Sensitive large-file export rejection (.env / secrets blocked)
24. Search operation budget (max_scan_bytes budget bounded without crashing)
25. Existing Phase 5 regression scenarios (get_file_info, binary detection, symbols)
26. GitHub independence test (local preview does not query or require GitHub API)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sutra.file_access import (
    DEFAULT_PREVIEW_LINES,
    MAX_DOWNLOAD_BYTES,
    MAX_DOWNLOAD_CHUNK_BYTES,
    MAX_PREVIEW_BYTES,
    MAX_PREVIEW_LINES,
    MAX_SINGLE_SCAN_BYTES,
    MAX_STREAM_BYTES,
    MAX_TOTAL_EXPORT_BYTES,
    ExportManifest,
    ExportPart,
    SparseLineIndex,
    build_export_plan,
    count_file_lines,
    detect_binary_file,
    export_file_to_directory,
    extract_file_symbols,
    get_binary_metadata,
    get_export_part_bytes,
    get_file_info,
    get_or_build_sparse_index,
    preview_file_bounded,
    reconstruct_file_from_parts,
    search_file_text,
    verify_export_integrity,
)
from sutra.runtime import Runtime


class LargeFileSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name).resolve()
        self.workspace = self.root / "test-workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text("[project]\nname='large-file-test'\n", encoding="utf-8")
        self.src_dir = self.workspace / "src"
        self.src_dir.mkdir(parents=True)

        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.runtime = Runtime(str(self.runtime_dir))
        self.runtime.store.add_workspace(str(self.workspace))
        self.runtime.project_index.set_roots([str(self.workspace)])
        self.runtime.project_index.schedule_refresh()
        await self.runtime.project_index.wait_for_refresh()

    async def asyncTearDown(self) -> None:
        self.runtime.store.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # 1. 10 MiB File (Single Download Chunk Boundary)
    # -----------------------------------------------------------------------
    async def test_01_10_mib_file(self) -> None:
        """A 10 MiB file exactly at MAX_DOWNLOAD_CHUNK_BYTES is classified as single chunk export."""
        target = self.src_dir / "file_10mb.bin"
        chunk_data = b"\x00\x01\x02\x03" * (2560 * 1024)  # 10 MB binary data
        target.write_bytes(chunk_data)

        info = get_file_info(target)
        self.assertEqual(info["size_bytes"], 10 * 1024 * 1024)
        self.assertTrue(info["download_available"])
        self.assertFalse(info["is_multipart"])

        # Directly downloadable via runtime
        res = await self.runtime.handle_remote_command("file.download", {
            "project_root": str(self.workspace),
            "path": str(target),
        })
        self.assertTrue(res.get("is_binary"))
        self.assertIn("content_base64", res)

    # -----------------------------------------------------------------------
    # 2. 11 MiB File (Multipart Export Trigger)
    # -----------------------------------------------------------------------
    async def test_02_11_mib_file(self) -> None:
        """An 11 MiB file exceeds single chunk limit and prepares a 2-part export plan."""
        target = self.src_dir / "file_11mb.dat"
        target.write_bytes(b"B" * (11 * 1024 * 1024))

        info = get_file_info(target)
        self.assertTrue(info["export_available"])
        self.assertTrue(info["is_multipart"])

        plan = build_export_plan(target, chunk_size=10 * 1024 * 1024)
        self.assertEqual(plan.part_count, 2)
        self.assertEqual(plan.parts[0].size, 10 * 1024 * 1024)
        self.assertEqual(plan.parts[1].size, 1 * 1024 * 1024)
        self.assertEqual(plan.parts[0].byte_start, 0)
        self.assertEqual(plan.parts[0].byte_end, 10 * 1024 * 1024 - 1)
        self.assertEqual(plan.parts[1].byte_start, 10 * 1024 * 1024)
        self.assertEqual(plan.parts[1].byte_end, 11 * 1024 * 1024 - 1)

        # Runtime returns multipart_export status
        res = await self.runtime.handle_remote_command("file.download", {
            "project_root": str(self.workspace),
            "path": str(target),
        })
        self.assertEqual(res["status"], "multipart_export")
        self.assertTrue(res["is_multipart"])
        self.assertEqual(res["manifest"]["part_count"], 2)

    # -----------------------------------------------------------------------
    # 3. 50 MiB File (Former Streaming Limit Boundary)
    # -----------------------------------------------------------------------
    def test_03_50_mib_file(self) -> None:
        """A 50 MiB file is fully previewable for bounded requests without memory issues."""
        target = self.src_dir / "data_50mb.txt"
        line_content = "def test_metric_calculation(sample_id): return sample_id * 2\n"
        repeat_count = (50 * 1024 * 1024) // len(line_content)
        with open(target, "w", encoding="utf-8") as f:
            for _ in range(repeat_count):
                f.write(line_content)

        preview = preview_file_bounded(target, start_line=1, max_lines=35)
        self.assertEqual(preview["start_line"], 1)
        self.assertEqual(preview["lines_returned"], 35)
        self.assertFalse(preview["truncated_by_bytes"])
        self.assertIn("def test_metric_calculation", preview["content"])

    # -----------------------------------------------------------------------
    # 4. 51 MiB File (Exceeds Former MAX_STREAM_BYTES, Successfully Previewable)
    # -----------------------------------------------------------------------
    def test_04_51_mib_file(self) -> None:
        """A 51 MiB file (which failed under legacy MAX_STREAM_BYTES) previews safely."""
        target = self.src_dir / "data_51mb.txt"
        line_content = "LOG_EVENT: user_session_active status=OK timestamp=1700000000\n"
        repeat_count = (51 * 1024 * 1024) // len(line_content) + 1
        with open(target, "w", encoding="utf-8") as f:
            for _ in range(repeat_count):
                f.write(line_content)

        self.assertGreater(target.stat().st_size, 50 * 1024 * 1024)
        # In Phase 5 before hardening, this threw ValueError("file size exceeds maximum streaming limit")
        preview = preview_file_bounded(target, start_line=500, max_lines=35)
        self.assertEqual(preview["start_line"], 500)
        self.assertEqual(preview["lines_returned"], 35)
        self.assertIn("LOG_EVENT:", preview["content"])

    # -----------------------------------------------------------------------
    # 5. 100 MiB File (Crosses GitHub Repo Single-File Limit)
    # -----------------------------------------------------------------------
    def test_05_100_mib_file(self) -> None:
        """A 100 MiB file (exceeding standard GitHub file limits) previews locally."""
        target = self.src_dir / "artifact_100mb.log"
        line_content = "INFO 2026-10-06 Pipeline trace metric heartbeat 42 ok\n"
        repeat_count = (100 * 1024 * 1024) // len(line_content) + 1
        with open(target, "w", encoding="utf-8") as f:
            for _ in range(repeat_count):
                f.write(line_content)

        self.assertGreaterEqual(target.stat().st_size, 100 * 1024 * 1024)
        preview = preview_file_bounded(target, start_line=10000, max_lines=20)
        self.assertEqual(preview["start_line"], 10000)
        self.assertEqual(preview["lines_returned"], 20)
        self.assertIn("Pipeline trace metric", preview["content"])

    # -----------------------------------------------------------------------
    # 6. 120 MiB Local File (Lines 17200-17234 Bounded Preview)
    # -----------------------------------------------------------------------
    def test_06_120_mib_local_file_preview(self) -> None:
        """A 120 MiB local file previewing lines 17200–17234 reads only that bounded region."""
        target = self.src_dir / "huge_120mb.txt"
        line_template = "RECORD: line_{:07d} payload_data_test_string_padding_xyz_12345\n"
        single_len = len(line_template.format(1))
        repeat_count = (120 * 1024 * 1024) // single_len + 500
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, repeat_count + 1):
                f.write(line_template.format(i))

        self.assertGreaterEqual(target.stat().st_size, 120 * 1024 * 1024)

        t0 = time.perf_counter()
        preview = preview_file_bounded(target, start_line=17200, end_line=17234, max_lines=35)
        elapsed = time.perf_counter() - t0

        self.assertEqual(preview["start_line"], 17200)
        self.assertEqual(preview["end_line"], 17234)
        self.assertEqual(preview["lines_returned"], 35)
        self.assertIn("line_0017200", preview["content"])
        self.assertIn("line_0017234", preview["content"])
        # Fast bounded read without reading entire file
        self.assertLess(elapsed, 2.0)

    # -----------------------------------------------------------------------
    # 7. Small Preview from a Large File
    # -----------------------------------------------------------------------
    def test_07_small_preview_from_large_file(self) -> None:
        """Requesting 5 lines from a multi-megabyte file returns exactly 5 lines."""
        target = self.src_dir / "small_slice.txt"
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, 20000):
                f.write(f"Row {i:05d} sample data\n")

        preview = preview_file_bounded(target, start_line=42, max_lines=5)
        self.assertEqual(preview["start_line"], 42)
        self.assertEqual(preview["end_line"], 46)
        self.assertEqual(preview["lines_returned"], 5)
        self.assertTrue(preview["has_more"])
        self.assertTrue(preview["has_prev"])

    # -----------------------------------------------------------------------
    # 8. Large-File Jump-to-Line via Sparse Index
    # -----------------------------------------------------------------------
    def test_08_large_file_jump_to_line(self) -> None:
        """Sparse checkpoint index allows fast jumping to high line numbers."""
        target = self.src_dir / "jump_sparse.txt"
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, 25000):
                f.write(f"TargetLine_{i:06d}\n")

        index = get_or_build_sparse_index(target, step_lines=1000)
        chk_line, chk_offset = index.find_nearest_checkpoint(18500)
        self.assertLessEqual(chk_line, 18500)
        self.assertGreaterEqual(chk_line, 18000)
        self.assertGreater(chk_offset, 0)

        preview = preview_file_bounded(target, start_line=18500, max_lines=10)
        self.assertEqual(preview["start_line"], 18500)
        self.assertIn("TargetLine_018500", preview["content"])

    # -----------------------------------------------------------------------
    # 9. Preview Byte Limit (MAX_PREVIEW_BYTES Truncation)
    # -----------------------------------------------------------------------
    def test_09_preview_byte_limit(self) -> None:
        """Preview stops when accumulated preview bytes exceed MAX_PREVIEW_BYTES."""
        target = self.src_dir / "byte_limit.txt"
        # Each line is 1,000 bytes; 100 lines = 100 KB
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, 101):
                f.write(f"Line_{i:03d} " + "A" * 990 + "\n")

        # Set max_preview_bytes to 5 KB (approx 5 lines)
        preview = preview_file_bounded(target, start_line=1, max_lines=50, max_preview_bytes=5000)
        self.assertTrue(preview["truncated_by_bytes"])
        self.assertEqual(preview["truncation_reason"], "size_limit")
        self.assertIn("Preview truncated by size limit", preview["message"])
        self.assertLessEqual(preview["lines_returned"], 6)

    # -----------------------------------------------------------------------
    # 10. Preview Line Limit
    # -----------------------------------------------------------------------
    def test_10_preview_line_limit(self) -> None:
        """Preview never exceeds MAX_PREVIEW_LINES (200) even if user requests 1,000 lines."""
        target = self.src_dir / "line_limit.txt"
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, 1000):
                f.write(f"Item {i}\n")

        preview = preview_file_bounded(target, start_line=1, max_lines=1000)
        self.assertEqual(preview["lines_returned"], MAX_PREVIEW_LINES)
        self.assertEqual(preview["end_line"], MAX_PREVIEW_LINES)

    # -----------------------------------------------------------------------
    # 11. Single Huge Line Pathological Case
    # -----------------------------------------------------------------------
    def test_11_single_huge_line(self) -> None:
        """A single line of 500 KB is truncated cleanly at max_preview_bytes without crashing."""
        target = self.src_dir / "huge_line.txt"
        target.write_text("START_" + "Z" * (500 * 1024) + "_END\n", encoding="utf-8")

        preview = preview_file_bounded(target, start_line=1, max_lines=10, max_preview_bytes=8192)
        self.assertTrue(preview["truncated_by_bytes"])
        self.assertEqual(preview["lines_returned"], 1)
        self.assertLessEqual(len(preview["content"]), 8500)
        self.assertIn("line truncated by size limit", preview["content"])

    # -----------------------------------------------------------------------
    # 12. UTF-8 Boundary Safety
    # -----------------------------------------------------------------------
    def test_12_utf8_boundary_safety(self) -> None:
        """Multi-byte UTF-8 sequences (e.g. emojis / symbols) are never split in half."""
        from sutra.file_access import _find_safe_utf8_boundary
        # 4-byte UTF-8 emoji: 🐍 is b'\xf0\x9f\x90\x8d'
        emoji_bytes = "Python 🐍 code".encode("utf-8")  # len = 7 + 4 + 5 = 16 bytes
        # Attempt to split right in the middle of the 4-byte sequence (at index 8, 9, or 10)
        cut = _find_safe_utf8_boundary(emoji_bytes[:9])
        # Safe cut must fall right before the leading byte \xf0 (index 7)
        self.assertEqual(cut, 7)
        valid_part = emoji_bytes[:cut].decode("utf-8")
        self.assertEqual(valid_part, "Python ")

    # -----------------------------------------------------------------------
    # 13. Large Text Export with Newline Boundary Splitting
    # -----------------------------------------------------------------------
    def test_13_large_text_export_newlines(self) -> None:
        """Text exports split on newline boundaries where practical."""
        target = self.src_dir / "text_export.py"
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, 5000):
                f.write(f"def generated_function_{i}():\n    return {i * 10}\n")

        # Split with 32 KB chunk size
        plan = build_export_plan(target, chunk_size=32 * 1024)
        self.assertGreater(plan.part_count, 1)

        # Retrieve Part 1 and verify it ends with newline
        part1, data1 = get_export_part_bytes(target, plan, 1)
        self.assertTrue(data1.endswith(b"\n"))
        self.assertIsNotNone(part1.line_start)
        self.assertIsNotNone(part1.line_end)

    # -----------------------------------------------------------------------
    # 14. Binary Export
    # -----------------------------------------------------------------------
    def test_14_binary_export(self) -> None:
        """Binary files chunk strictly by byte ranges without text decoding."""
        target = self.src_dir / "firmware.bin"
        raw_binary = bytes(range(256)) * 400  # 102.4 KB
        target.write_bytes(raw_binary)

        plan = build_export_plan(target, chunk_size=32 * 1024)
        self.assertTrue(plan.is_binary)
        self.assertEqual(plan.original_size, len(raw_binary))

        # Check part 1
        part1, data1 = get_export_part_bytes(target, plan, 1)
        self.assertEqual(len(data1), 32 * 1024)
        self.assertEqual(hashlib.sha256(data1).hexdigest(), part1.sha256)

    # -----------------------------------------------------------------------
    # 15. Multi-part Export Ordering
    # -----------------------------------------------------------------------
    def test_15_multipart_ordering(self) -> None:
        """Export parts have strictly ascending contiguous byte ranges."""
        target = self.src_dir / "ordered.dat"
        target.write_bytes(b"DATA" * 50000)  # 200 KB

        plan = build_export_plan(target, chunk_size=64 * 1024)
        for i, part in enumerate(plan.parts):
            self.assertEqual(part.index, i + 1)
            if i == 0:
                self.assertEqual(part.byte_start, 0)
            else:
                self.assertEqual(part.byte_start, plan.parts[i - 1].byte_end + 1)
            self.assertEqual(part.byte_end, part.byte_start + part.size - 1)

    # -----------------------------------------------------------------------
    # 16. Part Checksum Verification
    # -----------------------------------------------------------------------
    def test_16_part_checksum(self) -> None:
        """Every generated part has a verifiable SHA-256 matching manifest."""
        target = self.src_dir / "checksum_test.txt"
        target.write_text("Hello World\n" * 2000, encoding="utf-8")

        plan = build_export_plan(target, chunk_size=8192)
        for part in plan.parts:
            _, data = get_export_part_bytes(target, plan, part.index)
            self.assertEqual(hashlib.sha256(data).hexdigest(), part.sha256)

    # -----------------------------------------------------------------------
    # 17. Whole-File Checksum Verification
    # -----------------------------------------------------------------------
    def test_17_whole_file_checksum(self) -> None:
        """Manifest records the exact SHA-256 of the whole file."""
        target = self.src_dir / "whole_file.txt"
        target.write_text("Unique text string for whole-file hash testing.\n" * 1000, encoding="utf-8")

        expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        plan = build_export_plan(target, chunk_size=4096)
        self.assertEqual(plan.whole_file_sha256, expected_hash)

    # -----------------------------------------------------------------------
    # 18. Manifest Correctness
    # -----------------------------------------------------------------------
    def test_18_manifest_correctness(self) -> None:
        """Manifest dictionary contains schema_version, filenames, sizes, and hashes."""
        target = self.src_dir / "manifest_test.py"
        target.write_text("x = 1\ny = 2\n" * 500, encoding="utf-8")

        plan = build_export_plan(target, chunk_size=2048)
        d = plan.to_dict()
        self.assertEqual(d["schema_version"], 1)
        self.assertEqual(d["original_filename"], "manifest_test.py")
        self.assertEqual(d["original_size"], target.stat().st_size)
        self.assertIsInstance(d["parts"], list)
        self.assertEqual(len(d["parts"]), plan.part_count)

    # -----------------------------------------------------------------------
    # 19. Exact Reconstruction from Parts
    # -----------------------------------------------------------------------
    def test_19_exact_reconstruction(self) -> None:
        """Concatenating export parts reproduces original file byte-for-byte."""
        target = self.src_dir / "reconstruct_me.bin"
        original_bytes = os.urandom(128 * 1024)  # 128 KB random bytes
        target.write_bytes(original_bytes)

        plan = build_export_plan(target, chunk_size=32 * 1024)
        parts_data: dict[int, bytes] = {}
        for p in plan.parts:
            _, data = get_export_part_bytes(target, plan, p.index)
            parts_data[p.index] = data

        reconstructed_path = self.src_dir / "reconstructed.bin"
        success = reconstruct_file_from_parts(plan, parts_data, reconstructed_path)
        self.assertTrue(success)
        self.assertEqual(reconstructed_path.read_bytes(), original_bytes)

    # -----------------------------------------------------------------------
    # 20. Missing-Part Detection
    # -----------------------------------------------------------------------
    def test_20_missing_part_detection(self) -> None:
        """Integrity verification rejects incomplete part maps."""
        target = self.src_dir / "missing_part.dat"
        target.write_bytes(b"Z" * (64 * 1024))

        plan = build_export_plan(target, chunk_size=16 * 1024)
        parts_data: dict[int, bytes] = {}
        for p in plan.parts[:-1]:  # Drop last part
            _, data = get_export_part_bytes(target, plan, p.index)
            parts_data[p.index] = data

        valid, reason = verify_export_integrity(plan, parts_data)
        self.assertFalse(valid)
        self.assertIn("missing", reason.lower())

    # -----------------------------------------------------------------------
    # 21. Corrupted-Part Detection
    # -----------------------------------------------------------------------
    def test_21_corrupted_part_detection(self) -> None:
        """Integrity verification detects altered byte contents in any part."""
        target = self.src_dir / "corrupted_part.dat"
        target.write_bytes(b"A" * (64 * 1024))

        plan = build_export_plan(target, chunk_size=16 * 1024)
        parts_data: dict[int, bytes] = {}
        for p in plan.parts:
            _, data = get_export_part_bytes(target, plan, p.index)
            parts_data[p.index] = data

        # Corrupt part 2
        parts_data[2] = b"CORRUPTED" + parts_data[2][9:]
        valid, reason = verify_export_integrity(plan, parts_data)
        self.assertFalse(valid)
        self.assertIn("corrupted", reason.lower())

    # -----------------------------------------------------------------------
    # 22. File Modified During Export
    # -----------------------------------------------------------------------
    def test_22_file_modified_during_export(self) -> None:
        """get_export_part_bytes fails if file version changes on disk."""
        target = self.src_dir / "volatile.txt"
        target.write_text("Version 1 content\n" * 100, encoding="utf-8")

        plan = build_export_plan(target, chunk_size=500)
        # Modify file
        time.sleep(0.01)
        target.write_text("Version 2 mutated content\n" * 200, encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            get_export_part_bytes(target, plan, 1)
        self.assertIn("modified", str(ctx.exception).lower())

    # -----------------------------------------------------------------------
    # 23. Sensitive Large-File Export Rejection
    # -----------------------------------------------------------------------
    def test_23_sensitive_large_file_rejection(self) -> None:
        """Sensitive files (.env, keys) cannot be exported regardless of size."""
        target = self.workspace / ".env"
        target.write_text("SECRET_KEY=" + "A" * 1000 + "\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            build_export_plan(target)

    # -----------------------------------------------------------------------
    # 24. Search Operation Budget (MAX_SINGLE_SCAN_BYTES)
    # -----------------------------------------------------------------------
    def test_24_search_operation_budget(self) -> None:
        """Search stops when max_scan_bytes budget is reached without refusing the search."""
        target = self.src_dir / "search_budget.txt"
        # 10,000 lines of 100 bytes each = 1 MB
        with open(target, "w", encoding="utf-8") as f:
            for i in range(1, 10001):
                f.write(f"Line {i:05d} target_token data padding string here\n")

        # Set max_scan_bytes to 50 KB
        res = search_file_text(target, query="target_token", max_results=10, max_scan_bytes=50 * 1024)
        self.assertTrue(res["scan_budget_exceeded"])
        self.assertTrue(res["has_more"])
        self.assertIn("budget", res["message"].lower())
        self.assertGreater(len(res["matches"]), 0)

    # -----------------------------------------------------------------------
    # 25. Existing Phase 5 Regression Scenarios
    # -----------------------------------------------------------------------
    async def test_25_phase_5_regression_scenarios(self) -> None:
        """Standard Phase 5 operations continue working smoothly."""
        py_file = self.src_dir / "sample.py"
        py_file.write_text(
            "class Engine:\n"
            "    def start(self): pass\n\n"
            "def run_diagnostics(): pass\n",
            encoding="utf-8",
        )

        info = get_file_info(py_file)
        self.assertEqual(info["language"], "Python")
        self.assertEqual(info["total_lines"], 4)

        sym = extract_file_symbols(py_file)
        self.assertEqual(len(sym["classes"]), 1)
        self.assertEqual(len(sym["functions"]), 1)

        # Runtime command file.info
        res = await self.runtime.handle_remote_command("file.info", {
            "project_root": str(self.workspace),
            "path": str(py_file),
        })
        self.assertEqual(res["name"], "sample.py")

    # -----------------------------------------------------------------------
    # 26. GitHub Independence Test (Local Preview Never Queries GitHub)
    # -----------------------------------------------------------------------
    def test_26_github_independence(self) -> None:
        """Local file preview does not require, import, or query GitHub REST or MCP."""
        target = self.src_dir / "local_independent.txt"
        target.write_text("Local workstation file independent of GitHub\n" * 100, encoding="utf-8")

        # Mock httpx.AsyncClient to ensure no network calls are attempted
        with patch("httpx.AsyncClient") as mock_http:
            preview = preview_file_bounded(target, start_line=1, max_lines=10)
            self.assertEqual(preview["start_line"], 1)
            self.assertEqual(preview["lines_returned"], 10)
            mock_http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
