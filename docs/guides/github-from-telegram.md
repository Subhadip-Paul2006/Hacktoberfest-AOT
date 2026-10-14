# Using GitHub Collaboration & CI from Telegram (Phase 6B.5)

## 1. Introduction

With AgentBridge Phase 6B.5, you can manage the full lifecycle of GitHub Pull Requests, inspect code reviews, track CI pipelines, and read build logs directly from your mobile phone using Telegram ChatOps.

> [!NOTE]
> **Zero IDE Requirement**: You do not need VS Code, Cursor, or any active editor running on your workstation. AgentBridge communicates directly with local Git and the GitHub REST API.

---

## 2. Accessing the GitHub Hub

In your paired Telegram ChatOps session:

1. Tap **🔀 Git** on the main menu, or send `/git`.
2. Select **🌐 GitHub Hub** from the Git menu.

You will see the main GitHub collaboration dashboard:

```
🌐 GitHub Collaboration Hub
Repo: owner/repo
Remote: origin (https://github.com/owner/repo.git)

Select an option below to manage PRs, reviews, CI workflows, and releases:

[ 📋 Pull Requests ]  [ ➕ New PR ]
[ ⚙️ CI / Actions ]   [ 🏷️ Releases ]
[ 🔄 Refresh ]        [ 🔙 Git Menu ]
```

---

## 3. Pull Request Management

### 3.1 Listing Pull Requests
Tap **📋 Pull Requests** to see an overview of open PRs:
```
📋 Pull Requests (owner/repo)
Showing 1-10 of 12 pull requests

#42 · Fix authentication token expiry
     feature/auth → main · 🟢 OPEN
#41 · Optimize database indexing
     perf/db → main · 🟢 OPEN
#39 · Add GitHub collaboration docs
     docs/gh → main · 🟢 OPEN

[ PR #42 ]  [ PR #41 ]  [ PR #39 ]
[ ◀ Prev ]  [ Page 1/2 ]  [ Next ▶ ]
[ 🔄 Filter: OPEN ]  [ 🔙 GitHub Hub ]
```

### 3.2 Inspecting a PR Overview
Tap any PR number (e.g., `[ PR #42 ]`) or run `/pr 42`:
```
🔀 Pull Request #42
Fix authentication token expiry

• State: 🟢 OPEN
• Author: @octocat
• Branch: feature/auth → main
• Mergeable: ✅ Yes (clean)
• Changes: 6 files (+182 / -71 in 3 commits)
• Reviews: 1 approved, 0 requested changes, 1 pending
• CI Checks: ✅ 3 passed, ❌ 0 failed, 🟡 1 in progress

[ 📄 Files (6) ]    [ 💬 Comments (4) ]
[ 👥 Reviews (2) ]  [ ⚙️ Checks (4) ]
[ 🔀 Merge PR ]     [ 🔙 PR List ]
```

### 3.3 Inspecting Changed Files
Tap **📄 Files** to inspect modified files and lines:
```
📄 Changed Files — PR #42
Total: 6 files (+182 / -71)

• M sutra/git/github.py (+110 / -25)
• M sutra/apps/telegram/formatting.py (+45 / -12)
• A tests/test_github_collaboration.py (+180 / -0)
• M docs/architecture/github-collaboration.md (+35 / -2)

[ 🔙 Back to PR #42 ]
```

### 3.4 Comments & Discussions
- Tap **💬 Comments** to read recent discussion on the PR.
- To post a comment directly from Telegram:
  ```bash
  /pr comment 42 "Please verify token expiry handling with unit tests."
  ```
  *(Requires operator confirmation in ChatOps)*.

### 3.5 Merging a PR Safely
To merge a PR:
1. Tap **🔀 Merge PR** or run `/pr merge 42 squash`.
2. AgentBridge performs an automated safety pre-check:
   - Validates live GitHub state (ensures PR is not closed or already merged).
   - Validates branch conflicts and mergeability.
3. Displays an explicit confirmation prompt:
   ```
   ⚠️ PR Merge Confirmation
   Are you sure you want to merge PR #42 using method 'squash'?
   This action cannot be undone.

   [ ✅ Confirm Merge ]  [ ❌ Cancel ]
   ```
4. **Never auto-merges**: Even with active permission policies, merging always requires real-time human authorization (`ALLOW_ONCE`).

---

## 4. GitHub Actions & CI Visibility

### 4.1 Viewing Recent CI Runs
Tap **⚙️ CI / Actions** or run `/ci`:
```
⚙️ GitHub Actions — Workflow Runs
Repo: owner/repo

#982134 · CI Test Suite · branch: feature/auth
         Status: ❌ completed (failure) · 2m ago
#982110 · Lint & Format · branch: feature/auth
         Status: ✅ completed (success) · 5m ago
#982050 · Security Audit · branch: main
         Status: ✅ completed (success) · 1h ago

[ Run #982134 ]  [ Run #982110 ]
[ 🔄 Refresh ]   [ 🔙 GitHub Hub ]
```

### 4.2 Inspecting a Run & Reading Logs
Tap on a failed run (e.g., `[ Run #982134 ]`):
```
⚙️ Workflow Run #982134
Workflow: CI Test Suite
Status: ❌ completed (failure)
Branch: feature/auth · Commit: 9a7b3c1

[ 📜 View Logs ]
[ 🔁 Rerun Workflow ]
[ 🔙 CI Runs ]
```
Tap **📜 View Logs** to retrieve tail-truncated, secret-redacted output highlighting the failure point.

### 4.3 Rerunning or Cancelling Workflows
- **Rerun**: Tap `[ 🔁 Rerun Workflow ]` (requires `PermissionEngine` prompt).
- **Cancel**: Tap `[ 🛑 Cancel Workflow ]` for active running jobs.

---

## 5. Releases & Tags (Read-Only)
Tap **🏷️ Releases** to view published releases, tags, creation dates, and release URLs without mutating repository tags.
