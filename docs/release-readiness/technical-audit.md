# AgentBridge Release Readiness: Technical Audit

**Assessment date:** 2026-10-12
**Repository:** `sutra-local` 0.1.0 (`sutra` CLI), Python 3.11+
**Decision:** **Not ready for an MVP release. Limited local manual testing is reasonable after the final automated suite passes.**
**Audit scope:** Repository inventory, selected security/runtime paths, regression evidence, a completed pre-change baseline suite, and an interrupted post-change run. This was not a line-by-line review of every source file; see [file-review-matrix.md](file-review-matrix.md).

## Findings fixed in this pass

- **Telegram permission callback reported success despite a failed decision write.** The callback now reports success only when `PermissionEngine.apply_decision` returns true; failures remain retryable and stale requests are reported as stale. Regression tests cover false returns and exceptions.
- **Terminal subprocess output was fully buffered before truncation.** Output and error streams are now drained concurrently in bounded chunks, retaining no more than the configured cap. A regression test streams 16 MiB and asserts peak traced Python allocation stays below 4 MiB with a 1 KiB retained-output limit.
- **Generic CLI forced cleanup could fail and target the caller’s process group.** The adapter now starts a separate POSIX session and guards platform-specific kill APIs. A POSIX-only regression is skipped on Windows, so this fix still needs Linux/macOS execution.
- **Queued coroutine objects could be abandoned unclosed.** Rejected, deduplicated, canceled, and shutdown-queued coroutine objects are now closed. Regression tests cover rejection and queued cancellation.

## Release blockers and residual risks

1. **MCP resource and prompt operations bypass the permission engine and ActivityStore.** Tool calls are policy-checked and audited; `_execute_read_resource` and `_execute_get_prompt` only check declared capability and connection state. A connected MCP server can expose sensitive data through those paths without centralized authorization/audit. Fix and regression coverage are required before release.
2. **Terminal path validation is incomplete for command arguments.** It rejects traversal when `..` resolves outside authorized roots and screens some sensitive path strings, but ordinary absolute paths without `..` are not checked. Since arbitrary executables can interpret arguments as filesystem targets, workspace confinement does not prove filesystem confinement. Restrict/validate known path arguments or narrow the security claim before release.
3. **Platform support has only been exercised on Windows here.** The POSIX process-group test is skipped on Windows; macOS/Linux behavior, packaging, signal cleanup, and platform-specific runtime paths remain unverified.
4. **No CI workflow or repository lint/type-check configuration is present.** Baseline Ruff reported 1,889 findings with no project rule configuration; baseline mypy reported 72 errors across 20 files. These are baseline diagnostics, not all release blockers by themselves, but there is no automated release gate.
5. **External-provider validation is mocked/local only.** No live Telegram, GitHub, or Supabase credentials or remote services were used. Slack and Discord are documented as planned, not supported.
6. **The entire repository was inventoried, but not every file was line-reviewed.** Review coverage and exclusions are explicit in [file-review-matrix.md](file-review-matrix.md).

## Verification record

- Pre-change baseline: `python -m pytest -q` — **806 passed, 2 skipped, 21 warnings, 13 subtests passed**; 808 tests collected. Warnings included asyncio coroutine warnings in task-executor rejection/cancellation paths; this pass adds regressions for those paths.
- Focused hardening checks after changes: **6 passed, 22 deselected** (terminal/permission regressions); hardening file **4 passed, 1 skipped**; task-executor regressions **3 passed, 14 deselected**.
- `python -m compileall -q sutra tests` succeeded at baseline; changed modules compiled after the fixes.
- `python -m pip check` reported no broken requirements.
- Baseline `ruff check sutra tests`: 1,889 findings; no Ruff configuration exists.
- Baseline `mypy sutra --ignore-missing-imports`: 72 errors in 20 files. The touched `generic_cli.py` errors were corrected; remaining errors are in pre-existing dependency/module typing paths.
- No live external API requests were made.
- Post-change full suite: the user interrupted the run at approximately 51%. Two failures were visible before cancellation, but the output did not include tracebacks. This is not a completed test result; no rerun occurred during the documentation-only task.

## Release gates

Before MVP release: close the MCP authorization/audit gap; strengthen terminal path argument controls or revise the confinement contract; run the full suite on Windows, Linux, and macOS (or document unsupported platforms); establish CI checks for tests, package build, lint, and types; perform the manual plan using disposable workspaces and real provider test accounts; update release notes and test-count claims.

## Repository hygiene

The working tree contained pre-existing modifications and untracked feature/test/config files before this pass. They were preserved and included in the baseline test run. No commit, push, deployment, or live external side effect was performed. `.env` is local, untracked, and was not opened or copied. See Git status before staging any changes.
