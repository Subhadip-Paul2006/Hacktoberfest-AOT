# AgentBridge Git Service API Reference

Reference specification for structured Git commands, models, and payloads across the Control Plane and Local Runtime.

---

## 1. Structured Control Plane Commands

All commands require `project_root` resolving to an authorized workspace directory.

### `git.status`
Returns structured status parsed from `git status --porcelain=v1 -b`.
- **Payload**: `{"project_root": str}`
- **Response**:
  ```json
  {
    "is_git": true,
    "repo_root": "/path/to/repo",
    "current_branch": "main",
    "tracking_branch": "origin/main",
    "commits_ahead": 1,
    "commits_behind": 0,
    "staged_files": [{"path": "auth.py", "change_type": "modified", "staged": true}],
    "unstaged_files": [],
    "untracked_files": [],
    "conflicted_files": [],
    "is_clean": false,
    "total_changes": 1
  }
  ```

### `git.branches`
Returns branch topology.
- **Payload**: `{"project_root": str}`
- **Response**:
  ```json
  {
    "current": "feature/auth",
    "local_branches": ["main", "feature/auth"],
    "tracking_remote": "origin/feature/auth",
    "commits_ahead": 2,
    "commits_behind": 0
  }
  ```

### `git.diff`
Returns bounded, secret-redacted diff text.
- **Payload**:
  - `project_root` (str): Project root.
  - `staged` (bool, optional): Whether to inspect staged index diff. Default `false`.
  - `path` (str, optional): Target file relative path.
  - `max_lines` (int, optional): Truncation limit. Default `200`.
- **Response**:
  ```json
  {
    "diff_text": "diff --git a/app.py b/app.py...",
    "total_lines": 42,
    "truncated": false,
    "staged": false,
    "files_changed": ["app.py"],
    "stat_summary": "app.py | 4 ++--"
  }
  ```

### `git.stage`
Stages specified files safely.
- **Payload**:
  - `project_root` (str)
  - `files` (list[str]): Relative file paths inside the repository.
- **Response**:
  ```json
  {
    "success": true,
    "staged_count": 2,
    "modified_files": ["app.py", "auth.py"]
  }
  ```

### `git.unstage`
Unstages specified files.
- **Payload**:
  - `project_root` (str)
  - `files` (list[str])
- **Response**:
  ```json
  {
    "success": true,
    "staged_count": 2,
    "modified_files": ["app.py", "auth.py"]
  }
  ```

### `git.commit`
Executes safe commit after verifying staged changes exist.
- **Payload**:
  - `project_root` (str)
  - `message` (str): Non-empty commit message.
  - `author` (str, optional)
- **Response**:
  ```json
  {
    "success": true,
    "commit_hash": "a1b2c3d4e5f67890",
    "message": "Add auth support",
    "files_committed": 2
  }
  ```

### `git.push_preview`
Returns summary of commits ahead and upstream target.
- **Payload**:
  - `project_root` (str)
  - `remote` (str, optional, default: `"origin"`)
- **Response**:
  ```json
  {
    "branch": "main",
    "remote": "origin",
    "remote_url": "https://github.com/org/repo.git",
    "upstream": "origin/main",
    "commits_ahead": 1,
    "commit_summaries": ["a1b2c3d Add auth support"],
    "ready_to_push": true,
    "warning": null
  }
  ```

### `git.push`
Executes remote push without force flags.
- **Payload**:
  - `project_root` (str)
  - `remote` (str, optional, default: `"origin"`)
  - `branch` (str, optional)
- **Response**:
  ```json
  {
    "success": true,
    "branch": "main",
    "remote": "origin",
    "message": "Pushed main to origin successfully."
  }
  ```

### `git.pr_create`
Creates a GitHub Pull Request via the REST API.
- **Payload**:
  - `project_root` (str)
  - `title` (str)
  - `body` (str, optional)
  - `head` (str, optional)
  - `base` (str, optional, default: `"main"`)
- **Response**:
  ```json
  {
    "success": true,
    "number": 42,
    "html_url": "https://github.com/org/repo/pull/42",
    "title": "Add auth support",
    "head": "feature/auth",
    "base": "main",
    "state": "open"
  }
  ```

---

## 2. Risk Classification Reference

| Operation | Risk Level | Auto-Approved? | Authorization Required? |
|---|---|---|---|
| `git.status` | `LOW` | Yes | No |
| `git.branches` | `LOW` | Yes | No |
| `git.diff` | `LOW` | Yes | No |
| `git.push_preview`| `LOW` | Yes | No |
| `git.stage` | `MEDIUM` | Policy-dependent | When requested |
| `git.unstage` | `MEDIUM` | Policy-dependent | When requested |
| `git.commit` | `HIGH` | No | **Yes** |
| `git.push` | `HIGH` | No | **Yes** |
| `git.pr_create` | `HIGH` | No | **Yes** |
| `force push` | `CRITICAL` | **Never** | **Blocked by Invariant** |
