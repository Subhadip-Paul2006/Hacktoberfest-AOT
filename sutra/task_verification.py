"""Verification layer comparing agent claims against actual filesystem and Git state."""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .git import GitManager
from .redaction import is_sensitive_file, HIDDEN_DIRECTORY_NAMES


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


@dataclass
class VerificationResult:
    status: VerificationStatus
    files_changed: list[str] = field(default_factory=list)
    claimed_files: list[str] = field(default_factory=list)
    missing_modifications: list[str] = field(default_factory=list)
    git_diff_detected: bool = False
    tests_passed: bool | None = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "files_changed": self.files_changed,
            "claimed_files": self.claimed_files,
            "missing_modifications": self.missing_modifications,
            "git_diff_detected": self.git_diff_detected,
            "tests_passed": self.tests_passed,
            "summary": self.summary,
        }


class TaskVerifier:
    """Takes filesystem snapshots before and after agent tasks to verify actual modifications."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._initial_snapshot: dict[str, float] = {}

    def capture_snapshot(self) -> dict[str, float]:
        """Capture mtimes of project files for change detection."""
        snapshot: dict[str, float] = {}
        for root, dirs, files in os.walk(str(self.project_root)):
            dirs[:] = [d for d in dirs if d.casefold() not in HIDDEN_DIRECTORY_NAMES and not is_sensitive_file(d)]
            for f in files:
                if is_sensitive_file(f):
                    continue
                fp = Path(root) / f
                try:
                    rel = str(fp.relative_to(self.project_root))
                    snapshot[rel] = fp.stat().st_mtime
                except (OSError, PermissionError):
                    continue
        self._initial_snapshot = snapshot
        return snapshot

    def verify_completion(
        self,
        claimed_modified_files: Sequence[str] | None = None,
        claimed_tests_passed: bool | None = None,
        test_exit_code: int | None = None,
    ) -> VerificationResult:
        """Compare post-task filesystem and Git state against pre-task snapshot and claims."""
        if not self._initial_snapshot:
            return VerificationResult(
                status=VerificationStatus.NOT_VERIFIABLE,
                summary="No baseline filesystem snapshot was captured prior to task execution.",
            )

        current_snapshot = {}
        for root, dirs, files in os.walk(str(self.project_root)):
            dirs[:] = [d for d in dirs if d.casefold() not in HIDDEN_DIRECTORY_NAMES and not is_sensitive_file(d)]
            for f in files:
                if is_sensitive_file(f):
                    continue
                fp = Path(root) / f
                try:
                    rel = str(fp.relative_to(self.project_root))
                    current_snapshot[rel] = fp.stat().st_mtime
                except (OSError, PermissionError):
                    continue

        # Detect files created or modified
        changed_files: list[str] = []
        for rel_path, mtime in current_snapshot.items():
            if rel_path not in self._initial_snapshot or self._initial_snapshot[rel_path] != mtime:
                changed_files.append(rel_path)

        # Detect deleted files
        for rel_path in self._initial_snapshot:
            if rel_path not in current_snapshot:
                changed_files.append(f"(deleted) {rel_path}")

        # Check Git status if repo
        git_has_diff = False
        if GitManager.is_repository(self.project_root):
            try:
                res = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(self.project_root),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0 and res.stdout.strip():
                    git_has_diff = True
            except Exception:
                pass

        claimed_list = list(claimed_modified_files or [])
        missing_mods: list[str] = []
        for cf in claimed_list:
            norm_cf = str(Path(cf)).replace("\\", "/")
            norm_changed = [c.replace("\\", "/") for c in changed_files]
            if not any(norm_cf in c for c in norm_changed):
                missing_mods.append(cf)

        # Check test claims
        test_contradiction = False
        if claimed_tests_passed is True and test_exit_code is not None and test_exit_code != 0:
            test_contradiction = True

        # Determine status
        if test_contradiction:
            return VerificationResult(
                status=VerificationStatus.FAILED_VERIFICATION,
                files_changed=changed_files,
                claimed_files=claimed_list,
                missing_modifications=missing_mods,
                git_diff_detected=git_has_diff,
                tests_passed=False,
                summary=f"Contradiction: Agent claimed tests passed, but test process exited with code {test_exit_code}.",
            )

        if claimed_list and missing_mods:
            if len(missing_mods) == len(claimed_list) and not changed_files and not git_has_diff:
                return VerificationResult(
                    status=VerificationStatus.FAILED_VERIFICATION,
                    files_changed=changed_files,
                    claimed_files=claimed_list,
                    missing_modifications=missing_mods,
                    git_diff_detected=git_has_diff,
                    summary="Agent claimed completion, but none of the claimed files were modified on disk.",
                )
            return VerificationResult(
                status=VerificationStatus.PARTIALLY_VERIFIED,
                files_changed=changed_files,
                claimed_files=claimed_list,
                missing_modifications=missing_mods,
                git_diff_detected=git_has_diff,
                summary=f"Partial verification: {len(changed_files)} files modified, but {len(missing_mods)} claimed files were untouched.",
            )

        if not changed_files and not git_has_diff and claimed_list:
            return VerificationResult(
                status=VerificationStatus.FAILED_VERIFICATION,
                files_changed=[],
                claimed_files=claimed_list,
                missing_modifications=claimed_list,
                git_diff_detected=False,
                summary="Agent claimed modifications, but no filesystem or Git changes detected.",
            )

        return VerificationResult(
            status=VerificationStatus.VERIFIED,
            files_changed=changed_files,
            claimed_files=claimed_list,
            missing_modifications=[],
            git_diff_detected=git_has_diff,
            tests_passed=True if test_exit_code == 0 else None,
            summary=f"Task verified: {len(changed_files)} files modified on disk.",
        )
