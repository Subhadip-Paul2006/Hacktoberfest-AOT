# AgentBridge Git & GitHub Workflow

This document specifies the lifecycle and state transitions for remote mobile code review, staging, commits, remote pushing, and pull request creation.

## 1. End-to-End Lifecycle Overview

```
1. Review Status
       │
       ▼
2. Select Files (Interactive Mobile Staging)
       │
       ▼
3. Review Diff (Secret-Redacted & Bounded)
       │
       ▼
4. Stage (MEDIUM risk / Workspace lock check)
       │
       ▼
5. Commit (HIGH risk / Human Authorization / Hash verification)
       │
       ▼
6. Push (HIGH risk / Upstream ahead check / Human Authorization)
       │
       ▼
7. Create Pull Request (GitHub REST API / Human Authorization)
```

---

## 2. Phase-by-Phase Execution Details

### A. Review Status
- **Action**: Operator taps `[📊 Status]` or opens `[🔀 Git]`.
- **Backend**: Executes `git status --porcelain=v1 -b`.
- **Output**: Clean working tree or categorized changes:
  - Staged changes (`M`, `A`, `D`, `R`, `C`)
  - Unstaged changes (`M`, `D`)
  - Untracked files (`??`)
  - Conflicted paths (`UU`, `AA`, `UD`, etc.)

### B. Interactive Mobile Staging
- **Action**: Operator opens `[☑️ Stage]`.
- **UX**: Displays list of candidate files with checkbox toggles (`☑️` / `☐`).
- **Safety**: Only explicitly selected files are staged. The repository is never staged blindly (`git add .` is disallowed).
- **Options**:
  - `[Stage Selected]`: Stages toggled items.
  - `[Stage All]`: Explicit button to stage all candidate items.
  - `[Unstage All]`: Unstages index changes safely via `git restore --staged`.

### C. Bounded Diff Review
- **Action**: Operator taps `[📝 Diff]`.
- **Options**: Toggle between *Working Tree (Unstaged)* and *Staged (Index)* diffs.
- **Bounds**: Max 80–120 lines transmitted to Telegram. If the diff exceeds the limit, a truncation notice indicates total remaining lines.
- **Redaction**: All secret tokens, API keys (`ghp_*`, `AKIA*`, Bearer tokens, private keys) are automatically masked with `[REDACTED]`.

### D. Safe Commit
- **Safety Precondition**:
  1. Working tree and index are queried in real time. If zero files are staged, commit is rejected.
  2. Workspace write locks are checked to ensure no active agent writes are ongoing.
  3. Commit message must be non-empty.
- **Execution**:
  - Sent via `/commit <message>` or `[💾 Quick Commit]`.
  - Evaluated by `PermissionEngine` (`HIGH` risk).
  - Creates commit and reports the resulting commit SHA.

### E. Remote Push
- **Safety Precondition**:
  1. Inspects remote tracking topology and counts commits ahead of remote.
  2. Ensures working tree has no unresolved merge conflicts.
  3. Ensures HEAD is not detached.
  4. Force-push is strictly rejected.
- **Execution**:
  - Requires human authorization via Telegram inline button confirmation.
  - Subprocess executed with `GIT_TERMINAL_PROMPT=0` to prevent hangs if credentials fail.
  - Reports clean error on authentication failure or non-fast-forward divergence.

### F. GitHub Pull Request Creation
- **Condition**: Remote must be recognized as hosted on `github.com`.
- **Execution**:
  - Initiated via `/pr <title>` or `[🔀 Create PR]`.
  - Requires human authorization (`HIGH` risk).
  - Posts to GitHub REST API endpoint `POST /repos/{owner}/{repo}/pulls`.
  - Returns PR number, title, and clickable link.
