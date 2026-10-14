"""GitHub Remote Detection and REST API Adapter for Pull Requests and Metadata."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .local import detect_remotes, validate_repo_path
from .models import (
    GitHubMergeMode,
    GitHubMergeResult,
    GitHubPRComment,
    GitHubPRDetails,
    GitHubPRFile,
    GitHubPRRequest,
    GitHubPRResult,
    GitHubReleaseInfo,
    GitHubRemoteInfo,
    GitHubReviewState,
    GitHubReviewSummary,
    GitHubWorkflowRun,
    GitHubWorkflowRunLog,
)
from .resilience import (
    GitHubErrorCode,
    GitHubResilienceBoundary,
    GitHubResilienceError,
    get_github_boundary,
    normalize_github_exception,
)
from ..redaction import redact_secrets

log = logging.getLogger(__name__)

# Matches SSH and HTTPS GitHub URLs
GITHUB_URL_PATTERNS = [
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^https?://(?:[^@]+@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
]


def _build_github_headers(token: Optional[str] = None) -> Dict[str, str]:
    """Construct standard GitHub REST API request headers."""
    auth_token = token or detect_github_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "SUTRA-Safe-Git-ChatOps/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def _make_github_cache_key(
    prefix: str,
    *args: Any,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Build a deterministic cache key with optional token and client instance scoping."""
    key_parts = [prefix] + [str(a) for a in args]
    base_key = ":".join(key_parts)
    suffixes = []
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
        suffixes.append(f"tok_{token_hash}")
    if client is not None:
        suffixes.append(f"cl_{id(client)}")
    if suffixes:
        return f"{base_key}:{':'.join(suffixes)}"
    return base_key


def parse_github_remote(remote_url: str, remote_name: str = "origin") -> GitHubRemoteInfo:
    """Analyze a remote URL and extract owner and repository name if hosted on GitHub."""
    if not remote_url:
        return GitHubRemoteInfo(is_github=False, remote_name=remote_name, remote_url="")

    clean_url = remote_url.strip()
    for pattern in GITHUB_URL_PATTERNS:
        match = pattern.match(clean_url)
        if match:
            owner, repo = match.groups()
            return GitHubRemoteInfo(
                is_github=True,
                owner=owner,
                repo=repo,
                remote_name=remote_name,
                remote_url=clean_url,
            )

    return GitHubRemoteInfo(
        is_github=False,
        owner=None,
        repo=None,
        remote_name=remote_name,
        remote_url=clean_url,
    )


def detect_github_remote(project_path: str | Path, remote_name: str = "origin") -> GitHubRemoteInfo:
    """Find configured GitHub remote for a given project directory."""
    path = validate_repo_path(project_path)
    remotes = detect_remotes(path)
    if remote_name in remotes:
        info = parse_github_remote(remotes[remote_name], remote_name)
        if info.is_github:
            return info

    # Check any other remote if origin is not GitHub
    for name, url in remotes.items():
        info = parse_github_remote(url, name)
        if info.is_github:
            return info

    first_url = next(iter(remotes.values())) if remotes else ""
    return GitHubRemoteInfo(
        is_github=False,
        remote_name=remote_name,
        remote_url=first_url,
    )


def detect_github_token() -> Optional[str]:
    """Retrieve GitHub token from environment variables or local gh CLI.
    
    Credentials are NEVER hardcoded or persisted in source code.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and token.strip():
        return token.strip()

    # Optional local GitHub CLI credential helper lookup
    gh_path = shutil.which("gh")
    if gh_path:
        try:
            proc = subprocess.run(
                [gh_path, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=3.0,
                shell=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            pass

    return None


async def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
    draft: bool = False,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> GitHubPRResult:
    """Create a pull request on GitHub using the REST API behind the resilience boundary."""
    auth_token = token or detect_github_token()
    if not auth_token:
        return GitHubPRResult(
            success=False,
            error="GitHub authentication token is not configured. Set GITHUB_TOKEN or authenticate using 'gh auth login'.",
        )

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = _build_github_headers(auth_token)
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    }

    async def _execute_create(cl: httpx.AsyncClient) -> GitHubPRResult:
        response = await cl.post(url, json=payload, headers=headers)
        if response.status_code == 201:
            data = response.json()
            return GitHubPRResult(
                success=True,
                number=data.get("number"),
                html_url=data.get("html_url"),
                title=data.get("title"),
                head=data.get("head", {}).get("ref"),
                base=data.get("base", {}).get("ref"),
                state=data.get("state"),
            )

        if response.status_code in (401, 403):
            return GitHubPRResult(
                success=False,
                error="GitHub API authentication failed. Please verify your token has repository write permissions.",
            )

        if response.status_code == 422:
            data = response.json()
            message = data.get("message", "Validation failed")
            errors = data.get("errors", [])
            error_details = []
            for err in errors:
                if isinstance(err, dict) and "message" in err:
                    error_details.append(err["message"])
            full_msg = f"{message}: {'; '.join(error_details)}" if error_details else message
            return GitHubPRResult(
                success=False,
                error=f"GitHub rejected Pull Request: {full_msg}",
            )

        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()

        return GitHubPRResult(
            success=False,
            error=f"GitHub API error (HTTP {response.status_code}): {response.text[:200]}",
        )

    boundary = get_github_boundary()
    client_scope = f":cl_{id(client)}" if client is not None else ""
    token_scope = f":tok_{hashlib.sha256(auth_token.encode('utf-8')).hexdigest()[:8]}" if auth_token else ""
    try:
        return await boundary.execute(
            operation_name="github.pr.create",
            coro_fn=_execute_create,
            owner=owner,
            repo=repo,
            user_id=user_id,
            is_mutation=True,
            idempotency_key=f"pr_create:{owner}:{repo}:{head}:{base}{token_scope}{client_scope}",
            invalidate_prefixes=[f"github.pr.list:{owner}:{repo}"],
            client=client,
        )
    except GitHubResilienceError as err:
        return GitHubPRResult(
            success=False,
            error=err.user_friendly_message,
        )
    except Exception as exc:
        log.exception("Error creating pull request for %s/%s", owner, repo)
        return GitHubPRResult(
            success=False,
            error=f"Network error communicating with GitHub API: {str(exc)}",
        )


async def list_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
    page: int = 1,
    per_page: int = 10,
    head: Optional[str] = None,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Retrieve list of pull requests for a repository with pagination and filters."""
    headers = _build_github_headers(token)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state={state}&page={page}&per_page={per_page}"
    if head:
        url += f"&head={head}"

    async def _execute_list(cl: httpx.AsyncClient) -> List[Dict[str, Any]]:
        response = await cl.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        return []

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.pr.list", owner, repo, state, page, per_page, head or "", token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.pr.list",
            coro_fn=_execute_list,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return []


async def get_pull_request(
    owner: str,
    repo: str,
    number: int,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
    force_fresh: bool = False,
) -> Optional[GitHubPRDetails]:
    """Retrieve structured details for a single pull request."""
    auth_token = token or detect_github_token()
    headers = _build_github_headers(auth_token)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"

    async def _execute_get(cl: httpx.AsyncClient) -> Optional[GitHubPRDetails]:
        response = await cl.get(url, headers=headers)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            if response.status_code in (429, 500, 502, 503, 504):
                response.raise_for_status()
            log.warning("Failed to fetch PR #%d for %s/%s (HTTP %d)", number, owner, repo, response.status_code)
            return None

        d = response.json()
        return GitHubPRDetails(
            number=d.get("number", number),
            title=d.get("title", ""),
            state=d.get("state", "open"),
            head_branch=d.get("head", {}).get("ref", ""),
            base_branch=d.get("base", {}).get("ref", "main"),
            author=d.get("user", {}).get("login", "unknown"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            body=d.get("body") or "",
            html_url=d.get("html_url", ""),
            draft=bool(d.get("draft", False)),
            merged=bool(d.get("merged", False)),
            mergeable=d.get("mergeable"),
            mergeable_state=d.get("mergeable_state"),
            comments_count=d.get("comments", 0),
            commits_count=d.get("commits", 0),
            changed_files_count=d.get("changed_files", 0),
            additions=d.get("additions", 0),
            deletions=d.get("deletions", 0),
        )

    boundary = get_github_boundary()
    cache_key = None if force_fresh else _make_github_cache_key(
        "github.pr.get", owner, repo, number, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.pr.get",
            coro_fn=_execute_get,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception as exc:
        log.exception("Network error retrieving PR #%d for %s/%s: %s", number, owner, repo, exc)
        return None


async def get_pull_request_files(
    owner: str,
    repo: str,
    number: int,
    page: int = 1,
    per_page: int = 30,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> List[GitHubPRFile]:
    """Retrieve changed files summary for a pull request."""
    headers = _build_github_headers(token)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files?page={page}&per_page={per_page}"

    async def _execute_files(cl: httpx.AsyncClient) -> List[GitHubPRFile]:
        response = await cl.get(url, headers=headers)
        if response.status_code != 200:
            if response.status_code in (429, 500, 502, 503, 504):
                response.raise_for_status()
            return []
        items = response.json()
        results: List[GitHubPRFile] = []
        for item in items:
            results.append(
                GitHubPRFile(
                    filename=item.get("filename", ""),
                    status=item.get("status", "modified"),
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                    changes=item.get("changes", 0),
                    patch=item.get("patch"),
                )
            )
        return results

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.pr.files", owner, repo, number, page, per_page, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.pr.files",
            coro_fn=_execute_files,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return []


async def list_pull_request_comments(
    owner: str,
    repo: str,
    number: int,
    page: int = 1,
    per_page: int = 20,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> List[GitHubPRComment]:
    """Retrieve discussion and review comments for a pull request."""
    headers = _build_github_headers(token)
    issue_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments?page={page}&per_page={per_page}"
    review_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/comments?page={page}&per_page={per_page}"

    async def _execute_comments(cl: httpx.AsyncClient) -> List[GitHubPRComment]:
        comments: List[GitHubPRComment] = []
        resp_issue = await cl.get(issue_url, headers=headers)
        if resp_issue.status_code == 200:
            for item in resp_issue.json():
                comments.append(
                    GitHubPRComment(
                        id=item.get("id", 0),
                        author=item.get("user", {}).get("login", "unknown"),
                        body=item.get("body", ""),
                        created_at=item.get("created_at", ""),
                        updated_at=item.get("updated_at", ""),
                        html_url=item.get("html_url"),
                        comment_type="issue",
                    )
                )
        elif resp_issue.status_code in (429, 500, 502, 503, 504):
            resp_issue.raise_for_status()

        resp_review = await cl.get(review_url, headers=headers)
        if resp_review.status_code == 200:
            for item in resp_review.json():
                comments.append(
                    GitHubPRComment(
                        id=item.get("id", 0),
                        author=item.get("user", {}).get("login", "unknown"),
                        body=item.get("body", ""),
                        created_at=item.get("created_at", ""),
                        updated_at=item.get("updated_at", ""),
                        html_url=item.get("html_url"),
                        comment_type="review",
                        path=item.get("path"),
                        line=item.get("line"),
                    )
                )
        elif resp_review.status_code in (429, 500, 502, 503, 504):
            resp_review.raise_for_status()

        comments.sort(key=lambda c: c.created_at)
        return comments

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.pr.comments", owner, repo, number, page, per_page, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.pr.comments",
            coro_fn=_execute_comments,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return []


async def add_pull_request_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    path: Optional[str] = None,
    line: Optional[int] = None,
    commit_id: Optional[str] = None,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> Optional[GitHubPRComment]:
    """Post an authorized discussion or review comment to a pull request.
    
    Never executes or interprets comment text as a shell command.
    """
    clean_body = body.strip() if body else ""
    if not clean_body:
        return None

    auth_token = token or detect_github_token()
    if not auth_token:
        log.warning("Cannot post PR comment: GitHub token is missing.")
        return None

    headers = _build_github_headers(auth_token)

    async def _execute_add(cl: httpx.AsyncClient) -> Optional[GitHubPRComment]:
        # If line-level coordinates and commit_id are specified and valid:
        if path and line and commit_id:
            review_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/comments"
            payload = {
                "body": clean_body,
                "commit_id": commit_id,
                "path": path,
                "line": int(line),
            }
            resp = await cl.post(review_url, json=payload, headers=headers)
            if resp.status_code == 201:
                item = resp.json()
                return GitHubPRComment(
                    id=item.get("id", 0),
                    author=item.get("user", {}).get("login", "unknown"),
                    body=item.get("body", clean_body),
                    created_at=item.get("created_at", ""),
                    updated_at=item.get("updated_at", ""),
                    html_url=item.get("html_url"),
                    comment_type="review",
                    path=path,
                    line=int(line),
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                resp.raise_for_status()
            log.warning("Line-specific review comment failed (HTTP %d): %s", resp.status_code, resp.text[:200])

        # Standard general PR discussion comment
        issue_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
        resp = await cl.post(issue_url, json={"body": clean_body}, headers=headers)
        if resp.status_code == 201:
            item = resp.json()
            return GitHubPRComment(
                id=item.get("id", 0),
                author=item.get("user", {}).get("login", "unknown"),
                body=item.get("body", clean_body),
                created_at=item.get("created_at", ""),
                updated_at=item.get("updated_at", ""),
                html_url=item.get("html_url"),
                comment_type="issue",
            )
        if resp.status_code in (429, 500, 502, 503, 504):
            resp.raise_for_status()
        log.warning("Posting PR comment failed (HTTP %d): %s", resp.status_code, resp.text[:200])
        return None

    boundary = get_github_boundary()
    comment_hash = f"{hash(clean_body)}_{hash(str(path))}_{hash(str(line))}"
    try:
        return await boundary.execute(
            operation_name="github.pr.comment.create",
            coro_fn=_execute_add,
            owner=owner,
            repo=repo,
            user_id=user_id,
            is_mutation=True,
            idempotency_key=f"pr_comment:{owner}:{repo}:{number}:{comment_hash}",
            invalidate_prefixes=[
                f"github.pr.comments:{owner}:{repo}:{number}",
                f"github.pr.get:{owner}:{repo}:{number}",
            ],
            client=client,
        )
    except Exception as exc:
        log.exception("Error adding comment to PR #%d: %s", number, exc)
        return None


async def get_pull_request_reviews(
    owner: str,
    repo: str,
    number: int,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> GitHubReviewSummary:
    """Inspect review status and requested reviewers for a pull request."""
    headers = _build_github_headers(token)
    reviews_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/reviews"
    reviewers_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/requested_reviewers"

    async def _execute_reviews(cl: httpx.AsyncClient) -> GitHubReviewSummary:
        reviews_list: List[GitHubReviewState] = []
        requested_reviewers: List[str] = []

        resp = await cl.get(reviews_url, headers=headers)
        if resp.status_code == 200:
            for item in resp.json():
                reviews_list.append(
                    GitHubReviewState(
                        reviewer=item.get("user", {}).get("login", "unknown"),
                        state=item.get("state", "PENDING"),
                        submitted_at=item.get("submitted_at"),
                        body=item.get("body"),
                    )
                )
        elif resp.status_code in (429, 500, 502, 503, 504):
            resp.raise_for_status()

        resp_req = await cl.get(reviewers_url, headers=headers)
        if resp_req.status_code == 200:
            users = resp_req.json().get("users", [])
            requested_reviewers = [u.get("login", "") for u in users if u.get("login")]
        elif resp_req.status_code in (429, 500, 502, 503, 504):
            resp_req.raise_for_status()

        approved = sum(1 for r in reviews_list if r.state == "APPROVED")
        changes_req = sum(1 for r in reviews_list if r.state == "CHANGES_REQUESTED")
        pending = len(requested_reviewers)

        summary_parts = []
        if approved:
            summary_parts.append(f"✅ {approved} approval" + ("s" if approved > 1 else ""))
        if changes_req:
            summary_parts.append(f"❌ {changes_req} changes requested")
        if pending:
            summary_parts.append(f"🟡 {pending} pending")
        if not summary_parts:
            summary_parts.append("⚪ No reviews submitted yet")

        return GitHubReviewSummary(
            reviews=reviews_list,
            requested_reviewers=requested_reviewers,
            approved_count=approved,
            changes_requested_count=changes_req,
            pending_count=pending,
            summary_text=" · ".join(summary_parts),
        )

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.pr.reviews", owner, repo, number, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.pr.reviews",
            coro_fn=_execute_reviews,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return GitHubReviewSummary(summary_text="⚪ Reviews unavailable")


async def merge_pull_request(
    owner: str,
    repo: str,
    number: int,
    merge_method: GitHubMergeMode | str = GitHubMergeMode.MERGE,
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
    sha: Optional[str] = None,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> GitHubMergeResult:
    """Safely merge a pull request via GitHub REST API with fresh state inspection and safe idempotency."""
    method_val = merge_method.value if isinstance(merge_method, GitHubMergeMode) else str(merge_method).lower()
    valid_methods = {m.value for m in GitHubMergeMode}
    if method_val not in valid_methods:
        return GitHubMergeResult(
            success=False,
            error=f"Unsupported merge method: '{merge_method}'. Supported methods: {', '.join(sorted(valid_methods))}.",
        )

    auth_token = token or detect_github_token()
    if not auth_token:
        return GitHubMergeResult(
            success=False,
            error="GitHub authentication token is not configured. Real-time merge requires authorized token.",
        )

    # Pre-flight state check: verify PR exists, open, and mergeable (Stale-State Protection)
    pr = await get_pull_request(owner, repo, number, token=auth_token, client=client, user_id=user_id, force_fresh=True)
    if not pr:
        return GitHubMergeResult(success=False, error=f"PR #{number} not found on GitHub.")
    if pr.merged:
        return GitHubMergeResult(success=True, merged=True, message=f"PR #{number} is already merged.")
    if pr.state == "closed":
        return GitHubMergeResult(success=False, error=f"PR #{number} is closed and cannot be merged.")
    if pr.mergeable is False:
        return GitHubMergeResult(
            success=False,
            error=f"PR #{number} cannot be merged: unresolved merge conflicts detected.",
        )

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/merge"
    headers = _build_github_headers(auth_token)
    payload: Dict[str, Any] = {"merge_method": method_val}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    if sha:
        payload["sha"] = sha

    async def _execute_merge(cl: httpx.AsyncClient) -> GitHubMergeResult:
        try:
            response = await cl.put(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as net_err:
            # Reconcile unknown result: inspect if merge succeeded during the network disruption
            reconcile_pr = await get_pull_request(owner, repo, number, token=auth_token, client=cl, user_id=user_id, force_fresh=True)
            if reconcile_pr and reconcile_pr.merged:
                return GitHubMergeResult(
                    success=True,
                    merged=True,
                    sha=reconcile_pr.head_branch,
                    message="Pull request merged successfully (reconciled after network timeout).",
                )
            raise net_err

        if response.status_code == 200:
            data = response.json()
            return GitHubMergeResult(
                success=True,
                merged=bool(data.get("merged", True)),
                sha=data.get("sha"),
                message=data.get("message", "Pull request merged successfully."),
            )

        if response.status_code == 405:
            return GitHubMergeResult(
                success=False,
                error="GitHub rejected merge: Base branch modified or merge conflict detected (HTTP 405).",
            )
        if response.status_code == 409:
            return GitHubMergeResult(
                success=False,
                error="GitHub rejected merge: Head branch conflict detected (HTTP 409).",
            )
        if response.status_code == 422:
            data = response.json()
            return GitHubMergeResult(
                success=False,
                error=f"GitHub rejected merge: {data.get('message', 'Unprocessable request')}",
            )

        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()

        return GitHubMergeResult(
            success=False,
            error=f"GitHub API merge error (HTTP {response.status_code}): {response.text[:200]}",
        )

    boundary = get_github_boundary()
    try:
        return await boundary.execute(
            operation_name="github.pr.merge",
            coro_fn=_execute_merge,
            owner=owner,
            repo=repo,
            user_id=user_id,
            is_mutation=True,
            idempotency_key=f"pr_merge:{owner}:{repo}:{number}:{sha or 'latest'}",
            invalidate_prefixes=[
                f"github.pr.get:{owner}:{repo}:{number}",
                f"github.pr.list:{owner}:{repo}",
                f"github.ci.runs:{owner}:{repo}",
            ],
            client=client,
        )
    except GitHubResilienceError as err:
        return GitHubMergeResult(
            success=False,
            error=err.user_friendly_message,
        )
    except Exception as exc:
        log.exception("Error merging PR #%d for %s/%s: %s", number, owner, repo, exc)
        return GitHubMergeResult(
            success=False,
            error=f"Network error during merge request: {str(exc)}",
        )


async def list_workflow_runs(
    owner: str,
    repo: str,
    branch: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> List[GitHubWorkflowRun]:
    """Retrieve recent GitHub Actions workflow runs for repository or branch."""
    headers = _build_github_headers(token)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs?page={page}&per_page={per_page}"
    if branch:
        url += f"&branch={branch}"

    async def _execute_runs(cl: httpx.AsyncClient) -> List[GitHubWorkflowRun]:
        response = await cl.get(url, headers=headers)
        if response.status_code != 200:
            if response.status_code in (429, 500, 502, 503, 504):
                response.raise_for_status()
            return []
        data = response.json()
        runs: List[GitHubWorkflowRun] = []
        for r in data.get("workflow_runs", []):
            runs.append(
                GitHubWorkflowRun(
                    id=r.get("id", 0),
                    name=r.get("name", "workflow"),
                    head_branch=r.get("head_branch", ""),
                    head_sha=r.get("head_sha", "")[:8],
                    status=r.get("status", "unknown"),
                    conclusion=r.get("conclusion"),
                    event=r.get("event", "push"),
                    created_at=r.get("created_at", ""),
                    updated_at=r.get("updated_at", ""),
                    html_url=r.get("html_url", ""),
                    run_number=r.get("run_number", 0),
                    display_title=r.get("display_title"),
                    workflow_id=r.get("workflow_id"),
                )
            )
        return runs

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.ci.runs", owner, repo, branch or "all", page, per_page, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.ci.runs",
            coro_fn=_execute_runs,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return []


async def get_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> Optional[GitHubWorkflowRun]:
    """Retrieve details for a specific workflow run."""
    headers = _build_github_headers(token)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"

    async def _execute_run(cl: httpx.AsyncClient) -> Optional[GitHubWorkflowRun]:
        response = await cl.get(url, headers=headers)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            if response.status_code in (429, 500, 502, 503, 504):
                response.raise_for_status()
            return None
        r = response.json()
        return GitHubWorkflowRun(
            id=r.get("id", run_id),
            name=r.get("name", "workflow"),
            head_branch=r.get("head_branch", ""),
            head_sha=r.get("head_sha", "")[:8],
            status=r.get("status", "unknown"),
            conclusion=r.get("conclusion"),
            event=r.get("event", "push"),
            created_at=r.get("created_at", ""),
            updated_at=r.get("updated_at", ""),
            html_url=r.get("html_url", ""),
            run_number=r.get("run_number", 0),
            display_title=r.get("display_title"),
            workflow_id=r.get("workflow_id"),
        )

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.ci.run", owner, repo, run_id, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.ci.run.get",
            coro_fn=_execute_run,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return None


async def get_workflow_run_logs(
    owner: str,
    repo: str,
    run_id: int,
    max_lines: int = 100,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> GitHubWorkflowRunLog:
    """Retrieve bounded, redacted summary logs for a workflow run."""
    headers = _build_github_headers(token)
    jobs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"

    async def _execute_logs(cl: httpx.AsyncClient) -> GitHubWorkflowRunLog:
        lines: List[str] = []
        response = await cl.get(jobs_url, headers=headers)
        if response.status_code == 200:
            jobs = response.json().get("jobs", [])
            for job in jobs:
                job_name = job.get("name", "Job")
                job_status = job.get("status", "")
                job_conclusion = job.get("conclusion") or "pending"
                lines.append(f"=== Job: {job_name} ({job_status} · {job_conclusion}) ===")
                for step in job.get("steps", []):
                    step_name = step.get("name", "Step")
                    step_conclusion = step.get("conclusion") or step.get("status", "pending")
                    lines.append(f"  • {step_name}: {step_conclusion}")
        elif response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        else:
            lines.append(f"Log retrieval returned HTTP {response.status_code}")

        total_lines = len(lines)
        truncated = False
        if total_lines > max_lines:
            lines = lines[-max_lines:]
            truncated = True
            lines.insert(0, f"... [Log bounded to last {max_lines} lines; {total_lines} total lines]")

        raw_text = "\n".join(lines)
        redacted_text = redact_secrets(raw_text)

        return GitHubWorkflowRunLog(
            run_id=run_id,
            log_text=redacted_text,
            total_lines=total_lines,
            truncated=truncated,
            redacted=True,
        )

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.ci.logs", owner, repo, run_id, max_lines, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.ci.run.logs",
            coro_fn=_execute_logs,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception as exc:
        return GitHubWorkflowRunLog(
            run_id=run_id,
            log_text=f"Unable to retrieve job steps: {redact_secrets(str(exc))}",
            total_lines=1,
            truncated=False,
            redacted=True,
        )


async def cancel_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> Dict[str, Any]:
    """Request cancellation of an active workflow run."""
    auth_token = token or detect_github_token()
    if not auth_token:
        return {"success": False, "error": "GitHub authentication token is required."}

    headers = _build_github_headers(auth_token)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"

    async def _execute_cancel(cl: httpx.AsyncClient) -> Dict[str, Any]:
        resp = await cl.post(url, headers=headers)
        if resp.status_code in (202, 200):
            return {"success": True, "message": f"Workflow run #{run_id} cancellation requested."}
        if resp.status_code in (429, 500, 502, 503, 504):
            resp.raise_for_status()
        return {"success": False, "error": f"Cancellation rejected (HTTP {resp.status_code}): {resp.text[:200]}"}

    boundary = get_github_boundary()
    try:
        return await boundary.execute(
            operation_name="github.ci.run.cancel",
            coro_fn=_execute_cancel,
            owner=owner,
            repo=repo,
            user_id=user_id,
            is_mutation=True,
            idempotency_key=f"ci_cancel:{owner}:{repo}:{run_id}",
            invalidate_prefixes=[
                f"github.ci.run:{owner}:{repo}:{run_id}",
                f"github.ci.runs:{owner}:{repo}",
            ],
            client=client,
        )
    except GitHubResilienceError as err:
        return {"success": False, "error": err.user_friendly_message}
    except Exception as exc:
        return {"success": False, "error": redact_secrets(str(exc))}


async def rerun_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> Dict[str, Any]:
    """Request re-running of a completed or failed workflow run."""
    auth_token = token or detect_github_token()
    if not auth_token:
        return {"success": False, "error": "GitHub authentication token is required."}

    headers = _build_github_headers(auth_token)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/rerun"

    async def _execute_rerun(cl: httpx.AsyncClient) -> Dict[str, Any]:
        resp = await cl.post(url, headers=headers)
        if resp.status_code in (201, 202, 200):
            return {"success": True, "message": f"Workflow run #{run_id} rerun requested."}
        if resp.status_code in (429, 500, 502, 503, 504):
            resp.raise_for_status()
        return {"success": False, "error": f"Rerun rejected (HTTP {resp.status_code}): {resp.text[:200]}"}

    boundary = get_github_boundary()
    try:
        return await boundary.execute(
            operation_name="github.ci.run.rerun",
            coro_fn=_execute_rerun,
            owner=owner,
            repo=repo,
            user_id=user_id,
            is_mutation=True,
            idempotency_key=f"ci_rerun:{owner}:{repo}:{run_id}",
            invalidate_prefixes=[
                f"github.ci.run:{owner}:{repo}:{run_id}",
                f"github.ci.runs:{owner}:{repo}",
            ],
            client=client,
        )
    except GitHubResilienceError as err:
        return {"success": False, "error": err.user_friendly_message}
    except Exception as exc:
        return {"success": False, "error": redact_secrets(str(exc))}


async def list_releases(
    owner: str,
    repo: str,
    page: int = 1,
    per_page: int = 5,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    user_id: Optional[Any] = None,
) -> List[GitHubReleaseInfo]:
    """Retrieve read-only information for repository releases and tags."""
    headers = _build_github_headers(token)
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?page={page}&per_page={per_page}"

    async def _execute_releases(cl: httpx.AsyncClient) -> List[GitHubReleaseInfo]:
        resp = await cl.get(url, headers=headers)
        if resp.status_code != 200:
            if resp.status_code in (429, 500, 502, 503, 504):
                resp.raise_for_status()
            return []
        items = resp.json()
        releases: List[GitHubReleaseInfo] = []
        for r in items:
            releases.append(
                GitHubReleaseInfo(
                    id=r.get("id", 0),
                    tag_name=r.get("tag_name", ""),
                    name=r.get("name"),
                    published_at=r.get("published_at"),
                    html_url=r.get("html_url", ""),
                    prerelease=bool(r.get("prerelease", False)),
                    draft=bool(r.get("draft", False)),
                    body=r.get("body"),
                )
            )
        return releases

    boundary = get_github_boundary()
    auth_token = token or detect_github_token()
    cache_key = _make_github_cache_key(
        "github.releases", owner, repo, page, per_page, token=auth_token, client=client
    )
    try:
        return await boundary.execute(
            operation_name="github.releases.list",
            coro_fn=_execute_releases,
            owner=owner,
            repo=repo,
            user_id=user_id,
            cache_key=cache_key,
            cache_ttl=None,
            is_mutation=False,
            client=client,
            allow_stale_on_error=True,
        )
    except Exception:
        return []


def build_ci_aware_pr_summary(
    pr: GitHubPRDetails,
    files: List[GitHubPRFile],
    reviews: GitHubReviewSummary,
    runs: List[GitHubWorkflowRun],
) -> str:
    """Construct a compact, CI-aware pull request summary for mobile Telegram."""
    lines = [
        f"🔀 PR #{pr.number} · {pr.title}",
        f"{pr.head_branch} → {pr.base_branch}",
        f"Status: {pr.state.upper()}",
        "",
        "Changes:",
        f"{len(files)} file" + ("s" if len(files) != 1 else "") + f" (+{pr.additions} / -{pr.deletions})",
        "",
        "Reviews:",
        reviews.summary_text or "⚪ No reviews",
        "",
        "CI / Actions:",
    ]
    if runs:
        for r in runs[:3]:
            icon = "✅" if r.conclusion == "success" else ("❌" if r.conclusion == "failure" else "🟡")
            lines.append(f"{icon} {r.name} ({r.conclusion or r.status})")
    else:
        lines.append("⚪ No workflow runs reported")

    lines.append("")
    if pr.merged:
        lines.append("Merge: 🟣 Merged")
    elif pr.state == "closed":
        lines.append("Merge: ⚪ Closed")
    elif pr.mergeable is False:
        lines.append("Merge: 🔴 Blocked (unresolved conflicts)")
    elif pr.mergeable is True:
        lines.append("Merge: 🟢 Clean")
    else:
        lines.append("Merge: 🟡 Pending GitHub check")

    return "\n".join(lines)
