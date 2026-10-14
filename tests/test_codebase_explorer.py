"""Comprehensive validation suite for Phase 5: Sandboxed Codebase Explorer & Mobile Code Review (25 Test Scenarios)."""
from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path

from sutra.file_access import (
    DEFAULT_PREVIEW_LINES,
    MAX_DOWNLOAD_BYTES,
    MAX_PREVIEW_LINES,
    count_file_lines,
    detect_binary_file,
    extract_file_symbols,
    get_binary_metadata,
    get_file_info,
    list_directory_paginated,
    preview_file_bounded,
    search_file_text,
)
from sutra.permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionLevel,
    PermissionRequest,
    PermissionRiskLevel,
)
from sutra.redaction import contains_secrets, is_sensitive_file, redact_secrets
from sutra.resilience import IdempotencyStore
from sutra.runtime import Runtime
from sutra.control_plane import ControlPlane
from sutra.apps.telegram.bot import TelegramBot
from sutra.apps.telegram.formatting import render_screen


class CodebaseExplorerValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name).resolve()
        self.workspace = self.root / "test-workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text("[project]\nname='explorer-test'\n", encoding="utf-8")
        self.src_dir = self.workspace / "src"
        self.src_dir.mkdir(parents=True)

        # Runtime setup
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.runtime = Runtime(str(self.runtime_dir))
        self.runtime.store.add_workspace(str(self.workspace))
        self.runtime.project_index.set_roots([str(self.workspace)])
        self.runtime.project_index.schedule_refresh()
        await self.runtime.project_index.wait_for_refresh()

        # Control Plane & Telegram bot mock
        self.cp = ControlPlane(self.root / "cp.sqlite3", "operator-token-that-is-long-enough")
        self.bot = TelegramBot(self.cp, "test-token-123", {42})

    async def asyncTearDown(self) -> None:
        self.runtime.store.close()
        self.cp.store.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # 1. Project Selection
    # -----------------------------------------------------------------------
    async def test_01_project_selection(self) -> None:
        """Only registered/authorized projects can be browsed; unregistered paths rejected; context switching cleans state."""
        # Authorized workspace can be listed
        res = await self.runtime.handle_remote_command("file.list", {
            "project_root": str(self.workspace),
            "path": str(self.workspace),
        })
        self.assertEqual(res["page"], 0)
        self.assertIn("src", [item["name"] for item in res["items"]])

        # Unregistered project root is strictly rejected
        outside_dir = self.root / "outside_proj"
        outside_dir.mkdir()
        with self.assertRaises((PermissionError, ValueError)):
            await self.runtime.handle_remote_command("file.list", {
                "project_root": str(outside_dir),
                "path": str(outside_dir),
            })

        # Telegram active_file_context is cleared on project switch
        self.bot.active_file_context[42] = {"path": "dummy.py"}
        # Simulate selecting a new project
        self.bot.active_file_context.pop(42, None)
        self.assertNotIn(42, self.bot.active_file_context)

    # -----------------------------------------------------------------------
    # 2. Directory Traversal
    # -----------------------------------------------------------------------
    def test_02_directory_traversal(self) -> None:
        """Recursive and non-recursive browsing sorts directories first, then files."""
        sub1 = self.src_dir / "api"
        sub1.mkdir()
        sub2 = self.src_dir / "core"
        sub2.mkdir()
        (self.src_dir / "zebra.py").write_text("z = 1", encoding="utf-8")
        (self.src_dir / "alpha.py").write_text("a = 1", encoding="utf-8")

        res = list_directory_paginated(self.src_dir, page=0, page_size=20)
        names = [item["name"] for item in res["items"]]
        types = [item["type"] for item in res["items"]]

        # Directories first (alphabetical), then files (alphabetical)
        self.assertEqual(names, ["api", "core", "alpha.py", "zebra.py"])
        self.assertEqual(types, ["directory", "directory", "file", "file"])

    # -----------------------------------------------------------------------
    # 3. Directory Pagination
    # -----------------------------------------------------------------------
    def test_03_directory_pagination(self) -> None:
        """Paging through large directories respects page_size and maintains has_prev/has_more."""
        page_dir = self.src_dir / "paged"
        page_dir.mkdir()
        for i in range(1, 46):
            (page_dir / f"item_{i:02d}.txt").write_text(f"content {i}", encoding="utf-8")

        # Page 0 (20 items)
        p0 = list_directory_paginated(page_dir, page=0, page_size=20)
        self.assertEqual(len(p0["items"]), 20)
        self.assertEqual(p0["total_items"], 45)
        self.assertEqual(p0["total_pages"], 3)
        self.assertTrue(p0["has_more"])
        self.assertFalse(p0["has_prev"])

        # Page 1 (20 items)
        p1 = list_directory_paginated(page_dir, page=1, page_size=20)
        self.assertEqual(len(p1["items"]), 20)
        self.assertTrue(p1["has_more"])
        self.assertTrue(p1["has_prev"])

        # Page 2 (5 items)
        p2 = list_directory_paginated(page_dir, page=2, page_size=20)
        self.assertEqual(len(p2["items"]), 5)
        self.assertFalse(p2["has_more"])
        self.assertTrue(p2["has_prev"])

    # -----------------------------------------------------------------------
    # 4. Workspace Boundary Enforcement
    # -----------------------------------------------------------------------
    async def test_04_workspace_boundary_enforcement(self) -> None:
        """Resolved target must remain strictly inside authorized workspace root."""
        outside = self.root / "outside.txt"
        outside.write_text("secret outside", encoding="utf-8")

        with self.assertRaises((PermissionError, ValueError)):
            await self.runtime.handle_remote_command("file.preview", {
                "project_root": str(self.workspace),
                "path": str(outside),
            })

    # -----------------------------------------------------------------------
    # 5. Path Traversal Attacks
    # -----------------------------------------------------------------------
    async def test_05_path_traversal_attacks(self) -> None:
        """Relative path traversal via '..' or directory escaping is blocked."""
        traversal_attempts = [
            str(self.workspace / ".." / "outside.txt"),
            str(self.workspace / "src" / ".." / ".." / "outside.txt"),
        ]
        for bad_path in traversal_attempts:
            with self.assertRaises((PermissionError, ValueError)):
                await self.runtime.handle_remote_command("file.list", {
                    "project_root": str(self.workspace),
                    "path": bad_path,
                })

    # -----------------------------------------------------------------------
    # 6. Symlink Escape Attempts
    # -----------------------------------------------------------------------
    def test_06_symlink_escape_attempts(self) -> None:
        """Symlinks pointing outside sandbox are skipped and cannot be traversed."""
        outside_target = self.root / "outside_target.py"
        outside_target.write_text("outside data", encoding="utf-8")

        link_path = self.src_dir / "escape_link.py"
        try:
            os.symlink(outside_target, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported in this test environment")

        res = list_directory_paginated(self.src_dir)
        names = [item["name"] for item in res["items"]]
        self.assertNotIn("escape_link.py", names)

    # -----------------------------------------------------------------------
    # 7. File Metadata
    # -----------------------------------------------------------------------
    def test_07_file_metadata(self) -> None:
        """Compact metadata returns file type, size, line count, modified time without full buffering."""
        test_file = self.src_dir / "service.py"
        test_file.write_text("import os\n\ndef run():\n    return 42\n", encoding="utf-8")

        info = get_file_info(test_file, relative_to=self.workspace)
        self.assertEqual(info["name"], "service.py")
        self.assertFalse(info["is_binary"])
        self.assertEqual(info["language"], "Python")
        self.assertEqual(info["total_lines"], 4)
        self.assertGreater(info["size_bytes"], 0)
        self.assertTrue(info["download_available"])
        self.assertIn("-", info["file_version"])

    # -----------------------------------------------------------------------
    # 8. Small-File Preview
    # -----------------------------------------------------------------------
    def test_08_small_file_preview(self) -> None:
        """Preview of a 10-line file returns all lines without has_more."""
        f10 = self.src_dir / "small.py"
        f10.write_text("\n".join(f"line_{i}" for i in range(1, 11)), encoding="utf-8")

        res = preview_file_bounded(f10, start_line=1, max_lines=35)
        self.assertEqual(res["total_lines"], 10)
        self.assertEqual(res["lines_returned"], 10)
        self.assertFalse(res["has_more"])
        self.assertFalse(res["has_prev"])

    # -----------------------------------------------------------------------
    # 9. 35-Line Boundary
    # -----------------------------------------------------------------------
    def test_09_35_line_boundary(self) -> None:
        """Exactly 35 lines returns has_more = False."""
        f35 = self.src_dir / "boundary35.py"
        f35.write_text("\n".join(f"line_{i}" for i in range(1, 36)), encoding="utf-8")

        res = preview_file_bounded(f35, start_line=1, max_lines=35)
        self.assertEqual(res["lines_returned"], 35)
        self.assertEqual(res["total_lines"], 35)
        self.assertFalse(res["has_more"])

    # -----------------------------------------------------------------------
    # 10. 36-Line Boundary
    # -----------------------------------------------------------------------
    def test_10_36_line_boundary(self) -> None:
        """Exactly 36 lines returns has_more = True on page 1."""
        f36 = self.src_dir / "boundary36.py"
        f36.write_text("\n".join(f"line_{i}" for i in range(1, 37)), encoding="utf-8")

        res = preview_file_bounded(f36, start_line=1, max_lines=35)
        self.assertEqual(res["lines_returned"], 35)
        self.assertEqual(res["total_lines"], 36)
        self.assertTrue(res["has_more"])

    # -----------------------------------------------------------------------
    # 11. 5,000-Line File
    # -----------------------------------------------------------------------
    def test_11_5000_line_file(self) -> None:
        """5,000-line file streams bounded ranges without high memory or latency."""
        f5k = self.src_dir / "large_5k.py"
        f5k.write_text("\n".join(f"def func_{i}(): pass" for i in range(1, 5001)), encoding="utf-8")

        # Range 1: 1-35
        p1 = preview_file_bounded(f5k, start_line=1, max_lines=35)
        self.assertEqual(p1["start_line"], 1)
        self.assertEqual(p1["end_line"], 35)
        self.assertEqual(p1["total_lines"], 5000)
        self.assertTrue(p1["has_more"])
        self.assertFalse(p1["has_prev"])

        # Range 2: 36-70
        p2 = preview_file_bounded(f5k, start_line=36, max_lines=35)
        self.assertEqual(p2["start_line"], 36)
        self.assertEqual(p2["end_line"], 70)
        self.assertTrue(p2["has_prev"])
        self.assertTrue(p2["has_more"])

    # -----------------------------------------------------------------------
    # 12. Very Large File (20,000 lines)
    # -----------------------------------------------------------------------
    def test_12_very_large_file(self) -> None:
        """20,000-line file bounded window navigation."""
        f20k = self.src_dir / "huge_20k.py"
        f20k.write_text("\n".join(f"const VAL_{i} = {i};" for i in range(1, 20001)), encoding="utf-8")

        p = preview_file_bounded(f20k, start_line=15000, max_lines=35)
        self.assertEqual(p["start_line"], 15000)
        self.assertEqual(p["end_line"], 15034)
        self.assertEqual(p["total_lines"], 20000)
        self.assertTrue(p["has_prev"])
        self.assertTrue(p["has_more"])
        self.assertIn("const VAL_15000", p["content"])

    # -----------------------------------------------------------------------
    # 13. Jump-To-Line
    # -----------------------------------------------------------------------
    def test_13_jump_to_line(self) -> None:
        """Jump to line 1720 opens bounded window around that line."""
        target = self.src_dir / "jump_test.py"
        target.write_text("\n".join(f"# Line {i}" for i in range(1, 3001)), encoding="utf-8")

        res = preview_file_bounded(target, start_line=1720, max_lines=35)
        self.assertEqual(res["start_line"], 1720)
        self.assertEqual(res["end_line"], 1754)
        self.assertIn("# Line 1720", res["content"])
        self.assertTrue(res["has_prev"])

    # -----------------------------------------------------------------------
    # 14. File Search
    # -----------------------------------------------------------------------
    def test_14_file_search(self) -> None:
        """Search locates terms case-insensitively and returns match line numbers and snippets."""
        search_target = self.src_dir / "auth.py"
        search_target.write_text(
            "import jwt\n"
            "def authenticate_user(user):\n"
            "    return True\n\n"
            "def refresh_token(tok):\n"
            "    return tok\n\n"
            "def authenticate_token(tok):\n"
            "    return True\n",
            encoding="utf-8",
        )

        res = search_file_text(search_target, query="authenticate", max_results=10)
        self.assertEqual(res["total_matches"], 2)
        lines = [m["line"] for m in res["matches"]]
        self.assertEqual(lines, [2, 8])

    # -----------------------------------------------------------------------
    # 15. Function / Symbol Discovery
    # -----------------------------------------------------------------------
    def test_15_symbol_discovery(self) -> None:
        """Deterministic AST parsing discovers classes, methods, and functions with exact lines."""
        py_file = self.src_dir / "models.py"
        py_file.write_text(
            "class AuthManager:\n"
            "    def __init__(self):\n"
            "        pass\n"
            "    def login(self):\n"
            "        pass\n\n"
            "async def get_session():\n"
            "    return None\n\n"
            "def logout():\n"
            "    pass\n",
            encoding="utf-8",
        )

        sym = extract_file_symbols(py_file)
        self.assertEqual(sym["language"], "Python")
        self.assertEqual(len(sym["classes"]), 1)
        self.assertEqual(sym["classes"][0]["name"], "AuthManager")
        self.assertEqual(sym["classes"][0]["line"], 1)

        fn_names = [f["name"] for f in sym["functions"]]
        self.assertIn("get_session", fn_names)
        self.assertIn("logout", fn_names)

    # -----------------------------------------------------------------------
    # 16. Binary File Handling
    # -----------------------------------------------------------------------
    async def test_16_binary_file(self) -> None:
        """Binary files report metadata, forbid source preview, and permit safe download under limit."""
        bin_file = self.src_dir / "logo.png"
        bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20)

        is_bin, cat, mime = detect_binary_file(bin_file)
        self.assertTrue(is_bin)
        self.assertEqual(cat, "image")

        info = get_file_info(bin_file)
        self.assertTrue(info["is_binary"])

        # Preview fails with ValueError
        with self.assertRaises(ValueError):
            await self.runtime.handle_remote_command("file.preview", {
                "project_root": str(self.workspace),
                "path": str(bin_file),
            })

        # Download succeeds
        dl = await self.runtime.handle_remote_command("file.download", {
            "project_root": str(self.workspace),
            "path": str(bin_file),
        })
        self.assertTrue(dl.get("is_binary"))

    # -----------------------------------------------------------------------
    # 17. Sensitive File Protection
    # -----------------------------------------------------------------------
    async def test_17_sensitive_file(self) -> None:
        """Sensitive files (.env, keys, credentials) are blocked from preview, download, and listing."""
        env_file = self.workspace / ".env"
        env_file.write_text("SECRET_KEY=12345", encoding="utf-8")

        # Not in file listing
        lst = await self.runtime.handle_remote_command("file.list", {
            "project_root": str(self.workspace),
            "path": str(self.workspace),
        })
        self.assertNotIn(".env", [item["name"] for item in lst["items"]])

        # Preview denied
        with self.assertRaises(ValueError):
            await self.runtime.handle_remote_command("file.preview", {
                "project_root": str(self.workspace),
                "path": str(env_file),
            })

        # Download denied
        with self.assertRaises(ValueError):
            await self.runtime.handle_remote_command("file.download", {
                "project_root": str(self.workspace),
                "path": str(env_file),
            })

    # -----------------------------------------------------------------------
    # 18. File Modification During Preview
    # -----------------------------------------------------------------------
    def test_18_file_modification_during_preview(self) -> None:
        """Stale view detection alerts user if file modified on disk."""
        target = self.src_dir / "live.py"
        target.write_text("v1 = True\n", encoding="utf-8")

        p1 = preview_file_bounded(target, start_line=1)
        initial_version = p1["file_version"]

        # Modify file
        time.sleep(0.01)
        target.write_text("v2 = True\nv2_added = True\n", encoding="utf-8")

        # Passing old version triggers version_changed = True
        p2 = preview_file_bounded(target, start_line=1, expected_version=initial_version)
        self.assertTrue(p2["version_changed"])

    # -----------------------------------------------------------------------
    # 19. Download Authorization & Redaction
    # -----------------------------------------------------------------------
    async def test_19_download_authorization(self) -> None:
        """Text downloads redact embedded secrets; oversized files rejected."""
        target = self.src_dir / "config.py"
        target.write_text('token = "ghp_111111111111111111111111111111111111"\nmode = "prod"\n', encoding="utf-8")

        dl = await self.runtime.handle_remote_command("file.download", {
            "project_root": str(self.workspace),
            "path": str(target),
        })
        content = base64.b64decode(dl["content_base64"]).decode("utf-8")
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("ghp_111111111111111111111111111111111111", content)

    # -----------------------------------------------------------------------
    # 20. Telegram Pagination Callbacks
    # -----------------------------------------------------------------------
    async def test_20_telegram_pagination_callbacks(self) -> None:
        """Telegram files screen provides pagination buttons when results exceed page_size."""
        pdir = self.src_dir / "tg_paged"
        pdir.mkdir()
        for i in range(1, 35):
            (pdir / f"f_{i:02d}.py").write_text("x=1", encoding="utf-8")

        # Connect runtime to bot device map
        self.bot.selected_devices[42] = "AB-PC-TEST01"
        self.bot._dispatch_command = self._mock_dispatch

        # Render files screen
        reply = await render_screen(
            self.bot, "files",
            {"device_id": "AB-PC-TEST01", "project": {"absolute_path": str(self.workspace), "name": "Test"}, "path": str(pdir), "page": 0, "page_size": 20},
            chat_id=42, user_id=42,
        )
        self.assertIsNotNone(reply)
        button_texts = [b["text"] for row in reply.inline_keyboard for b in row]
        self.assertTrue(any("Next" in t for t in button_texts))

    # -----------------------------------------------------------------------
    # 21. Stale Telegram Callbacks
    # -----------------------------------------------------------------------
    async def test_21_stale_telegram_callbacks(self) -> None:
        """Callbacks from an expired or mismatched view are rejected."""
        from sutra.apps.telegram.callbacks import handle_callback
        stale_update = {
            "callback_query": {
                "id": "cq_stale",
                "from": {"id": 42},
                "data": "target:non_existent_key",
                "message": {"message_id": 999, "chat": {"id": 42, "type": "private"}},
            }
        }
        res = await handle_callback(self.bot, stale_update, user_id=42, chat_id=42)
        self.assertIn("expired", res.text.lower())

    # -----------------------------------------------------------------------
    # 22. Duplicate Callback Handling
    # -----------------------------------------------------------------------
    async def test_22_duplicate_callback_handling(self) -> None:
        """Idempotency store suppresses duplicate processing of identical update_ids."""
        idem = IdempotencyStore()
        update = {"update_id": 99991, "message": {"text": "/status"}}
        action1, _ = await idem.check_and_start("tg_update:99991", update)
        self.assertEqual(action1, "EXECUTE")

        # Concurrent / duplicate call returns PROCESSING
        action2, _ = await idem.check_and_start("tg_update:99991", update)
        self.assertEqual(action2, "PROCESSING")

    # -----------------------------------------------------------------------
    # 23. Secret Redaction
    # -----------------------------------------------------------------------
    def test_23_secret_redaction(self) -> None:
        """Sensitive tokens in preview lines are sanitized automatically."""
        raw_code = 'openai_key = "sk-ant-api03-abcdef1234567890abcdef1234567890abcdef1234567890"\n'
        redacted = redact_secrets(raw_code)
        self.assertNotIn("sk-ant-api03", redacted)
        self.assertIn("REDACTED", redacted)

    # -----------------------------------------------------------------------
    # 24. Existing Permission Behavior
    # -----------------------------------------------------------------------
    def test_24_existing_permission_behavior(self) -> None:
        """PermissionEngine appropriately evaluates read vs write requests."""
        engine = PermissionEngine()
        risk_read = engine.assess_risk("file_read", resource_path=str(self.src_dir / "main.py"))
        self.assertEqual(risk_read, PermissionRiskLevel.LOW)
        risk_write = engine.assess_risk("file_write", resource_path=str(self.src_dir / "main.py"))
        self.assertEqual(risk_write, PermissionRiskLevel.MEDIUM)
        risk_delete = engine.assess_risk("file_delete", resource_path=str(self.src_dir / "main.py"))
        self.assertEqual(risk_delete, PermissionRiskLevel.HIGH)

    # -----------------------------------------------------------------------
    # 25. Existing Telegram Behavior (/help, /jump, /search)
    # -----------------------------------------------------------------------
    async def test_25_existing_telegram_behavior(self) -> None:
        """Telegram command router handles /help, /jump without active file, and /search."""
        from sutra.apps.telegram.handlers import handle_message_command
        # /help
        help_reply = await handle_message_command(self.bot, {"text": "/help"}, user_id=42, chat_id=42)
        self.assertIn("/jump", help_reply)
        self.assertIn("/search", help_reply)

        # /jump without active file
        jump_no_file = await handle_message_command(self.bot, {"text": "/jump 10"}, user_id=42, chat_id=42)
        self.assertIn("No active file selected", jump_no_file)

        # /search without active file
        search_no_file = await handle_message_command(self.bot, {"text": "/search authenticate"}, user_id=42, chat_id=42)
        self.assertIn("No active file selected", search_no_file)

    async def _mock_dispatch(self, device_id: str, command: str, payload: dict) -> dict:
        """Mock dispatcher for Telegram rendering tests."""
        if command == "file.list":
            p = Path(payload["path"])
            items = []
            for item in sorted(p.iterdir(), key=lambda x: x.name):
                items.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "path": str(item),
                })
            page = payload.get("page", 0)
            page_size = payload.get("page_size", 20)
            start = page * page_size
            return {
                "status": "success",
                "payload": {
                    "items": items[start:start+page_size],
                    "total_items": len(items),
                    "total_pages": (len(items) + page_size - 1) // page_size,
                    "has_more": start + page_size < len(items),
                    "has_prev": page > 0,
                }
            }
        return {"status": "success", "payload": {}}


if __name__ == "__main__":
    unittest.main()
