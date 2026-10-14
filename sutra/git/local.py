"""Local Git adapter executing repository inspections and safe state changes."""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import (
    GitBranchInfo,
    GitChangeType,
    GitCommitResult,
    GitDiffResult,
    GitFileChange,
    GitPushPreview,
    GitPushResult,
    GitStageResult,
    GitStatusResult,
)
from .security import (
    assert_no_force_push,
    sanitize_and_redact_diff,
    validate_files_in_repo,
    validate_repo_path,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = float(os.environ.get("SUTRA_GIT_TIMEOUT", "30.0"))


class GitSubprocessTimeoutError(Exception):
    """Raised when a local Git subprocess exceeds its bounded execution timeout."""

    def __init__(self, cmd: Sequence[str], timeout: float) -> None:
        super().__init__(f"Local Git command timed out after {timeout:.1f}s: {' '.join(cmd)}")
        self.cmd = list(cmd)
        self.timeout = timeout


def _run_git_cmd(
    args: Sequence[str],
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT,
    env_override: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a git command safely with structured arguments and timeout bounds."""
    assert_no_force_push(args)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if env_override:
        env.update(env_override)

    cmd = ["git"] + list(args)
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("git_cmd_timeout cmd=%s timeout=%.1fs", " ".join(cmd), timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"Local Git command timed out after {timeout:.1f}s.",
        )



def is_git_repository(project_path: str | Path) -> bool:
    """Detect whether a directory is a Git repository or inside a Git worktree."""
    try:
        path = Path(project_path)
        if not path.exists():
            return False
        if (path / ".git").exists():
            return True
        # Check via git rev-parse
        proc = _run_git_cmd(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=DEFAULT_TIMEOUT)
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def get_repo_root(project_path: str | Path) -> Optional[Path]:
    """Determine the canonical root directory of the Git repository."""
    try:
        path = validate_repo_path(project_path)
        proc = _run_git_cmd(["rev-parse", "--show-toplevel"], cwd=path, timeout=5.0)
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
        if (path / ".git").exists():
            return path
        return None
    except Exception:
        return None


def detect_remotes(project_path: str | Path) -> Dict[str, str]:
    """Discover configured remotes and their fetch URLs."""
    path = validate_repo_path(project_path)
    remotes: Dict[str, str] = {}
    try:
        proc = _run_git_cmd(["remote", "-v"], cwd=path, timeout=5.0)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    name = parts[0]
                    url = parts[1]
                    if name not in remotes or "(fetch)" in line:
                        remotes[name] = url
    except Exception as exc:
        log.warning("Failed to detect remotes in %s: %s", path, exc)
    return remotes


def get_git_branches(project_path: str | Path) -> GitBranchInfo:
    """Retrieve current branch, local branches, and tracking information."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitBranchInfo()

    current_branch: Optional[str] = None
    local_branches: List[str] = []
    tracking_remote: Optional[str] = None
    ahead = 0
    behind = 0

    # 1. Current branch
    proc_curr = _run_git_cmd(["branch", "--show-current"], cwd=path, timeout=5.0)
    if proc_curr.returncode == 0 and proc_curr.stdout.strip():
        current_branch = proc_curr.stdout.strip()
    else:
        # Detached HEAD fallback
        proc_head = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=path, timeout=5.0)
        if proc_head.returncode == 0 and proc_head.stdout.strip():
            current_branch = f"HEAD ({proc_head.stdout.strip()})"

    # 2. Local branches list
    proc_list = _run_git_cmd(["branch", "--format=%(refname:short)"], cwd=path, timeout=5.0)
    if proc_list.returncode == 0:
        local_branches = [b.strip() for b in proc_list.stdout.splitlines() if b.strip()]

    # 3. Tracking branch
    proc_track = _run_git_cmd(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path, timeout=5.0)
    if proc_track.returncode == 0 and proc_track.stdout.strip():
        tracking_remote = proc_track.stdout.strip()

        # Count ahead / behind
        proc_counts = _run_git_cmd(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=path, timeout=5.0)
        if proc_counts.returncode == 0 and proc_counts.stdout.strip():
            parts = proc_counts.stdout.strip().split()
            if len(parts) == 2:
                try:
                    ahead = int(parts[0])
                    behind = int(parts[1])
                except ValueError:
                    pass

    return GitBranchInfo(
        current=current_branch,
        local_branches=local_branches,
        tracking_remote=tracking_remote,
        commits_ahead=ahead,
        commits_behind=behind,
    )


def get_git_status(project_path: str | Path) -> GitStatusResult:
    """Parse porcelain v1 status into a rich structured report."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitStatusResult(is_git=False)

    repo_root = get_repo_root(path)
    proc = _run_git_cmd(["status", "--porcelain=v1", "-b"], cwd=path)
    if proc.returncode != 0:
        return GitStatusResult(is_git=False)

    lines = proc.stdout.splitlines()
    branch_line = lines[0] if lines and lines[0].startswith("##") else "##"
    data_lines = lines[1:] if lines and lines[0].startswith("##") else lines

    # Parse branch header: ## branch...upstream [ahead X, behind Y]
    current_branch: Optional[str] = None
    tracking_branch: Optional[str] = None
    ahead = 0
    behind = 0

    branch_header = branch_line[2:].strip()
    if branch_header and not branch_header.startswith("Initial commit on") and not branch_header.startswith("No commits yet on"):
        if "..." in branch_header:
            parts = branch_header.split("...", 1)
            current_branch = parts[0].strip()
            rest = parts[1].strip()
            if "[" in rest:
                track_part, count_part = rest.split("[", 1)
                tracking_branch = track_part.strip()
                ahead_match = re.search(r"ahead\s+(\d+)", count_part)
                behind_match = re.search(r"behind\s+(\d+)", count_part)
                if ahead_match:
                    ahead = int(ahead_match.group(1))
                if behind_match:
                    behind = int(behind_match.group(1))
            else:
                tracking_branch = rest.strip()
        else:
            current_branch = branch_header.split()[0] if branch_header else None
    elif "on " in branch_header:
        current_branch = branch_header.split("on ", 1)[1].strip()

    staged_files: List[GitFileChange] = []
    unstaged_files: List[GitFileChange] = []
    untracked_files: List[GitFileChange] = []
    conflicted_files: List[GitFileChange] = []

    conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

    for line in data_lines:
        if len(line) < 4:
            continue
        x = line[0]
        y = line[1]
        raw_code = line[:2]
        filepath_part = line[3:].strip()
        orig_path = None
        if " -> " in filepath_part:
            parts = filepath_part.split(" -> ", 1)
            orig_path = parts[0].strip().strip('"')
            filepath_part = parts[1].strip().strip('"')
        else:
            filepath_part = filepath_part.strip('"')

        # Check conflict first
        if raw_code in conflict_codes or x == "U" or y == "U":
            conflicted_files.append(
                GitFileChange(
                    path=filepath_part,
                    change_type=GitChangeType.CONFLICTED,
                    staged=False,
                    original_path=orig_path,
                    raw_status=raw_code,
                )
            )
            continue

        # Untracked files
        if raw_code == "??":
            untracked_files.append(
                GitFileChange(
                    path=filepath_part,
                    change_type=GitChangeType.UNTRACKED,
                    staged=False,
                    raw_status=raw_code,
                )
            )
            continue

        # Staged changes (index)
        if x in ("M", "A", "D", "R", "C"):
            ctype = {
                "M": GitChangeType.MODIFIED,
                "A": GitChangeType.ADDED,
                "D": GitChangeType.DELETED,
                "R": GitChangeType.RENAMED,
                "C": GitChangeType.ADDED,
            }.get(x, GitChangeType.MODIFIED)
            staged_files.append(
                GitFileChange(
                    path=filepath_part,
                    change_type=ctype,
                    staged=True,
                    original_path=orig_path,
                    raw_status=x,
                )
            )

        # Unstaged changes (worktree)
        if y in ("M", "D"):
            ctype = GitChangeType.MODIFIED if y == "M" else GitChangeType.DELETED
            unstaged_files.append(
                GitFileChange(
                    path=filepath_part,
                    change_type=ctype,
                    staged=False,
                    original_path=orig_path,
                    raw_status=y,
                )
            )

    total_changes = len(staged_files) + len(unstaged_files) + len(untracked_files) + len(conflicted_files)
    is_clean = total_changes == 0

    return GitStatusResult(
        is_git=True,
        repo_root=str(repo_root) if repo_root else str(path),
        current_branch=current_branch,
        tracking_branch=tracking_branch,
        commits_ahead=ahead,
        commits_behind=behind,
        staged_files=staged_files,
        unstaged_files=unstaged_files,
        untracked_files=untracked_files,
        conflicted_files=conflicted_files,
        is_clean=is_clean,
        total_changes=total_changes,
    )


def get_git_diff(
    project_path: str | Path,
    staged: bool = False,
    filepath: Optional[str] = None,
    max_lines: int = 200,
) -> GitDiffResult:
    """Retrieve working-tree or staged diff with secret redaction and line bounding."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitDiffResult(diff_text="Not a Git repository.", total_lines=1, truncated=False, staged=staged)

    cmd = ["diff"]
    if staged:
        cmd.append("--staged")
    if filepath:
        clean = validate_files_in_repo(path, [filepath])
        if clean:
            cmd.extend(["--", clean[0]])

    proc = _run_git_cmd(cmd, cwd=path)
    raw_diff = proc.stdout if proc.returncode == 0 else proc.stderr

    # Also obtain stat summary
    stat_cmd = ["diff", "--stat"]
    if staged:
        stat_cmd.append("--staged")
    if filepath and clean:
        stat_cmd.extend(["--", clean[0]])
    proc_stat = _run_git_cmd(stat_cmd, cwd=path, timeout=5.0)
    stat_summary = proc_stat.stdout.strip() if proc_stat.returncode == 0 else ""

    # Redact secrets and bound output lines
    sanitized, total_lines, is_truncated = sanitize_and_redact_diff(raw_diff, max_lines=max_lines)

    # Determine files changed
    files_changed: List[str] = []
    for line in stat_summary.splitlines():
        if "|" in line:
            f_name = line.split("|", 1)[0].strip()
            if f_name:
                files_changed.append(f_name)

    return GitDiffResult(
        diff_text=sanitized,
        total_lines=total_lines,
        truncated=is_truncated,
        staged=staged,
        files_changed=files_changed,
        stat_summary=stat_summary,
    )


def stage_files(
    project_path: str | Path,
    files: Sequence[str],
) -> GitStageResult:
    """Stage specific files explicitly. Rejects path traversal and empty selections."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitStageResult(success=False, staged_count=0, error="Not a Git repository.")

    if not files:
        return GitStageResult(success=False, staged_count=0, error="No files specified for staging.")

    clean_files = validate_files_in_repo(path, files)
    if not clean_files:
        return GitStageResult(success=False, staged_count=0, error="No valid files to stage.")

    # Execute git add
    cmd = ["add", "--"] + clean_files
    proc = _run_git_cmd(cmd, cwd=path)
    if proc.returncode != 0:
        return GitStageResult(success=False, staged_count=0, error=proc.stderr.strip() or "git add failed.")

    return GitStageResult(
        success=True,
        staged_count=len(clean_files),
        modified_files=clean_files,
    )


def unstage_files(
    project_path: str | Path,
    files: Sequence[str],
) -> GitStageResult:
    """Unstage specified files safely."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitStageResult(success=False, staged_count=0, error="Not a Git repository.")

    if not files:
        return GitStageResult(success=False, staged_count=0, error="No files specified for unstaging.")

    clean_files = validate_files_in_repo(path, files)
    if not clean_files:
        return GitStageResult(success=False, staged_count=0, error="No valid files to unstage.")

    # Try git restore --staged first, fallback to git reset HEAD
    cmd = ["restore", "--staged", "--"] + clean_files
    proc = _run_git_cmd(cmd, cwd=path)
    if proc.returncode != 0:
        cmd_reset = ["reset", "HEAD", "--"] + clean_files
        proc = _run_git_cmd(cmd_reset, cwd=path)

    if proc.returncode != 0:
        return GitStageResult(success=False, staged_count=0, error=proc.stderr.strip() or "git unstage failed.")

    return GitStageResult(
        success=True,
        staged_count=len(clean_files),
        modified_files=clean_files,
    )


def commit(
    project_path: str | Path,
    message: str,
    author: Optional[str] = None,
) -> GitCommitResult:
    """Commit staged changes after verifying repository state and message validity."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitCommitResult(success=False, error="Not a Git repository.")

    if not message or not message.strip():
        return GitCommitResult(success=False, error="Commit message cannot be empty.")

    # Race condition check: verify there are staged changes right now
    status = get_git_status(path)
    if not status.staged_files:
        return GitCommitResult(success=False, error="No staged changes found to commit.")

    cmd = ["commit", "-m", message.strip()]
    if author:
        cmd.extend(["--author", author])

    proc = _run_git_cmd(cmd, cwd=path)
    if proc.returncode != 0:
        return GitCommitResult(success=False, error=proc.stderr.strip() or "git commit failed.")

    # Retrieve created commit hash
    proc_hash = _run_git_cmd(["rev-parse", "HEAD"], cwd=path, timeout=DEFAULT_TIMEOUT)
    commit_hash = proc_hash.stdout.strip() if proc_hash.returncode == 0 else "unknown"

    return GitCommitResult(
        success=True,
        commit_hash=commit_hash,
        message=message.strip(),
        files_committed=len(status.staged_files),
    )


def get_push_preview(
    project_path: str | Path,
    remote: str = "origin",
) -> GitPushPreview:
    """Analyze pending commits and upstream state before initiating push."""
    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitPushPreview(
            branch="unknown",
            remote=remote,
            remote_url="",
            ready_to_push=False,
            warning="Not a Git repository.",
        )

    branches = get_git_branches(path)
    branch = branches.current or "main"
    remotes = detect_remotes(path)
    remote_url = remotes.get(remote, "")

    commit_summaries: List[str] = []
    ahead = branches.commits_ahead

    # Log ahead commits
    if branches.tracking_remote:
        proc_log = _run_git_cmd(["log", f"{branches.tracking_remote}..HEAD", "--oneline", "-n", "10"], cwd=path)
        if proc_log.returncode == 0 and proc_log.stdout.strip():
            commit_summaries = proc_log.stdout.strip().splitlines()
    else:
        # No upstream set yet
        proc_log = _run_git_cmd(["log", "--oneline", "-n", "5"], cwd=path)
        if proc_log.returncode == 0 and proc_log.stdout.strip():
            commit_summaries = proc_log.stdout.strip().splitlines()
            ahead = len(commit_summaries)

    warning = None
    if not remote_url:
        warning = f"No remote URL found for remote '{remote}'."
    elif ahead == 0:
        warning = "No new commits ahead of remote tracking branch."

    return GitPushPreview(
        branch=branch,
        remote=remote,
        remote_url=remote_url,
        upstream=branches.tracking_remote,
        commits_ahead=ahead,
        commit_summaries=commit_summaries,
        ready_to_push=bool(remote_url and ahead > 0),
        warning=warning,
    )


def push(
    project_path: str | Path,
    remote: str = "origin",
    branch: Optional[str] = None,
    force: bool = False,
) -> GitPushResult:
    """Safely execute git push with strict protection against force-push."""
    if force:
        raise ValueError("Force push is strictly prohibited by policy invariant.")

    path = validate_repo_path(project_path)
    if not is_git_repository(path):
        return GitPushResult(success=False, branch=branch or "", remote=remote, error="Not a Git repository.")

    # Detect current branch if not specified
    if not branch:
        branches = get_git_branches(path)
        branch = branches.current
        if not branch:
            return GitPushResult(success=False, branch="", remote=remote, error="Cannot push detached HEAD.")

    # Re-check repository state before push
    status = get_git_status(path)
    if status.conflicted_files:
        return GitPushResult(
            success=False,
            branch=branch,
            remote=remote,
            error="Repository has unresolved merge conflicts. Resolve conflicts before pushing.",
        )

    # Build command arguments (no force flag allowed)
    args = ["push", remote, branch]
    assert_no_force_push(args)

    proc = _run_git_cmd(args, cwd=path, timeout=30.0)
    if proc.returncode != 0:
        stderr_lower = proc.stderr.lower()
        if "authentication failed" in stderr_lower or "fatal: could not read username" in stderr_lower or "permission denied (publickey)" in stderr_lower:
            return GitPushResult(
                success=False,
                branch=branch,
                remote=remote,
                error="Authentication failed: Remote credentials were rejected or credential helper unavailable.",
            )
        if "rejected" in stderr_lower and "fetch first" in stderr_lower or "non-fast-forward" in stderr_lower:
            return GitPushResult(
                success=False,
                branch=branch,
                remote=remote,
                error="Push rejected: Remote contains changes not present locally. Pull before pushing.",
            )
        return GitPushResult(
            success=False,
            branch=branch,
            remote=remote,
            error=proc.stderr.strip() or "git push failed.",
        )

    return GitPushResult(
        success=True,
        branch=branch,
        remote=remote,
        message=f"Pushed {branch} to {remote} successfully.",
    )
