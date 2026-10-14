"""SŪTRA Git & GitHub Integration Service.

Provides a clean separation between Local Git operations and remote GitHub APIs,
with bounded diffs, secret redaction, interactive staging, safe commit, permission-gated
push, and PR workflows.
"""
from __future__ import annotations

from pathlib import Path

from .github import (
    add_pull_request_comment,
    build_ci_aware_pr_summary,
    cancel_workflow_run,
    create_pull_request,
    detect_github_remote,
    detect_github_token,
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
from .local import (
    GitSubprocessTimeoutError,
    commit,
    detect_remotes,
    get_git_branches,
    get_git_diff,
    get_git_status,
    get_push_preview,
    get_repo_root,
    is_git_repository,
    push,
    stage_files,
    unstage_files,
)
from .resilience import (
    GitHubErrorCode,
    GitHubResilienceBoundary,
    GitHubResilienceConfig,
    GitHubResilienceError,
    get_github_boundary,
    normalize_github_exception,
    poll_workflow_run_until_completed,
    set_github_boundary,
)

from .models import (
    GitBranchInfo,
    GitChangeType,
    GitCommitRequest,
    GitCommitResult,
    GitDiffRequest,
    GitDiffResult,
    GitFileChange,
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
    GitPushPreview,
    GitPushRequest,
    GitPushResult,
    GitStageRequest,
    GitStageResult,
    GitStatusResult,
)
from .security import (
    assert_no_force_push,
    check_workspace_lock,
    sanitize_and_redact_diff,
    validate_files_in_repo,
    validate_repo_path,
)


class GitManager:
    """Backward-compatible repository detector preserved from Phase 1."""

    @staticmethod
    def is_repository(project_path: str | Path) -> bool:
        """Detect a Git worktree or submodule by its .git directory/file marker."""
        return is_git_repository(project_path)


__all__ = [
    "GitManager",
    "is_git_repository",
    "get_repo_root",
    "get_git_branches",
    "get_git_status",
    "get_git_diff",
    "stage_files",
    "unstage_files",
    "commit",
    "get_push_preview",
    "push",
    "detect_remotes",
    "parse_github_remote",
    "detect_github_remote",
    "detect_github_token",
    "create_pull_request",
    "list_pull_requests",
    "get_pull_request",
    "get_pull_request_files",
    "list_pull_request_comments",
    "add_pull_request_comment",
    "get_pull_request_reviews",
    "merge_pull_request",
    "list_workflow_runs",
    "get_workflow_run",
    "get_workflow_run_logs",
    "cancel_workflow_run",
    "rerun_workflow_run",
    "list_releases",
    "build_ci_aware_pr_summary",
    "validate_repo_path",
    "validate_files_in_repo",
    "assert_no_force_push",
    "sanitize_and_redact_diff",
    "check_workspace_lock",
    "GitChangeType",
    "GitFileChange",
    "GitBranchInfo",
    "GitStatusResult",
    "GitDiffRequest",
    "GitDiffResult",
    "GitStageRequest",
    "GitStageResult",
    "GitCommitRequest",
    "GitCommitResult",
    "GitPushPreview",
    "GitPushRequest",
    "GitPushResult",
    "GitHubRemoteInfo",
    "GitHubPRRequest",
    "GitHubPRResult",
    "GitHubMergeMode",
    "GitHubMergeResult",
    "GitHubPRComment",
    "GitHubPRDetails",
    "GitHubPRFile",
    "GitHubReleaseInfo",
    "GitHubReviewState",
    "GitHubReviewSummary",
    "GitHubWorkflowRun",
    "GitHubWorkflowRunLog",
    "GitHubErrorCode",
    "GitHubResilienceError",
    "GitHubResilienceBoundary",
    "GitHubResilienceConfig",
    "get_github_boundary",
    "set_github_boundary",
    "poll_workflow_run_until_completed",
    "GitSubprocessTimeoutError",
    "normalize_github_exception",
]


