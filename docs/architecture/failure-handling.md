# AgentBridge — Failure Handling & Error Normalization

## 1. Overview & Failure Philosophy

Distributed systems experience partial failures, network partitions, upstream rate limiting, and ambiguous execution outcomes. AgentBridge addresses failure handling under two non-negotiable principles:
1. **Deterministic Containment**: Failures must never leak raw exceptions, stack traces, HTTP status blobs, or credentials to end-user displays.
2. **Safe Idempotency & Reconciliation**: Ambiguous mutations (e.g. timeout on merge) must be reconciled rather than blindly retried.

---

## 2. Normalized Error Taxonomy

All exceptions across Git and GitHub layers are mapped into normalized domain error categories defined by [`GitHubErrorCode`](../../sutra/git/resilience.py):

| Error Code | HTTP Code / Trigger | User-Friendly Telegram Message | Retriable? |
| :--- | :--- | :--- | :--- |
| `AUTHENTICATION_ERROR` | 401 | *"GitHub authentication failed. Please check your token or run 'gh auth login'."* | ❌ No |
| `AUTHORIZATION_ERROR` | 403 (Forbidden) | *"GitHub permission denied. Please verify repository permissions."* | ❌ No |
| `RATE_LIMITED` | 429 / 403 Rate Limit | *"GitHub rate-limited you. Retry after X seconds."* | ✅ Yes |
| `TIMEOUT` | Client Timeout | *"GitHub request timed out. Local Git remains operational."* | ✅ Yes |
| `NETWORK_ERROR` | Connection reset / drop | *"Network error communicating with GitHub. Local Git remains operational."* | ✅ Yes |
| `NOT_FOUND` | 404 | *"The requested GitHub resource was not found."* | ❌ No |
| `CONFLICT` | 409 / Idempotency clash | *"Operation conflict detected on GitHub. State may have changed."* | ❌ No |
| `STALE_STATE` | PR Closed/Merged/SHA drift | *"Pull request state changed since preview. Please inspect latest PR state."* | ❌ No |
| `VALIDATION_ERROR` | 422 | *"GitHub validation rejected request: <redacted message>"* | ❌ No |
| `PROVIDER_UNAVAILABLE` | 500, 502, 503, 504 | *"GitHub is temporarily experiencing server issues. Local Git remains available."* | ✅ Yes |
| `CIRCUIT_OPEN` | Breaker OPEN | *"GitHub service is temporarily unavailable (circuit open). Retry in X seconds."* | ❌ Fast-Fail |
| `LOCAL_GIT_TIMEOUT` | Process timeout (124) | *"Local Git command timed out after 30.0s."* | ❌ No |
| `WORKSPACE_CONFLICT` | .git/index.lock | *"Workspace is locked by another Git operation."* | ❌ No |
| `UNKNOWN_ERROR` | Unclassified exception | *"GitHub error (HTTP X). Please try again later."* | ❌ No |

---

## 3. Rate Limiting & 429 / Retry-After Handling

When GitHub responds with HTTP 429 or HTTP 403 (with `x-ratelimit-remaining: 0`), AgentBridge:
1. Extracts the `Retry-After` header or calculates seconds until `x-ratelimit-reset`.
2. Emits a structured [`GitHubResilienceError`](../../sutra/git/resilience.py) containing the exact wait duration.
3. Does **not** hammer GitHub with blind immediate calls.
4. If executing within a retriable policy, sleeps for the required duration plus jitter before a bounded retry.

---

## 4. Stale-State Protection & Mutation Guards

### 4.1 Pre-Flight Fresh Inspection
Before executing any remote mutation (such as `merge_pull_request`), AgentBridge fetches fresh, un-cached state (`force_fresh=True`) to verify:
- PR is still `open` (not closed or already merged)
- `mergeable` is not `False` (no merge conflicts)
- Head branch SHA matches expected commit if specified

### 4.2 The Unknown-Result Problem (Merge Reconciliation)
When a network connection times out during a `POST` or `PUT` merge request:
```
POST /pulls/42/merge
       │
       ▼
GitHub succeeds
       │
 Network Drop / Timeout
       │
 AgentBridge receives TimeoutError
       │
 (DO NOT blindly retry POST!)
       │
       ▼
Reconcile: Fetch PR #42 state (force_fresh)
 ├─ PR.merged == True  ──► Return Success (Reconciled!)
 └─ PR.merged == False ──► Re-raise TimeoutError safely
```
This protects repositories against corrupt merge histories and duplicate actions.

---

## 5. Bounded CI Polling Control

Polling CI workflow runs can lead to rapid rate-limit exhaustion. Function [`poll_workflow_run_until_completed`](../../sutra/git/resilience.py) enforces strict polling bounds:
- **Maximum Polls**: Default 15 iterations.
- **Poll Interval**: Default 5.0 seconds with backoff.
- **Deadline**: Maximum overall timeout (e.g. 120 seconds).
- **Circuit Breaker Aware**: If the breaker trips during polling, polling aborts immediately returning the last known run state.

---

## 6. Centralized Secret Redaction

All error messages, log lines, and responses are sanitized via [`redact_secrets`](../../sutra/redaction.py):
- `ghp_...`, `gho_...`, `ghu_...`, `ghs_...`, `ghr_...` tokens are replaced with `[REDACTED_GH_TOKEN]`.
- HTTP `Authorization: Bearer ...` headers are never serialized.
- Raw URLs containing inline credentials (`https://user:pass@...`) are stripped before display.
