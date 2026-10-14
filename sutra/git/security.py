"""Security validations, secret redaction, and guardrails for Git operations."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, List, Sequence, Tuple

from ..redaction import redact_secrets
from ..security import _canonical
from ..workspace_lock import LockMode, WorkspaceConflictError, WorkspaceLockManager

FORBIDDEN_FORCE_FLAGS = {"--force", "-f", "--force-with-lease", "--force-if-includes"}


def validate_repo_path(project_path: str | Path) -> Path:
    """Validate and return canonical project root."""
    if not project_path:
        raise ValueError("Project path must not be empty")
    canon = _canonical(project_path)
    if not canon.exists() or not canon.is_dir():
        raise ValueError(f"Project path does not exist or is not a directory: {canon}")
    return canon


def validate_files_in_repo(repo_root: Path, filepaths: Sequence[str]) -> List[str]:
    """Validate that all filepaths are located strictly within the repo_root boundary.
    
    Returns clean relative paths relative to repo_root.
    """
    clean_paths: List[str] = []
    canon_root = _canonical(repo_root)

    for fp in filepaths:
        if not fp or not isinstance(fp, str):
            continue
        # Strip leading slashes to prevent root-relative evasion
        raw_target = fp.strip()
        if not raw_target:
            continue

        target_path = _canonical(canon_root / raw_target)
        try:
            rel = target_path.relative_to(canon_root)
            # Normalized POSIX string for git operations
            clean_paths.append(str(rel).replace("\\", "/"))
        except ValueError:
            raise ValueError(f"Path traversal detected: {fp} is outside repository root")

    return clean_paths


def assert_no_force_push(args: Sequence[str]) -> None:
    """Enforce strict invariant: force-push is never permitted under any circumstances."""
    for arg in args:
        lower = arg.lower()
        if lower in FORBIDDEN_FORCE_FLAGS or any(lower.startswith(f + "=") for f in FORBIDDEN_FORCE_FLAGS):
            raise ValueError("Force push is strictly prohibited by policy invariant.")


def sanitize_and_redact_diff(diff_raw: str, max_lines: int = 200) -> Tuple[str, int, bool]:
    """Redact secrets from diff output and bound total lines to prevent massive transmissions.
    
    Returns:
        (sanitized_diff, total_lines, is_truncated)
    """
    if not diff_raw:
        return ("", 0, False)

    # 1. Redact secrets using the centralized redaction engine
    redacted = redact_secrets(diff_raw)

    # 2. Bound line count
    lines = redacted.splitlines()
    total_lines = len(lines)
    is_truncated = total_lines > max_lines

    if is_truncated:
        bounded_lines = lines[:max_lines]
        truncated_diff = "\n".join(bounded_lines) + f"\n\n... [Diff truncated: {total_lines - max_lines} more lines]"
        return (truncated_diff, total_lines, True)

    return (redacted, total_lines, False)


def check_workspace_lock(
    lock_manager: WorkspaceLockManager | None,
    repo_root: Path,
    current_session: str | None = None,
) -> None:
    """Verify that no conflicting exclusive WRITE lock is held on the repo by another session."""
    if lock_manager is None:
        return

    canon_root = _canonical(repo_root)
    for path, entries in list(lock_manager._locks.items()):
        for entry in entries:
            if entry.mode == LockMode.WRITE:
                if current_session and entry.session_id == current_session:
                    continue
                # Check overlap
                if lock_manager._is_path_overlapping(canon_root, True, entry.path, entry.is_dir):
                    raise WorkspaceConflictError(
                        f"Workspace locked by active agent session ({entry.session_id}). Git mutation blocked.",
                        path=str(canon_root),
                        mode=LockMode.WRITE,
                        held_by_session=entry.session_id,
                    )
