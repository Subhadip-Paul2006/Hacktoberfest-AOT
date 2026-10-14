"""Comprehensive test suite for Phase 6C: Reliability Hardening.

Covers all 10 architectural categories:
Group A: Rate Limiting & 429 Handling
Group B: Retry Policy & Classification
Group C: Circuit Breaker State Machine & Fast-Fail
Group D: Bulkhead & Bounded Concurrency
Group E: Cache & Invalidation
Group F: Idempotency & Duplicate Request Protection
Group G: Stale-State Protection & Reconciliation
Group H: Local Git Subprocess Safeguards & Timeouts
Group I: Graceful Degradation & Secret Redaction
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sutra.git import (
    GitHubErrorCode,
    GitHubMergeMode,
    GitHubMergeResult,
    GitHubPRComment,
    GitHubPRDetails,
    GitHubPRFile,
    GitHubPRResult,
    GitHubResilienceBoundary,
    GitHubResilienceConfig,
    GitHubResilienceError,
    GitSubprocessTimeoutError,
    add_pull_request_comment,
    cancel_workflow_run,
    create_pull_request,
    get_github_boundary,
    get_git_branches,
    get_git_status,
    get_pull_request,
    get_pull_request_files,
    is_git_repository,
    list_pull_request_comments,
    list_pull_requests,
    merge_pull_request,
    poll_workflow_run_until_completed,
    rerun_workflow_run,
    set_github_boundary,
)
from sutra.git.local import _run_git_cmd, assert_no_force_push
from sutra.metrics import get_metrics
from sutra.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    IdempotencyStore,
    RetryPolicy,
    SlidingWindowRateLimiter,
)


def _mock_resp(status_code: int = 200, json_data: dict | list | None = None, headers: dict | None = None) -> httpx.Response:
    req = httpx.Request("GET", "https://api.github.com")
    resp_headers = headers or {}
    return httpx.Response(status_code=status_code, json=json_data or {}, headers=resp_headers, request=req)


# ---------------------------------------------------------------------------
# Group A: Rate Limiting & 429 Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a1_rate_limit_per_user():
    config = GitHubResilienceConfig(rate_limit_user=2, rate_limit_window=10.0)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_resp(200, [{"number": 1, "title": "Test"}])

    # 1st and 2nd allowed for user 101
    res1 = await list_pull_requests("owner", "repo", client=mock_client, user_id=101)
    assert len(res1) == 1
    res2 = await list_pull_requests("owner", "repo", client=mock_client, user_id=101)
    assert len(res2) == 1

    # 3rd request rejected for user 101
    with pytest.raises(GitHubResilienceError) as exc_info:
        await boundary.execute(
            "test_op",
            lambda cl: cl.get("https://api.github.com"),
            owner="owner",
            repo="repo",
            user_id=101,
            client=mock_client,
        )
    assert exc_info.value.error_code == GitHubErrorCode.RATE_LIMITED
    assert exc_info.value.retry_after is not None
    assert exc_info.value.retry_after > 0

    # User 102 should still be allowed (per-user isolation) on a fresh query
    res_user2 = await list_pull_requests("owner", "repo", client=mock_client, user_id=102, page=2)
    assert len(res_user2) == 1


@pytest.mark.asyncio
async def test_a2_rate_limit_per_repo():
    config = GitHubResilienceConfig(rate_limit_repo=2, rate_limit_window=10.0)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_resp(200, [{"number": 1}])

    await boundary.execute("op1", lambda cl: cl.get("https://api.github.com"), owner="org", repo="repo1", client=mock_client)
    await boundary.execute("op2", lambda cl: cl.get("https://api.github.com"), owner="org", repo="repo1", client=mock_client)

    # 3rd call to repo1 hits rate limit
    with pytest.raises(GitHubResilienceError) as exc_info:
        await boundary.execute(
            "test_op",
            lambda cl: cl.get("https://api.github.com"),
            owner="org",
            repo="repo1",
            client=mock_client,
        )
    assert exc_info.value.error_code == GitHubErrorCode.RATE_LIMITED

    # repo2 should still be allowed
    res_repo2 = await boundary.execute("op3", lambda cl: cl.get("https://api.github.com"), owner="org", repo="repo2", client=mock_client)
    assert res_repo2.status_code == 200



@pytest.mark.asyncio
async def test_a3_rate_limit_429_retry_after_header():
    config = GitHubResilienceConfig(max_retries=1, base_delay=0.1)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Return 429 with Retry-After: 0.1, then 200
    r429 = _mock_resp(429, {"message": "rate limited"}, headers={"Retry-After": "0.1"})
    r200 = _mock_resp(200, [{"number": 42}])
    mock_client.get.side_effect = [r429, r200]

    prs = await list_pull_requests("owner", "repo", client=mock_client)
    assert len(prs) == 1
    assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# Group B: Retry Policy & Classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b1_retry_on_500_502_503_504_success():
    config = GitHubResilienceConfig(max_retries=2, base_delay=0.01)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Fail with 503, then 502, then succeed with 200
    mock_client.get.side_effect = [
        _mock_resp(503, {"message": "Service Unavailable"}),
        _mock_resp(502, {"message": "Bad Gateway"}),
        _mock_resp(200, [{"number": 99}]),
    ]

    prs = await list_pull_requests("owner", "repo", client=mock_client)
    assert len(prs) == 1
    assert mock_client.get.call_count == 3


@pytest.mark.asyncio
async def test_b2_retry_on_timeout():
    config = GitHubResilienceConfig(max_retries=1, base_delay=0.01)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    req = httpx.Request("GET", "https://api.github.com")
    mock_client.get.side_effect = [
        httpx.ReadTimeout("Timeout reading from socket", request=req),
        _mock_resp(200, [{"number": 100}]),
    ]

    prs = await list_pull_requests("owner", "repo", client=mock_client)
    assert len(prs) == 1
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_b3_no_retry_on_401_403_404_422():
    config = GitHubResilienceConfig(max_retries=2, base_delay=0.01)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    for status_code in (401, 403, 404, 422):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_resp(status_code, {"message": "client error"})

        with pytest.raises(GitHubResilienceError):
            await boundary.execute(
                "test_op",
                lambda cl: cl.get("https://api.github.com"),
                owner="owner",
                repo="repo",
                client=mock_client,
            )
        # MUST NOT retry — exactly 1 call made!
        assert mock_client.get.call_count == 1, f"Status {status_code} should not be retried"


# ---------------------------------------------------------------------------
# Group C: Circuit Breaker State Machine & Fast-Fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c1_circuit_breaker_closed_to_open_and_fast_fail():
    config = GitHubResilienceConfig(circuit_threshold=3, circuit_cooldown=10.0, max_retries=0)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    assert boundary.circuit_breaker.state == "CLOSED"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_resp(500, {"message": "Server Error"})

    # 3 consecutive 500 errors trip the breaker
    for _ in range(3):
        with pytest.raises(GitHubResilienceError):
            await boundary.execute("test", lambda cl: cl.get("https://api.github.com"), client=mock_client)

    assert boundary.circuit_breaker.state == "OPEN"
    call_count_before = mock_client.get.call_count

    # Subsequent request fails FAST without touching downstream HTTP client
    with pytest.raises(GitHubResilienceError) as exc_info:
        await boundary.execute("test", lambda cl: cl.get("https://api.github.com"), client=mock_client)

    assert exc_info.value.error_code == GitHubErrorCode.CIRCUIT_OPEN
    assert "circuit open" in exc_info.value.user_friendly_message.lower()
    # Mock client was not called again
    assert mock_client.get.call_count == call_count_before


@pytest.mark.asyncio
async def test_c2_circuit_breaker_recovery_half_open_to_closed():
    config = GitHubResilienceConfig(circuit_threshold=2, circuit_cooldown=0.05, max_retries=0)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_resp(500, {})

    # Trip to OPEN
    for _ in range(2):
        with pytest.raises(GitHubResilienceError):
            await boundary.execute("test", lambda cl: cl.get("https://api.github.com"), client=mock_client)
    assert boundary.circuit_breaker.state == "OPEN"

    # Wait for cooldown
    await asyncio.sleep(0.06)

    # Next call allows probe in HALF_OPEN; probe succeeds -> transitions to CLOSED
    mock_client.get.return_value = _mock_resp(200, [{"id": 1}])
    res = await boundary.execute("test", lambda cl: cl.get("https://api.github.com"), client=mock_client)
    assert res.status_code == 200
    assert boundary.circuit_breaker.state == "CLOSED"


# ---------------------------------------------------------------------------
# Group D: Bulkhead & Bounded Concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d1_bulkhead_github_bounded_concurrency():
    config = GitHubResilienceConfig(max_concurrency_github=3)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    active_concurrent = 0
    max_observed_concurrent = 0

    async def _slow_op(cl: httpx.AsyncClient):
        nonlocal active_concurrent, max_observed_concurrent
        active_concurrent += 1
        max_observed_concurrent = max(max_observed_concurrent, active_concurrent)
        await asyncio.sleep(0.05)
        active_concurrent -= 1
        return "ok"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Launch 10 concurrent operations
    tasks = [
        boundary.execute(f"op_{i}", _slow_op, client=mock_client)
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)
    assert all(r == "ok" for r in results)
    assert max_observed_concurrent <= 3, f"Concurrency exceeded bulkhead limit: {max_observed_concurrent}"


# ---------------------------------------------------------------------------
# Group E: Cache & Invalidation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e1_cache_hit_and_ttl_expiration():
    config = GitHubResilienceConfig(cache_ttl_seconds=0.1)
    boundary = GitHubResilienceBoundary(config=config)
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = _mock_resp(200, [{"number": 1, "title": "Cache Test"}])

    # First call: hits HTTP client
    prs1 = await list_pull_requests("owner", "repo", client=mock_client)
    assert len(prs1) == 1
    assert mock_client.get.call_count == 1

    # Second call within TTL: served from cache without calling HTTP client
    prs2 = await list_pull_requests("owner", "repo", client=mock_client)
    assert len(prs2) == 1
    assert mock_client.get.call_count == 1

    # Wait for TTL to expire
    await asyncio.sleep(0.12)

    # Third call after TTL: fresh HTTP fetch
    prs3 = await list_pull_requests("owner", "repo", client=mock_client)
    assert len(prs3) == 1
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_e2_mutation_invalidates_cache():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    # Seed PR detail in cache
    boundary.cache_store.set("github.pr.get:owner:repo:42", "cached_details", ttl_seconds=60.0)
    boundary.cache_store.set("github.pr.comments:owner:repo:42:1:20", ["c1"], ttl_seconds=60.0)

    assert boundary.cache_store.get("github.pr.get:owner:repo:42") == "cached_details"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _mock_resp(201, {"id": 10, "body": "New comment"})

    # Post comment (mutation)
    with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
        await add_pull_request_comment("owner", "repo", 42, "New comment", client=mock_client)

    # Verifies cache was invalidated on mutation!
    assert boundary.cache_store.get("github.pr.get:owner:repo:42") is None
    assert boundary.cache_store.get("github.pr.comments:owner:repo:42:1:20") is None


# ---------------------------------------------------------------------------
# Group F: Idempotency & Duplicate Request Protection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f1_duplicate_comment_suppression():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _mock_resp(201, {"id": 101, "body": "Identical body"})

    with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
        res1 = await add_pull_request_comment("owner", "repo", 5, "Identical body", client=mock_client)
        res2 = await add_pull_request_comment("owner", "repo", 5, "Identical body", client=mock_client)

    assert res1 is not None and res2 is not None
    assert res1.id == 101
    assert res2.id == 101
    # Idempotency store returned cached completed result; only 1 POST issued!
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_f2_duplicate_ci_rerun_and_cancel():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _mock_resp(201, {"message": "rerun requested"})

    with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
        r1 = await rerun_workflow_run("owner", "repo", 1234, client=mock_client)
        r2 = await rerun_workflow_run("owner", "repo", 1234, client=mock_client)

    assert r1.get("success") is True
    assert r2.get("success") is True
    assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# Group G: Stale-State Protection & Reconciliation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g1_merge_rejected_if_pr_closed_or_conflicted():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    # 1. Closed PR
    with patch("sutra.git.github.get_pull_request") as mock_get_pr:
        mock_get_pr.return_value = GitHubPRDetails(
            number=10, title="Closed PR", state="closed", head_branch="fix", base_branch="main",
            author="dev", created_at="", updated_at="", merged=False, mergeable=True,
        )
        with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
            res = await merge_pull_request("owner", "repo", 10)
        assert res.success is False
        assert "closed" in res.error.lower()

    # 2. Conflicted PR (mergeable=False)
    with patch("sutra.git.github.get_pull_request") as mock_get_pr:
        mock_get_pr.return_value = GitHubPRDetails(
            number=11, title="Conflicted PR", state="open", head_branch="fix", base_branch="main",
            author="dev", created_at="", updated_at="", merged=False, mergeable=False,
        )
        with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
            res = await merge_pull_request("owner", "repo", 11)
        assert res.success is False
        assert "conflicts" in res.error.lower()


@pytest.mark.asyncio
async def test_g2_merge_idempotent_if_already_merged():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    with patch("sutra.git.github.get_pull_request") as mock_get_pr:
        mock_get_pr.return_value = GitHubPRDetails(
            number=12, title="Already Merged", state="closed", head_branch="fix", base_branch="main",
            author="dev", created_at="", updated_at="", merged=True, mergeable=None,
        )
        with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
            res = await merge_pull_request("owner", "repo", 12)
        assert res.success is True
        assert res.merged is True
        assert "already merged" in res.message.lower()


@pytest.mark.asyncio
async def test_g3_merge_reconciled_after_network_error():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    # State before: open. During PUT: network disconnects. After: PR is merged!
    pr_open = GitHubPRDetails(
        number=15, title="Network test", state="open", head_branch="sha_xyz", base_branch="main",
        author="dev", created_at="", updated_at="", merged=False, mergeable=True,
    )
    pr_merged = GitHubPRDetails(
        number=15, title="Network test", state="closed", head_branch="sha_xyz", base_branch="main",
        author="dev", created_at="", updated_at="", merged=True, mergeable=None,
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    req = httpx.Request("PUT", "https://api.github.com/repos/owner/repo/pulls/15/merge")
    mock_client.put.side_effect = httpx.ReadTimeout("Connection dropped after write", request=req)

    with patch("sutra.git.github.get_pull_request", side_effect=[pr_open, pr_merged]):
        with patch("sutra.git.github.detect_github_token", return_value="fake_token"):
            res = await merge_pull_request("owner", "repo", 15, client=mock_client)

    assert res.success is True
    assert res.merged is True
    assert "reconciled" in res.message.lower()


# ---------------------------------------------------------------------------
# Group H: Local Git Subprocess Safeguards & Timeouts
# ---------------------------------------------------------------------------

def test_h1_local_git_timeout_handling(tmp_path: Path):
    # Simulate a subprocess timeout by mocking subprocess.run to raise TimeoutExpired
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git", "status"], timeout=0.1)):
        proc = _run_git_cmd(["status"], cwd=tmp_path, timeout=0.1)
        assert proc.returncode == 124
        assert "timed out after 0.1s" in proc.stderr


def test_h2_local_git_terminal_prompt_disabled(tmp_path: Path):
    captured_env = {}

    def _fake_run(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="true", stderr="")

    with patch("subprocess.run", side_effect=_fake_run):
        _run_git_cmd(["status"], cwd=tmp_path)

    # Crucial safety invariant: GIT_TERMINAL_PROMPT must always be "0"
    assert captured_env.get("GIT_TERMINAL_PROMPT") == "0"


def test_h3_local_git_force_push_strictly_prohibited():
    with pytest.raises(ValueError, match="Force push is strictly prohibited"):
        assert_no_force_push(["push", "--force"])

    with pytest.raises(ValueError, match="Force push is strictly prohibited"):
        assert_no_force_push(["push", "-f"])

    with pytest.raises(ValueError, match="Force push is strictly prohibited"):
        assert_no_force_push(["push", "--force-with-lease"])


# ---------------------------------------------------------------------------
# Group I: Graceful Degradation & Secret Redaction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_i1_github_down_local_git_and_runtime_remain_operational(tmp_path: Path):
    # Force GitHub circuit breaker to OPEN
    boundary = GitHubResilienceBoundary()
    boundary.circuit_breaker.state = "OPEN"
    boundary.circuit_breaker.opened_at = time.monotonic()
    set_github_boundary(boundary)

    # 1. GitHub operation fails fast with CIRCUIT_OPEN
    with pytest.raises(GitHubResilienceError) as exc_info:
        await boundary.execute("test_gh", AsyncMock())
    assert exc_info.value.error_code == GitHubErrorCode.CIRCUIT_OPEN

    # 2. Local Git remains 100% operational!
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True, capture_output=True)

    (repo / "test.txt").write_text("content", encoding="utf-8")
    assert is_git_repository(repo) is True
    st = get_git_status(repo)
    assert st.is_git is True
    assert len(st.untracked_files) == 1
    branches = get_git_branches(repo)
    assert branches.current == "main"


@pytest.mark.asyncio
async def test_i2_secrets_never_leaked_in_errors_or_messages():
    secret_token = "ghp_1234567890abcdef1234567890abcdef"

    # Simulate an error with secrets in URL/headers
    err = httpx.HTTPStatusError(
        f"403 Forbidden with token {secret_token}",
        request=httpx.Request("GET", f"https://api.github.com?token={secret_token}"),
        response=httpx.Response(403, text=f"Token {secret_token} rejected"),
    )
    from sutra.git.resilience import normalize_github_exception
    norm = normalize_github_exception(err)

    # Invariant: Secret redaction strips sensitive tokens!
    assert secret_token not in norm.message
    assert secret_token not in norm.user_friendly_message
    assert "[REDACTED]" in norm.message or "[REDACTED" in norm.user_friendly_message or secret_token not in norm.message



@pytest.mark.asyncio
async def test_i3_ci_polling_bounded_and_aborts_on_circuit_open():
    boundary = GitHubResilienceBoundary()
    set_github_boundary(boundary)

    poll_count = 0

    async def _mock_poll():
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2:
            # Trip circuit breaker on second attempt
            boundary.circuit_breaker.state = "OPEN"
            boundary.circuit_breaker.opened_at = time.monotonic()
        return MagicMock(status="in_progress")

    run, is_degraded = await poll_workflow_run_until_completed(
        _mock_poll,
        max_attempts=10,
        interval_seconds=0.01,
        boundary=boundary,
    )

    # Must stop polling immediately and return degraded state
    assert is_degraded is True
    assert poll_count == 2
