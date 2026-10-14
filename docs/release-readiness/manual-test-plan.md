# Manual MVP Test Plan

Run these checks only against a disposable local account, test bot/project, and temporary workspace. Do not use a production workspace for destructive-operation checks. Do not paste tokens into command lines or test reports.

## 1. Install and startup

1. Create an isolated Python 3.11+ virtual environment and install the project using `python -m pip install -e ".[dev]"`; the `dev` extra includes cryptography for development certificate generation.
2. Run `python -m sutra --help`, `python -m sutra setup --help`, `python -m sutra connectors --help`, and `python -m sutra local --help`.
3. Set `SUTRA_HOME` to a new temporary directory and run `python -m sutra setup`. Confirm the home directory and development certificate are created at the selected paths. Start the runtime to confirm it creates its SQLite state database; verify the default home remains `~/.sutra` when unset.
4. Run `python -m sutra doctor` and record missing optional dependencies without inserting credentials into logs.

**Pass:** commands exit cleanly, paths are predictable, no credentials are printed, and a second setup run behaves safely/idempotently.

## 2. Workspace and permission boundary

1. Register a disposable workspace containing a small Git repository and a separate directory outside it.
2. Exercise allowed reads: workspace listing, `git status`, file preview with a short file, and a large file near the configured preview limits.
3. Attempt access through `..`, URL-encoded traversal, symlinks/junctions, absolute paths outside the workspace, and a sensitive-looking file. Record whether each attempt is denied before reading data.
4. Exercise LOW-risk auto-approval, HIGH-risk pending approval, explicit deny, expiry, duplicate/stale Telegram callback, and callback engine failure.
5. Confirm audit history records requester, action, decision, result, and timing without storing tokens or unredacted secrets.

**Pass:** every denied access stays denied, stale approval cannot authorize a later request, failed writes are not reported as recorded, and audit records correlate with request IDs.

## 3. Terminal and process lifecycle

1. Run a permitted read-only command from an authorized workspace and a command from an unauthorized CWD.
2. Run a command that writes more output than the configured cap; confirm retained stdout and stderr stay bounded and report truncation.
3. Exercise timeout, explicit cancellation, disconnect, and shutdown with a child process that spawns a child. Confirm the process tree exits and workspace locks release.
4. Exercise an executable allowlist and safe-read-only profile. Try mutating Git commands and absolute path arguments outside the workspace in a disposable VM.

**Pass:** no shell interpretation, no child survives forced cleanup, output remains bounded, policy decisions are enforced, and lock state is released.

## 4. MCP connectors

1. Use the included local test servers for HTTP/stdio MCP, GitHub MCP, and Supabase MCP before connecting to any external provider.
2. Verify discovery, reconnect, timeout, malformed response, authentication failure, read-only resources, prompt rendering, and a denied mutating tool.
3. Assert resource reads, prompt requests, and tool calls each pass the central policy boundary and create suitable ActivityStore records before considering release.
4. For optional live checks, use a disposable GitHub account/repository and Supabase test project. Grant only test credentials; confirm revocation after the run.

**Pass:** unauthorized operations never reach the provider; credential errors are redacted; no token appears in logs, SQLite, or returned metadata.

## 5. Telegram and local control plane

1. Use a test bot restricted to a test user ID and local TLS control plane with temporary certificates.
2. Complete pairing, reconnect, command delivery, permission approve/deny, callback expiry, webhook-secret rejection, and bot restart.
3. Confirm unauthorized users receive no workspace details and bad webhook secrets fail with a generic response.

**Pass:** pairing/session rotation works, callback status matches persisted policy, and TLS/auth boundaries remain enforced after restart.

## 6. Release evidence

Record OS, Python version, package build/install command, test account type (never secret), command result, expected/actual result, and sanitized logs. Repeat automated tests on Windows, Linux, and macOS. A skipped POSIX process-group test on Windows is not evidence of POSIX success.
