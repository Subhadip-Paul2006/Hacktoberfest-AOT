# AgentBridge GitHub Collaboration Architecture (Phase 6B.5)

## 1. Architectural Philosophy & Boundaries

AgentBridge Phase 6B.5 delivers **Advanced GitHub Collaboration, PR Management & CI Visibility**. It extends the remote engineering capabilities of AgentBridge, enabling developers to monitor pull requests, review changed files, view and post comments, inspect review statuses, and track GitHub Actions CI runs directly from Telegram without needing an active IDE or desktop session.

### The Source of Truth Boundary
The architecture maintains a strict, decoupled boundary between local repository operations and remote GitHub collaboration:

```
+-------------------------------------------------------------------------+
|                              Telegram ChatOps                           |
|      [PRs]  [Files]  [Comments]  [Reviews]  [CI Runs]  [Merge]          |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
|                    Control Plane & Permission Engine                    |
|   - Deterministic Risk Evaluation (LOW, MEDIUM, HIGH, CRITICAL)         |
|   - Real-Time Human Authorization for PR Merge & CI Mutations           |
|   - Strict Replay, Idempotency & Stale-State Protection                |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
|                        Local Runtime Dispatcher                         |
|   - Workspace Boundary Containment                                      |
|   - Workspace Lock Manager (Concurrent mutation lock)                   |
+-------------------------------------------------------------------------+
                                    │
                  ┌─────────────────┴─────────────────┐
                  ▼                                   ▼
+-----------------------------------+   +---------------------------------+
|        Local Git Service          |   |       GitHub REST Service       |
|  (sutra/git/local.py)             |   |  (sutra/git/github.py)          |
|  - Source of Truth: Local .git    |   |  - Source of Truth: Remote PRs  |
|  - Porcelain v1 Status            |   |  - PR Listing & Bounded Detail  |
|  - Bounded Secret-Redacted Diff   |   |  - Changed Files & Additions    |
|  - Staging, Commit & Local Branch |   |  - Discussion & Review Comments |
|  - Protected Push to Remote       |   |  - Review Status (Approve/Req)  |
|                                   |   |  - Guarded PR Merge API         |
|                                   |   |  - CI Workflow Runs & Run Logs  |
|                                   |   |  - Release & Tag Metadata       |
+-----------------------------------+   +---------------------------------+
```

> [!IMPORTANT]
> **No IDE Connection Required**: AgentBridge does not require VS Code, Cursor, JetBrains, or any desktop editor to be open. Local operations communicate directly with the local `.git` engine via bounded CLI commands, while remote collaboration communicates directly with the GitHub REST API using discovered credentials.
>
> **GitHub MCP Boundary**: GitHub MCP is **NOT** a mandatory runtime dependency for Phase 6B.5. All operations use the built-in, resilient `GitHubAdapter` over standard HTTPS. MCP remains an optional future connector.

---

## 2. Core Functional Subsystems

### 2.1 Structured PR Listing & Pagination
- **Filters**: Supports filtering by PR state (`open`, `closed`, `all`).
- **Pagination**: Paginated retrieval (`per_page=10`, `page=N`) prevents unbounded payload sizes in Telegram.
- **Compact Presentation**: Displays concise summaries (`#42 · Fix token refresh · feature/auth → main · OPEN`).

### 2.2 Deep PR Inspection & CI-Aware Summary
- Detailed PR overview combines:
  - Header & status: PR number, title, state, draft status, mergeable status.
  - Branches: Source branch (`head`) and target branch (`base`).
  - Metadata: Author, creation timestamp, last updated timestamp.
  - Metrics: Total commits, changed files count, additions (`+N`), deletions (`-N`).
  - Review status: Approvals, changes requested, pending reviews.
  - CI status: Combined workflow checks summary (success, in progress, failed).
- Stored as `GitHubPRDetails` dataclass.

### 2.3 Bounded Changed Files View
- Lists files modified in the PR with status (`added`, `modified`, `removed`), additions, and deletions.
- Limits file display to bounded sets (first 10 files with truncation notice for larger PRs).
- Prepares for drill-down file inspections without overloading mobile bandwidth.

### 2.4 PR Comments & Discussion
- **View Discussion**: Bounded display of latest PR discussion comments with author and timestamps.
- **Post Comment**: Structured command (`/pr comment <number> <message>`) and Telegram input callback.
- **Safety**: Comment strings are strictly validated and sent as JSON payloads to GitHub REST; they are never executed as shell commands.

### 2.5 Review State Inspection
- Inspects submitted reviews via `/repos/{owner}/{repo}/pulls/{number}/reviews`.
- Categorizes latest review states per reviewer (`APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `PENDING`).
- Displays human-readable badge indicators (✅ Approved, ❌ Changes Requested, 🟡 Pending).

### 2.6 Guarded Pull Request Merge
- **Explicit Safety Sequence**:
  1. User triggers merge request for a PR.
  2. Live fresh inspection of PR status, mergeability (`mergeable == True`), and branch state.
  3. Interactive preview displayed with mode selection (`merge`, `squash`, `rebase`).
  4. Permission evaluation: Requires explicit real-time approval (`HIGH` risk).
  5. Policy invariant: `ALLOW_SESSION` auto-grant is demoted to `ALLOW_ONCE`; persistent policies cannot auto-merge.
  6. Execution via GitHub Merge API (`PUT /repos/{owner}/{repo}/pulls/{number}/merge`).
  7. Clear error reporting if GitHub reports merge conflicts (`409 Conflict`), branch protection blocks, or failing required checks.

---

## 3. Permission Engine Integration & Risk Tiers

Every GitHub operation is categorized within the central `PermissionEngine`:

| Risk Tier | Operations | Authorization Behavior |
| :--- | :--- | :--- |
| **LOW** | `git_pr_list`, `git_pr_view`, `git_pr_files`, `git_pr_comments`, `git_pr_reviews`, `git_ci_runs`, `git_ci_run_get`, `git_ci_logs`, `git_releases_list` | Automatically allowed or covered by read policies. |
| **MEDIUM** | `github_pr_label`, `github_pr_assign`, `github_pr_unlabel` | Evaluates persistent policies or prompts operator. |
| **HIGH** | `git_pr_comment`, `git_ci_rerun`, `git_ci_cancel`, `github_pr_review_request`, `github_pr_update` | Requires operator prompt or project-specific persistent grant. |
| **HIGH / GUARDED** | `git_pr_merge`, `github_pr_merge` | **Never auto-merged.** Invariant strictly demotes session-wide grants to single-use `ALLOW_ONCE`. Real-time confirmation required. |
| **CRITICAL** | Destructive branch operations (`push --force`, repository deletion) | Blocked / Strictly prohibited. |

---

## 4. Resilience, Caching & Idempotency

1. **Stale State Protection**:
   - Before executing state-changing operations (such as PR merge, CI cancel, or CI rerun), AgentBridge queries live GitHub state to ensure the PR has not already been merged, closed, or fallen into conflict.
2. **Duplicate Request Protection**:
   - Telegram action callbacks enforce request tracking to prevent double-click race conditions from triggering duplicate comments or multiple merge attempts.
3. **Secret Redaction**:
   - All PR diffs, comment previews, and CI logs pass through AgentBridge's centralized `redact_secrets` pipeline before rendering in Telegram.
