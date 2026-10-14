# AgentBridge GitHub REST API Reference (Phase 6B.5)

## 1. Overview

The `GitHubAdapter` class in `sutra.git.github` provides a resilient, asynchronous HTTP client interface to GitHub's REST API (v3 / `application/vnd.github+json`). It handles token authentication, rate limit tracking, pagination, structured error transformation, and secret redaction.

---

## 2. Authentication & Credential Discovery

`GitHubAdapter` dynamically discovers authentication tokens using the following precedence:

1. Explicit constructor argument `token`
2. Environment variable `GITHUB_TOKEN`
3. Environment variable `GH_TOKEN`
4. GitHub CLI authentication token via `gh auth token`

Tokens are passed strictly via the `Authorization: Bearer <TOKEN>` header. Tokens are never exposed in user logs, Telegram messages, or audit entries.

---

## 3. Endpoints & Methods

### 3.1 Pull Requests

#### `list_pull_requests(owner, repo, state="open", per_page=10, page=1)`
- **HTTP**: `GET /repos/{owner}/{repo}/pulls?state={state}&per_page={per_page}&page={page}`
- **Risk**: `LOW`
- **Returns**: `list[GitHubPRDetails]`
- **Description**: Returns paginated list of pull requests matching the requested state (`open`, `closed`, `all`).

#### `get_pull_request(owner, repo, number)`
- **HTTP**: `GET /repos/{owner}/{repo}/pulls/{number}`
- **Risk**: `LOW`
- **Returns**: `GitHubPRDetails`
- **Description**: Returns full details for a pull request including head/base branches, additions, deletions, commit counts, and mergeability status.

#### `get_pull_request_files(owner, repo, number, per_page=30, page=1)`
- **HTTP**: `GET /repos/{owner}/{repo}/pulls/{number}/files?per_page={per_page}&page={page}`
- **Risk**: `LOW`
- **Returns**: `list[GitHubPRFile]`
- **Description**: Lists files modified by the PR with status (`added`, `modified`, `removed`), additions, and deletions.

#### `list_pull_request_comments(owner, repo, number, per_page=20, page=1)`
- **HTTP**: `GET /repos/{owner}/{repo}/issues/{number}/comments?per_page={per_page}&page={page}`
- **Risk**: `LOW`
- **Returns**: `list[GitHubPRComment]`
- **Description**: Retrieves PR discussion comments.

#### `add_pull_request_comment(owner, repo, number, body)`
- **HTTP**: `POST /repos/{owner}/{repo}/issues/{number}/comments`
- **Payload**: `{"body": body}`
- **Risk**: `HIGH`
- **Returns**: `GitHubPRComment`
- **Description**: Posts a discussion comment to the PR. Never executed as a shell command.

#### `get_pull_request_reviews(owner, repo, number)`
- **HTTP**: `GET /repos/{owner}/{repo}/pulls/{number}/reviews`
- **Risk**: `LOW`
- **Returns**: `GitHubReviewSummary`
- **Description**: Aggregates reviewer states into counts of approvals, changes requested, and pending reviews.

#### `merge_pull_request(owner, repo, number, merge_method="merge", commit_title=None, commit_message=None)`
- **HTTP**: `PUT /repos/{owner}/{repo}/pulls/{number}/merge`
- **Payload**: `{"merge_method": method, "commit_title": ..., "commit_message": ...}`
- **Risk**: `HIGH` (Guarded: Never auto-merged; always requires single-use human authorization)
- **Returns**: `GitHubMergeResult`
- **Description**: Merges a pull request using mode (`merge`, `squash`, `rebase`). Validates live mergeability before invocation.

---

### 3.2 GitHub Actions & CI Workflows

#### `list_workflow_runs(owner, repo, branch=None, per_page=10, page=1)`
- **HTTP**: `GET /repos/{owner}/{repo}/actions/runs?per_page={per_page}&page={page}[&branch={branch}]`
- **Risk**: `LOW`
- **Returns**: `list[GitHubWorkflowRun]`
- **Description**: Lists recent CI workflow runs with status, conclusion, commit SHA, and branch.

#### `get_workflow_run(owner, repo, run_id)`
- **HTTP**: `GET /repos/{owner}/{repo}/actions/runs/{run_id}`
- **Risk**: `LOW`
- **Returns**: `GitHubWorkflowRun`
- **Description**: Returns detailed execution information for a workflow run.

#### `get_workflow_run_logs(owner, repo, run_id, max_lines=80)`
- **HTTP**: `GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs`
- **Risk**: `LOW`
- **Returns**: `GitHubWorkflowRunLog`
- **Description**: Retrieves bounded, tail-truncated, secret-redacted execution logs for a workflow run.

#### `rerun_workflow_run(owner, repo, run_id)`
- **HTTP**: `POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun`
- **Risk**: `HIGH`
- **Returns**: `bool`
- **Description**: Triggers a re-run of a failed workflow run under human authorization.

#### `cancel_workflow_run(owner, repo, run_id)`
- **HTTP**: `POST /repos/{owner}/{repo}/actions/runs/{run_id}/cancel`
- **Risk**: `HIGH`
- **Returns**: `bool`
- **Description**: Cancels an active or queued workflow run under human authorization.

---

### 3.3 Releases & Tags (Read-Only)

#### `list_releases(owner, repo, per_page=5, page=1)`
- **HTTP**: `GET /repos/{owner}/{repo}/releases?per_page={per_page}&page={page}`
- **Risk**: `LOW`
- **Returns**: `list[GitHubReleaseInfo]`
- **Description**: Lists published releases with tag name, release title, published timestamp, and URL.

---

## 4. Structured Error Handling

All HTTP errors are mapped to `GitHubAPIError` with rich metadata:

| Status Code | Description | Handler Action |
| :--- | :--- | :--- |
| **401 Unauthorized** | Missing or invalid token | Prompts user to configure `GITHUB_TOKEN` or run `gh auth login`. |
| **403 Forbidden** | Insufficient permissions or rate limit exceeded | Reports permission error or rate limit reset time. |
| **404 Not Found** | PR, run, or repository not found | Returns structured resource not found error. |
| **409 Conflict** | Merge conflict or unmergeable state | Prevents merge mutation and reports live branch conflict. |
| **422 Unprocessable** | Invalid parameters or branch validation error | Reports specific GitHub validation message. |
| **429 Too Many Requests** | Rate limit throttled | Reports retry delay based on `Retry-After` header. |
| **5xx Server Error** | GitHub upstream outage | Triggers exponential retry or returns structured outage message. |
