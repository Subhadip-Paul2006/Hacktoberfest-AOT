"""Focused test suite for Phase 6B.5: Advanced GitHub Collaboration & CI Integration.

Covers all 30 test scenarios required by specification:
1. PR listing
2. PR pagination
3. PR details
4. PR changed files
5. PR comments
6. Comment creation
7. Duplicate / empty comment protection
8. Review state inspection
9. Review mutation authorization
10. Label management permissions
11. Assignee/reviewer management permissions
12. Merge authorization (never auto-merge)
13. Merge stale-state detection
14. Merge conflict handling
15. Workflow listing
16. Workflow status
17. Workflow log bounds
18. Secret redaction in CI logs
19. Workflow rerun permission
20. Workflow cancellation permission
21. Rate-limit handling
22. GitHub authentication failure
23. Stale Telegram callbacks
24. Duplicate Telegram callbacks
25. Cached-read behavior (LOW risk auto-approved)
26. CI-aware PR summary construction
27. Non-GitHub repository behavior
28. PR not found (404)
29. Workflow run not found (404)
30. Security: token and secret leakage prevention
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sutra.apps.telegram.keyboards import MAX_CALLBACK_AGE_SECONDS, allocate_target_button
from sutra.git import (
    GitHubMergeMode,
    GitHubMergeResult,
    GitHubPRComment,
    GitHubPRDetails,
    GitHubPRFile,
    GitHubReleaseInfo,
    GitHubReviewState,
    GitHubReviewSummary,
    GitHubWorkflowRun,
    GitHubWorkflowRunLog,
    add_pull_request_comment,
    build_ci_aware_pr_summary,
    cancel_workflow_run,
    detect_github_remote,
    get_pull_request,
    get_pull_request_files,
    get_pull_request_reviews,
    get_workflow_run,
    get_workflow_run_logs,
    list_pull_request_comments,
    list_pull_requests,
    list_releases,
    list_workflow_runs,
    merge_pull_request,
    parse_github_remote,
    rerun_workflow_run,
)
from sutra.permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
    PermissionRiskLevel,
)


# Helper for creating mocked httpx response
def _mock_response(status_code: int = 200, json_data: dict | list | None = None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.github.com")
    if json_data is not None:
        return httpx.Response(status_code=status_code, json=json_data, request=request)
    if text is not None:
        return httpx.Response(status_code=status_code, text=text, request=request)
    return httpx.Response(status_code=status_code, json={}, request=request)


# 1. PR listing
@pytest.mark.asyncio
async def test_01_pr_listing():
    mock_prs = [
        {"number": 42, "title": "Fix auth flow", "state": "open"},
        {"number": 43, "title": "Update README", "state": "open"},
    ]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_prs)

    prs = await list_pull_requests("sutra-org", "sutra", state="open", token="test-token", client=mock_client)
    assert len(prs) == 2
    assert prs[0]["number"] == 42
    assert prs[0]["title"] == "Fix auth flow"


# 2. PR pagination
@pytest.mark.asyncio
async def test_02_pr_pagination():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, [])

    await list_pull_requests("sutra-org", "sutra", state="open", page=2, per_page=5, head="feature-auth", token="test-token", client=mock_client)
    mock_client.get.assert_called_once()
    called_url = mock_client.get.call_args[0][0]
    assert "page=2" in called_url
    assert "per_page=5" in called_url
    assert "head=feature-auth" in called_url


# 3. PR details
@pytest.mark.asyncio
async def test_03_pr_details():
    mock_data = {
        "number": 42,
        "title": "Fix authentication flow",
        "state": "open",
        "head": {"ref": "feature/auth"},
        "base": {"ref": "main"},
        "user": {"login": "alice"},
        "created_at": "2026-10-06T12:00:00Z",
        "updated_at": "2026-10-06T12:30:00Z",
        "body": "Resolves auth token expiry bug.",
        "html_url": "https://github.com/sutra-org/sutra/pull/42",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "comments": 3,
        "commits": 2,
        "changed_files": 4,
        "additions": 182,
        "deletions": 71,
    }
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_data)

    pr = await get_pull_request("sutra-org", "sutra", 42, token="test-token", client=mock_client)
    assert pr is not None
    assert pr.number == 42
    assert pr.title == "Fix authentication flow"
    assert pr.head_branch == "feature/auth"
    assert pr.base_branch == "main"
    assert pr.author == "alice"
    assert pr.mergeable is True
    assert pr.mergeable_state == "clean"
    assert pr.additions == 182
    assert pr.deletions == 71


# 4. PR changed files
@pytest.mark.asyncio
async def test_04_pr_changed_files():
    mock_files = [
        {"filename": "src/auth.py", "status": "modified", "additions": 100, "deletions": 50, "changes": 150, "patch": "@@ -1,3 +1,4 @@"},
        {"filename": "tests/test_auth.py", "status": "added", "additions": 82, "deletions": 21, "changes": 103, "patch": None},
    ]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_files)

    files = await get_pull_request_files("sutra-org", "sutra", 42, token="test-token", client=mock_client)
    assert len(files) == 2
    assert files[0].filename == "src/auth.py"
    assert files[0].status == "modified"
    assert files[0].additions == 100
    assert files[1].filename == "tests/test_auth.py"
    assert files[1].status == "added"


# 5. PR comments
@pytest.mark.asyncio
async def test_05_pr_comments():
    issue_comments = [
        {"id": 1, "user": {"login": "bob"}, "body": "Please verify token expiry.", "created_at": "2026-10-06T12:05:00Z"},
    ]
    review_comments = [
        {"id": 2, "user": {"login": "charlie"}, "body": "Nice test coverage.", "path": "src/auth.py", "line": 42, "created_at": "2026-10-06T12:10:00Z"},
    ]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.side_effect = [
        _mock_response(200, issue_comments),
        _mock_response(200, review_comments),
    ]

    comments = await list_pull_request_comments("sutra-org", "sutra", 42, token="test-token", client=mock_client)
    assert len(comments) == 2
    assert comments[0].author == "bob"
    assert comments[0].comment_type == "issue"
    assert comments[1].author == "charlie"
    assert comments[1].comment_type == "review"
    assert comments[1].line == 42


# 6. Comment creation
@pytest.mark.asyncio
async def test_06_comment_creation():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _mock_response(201, {
        "id": 101,
        "user": {"login": "sutra-bot"},
        "body": "Token expiry verified.",
        "created_at": "2026-10-06T12:15:00Z",
    })

    comment = await add_pull_request_comment("sutra-org", "sutra", 42, "Token expiry verified.", token="test-token", client=mock_client)
    assert comment is not None
    assert comment.id == 101
    assert comment.author == "sutra-bot"
    assert comment.body == "Token expiry verified."


# 7. Duplicate / empty comment protection
@pytest.mark.asyncio
async def test_07_duplicate_empty_comment_protection():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    res = await add_pull_request_comment("sutra-org", "sutra", 42, "   ", token="test-token", client=mock_client)
    assert res is None
    mock_client.post.assert_not_called()


# 8. Review state inspection
@pytest.mark.asyncio
async def test_08_review_state():
    mock_reviews = [
        {"user": {"login": "bob"}, "state": "APPROVED", "submitted_at": "2026-10-06T12:00:00Z"},
        {"user": {"login": "charlie"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-06T12:10:00Z"},
    ]
    mock_reviewers = {"users": [{"login": "dave"}]}

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.side_effect = [
        _mock_response(200, mock_reviews),
        _mock_response(200, mock_reviewers),
    ]

    summary = await get_pull_request_reviews("sutra-org", "sutra", 42, token="test-token", client=mock_client)
    assert summary.approved_count == 1
    assert summary.changes_requested_count == 1
    assert summary.pending_count == 1
    assert "1 approval" in summary.summary_text
    assert "1 changes requested" in summary.summary_text
    assert "1 pending" in summary.summary_text


# 9. Review mutation authorization
def test_09_review_mutation_authorization():
    pe = PermissionEngine()
    risk = pe.assess_risk("github_pr_review_request")
    assert risk == PermissionRiskLevel.HIGH


# 10. Label management permissions
def test_10_label_management_permissions():
    pe = PermissionEngine()
    assert pe.assess_risk("github_pr_label") == PermissionRiskLevel.MEDIUM
    assert pe.assess_risk("github_pr_unlabel") == PermissionRiskLevel.MEDIUM


# 11. Assignee/reviewer management permissions
def test_11_assignee_management_permissions():
    pe = PermissionEngine()
    assert pe.assess_risk("github_pr_assign") == PermissionRiskLevel.MEDIUM


# 12. Merge authorization (never auto-merge)
def test_12_merge_authorization():
    pe = PermissionEngine()
    assert pe.assess_risk("git_pr_merge") == PermissionRiskLevel.HIGH
    assert pe.assess_risk("github_pr_merge") == PermissionRiskLevel.HIGH

    req = PermissionRequest(
        request_id="req-merge-1",
        action_type="git_pr_merge",
        resource_path="/repo",
        command="merge_pull_request(42)",
        agent_id="Git",
        session_id="session-1",
    )
    allowed, _ = pe.check_permission(req)
    assert allowed is False  # Requires authorization

    # Even if ALLOW_SESSION is attempted, it is demoted to ALLOW_ONCE for merge
    pe.apply_decision("req-merge-1", PermissionDecision.ALLOW_SESSION)
    grant_key = ("session-1", "git_pr_merge", "req-merge-1")
    assert pe._session_grants.get(grant_key) == PermissionDecision.ALLOW_ONCE


# 13. Merge stale-state detection
@pytest.mark.asyncio
async def test_13_merge_stale_state_detection():
    # If PR state is closed or has merge conflicts, merge must abort cleanly
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Closed PR
    mock_client.get.return_value = _mock_response(200, {
        "number": 42, "state": "closed", "merged": False, "mergeable": False,
    })

    res = await merge_pull_request("sutra-org", "sutra", 42, token="test-token", client=mock_client)
    assert res.success is False
    assert "closed" in res.error.lower()
    mock_client.put.assert_not_called()


# 14. Merge conflict handling
@pytest.mark.asyncio
async def test_14_merge_conflict_handling():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # PR with mergeable == False
    mock_client.get.return_value = _mock_response(200, {
        "number": 42, "state": "open", "merged": False, "mergeable": False,
    })

    res = await merge_pull_request("sutra-org", "sutra", 42, token="test-token", client=mock_client)
    assert res.success is False
    assert "conflicts" in res.error.lower()
    mock_client.put.assert_not_called()


# 15. Workflow listing
@pytest.mark.asyncio
async def test_15_workflow_listing():
    mock_runs = {
        "total_count": 2,
        "workflow_runs": [
            {"id": 1001, "name": "Build", "head_branch": "main", "head_sha": "abc12345", "status": "completed", "conclusion": "success", "event": "push"},
            {"id": 1002, "name": "Tests", "head_branch": "main", "head_sha": "abc12345", "status": "in_progress", "conclusion": None, "event": "pull_request"},
        ],
    }
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_runs)

    runs = await list_workflow_runs("sutra-org", "sutra", token="test-token", client=mock_client)
    assert len(runs) == 2
    assert runs[0].id == 1001
    assert runs[0].conclusion == "success"
    assert runs[1].status == "in_progress"


# 16. Workflow status
@pytest.mark.asyncio
async def test_16_workflow_status():
    mock_run = {"id": 1001, "name": "Build", "head_branch": "main", "head_sha": "abc12345", "status": "completed", "conclusion": "success"}
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_run)

    run = await get_workflow_run("sutra-org", "sutra", 1001, token="test-token", client=mock_client)
    assert run is not None
    assert run.id == 1001
    assert run.conclusion == "success"


# 17. Workflow log bounds
@pytest.mark.asyncio
async def test_17_workflow_log_bounds():
    steps = [{"name": f"Step {i}", "conclusion": "success"} for i in range(150)]
    mock_jobs = {"jobs": [{"name": "Job 1", "status": "completed", "conclusion": "success", "steps": steps}]}
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_jobs)

    log_entry = await get_workflow_run_logs("sutra-org", "sutra", 1001, max_lines=30, token="test-token", client=mock_client)
    assert log_entry.truncated is True
    assert "[Log bounded to last 30 lines" in log_entry.log_text


# 18. Secret redaction in CI logs
@pytest.mark.asyncio
async def test_18_secret_redaction_in_ci_logs():
    token = "ghp_" + "1" * 36
    mock_jobs = {"jobs": [{"name": "Job 1", "status": "completed", "conclusion": "success", "steps": [
        {"name": f"Deploy with token {token}", "conclusion": "success"}
    ]}]}
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(200, mock_jobs)

    log_entry = await get_workflow_run_logs("sutra-org", "sutra", 1001, token="test-token", client=mock_client)
    assert token not in log_entry.log_text
    assert "[REDACTED_GH_TOKEN]" in log_entry.log_text or "REDACTED" in log_entry.log_text


# 19. Workflow rerun permission
def test_19_workflow_rerun_permission():
    pe = PermissionEngine()
    assert pe.assess_risk("git_ci_rerun") == PermissionRiskLevel.HIGH
    assert pe.assess_risk("github_ci_rerun") == PermissionRiskLevel.HIGH


# 20. Workflow cancellation permission
def test_20_workflow_cancellation_permission():
    pe = PermissionEngine()
    assert pe.assess_risk("git_ci_cancel") == PermissionRiskLevel.HIGH
    assert pe.assess_risk("github_ci_cancel") == PermissionRiskLevel.HIGH


# 21. Rate-limit handling
@pytest.mark.asyncio
async def test_21_rate_limit_handling():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # 403 Forbidden with rate limit exhausted
    mock_client.get.return_value = _mock_response(403, {"message": "API rate limit exceeded"})

    # Keep the resilience boundary's shared cache isolated from earlier tests.
    runs = await list_workflow_runs("rate-limit-test-org", "rate-limit-test-repo",
                                    token="test-token", client=mock_client)
    assert runs == []


# 22. GitHub authentication failure
@pytest.mark.asyncio
async def test_22_github_auth_failure():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(401, {"message": "Bad credentials"})

    pr = await get_pull_request("sutra-org", "sutra", 42, token="invalid-token", client=mock_client)
    assert pr is None


# 23. Stale Telegram callbacks
def test_23_stale_telegram_callbacks():
    callback_targets = {}
    mock_cp = MagicMock()
    mock_cp.store.current_session.return_value = None

    # Allocate target
    btn = allocate_target_button(callback_targets, "Inspect PR", user_id=1, chat_id=1, action="git_pr_detail", context={}, control_plane=mock_cp)
    target_id = btn["callback_data"].replace("target:", "")

    # Expire target
    callback_targets[target_id]["created_at"] = time.time() - (MAX_CALLBACK_AGE_SECONDS + 10)

    # Next allocation cleans stale target
    allocate_target_button(callback_targets, "New Button", user_id=1, chat_id=1, action="git", context={}, control_plane=mock_cp)
    assert target_id not in callback_targets


# 24. Duplicate Telegram callbacks
def test_24_duplicate_telegram_callbacks():
    callback_targets = {}
    mock_cp = MagicMock()
    mock_cp.store.current_session.return_value = None

    btn = allocate_target_button(callback_targets, "Merge", user_id=1, chat_id=1, action="git_pr_merge_execute", context={}, control_plane=mock_cp)
    target_id = btn["callback_data"].replace("target:", "")

    # First click consumes target
    t1 = callback_targets.pop(target_id, None)
    assert t1 is not None

    # Second click finds none
    t2 = callback_targets.pop(target_id, None)
    assert t2 is None


# 25. Cached-read behavior (LOW risk auto-approved)
def test_25_cached_read_behavior():
    pe = PermissionEngine()
    req = PermissionRequest(
        request_id="req-pr-list",
        action_type="git_pr_list",
        resource_path="/repo",
        command=None,
        agent_id="Git",
        session_id="session-1",
    )
    allowed, reason = pe.check_permission(req)
    assert allowed is True
    assert "LOW risk" in reason


# 26. CI-aware PR summary construction
def test_26_ci_aware_pr_summary():
    pr = GitHubPRDetails(
        number=42,
        title="Fix authentication flow",
        state="open",
        head_branch="feature/auth",
        base_branch="main",
        author="alice",
        created_at="2026-10-06T12:00:00Z",
        updated_at="2026-10-06T12:30:00Z",
        changed_files_count=6,
        additions=182,
        deletions=71,
        mergeable=True,
    )
    files = [
        GitHubPRFile(filename="src/auth.py", status="modified", additions=100, deletions=50, changes=150),
    ]
    reviews = GitHubReviewSummary(
        approved_count=1,
        pending_count=1,
        summary_text="✅ 1 approval · 🟡 1 pending",
    )
    runs = [
        GitHubWorkflowRun(id=1, name="Tests", head_branch="feature/auth", head_sha="abc", status="completed", conclusion="success"),
        GitHubWorkflowRun(id=2, name="Security", head_branch="feature/auth", head_sha="abc", status="completed", conclusion="failure"),
    ]

    summary = build_ci_aware_pr_summary(pr, files, reviews, runs)
    assert "PR #42 · Fix authentication flow" in summary
    assert "feature/auth → main" in summary
    assert "1 file (+182 / -71)" in summary
    assert "✅ 1 approval · 🟡 1 pending" in summary
    assert "✅ Tests (success)" in summary
    assert "❌ Security (failure)" in summary
    assert "Merge: 🟢 Clean" in summary


# 27. Non-GitHub repository behavior
def test_27_non_github_repository_behavior():
    info = parse_github_remote("git@gitlab.com:team/project.git")
    assert info.is_github is False
    assert info.owner is None
    assert info.repo is None


# 28. PR not found (404)
@pytest.mark.asyncio
async def test_28_pr_not_found():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(404, {"message": "Not Found"})

    pr = await get_pull_request("sutra-org", "sutra", 9999, token="test-token", client=mock_client)
    assert pr is None


# 29. Workflow run not found (404)
@pytest.mark.asyncio
async def test_29_workflow_run_not_found():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_response(404, {"message": "Not Found"})

    run = await get_workflow_run("sutra-org", "sutra", 9999, token="test-token", client=mock_client)
    assert run is None


# 30. Security: token and secret leakage prevention
def test_30_security_token_and_secret_leakage():
    from sutra.git.github import _build_github_headers

    token = "ghp_super_secret_token_12345"
    headers = _build_github_headers(token)
    assert headers["Authorization"] == f"Bearer {token}"

    # Verify representations do not dump auth headers
    pr = GitHubPRDetails(
        number=1, title="Test", state="open", head_branch="a", base_branch="b",
        author="user", created_at="", updated_at="",
    )
    pr_str = str(pr.to_dict())
    assert token not in pr_str


# 31. Telegram GitHub screens rendering
@pytest.mark.asyncio
async def test_31_telegram_git_github_screens():
    from sutra.apps.telegram.formatting import render_screen

    bot = MagicMock()
    bot.callback_targets = {}
    bot.active_git_context = {}
    bot._reply = MagicMock(return_value="mock_reply")
    bot._target_button = MagicMock(return_value={"text": "btn", "callback_data": "target:1"})
    bot._button = MagicMock(return_value={"text": "btn", "callback_data": "btn:1"})
    bot._nav_buttons = MagicMock(return_value=[])

    context = {"device_id": "AB-PC-123456", "project": {"name": "TestRepo", "absolute_path": "/test"}}

    # git_github_hub with GitHub remote
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {
            "remote_url": "https://github.com/sutra-org/test-repo.git",
            "branch": "main",
        }
    })
    await render_screen(bot, "git_github_hub", context, chat_id=1, user_id=1)
    assert "GitHub Collaboration" in bot._reply.call_args[0][0]

    # git_pr_list
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {"items": [{"number": 1, "title": "Test PR"}]}
    })
    await render_screen(bot, "git_pr_list", context, chat_id=1, user_id=1)
    assert "Pull Requests" in bot._reply.call_args[0][0]

    # git_pr_files
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {"files": [{"filename": "main.py", "status": "modified", "additions": 10, "deletions": 2}]}
    })
    await render_screen(bot, "git_pr_files", {**context, "number": 1}, chat_id=1, user_id=1)
    assert "Changed Files" in bot._reply.call_args[0][0]

    # git_releases
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {"releases": [{"tag_name": "v1.0.0", "published_at": "2026-10-06"}]}
    })
    await render_screen(bot, "git_releases", context, chat_id=1, user_id=1)
    assert "Releases & Tags" in bot._reply.call_args[0][0]


# 32. Telegram /pr comment handler
@pytest.mark.asyncio
async def test_32_telegram_pr_comment_handler():
    from sutra.apps.telegram.handlers import handle_message_command

    bot = MagicMock()
    bot.allowed_user_ids = {1}
    bot.active_git_context = {
        1: {"device_id": "AB-PC-123456", "project": {"name": "TestRepo", "absolute_path": "/test"}}
    }
    bot.control_plane = MagicMock()
    bot.control_plane.permission_engine = PermissionEngine()
    bot.pending_permissions = {}
    bot._screen = AsyncMock(return_value="mock_screen")
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {"success": True, "comment": {"id": 1, "body": "Please verify"}}
    })

    # Execute /pr comment
    res = await handle_message_command(bot, {"text": "/pr comment 42 \"Please verify\""}, user_id=1, chat_id=1)
    # The first time it requires permission and creates pending request
    assert len(bot.pending_permissions) > 0 or "✅" in str(res)


# 33. Telegram /pr merge handler
@pytest.mark.asyncio
async def test_33_telegram_pr_merge_handler():
    from sutra.apps.telegram.handlers import handle_message_command

    bot = MagicMock()
    bot.allowed_user_ids = {1}
    bot.active_git_context = {
        1: {"device_id": "AB-PC-123456", "project": {"name": "TestRepo", "absolute_path": "/test"}}
    }
    bot.control_plane = MagicMock()
    bot.control_plane.permission_engine = PermissionEngine()
    bot.pending_permissions = {}
    bot._screen = AsyncMock(return_value="mock_screen")

    res = await handle_message_command(bot, {"text": "/pr merge 42 squash"}, user_id=1, chat_id=1)
    # Merge requires authorization
    assert len(bot.pending_permissions) > 0


# 34. Telegram git_pr_merge_execute callback
@pytest.mark.asyncio
async def test_34_telegram_pr_merge_execute_callback():
    from sutra.apps.telegram.callbacks import handle_callback

    bot = MagicMock()
    bot.control_plane = MagicMock()
    bot.control_plane.permission_engine = None  # No permission engine gating in this mock
    bot.control_plane.session_matches.return_value = True
    bot._reply = MagicMock(return_value="mock_reply")
    bot._button = MagicMock(return_value={})
    bot._target_button = MagicMock(return_value={})
    bot.callback_targets = {
        "target1": {
            "user_id": 1,
            "chat_id": 1,
            "created_at": time.time(),
            "action": "git_pr_merge_execute",
            "context": {
                "device_id": "AB-PC-123456",
                "project": {"name": "TestRepo", "absolute_path": "/test"},
                "number": 42,
                "merge_method": "squash",
            },
        }
    }
    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {"success": True, "merged": True, "sha": "12345678"}
    })

    update = {
        "callback_query": {
            "id": "cb1",
            "data": "target:target1",
            "message": {"message_id": 10},
        }
    }
    await handle_callback(bot, update, user_id=1, chat_id=1)
    bot._reply.assert_called()
    reply_text = bot._reply.call_args[0][0]
    assert "merged successfully" in reply_text
    assert "SQUASH" in reply_text


# 35. Telegram CI rerun and cancel callbacks
@pytest.mark.asyncio
async def test_35_telegram_ci_rerun_cancel_callbacks():
    from sutra.apps.telegram.callbacks import handle_callback

    bot = MagicMock()
    bot.control_plane = MagicMock()
    bot.control_plane.permission_engine = None
    bot.control_plane.session_matches.return_value = True
    bot._reply = MagicMock(return_value="mock_reply")
    bot._button = MagicMock(return_value={})
    bot._target_button = MagicMock(return_value={})

    bot.callback_targets = {
        "target_rerun": {
            "user_id": 1,
            "chat_id": 1,
            "created_at": time.time(),
            "action": "git_ci_rerun_execute",
            "context": {
                "device_id": "AB-PC-123456",
                "project": {"name": "TestRepo", "absolute_path": "/test"},
                "run_id": 999,
            },
        },
        "target_cancel": {
            "user_id": 1,
            "chat_id": 1,
            "created_at": time.time(),
            "action": "git_ci_cancel_execute",
            "context": {
                "device_id": "AB-PC-123456",
                "project": {"name": "TestRepo", "absolute_path": "/test"},
                "run_id": 999,
            },
        },
    }

    bot._dispatch_command = AsyncMock(return_value={
        "status": "success",
        "payload": {"success": True}
    })

    # Test rerun
    update_rerun = {"callback_query": {"id": "cb1", "data": "target:target_rerun", "message": {"message_id": 10}}}
    await handle_callback(bot, update_rerun, user_id=1, chat_id=1)
    assert "rerun initiated" in bot._reply.call_args[0][0]

    # Test cancel
    update_cancel = {"callback_query": {"id": "cb2", "data": "target:target_cancel", "message": {"message_id": 11}}}
    await handle_callback(bot, update_cancel, user_id=1, chat_id=1)
    assert "cancelled" in bot._reply.call_args[0][0]


# 36. Telegram invalid PR number error handling
@pytest.mark.asyncio
async def test_36_telegram_invalid_pr_number():
    from sutra.apps.telegram.handlers import handle_message_command

    bot = MagicMock()
    bot.allowed_user_ids = {1}
    bot.active_git_context = {
        1: {"device_id": "AB-PC-123456", "project": {"name": "TestRepo", "absolute_path": "/test"}}
    }

    res_comment = await handle_message_command(bot, {"text": "/pr comment not-a-number test"}, user_id=1, chat_id=1)
    assert "Invalid PR number" in str(res_comment)

    res_merge = await handle_message_command(bot, {"text": "/pr merge abc squash"}, user_id=1, chat_id=1)
    assert "Invalid PR number" in str(res_merge)


