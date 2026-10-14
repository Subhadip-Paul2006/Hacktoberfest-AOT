"""Data models and structured types for Safe Git & GitHub Integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class GitChangeType(str, Enum):
    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNTRACKED = "untracked"
    CONFLICTED = "conflicted"


@dataclass
class GitFileChange:
    """Represents a change in a single file in the repository."""
    path: str
    change_type: GitChangeType
    staged: bool
    original_path: Optional[str] = None
    raw_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type.value,
            "staged": self.staged,
            "original_path": self.original_path,
            "raw_status": self.raw_status,
        }


@dataclass
class GitBranchInfo:
    """Branch topology and tracking details."""
    current: Optional[str] = None
    local_branches: List[str] = field(default_factory=list)
    tracking_remote: Optional[str] = None
    commits_ahead: int = 0
    commits_behind: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "local_branches": self.local_branches,
            "tracking_remote": self.tracking_remote,
            "commits_ahead": self.commits_ahead,
            "commits_behind": self.commits_behind,
        }


@dataclass
class GitStatusResult:
    """Structured result of git status examination."""
    is_git: bool
    repo_root: Optional[str] = None
    current_branch: Optional[str] = None
    tracking_branch: Optional[str] = None
    commits_ahead: int = 0
    commits_behind: int = 0
    staged_files: List[GitFileChange] = field(default_factory=list)
    unstaged_files: List[GitFileChange] = field(default_factory=list)
    untracked_files: List[GitFileChange] = field(default_factory=list)
    conflicted_files: List[GitFileChange] = field(default_factory=list)
    is_clean: bool = True
    total_changes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_git": self.is_git,
            "repo_root": self.repo_root,
            "current_branch": self.current_branch,
            "tracking_branch": self.tracking_branch,
            "commits_ahead": self.commits_ahead,
            "commits_behind": self.commits_behind,
            "staged_files": [f.to_dict() for f in self.staged_files],
            "unstaged_files": [f.to_dict() for f in self.unstaged_files],
            "untracked_files": [f.to_dict() for f in self.untracked_files],
            "conflicted_files": [f.to_dict() for f in self.conflicted_files],
            "is_clean": self.is_clean,
            "total_changes": self.total_changes,
        }


@dataclass
class GitDiffRequest:
    """Structured request for git diff."""
    project_root: str
    staged: bool = False
    filepath: Optional[str] = None
    max_lines: int = 200


@dataclass
class GitDiffResult:
    """Structured and bounded git diff result with redaction metadata."""
    diff_text: str
    total_lines: int
    truncated: bool
    staged: bool
    files_changed: List[str] = field(default_factory=list)
    stat_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "diff_text": self.diff_text,
            "total_lines": self.total_lines,
            "truncated": self.truncated,
            "staged": self.staged,
            "files_changed": self.files_changed,
            "stat_summary": self.stat_summary,
        }


@dataclass
class GitStageRequest:
    """Structured request to stage/unstage files."""
    project_root: str
    files: List[str] = field(default_factory=list)
    all_files: bool = False


@dataclass
class GitStageResult:
    """Result of staging or unstaging files."""
    success: bool
    staged_count: int
    modified_files: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "staged_count": self.staged_count,
            "modified_files": self.modified_files,
            "error": self.error,
        }


@dataclass
class GitCommitRequest:
    """Structured request to perform a Git commit."""
    project_root: str
    message: str
    author: Optional[str] = None


@dataclass
class GitCommitResult:
    """Result of a git commit operation."""
    success: bool
    commit_hash: Optional[str] = None
    message: str = ""
    files_committed: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "commit_hash": self.commit_hash,
            "message": self.message,
            "files_committed": self.files_committed,
            "error": self.error,
        }


@dataclass
class GitPushPreview:
    """Preview of what will be pushed before execution."""
    branch: str
    remote: str
    remote_url: str
    upstream: Optional[str] = None
    commits_ahead: int = 0
    commit_summaries: List[str] = field(default_factory=list)
    ready_to_push: bool = True
    warning: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "remote": self.remote,
            "remote_url": self.remote_url,
            "upstream": self.upstream,
            "commits_ahead": self.commits_ahead,
            "commit_summaries": self.commit_summaries,
            "ready_to_push": self.ready_to_push,
            "warning": self.warning,
        }


@dataclass
class GitPushRequest:
    """Structured request to push commits to a remote."""
    project_root: str
    remote: str = "origin"
    branch: Optional[str] = None
    force: bool = False  # Note: Force push is strictly prohibited by safety invariant


@dataclass
class GitPushResult:
    """Result of a git push execution."""
    success: bool
    branch: str
    remote: str
    message: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "branch": self.branch,
            "remote": self.remote,
            "message": self.message,
            "error": self.error,
        }


@dataclass
class GitHubRemoteInfo:
    """Metadata regarding configured GitHub remote."""
    is_github: bool
    owner: Optional[str] = None
    repo: Optional[str] = None
    remote_name: str = "origin"
    remote_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_github": self.is_github,
            "owner": self.owner,
            "repo": self.repo,
            "remote_name": self.remote_name,
            "remote_url": self.remote_url,
        }


@dataclass
class GitHubPRRequest:
    """Structured request to create a GitHub Pull Request."""
    project_root: str
    title: str
    body: str
    head: Optional[str] = None
    base: str = "main"
    draft: bool = False


@dataclass
class GitHubPRResult:
    """Result of a GitHub Pull Request creation."""
    success: bool
    number: Optional[int] = None
    html_url: Optional[str] = None
    title: Optional[str] = None
    head: Optional[str] = None
    base: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "number": self.number,
            "html_url": self.html_url,
            "title": self.title,
            "head": self.head,
            "base": self.base,
            "state": self.state,
            "error": self.error,
        }


class GitHubMergeMode(str, Enum):
    """Permitted merge strategies for GitHub Pull Requests."""
    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


@dataclass
class GitHubPRDetails:
    """Detailed metadata for a GitHub Pull Request."""
    number: int
    title: str
    state: str  # "open", "closed", "merged"
    head_branch: str
    base_branch: str
    author: str
    created_at: str
    updated_at: str
    body: str = ""
    html_url: str = ""
    draft: bool = False
    merged: bool = False
    mergeable: Optional[bool] = None
    mergeable_state: Optional[str] = None
    comments_count: int = 0
    commits_count: int = 0
    changed_files_count: int = 0
    additions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "head_branch": self.head_branch,
            "base_branch": self.base_branch,
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "body": self.body,
            "html_url": self.html_url,
            "draft": self.draft,
            "merged": self.merged,
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "comments_count": self.comments_count,
            "commits_count": self.commits_count,
            "changed_files_count": self.changed_files_count,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class GitHubPRFile:
    """Changed file in a GitHub Pull Request."""
    filename: str
    status: str  # "added", "modified", "removed", "renamed"
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "changes": self.changes,
            "patch": self.patch,
        }


@dataclass
class GitHubPRComment:
    """Discussion or review comment on a GitHub Pull Request."""
    id: int
    author: str
    body: str
    created_at: str
    updated_at: str
    html_url: Optional[str] = None
    comment_type: str = "issue"  # "issue" for discussion, "review" for code
    path: Optional[str] = None
    line: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "html_url": self.html_url,
            "comment_type": self.comment_type,
            "path": self.path,
            "line": self.line,
        }


@dataclass
class GitHubReviewState:
    """Individual review entry for a GitHub Pull Request."""
    reviewer: str
    state: str  # "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"
    submitted_at: Optional[str] = None
    body: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer": self.reviewer,
            "state": self.state,
            "submitted_at": self.submitted_at,
            "body": self.body,
        }


@dataclass
class GitHubReviewSummary:
    """Aggregated review status of a GitHub Pull Request."""
    reviews: List[GitHubReviewState] = field(default_factory=list)
    requested_reviewers: List[str] = field(default_factory=list)
    approved_count: int = 0
    changes_requested_count: int = 0
    pending_count: int = 0
    summary_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviews": [r.to_dict() for r in self.reviews],
            "requested_reviewers": self.requested_reviewers,
            "approved_count": self.approved_count,
            "changes_requested_count": self.changes_requested_count,
            "pending_count": self.pending_count,
            "summary_text": self.summary_text,
        }


@dataclass
class GitHubMergeResult:
    """Result of a GitHub Pull Request merge attempt."""
    success: bool
    merged: bool = False
    sha: Optional[str] = None
    message: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "merged": self.merged,
            "sha": self.sha,
            "message": self.message,
            "error": self.error,
        }


@dataclass
class GitHubWorkflowRun:
    """Metadata for a GitHub Actions workflow run."""
    id: int
    name: str
    head_branch: str
    head_sha: str
    status: str  # "queued", "in_progress", "completed"
    conclusion: Optional[str] = None  # "success", "failure", "neutral", "cancelled", etc.
    event: str = "push"
    created_at: str = ""
    updated_at: str = ""
    html_url: str = ""
    run_number: int = 0
    display_title: Optional[str] = None
    workflow_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "head_branch": self.head_branch,
            "head_sha": self.head_sha,
            "status": self.status,
            "conclusion": self.conclusion,
            "event": self.event,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "html_url": self.html_url,
            "run_number": self.run_number,
            "display_title": self.display_title,
            "workflow_id": self.workflow_id,
        }


@dataclass
class GitHubWorkflowRunLog:
    """Bounded, redacted log output from a GitHub Actions workflow run."""
    run_id: int
    log_text: str
    total_lines: int
    truncated: bool = False
    redacted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "log_text": self.log_text,
            "total_lines": self.total_lines,
            "truncated": self.truncated,
            "redacted": self.redacted,
        }


@dataclass
class GitHubReleaseInfo:
    """Read-only metadata for a GitHub release or tag."""
    id: int
    tag_name: str
    name: Optional[str] = None
    published_at: Optional[str] = None
    html_url: str = ""
    prerelease: bool = False
    draft: bool = False
    body: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tag_name": self.tag_name,
            "name": self.name,
            "published_at": self.published_at,
            "html_url": self.html_url,
            "prerelease": self.prerelease,
            "draft": self.draft,
            "body": self.body,
        }

