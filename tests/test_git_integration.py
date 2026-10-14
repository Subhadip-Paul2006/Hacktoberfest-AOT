"""Comprehensive tests for Safe Git & GitHub Integration (Phase 6).

Covers all 30 scenarios from specification:
1. Git repository detection
2. Non-Git project
3. Git status parsing
4. Modified files
5. Untracked files
6. Deleted files
7. Renamed files
8. Conflict state
9. Current branch
10. Remote detection
11. Non-GitHub remote
12. GitHub remote
13. Diff retrieval
14. Large diff chunking
15. Secret redaction in diff
16. File staging
17. Partial staging
18. Commit workflow
19. Duplicate commit request
20. Push permission
21. Push authentication failure
22. Push success
23. Force-push rejection
24. Workspace lock conflict
25. State changed before commit
26. State changed before push
27. GitHub API authentication failure
28. PR creation
29. PR duplicate request
30. Telegram Git callbacks
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sutra.git import (
    GitChangeType,
    GitManager,
    commit,
    create_pull_request,
    detect_github_remote,
    detect_remotes,
    get_git_branches,
    get_git_diff,
    get_git_status,
    get_push_preview,
    is_git_repository,
    parse_github_remote,
    push,
    stage_files,
    unstage_files,
    validate_files_in_repo,
    assert_no_force_push,
    check_workspace_lock,
)
from sutra.permissions import PermissionEngine, PermissionRequest, PermissionRiskLevel
from sutra.workspace_lock import LockMode, WorkspaceConflictError, WorkspaceLockManager


def _init_repo(path: Path) -> Path:
    """Helper to initialize an isolated git repository with standard user credentials."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Sutra Test"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@sutra.dev"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(path), check=True, capture_output=True)
    return path


# 1. Git repository detection
def test_01_git_repository_detection(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    assert is_git_repository(repo) is True
    assert GitManager.is_repository(repo) is True


# 2. Non-Git project
def test_02_non_git_project(tmp_path: Path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    assert is_git_repository(plain_dir) is False
    status = get_git_status(plain_dir)
    assert status.is_git is False
    assert status.is_clean is True


# 3. Git status parsing
def test_03_git_status_parsing(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "init.txt").write_text("hello", encoding="utf-8")
    stage_files(repo, ["init.txt"])
    commit(repo, "initial commit")

    status = get_git_status(repo)
    assert status.is_git is True
    assert status.is_clean is True
    assert status.current_branch == "main"
    assert status.total_changes == 0


# 4. Modified files
def test_04_modified_files(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    file_path = repo / "hello.py"
    file_path.write_text("print('hello')", encoding="utf-8")
    stage_files(repo, ["hello.py"])
    commit(repo, "add hello")

    # Modify
    file_path.write_text("print('hello world')", encoding="utf-8")
    status = get_git_status(repo)
    assert status.is_clean is False
    assert len(status.unstaged_files) == 1
    assert status.unstaged_files[0].path == "hello.py"
    assert status.unstaged_files[0].change_type == GitChangeType.MODIFIED


# 5. Untracked files
def test_05_untracked_files(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "untracked.log").write_text("debug output", encoding="utf-8")

    status = get_git_status(repo)
    assert len(status.untracked_files) == 1
    assert status.untracked_files[0].path == "untracked.log"
    assert status.untracked_files[0].change_type == GitChangeType.UNTRACKED


# 6. Deleted files
def test_06_deleted_files(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    del_file = repo / "temp.txt"
    del_file.write_text("to delete", encoding="utf-8")
    stage_files(repo, ["temp.txt"])
    commit(repo, "add temp")

    del_file.unlink()
    status = get_git_status(repo)
    assert len(status.unstaged_files) == 1
    assert status.unstaged_files[0].path == "temp.txt"
    assert status.unstaged_files[0].change_type == GitChangeType.DELETED


# 7. Renamed files
def test_07_renamed_files(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    orig_file = repo / "orig.txt"
    orig_file.write_text("content", encoding="utf-8")
    stage_files(repo, ["orig.txt"])
    commit(repo, "add orig")

    subprocess.run(["git", "mv", "orig.txt", "renamed.txt"], cwd=str(repo), check=True, capture_output=True)
    status = get_git_status(repo)
    assert len(status.staged_files) == 1
    assert status.staged_files[0].path == "renamed.txt"
    assert status.staged_files[0].change_type == GitChangeType.RENAMED


# 8. Conflict state
def test_08_conflict_state(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    # Simulate conflict porcelain parsing
    with patch("sutra.git.local._run_git_cmd") as mock_cmd:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "## main\nUU conflict.py\n"
        mock_cmd.return_value = mock_proc

        status = get_git_status(repo)
        assert len(status.conflicted_files) == 1
        assert status.conflicted_files[0].path == "conflict.py"
        assert status.conflicted_files[0].change_type == GitChangeType.CONFLICTED


# 9. Current branch
def test_09_current_branch(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "init.txt").write_text("x", encoding="utf-8")
    stage_files(repo, ["init.txt"])
    commit(repo, "c1")

    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=str(repo), check=True, capture_output=True)
    branches = get_git_branches(repo)
    assert branches.current == "feature/auth"
    assert "feature/auth" in branches.local_branches


# 10. Remote detection
def test_10_remote_detection(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    subprocess.run(["git", "remote", "add", "origin", "https://example.com/repo.git"], cwd=str(repo), check=True, capture_output=True)
    remotes = detect_remotes(repo)
    assert "origin" in remotes
    assert remotes["origin"] == "https://example.com/repo.git"


# 11. Non-GitHub remote
def test_11_non_github_remote():
    info = parse_github_remote("https://gitlab.com/sutra-team/sutra.git")
    assert info.is_github is False
    assert info.owner is None
    assert info.repo is None


# 12. GitHub remote
def test_12_github_remote():
    info_https = parse_github_remote("https://github.com/sutra-team/sutra.git")
    assert info_https.is_github is True
    assert info_https.owner == "sutra-team"
    assert info_https.repo == "sutra"

    info_ssh = parse_github_remote("git@github.com:octocat/Hello-World.git")
    assert info_ssh.is_github is True
    assert info_ssh.owner == "octocat"
    assert info_ssh.repo == "Hello-World"


# 13. Diff retrieval
def test_13_diff_retrieval(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    f = repo / "calc.py"
    f.write_text("x = 1\n", encoding="utf-8")
    stage_files(repo, ["calc.py"])
    commit(repo, "c1")

    f.write_text("x = 2\ny = 3\n", encoding="utf-8")
    diff = get_git_diff(repo, staged=False)
    assert "calc.py" in diff.stat_summary
    assert "+x = 2" in diff.diff_text
    assert diff.truncated is False


# 14. Large diff chunking
def test_14_large_diff_chunking(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    f = repo / "big.txt"
    f.write_text("line\n" * 300, encoding="utf-8")
    stage_files(repo, ["big.txt"])
    commit(repo, "c1")

    f.write_text("changed\n" * 300, encoding="utf-8")
    diff = get_git_diff(repo, staged=False, max_lines=50)
    assert diff.truncated is True
    assert "... [Diff truncated:" in diff.diff_text


# 15. Secret redaction in diff
def test_15_secret_redaction_in_diff(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    f = repo / "config.py"
    f.write_text("KEY = ''\n", encoding="utf-8")
    stage_files(repo, ["config.py"])
    commit(repo, "c1")

    token = "ghp_" + "1" * 36
    f.write_text(f"GITHUB_TOKEN = '{token}'\n", encoding="utf-8")
    diff = get_git_diff(repo, staged=False)
    assert token not in diff.diff_text
    assert "[REDACTED_GH_TOKEN]" in diff.diff_text or "REDACTED" in diff.diff_text


# 16. File staging
def test_16_file_staging(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    f = repo / "staged.txt"
    f.write_text("content", encoding="utf-8")

    res = stage_files(repo, ["staged.txt"])
    assert res.success is True
    assert res.staged_count == 1
    status = get_git_status(repo)
    assert len(status.staged_files) == 1
    assert status.staged_files[0].path == "staged.txt"


# 17. Partial staging
def test_17_partial_staging(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "f1.txt").write_text("1", encoding="utf-8")
    (repo / "f2.txt").write_text("2", encoding="utf-8")

    stage_files(repo, ["f1.txt"])
    status = get_git_status(repo)
    assert len(status.staged_files) == 1
    assert status.staged_files[0].path == "f1.txt"
    assert len(status.untracked_files) == 1
    assert status.untracked_files[0].path == "f2.txt"


# 18. Commit workflow
def test_18_commit_workflow(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('app')", encoding="utf-8")
    stage_files(repo, ["app.py"])

    res = commit(repo, "add app")
    assert res.success is True
    assert res.commit_hash is not None
    assert len(res.commit_hash) >= 7
    assert res.files_committed == 1


# 19. Duplicate commit request
def test_19_duplicate_commit_request(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("app", encoding="utf-8")
    stage_files(repo, ["app.py"])
    res1 = commit(repo, "commit 1")
    assert res1.success is True

    # Immediate second commit without staging
    res2 = commit(repo, "commit 2")
    assert res2.success is False
    assert "No staged changes found to commit" in res2.error


# 20. Push permission
def test_20_push_permission():
    pe = PermissionEngine()
    req = PermissionRequest(
        request_id="req-push-1",
        action_type="git_push",
        resource_path="/repo",
        command="git push origin main",
        agent_id="test",
        session_id="s1",
    )
    allowed, reason = pe.check_permission(req)
    assert allowed is False
    assert "HIGH risk action requires authorization" in reason


# 21. Push authentication failure
def test_21_push_authentication_failure(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(repo, ["a.txt"])
    commit(repo, "c1")
    subprocess.run(["git", "remote", "add", "origin", "https://invalid-host.github.internal/repo.git"], cwd=str(repo), check=True)

    from sutra.git.local import _run_git_cmd as real_run
    def mock_run(args, cwd, **kwargs):
        if args and args[0] == "push":
            mock_proc = MagicMock()
            mock_proc.returncode = 128
            mock_proc.stderr = "fatal: could not read Username for 'https://invalid-host.github.internal': No such file or directory\n"
            return mock_proc
        return real_run(args, cwd, **kwargs)

    with patch("sutra.git.local._run_git_cmd", side_effect=mock_run):
        res = push(repo, remote="origin")
        assert res.success is False
        assert "Authentication failed" in res.error


# 22. Push success
def test_22_push_success(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=str(repo), check=True)
    (repo / "file.txt").write_text("data", encoding="utf-8")
    stage_files(repo, ["file.txt"])
    commit(repo, "initial commit")

    res = push(repo, remote="origin", branch="main")
    assert res.success is True
    assert "successfully" in res.message


# 23. Force-push rejection
def test_23_force_push_rejection(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="Force push is strictly prohibited"):
        push(repo, force=True)

    with pytest.raises(ValueError, match="Force push is strictly prohibited"):
        assert_no_force_push(["push", "--force", "origin", "main"])

    with pytest.raises(ValueError, match="Force push is strictly prohibited"):
        assert_no_force_push(["push", "--force-with-lease", "origin", "main"])


# 24. Workspace lock conflict
@pytest.mark.asyncio
async def test_24_workspace_lock_conflict(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    lock_mgr = WorkspaceLockManager()
    await lock_mgr.acquire(repo, session_id="agent-session-1", mode=LockMode.WRITE)

    with pytest.raises(WorkspaceConflictError):
        check_workspace_lock(lock_mgr, repo, current_session="different-session")


# 25. State changed before commit
def test_25_state_changed_before_commit(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    f = repo / "f.txt"
    f.write_text("v1", encoding="utf-8")
    stage_files(repo, ["f.txt"])

    # Simulate race condition: file was unstaged before commit
    unstage_files(repo, ["f.txt"])
    res = commit(repo, "should fail")
    assert res.success is False
    assert "No staged changes found" in res.error


# 26. State changed before push
def test_26_state_changed_before_push(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    with patch("sutra.git.local.get_git_status") as mock_st:
        from sutra.git.models import GitFileChange, GitStatusResult
        mock_st.return_value = GitStatusResult(
            is_git=True,
            conflicted_files=[GitFileChange(path="x.py", change_type=GitChangeType.CONFLICTED, staged=False)],
        )
        res = push(repo, branch="main")
        assert res.success is False
        assert "unresolved merge conflicts" in res.error


# 27. GitHub API authentication failure
@pytest.mark.asyncio
async def test_27_github_api_authentication_failure():
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    res = await create_pull_request(
        owner="org",
        repo="repo",
        title="Add feature",
        body="details",
        head="feature",
        token="bad_token",
        client=mock_client,
    )
    assert res.success is False
    assert "authentication failed" in res.error.lower()


# 28. PR creation
@pytest.mark.asyncio
async def test_28_pr_creation():
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "number": 42,
        "html_url": "https://github.com/org/repo/pull/42",
        "title": "Add feature",
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
        "state": "open",
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    res = await create_pull_request(
        owner="org",
        repo="repo",
        title="Add feature",
        body="details",
        head="feature",
        token="valid_token",
        client=mock_client,
    )
    assert res.success is True
    assert res.number == 42
    assert res.html_url == "https://github.com/org/repo/pull/42"


# 29. PR duplicate request
@pytest.mark.asyncio
async def test_29_pr_duplicate_request():
    from sutra.git.resilience import get_github_boundary
    get_github_boundary().idempotency_store.clear()
    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.json.return_value = {
        "message": "Validation Failed",
        "errors": [{"message": "A pull request already exists for org:feature."}],
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    res = await create_pull_request(
        owner="org",
        repo="repo",
        title="Add feature",
        body="details",
        head="feature",
        token="valid_token",
        client=mock_client,
    )
    assert res.success is False
    assert "already exists" in res.error


# 30. Telegram Git callbacks
@pytest.mark.asyncio
async def test_30_telegram_git_callbacks():
    from sutra.apps.telegram.callbacks import handle_callback
    from sutra.apps.telegram.formatting import render_screen

    bot = MagicMock()
    bot.callback_targets = {}
    bot.active_git_context = {}
    bot.active_stage_selection = {}
    bot.screen_messages = {}
    bot.selected_devices = {}
    bot.navigation_history = {}
    bot._reply = MagicMock(return_value="mock_reply")
    bot._target_button = MagicMock(return_value={"text": "btn", "callback_data": "target:1"})
    bot._button = MagicMock(return_value={"text": "btn", "callback_data": "btn:1"})
    bot._nav_buttons = MagicMock(return_value=[])

    # Test screen rendering for git screen with non-git repo
    bot._dispatch_command = AsyncMock(return_value={"status": "success", "payload": {"is_git": False}})
    context = {"device_id": "AB-PC-123456", "project": {"name": "TestProj", "absolute_path": "/test"}}
    await render_screen(bot, "git", context, chat_id=123, user_id=456)
    bot._reply.assert_called()
    assert "Not a Git repository" in bot._reply.call_args[0][0]

    # Test screen rendering for git screen with git repo
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {
            "is_git": True,
            "current_branch": "main",
            "is_clean": True,
            "commits_ahead": 0,
            "commits_behind": 0,
        }
    })
    await render_screen(bot, "git", context, chat_id=123, user_id=456)
    assert "Clean working tree" in bot._reply.call_args[0][0]
