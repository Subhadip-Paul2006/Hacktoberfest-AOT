# SŪTRA — Manual Testing Checklist for Hackathon Demonstration

> **Document:** `MANUAL_TEST_CHECKLIST.md`  
> **Repository:** `sutra-local` (SAMANVAYA · SŪTRA)  
> **Audited Commit:** `c9917e5afdf55f19214645dfcb1441f21d5fad11`  
> **Status:** Active & Ready for Manual Execution  
> **Initial Execution State:** ALL CASES INITIALLY NOT RUN  

---

## Instructions for Testers

1. **Safety First:** Use only disposable, temporary directories for `SUTRA_HOME` and authorized workspaces. Never use production repositories or live production credentials.
2. **Secrets Handling:** Never paste real tokens or API keys into command lines, console logs, or shared test reports.
3. **Recording Evidence:** Capture console outputs, exit codes, SQLite record counts, and sanitized log entries in the `Evidence` field for each test case.

---

## 1. Installation, Setup, and Environment Diagnostics

### TC-CLI-01: CLI Help and Subcommand Discovery
- **Test ID:** TC-CLI-01
- **Feature/Module:** CLI Dispatcher (`sutra/cli.py`)
- **Objective:** Verify all CLI commands and options are discoverable and render cleanly.
- **Preconditions:** Active Python 3.11+ environment with `sutra-local` installed in editable mode (`pip install -e .`).
- **Test Data / Environment:** Terminal console (PowerShell / Bash).
- **Steps:**
  1. Run `python -m sutra --help`.
  2. Run `python -m sutra setup --help`.
  3. Run `python -m sutra local --help`.
  4. Run `python -m sutra control-plane --help`.
  5. Run `python -m sutra doctor --help`.
  6. Run `python -m sutra connectors --help`.
- **Expected Result:** Commands display descriptions, arguments, and exit code 0 without traceback errors.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Console output text.
- **Related Finding / Test:** `tests/test_cli_ui.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** None.

---

### TC-CLI-02: Setup Command & Certificate Generation
- **Test ID:** TC-CLI-02
- **Feature/Module:** Environment Setup (`sutra/dev_certificate.py`)
- **Objective:** Verify idempotent initialization of SŪTRA home directory and TLS development certificates.
- **Preconditions:** Temporary test directory chosen (e.g., `temp_sutra_home`).
- **Test Data / Environment:** `$env:SUTRA_HOME="<temp_path>"`
- **Steps:**
  1. Set environment variable `SUTRA_HOME` to a fresh temporary path.
  2. Run `python -m sutra setup --output-dir temp_certs`.
  3. Verify directory contents of `temp_certs` and `SUTRA_HOME`.
  4. Re-run `python -m sutra setup --output-dir temp_certs` a second time.
- **Expected Result:** First run creates valid certificate (`cert.pem`) and private key (`key.pem`) without error; second run executes idempotently without crashing.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** File paths created and console output.
- **Related Finding / Test:** `tests/test_idempotency.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Delete `temp_certs` and `temp_sutra_home`.

---

### TC-CLI-03: Doctor Diagnostic Pre-flight Check
- **Test ID:** TC-CLI-03
- **Feature/Module:** Diagnostics (`sutra/doctor.py`)
- **Objective:** Validate that `sutra doctor` detects platform capabilities and reports missing optional dependencies without leaking secrets.
- **Preconditions:** Standard terminal session.
- **Test Data / Environment:** No extra environment variables set.
- **Steps:**
  1. Run `python -m sutra doctor`.
- **Expected Result:** Clean status table showing Python version (>=3.11), platform, certificate status, and optional tool presence (Git, Docker). Exit code 0.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Doctor output summary table.
- **Related Finding / Test:** `tests/test_cli_ui.py`
- **Priority:** P2 (Important)
- **Cleanup Requirements:** None.

---

## 2. Workspace Management and Security Guardrails

### TC-SEC-01: Workspace Registration and Path Confinement
- **Test ID:** TC-SEC-01
- **Feature/Module:** Workspace Confinement (`sutra/security.py`, `sutra/state.py`)
- **Objective:** Verify workspace addition, canonicalization, and isolation against unapproved paths.
- **Preconditions:** Fresh test home directory (`SUTRA_HOME`).
- **Test Data / Environment:** Two temporary directories: `workspace_alpha` and `outside_alpha`.
- **Steps:**
  1. Create `workspace_alpha` and `outside_alpha`.
  2. Run `python -m sutra workspace add workspace_alpha`.
  3. Run `python -m sutra workspace list`.
  4. Attempt to add invalid/nonexistent path: `python -m sutra workspace add ./nonexistent_path_xyz`.
- **Expected Result:** `workspace_alpha` is registered in `state.sqlite3`; list outputs its canonical path; adding a nonexistent path safely fails with error code.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** SQLite table rows from `state.sqlite3` and CLI output.
- **Related Finding / Test:** `tests/test_codebase_explorer.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Run `python -m sutra workspace remove workspace_alpha`.

---

### TC-SEC-02: Path Traversal Rejection
- **Test ID:** TC-SEC-02
- **Feature/Module:** Sandboxed File Access (`sutra/file_access.py`)
- **Objective:** Verify path traversal attempts using `..`, URL-encoding, or escaped slashes are strictly blocked.
- **Preconditions:** `workspace_alpha` registered.
- **Test Data / Environment:** Subprocess or test script calling `file_access.read_file()`.
- **Steps:**
  1. Attempt file read on `workspace_alpha/../outside_alpha/secret.txt`.
  2. Attempt file read on `workspace_alpha/%2e%2e/outside_alpha/secret.txt`.
  3. Attempt file read on `C:\Windows\System32\drivers\etc\hosts` (or `/etc/passwd`).
- **Expected Result:** Every operation raises `PermissionError` or returns `False`; zero file bytes leak from outside the workspace.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Exception message and ActivityStore audit event.
- **Related Finding / Test:** `tests/test_system_hardening.py`
- **Priority:** P0 (Critical Security)
- **Cleanup Requirements:** Delete test files.

---

## 3. Terminal Execution and Shell Injection Prevention

### TC-TRM-01: Valid Command Execution & Output Bounding
- **Test ID:** TC-TRM-01
- **Feature/Module:** Terminal Connector (`sutra/connectors/terminal/connector.py`)
- **Objective:** Verify permitted commands run without shell interpretation and outputs remain strictly bounded.
- **Preconditions:** Disposable workspace initialized with git.
- **Test Data / Environment:** Discrete command array `["git", "status"]`.
- **Steps:**
  1. Execute `git status` inside `workspace_alpha` via terminal connector.
  2. Execute command generating 100,000 lines of output (e.g. `python -c "print('X'*1000000)"`).
- **Expected Result:** Command 1 completes with exit code 0; Command 2 returns truncated output respecting max byte cap without consuming excessive RAM.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Captured stdout byte length and truncation flag.
- **Related Finding / Test:** `tests/test_terminal_connector.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** None.

---

### TC-TRM-02: Shell Metacharacter Injection Rejection
- **Test ID:** TC-TRM-02
- **Feature/Module:** Terminal Connector Security (`sutra/connectors/terminal/security.py`)
- **Objective:** Verify shell metacharacters (`|`, `;`, `&&`, `` ` ``, `$()`) are rejected.
- **Preconditions:** Terminal connector initialized.
- **Test Data / Environment:** Malicious command strings.
- **Steps:**
  1. Attempt execution with argument `["git", "status; whoami"]`.
  2. Attempt execution with pipe `["git", "log | dir"]`.
  3. Attempt command substitution `["git", "commit", "-m", "$(calc.exe)"]`.
- **Expected Result:** Security check blocks execution before subprocess dispatch; raises `SecurityError` or returns rejected status.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Security rejection log and audit record.
- **Related Finding / Test:** `tests/test_terminal_connector.py`
- **Priority:** P0 (Critical Security)
- **Cleanup Requirements:** None.

---

## 4. AI Provider and Model Override Configuration

### TC-LLM-01: Default AI Provider Resolution
- **Test ID:** TC-LLM-01
- **Feature/Module:** AI Providers (`sutra/ai_providers.py`)
- **Objective:** Verify AIConfig correctly resolves global provider from environment variables.
- **Preconditions:** Clean environment without API keys.
- **Test Data / Environment:** Set `SUTRA_AI_PROVIDER=ollama`, `OLLAMA_MODEL=qwen2.5-coder:7b`.
- **Steps:**
  1. Instantiate `AIConfig.from_env()`.
  2. Inspect `mode`, `provider`, and `model`.
  3. Call `redacted_summary()`.
- **Expected Result:** Provider resolves to `AIProvider.OLLAMA`, mode to `AIMode.LOCAL`, and model to `qwen2.5-coder:7b`.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Summary dictionary printout.
- **Related Finding / Test:** `tests/test_ai_providers.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Remove test environment variables.

---

### TC-LLM-02: Agent-Specific Model Override (Workaround Verification)
- **Test ID:** TC-LLM-02
- **Feature/Module:** AI Providers (`sutra/ai_providers.py`)
- **Objective:** Verify agent model overrides function when keys are lowercased (verifying DEF-002 workaround).
- **Preconditions:** Python session.
- **Test Data / Environment:** `agent_overrides={"research": AgentModelConfig(provider="anthropic", model="claude-3-5-sonnet-20241022")}`.
- **Steps:**
  1. Configure `AIConfig` with global `openai/gpt-4o` and lowercase override `"research"`.
  2. Query `effective_for_agent("research")`.
  3. Query `effective_for_agent("Research")`.
- **Expected Result:** Step 2 returns `('anthropic', 'claude-3-5-sonnet-20241022')`; Step 3 returns `('anthropic', 'claude-3-5-sonnet-20241022')` if casefolded or fallback if unnormalized.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Returned tuple for each query.
- **Related Finding / Test:** DEF-002 / `tests/test_ai_providers.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** None.

---

## 5. Control Plane and Device Pairing Lifecycle

### TC-CTL-01: Control Plane Startup and Health Check
- **Test ID:** TC-CTL-01
- **Feature/Module:** Control Plane (`sutra/control_plane.py`)
- **Objective:** Start the TLS control plane locally and query health endpoint over HTTPS.
- **Preconditions:** Development certificate and key generated via `sutra setup`.
- **Test Data / Environment:** Operator token set (`SUTRA_CONTROL_PLANE_TOKEN="test_token_with_at_least_32_characters_12345"`).
- **Steps:**
  1. Launch `python -m sutra control-plane --host 127.0.0.1 --port 8766 --tls-certfile .dev-certs/cert.pem --tls-keyfile .dev-certs/key.pem` in background.
  2. In another terminal, query `curl -k https://127.0.0.1:8766/health`.
- **Expected Result:** Control plane starts cleanly; returns HTTP 200 with JSON payload `{"status": "ok"}`.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** HTTP response code and body.
- **Related Finding / Test:** `tests/test_control_plane.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Terminate control plane process.

---

### TC-CTL-02: Device Pairing and Bearer Token Generation
- **Test ID:** TC-CTL-02
- **Feature/Module:** Pairing Protocol (`sutra/pairing.py`, `sutra/control_plane.py`)
- **Objective:** Execute pairing request and verify token registration in SQLite.
- **Preconditions:** Control plane running on port 8766.
- **Test Data / Environment:** Loopback REST pairing request.
- **Steps:**
  1. Initiate pairing handshake for device `AB-PC-DEMO01`.
  2. Submit high-entropy 6-character pairing code.
  3. Verify approval in control plane SQLite database (`control-plane.sqlite3`).
- **Expected Result:** Device receives persistent bearer token; pairing code expires; unapproved device cannot connect to WSS.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** SQLite `paired_devices` row and HTTP response.
- **Related Finding / Test:** `tests/test_control_plane.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Clean temporary database.

---

## 6. Telegram ChatOps Integration (Staging Verification)

### TC-TEL-01: Telegram Bot Startup and Command Whitelist
- **Test ID:** TC-TEL-01
- **Feature/Module:** Telegram Bot (`sutra/apps/telegram/bot.py`)
- **Objective:** Verify bot initializes in long-polling mode and rejects unauthorized user IDs.
- **Preconditions:** Dedicated test Telegram bot token and test user ID available.
- **Test Data / Environment:** `SUTRA_TELEGRAM_BOT_TOKEN="<disposable_token>"`, `SUTRA_TELEGRAM_ALLOWED_USER_IDS="<tester_user_id>"`.
- **Steps:**
  1. Configure environment variables for test bot.
  2. Start runtime or control plane with Telegram enabled.
  3. Send `/start` from authorized user ID.
  4. Send `/start` from an unauthorized secondary account or verify log rejection.
- **Expected Result:** Authorized account receives greeting and command menu; unauthorized account receives no response or generic access denied.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Telegram chat screenshot and console log.
- **Related Finding / Test:** `tests/apps/telegram/test_bot.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Revoke bot token or stop bot process.

---

### TC-TEL-02: Interactive Inline Approval Keyboard
- **Test ID:** TC-TEL-02
- **Feature/Module:** Telegram Approvals (`sutra/apps/telegram/callbacks.py`, `sutra/policy_engine.py`)
- **Objective:** Verify high-risk operations trigger inline keyboard approval and apply decision to PolicyEngine.
- **Preconditions:** Working Telegram bot connected to runtime.
- **Test Data / Environment:** Action requiring confirmation (e.g. `git push` or file modification).
- **Steps:**
  1. Trigger an operation classified as `HIGH` risk.
  2. Observe approval message with `[Approve]` and `[Deny]` buttons in Telegram.
  3. Click `[Approve]`.
  4. Verify ActivityStore logs approval and the action executes.
- **Expected Result:** Telegram message updates to show approved status; operation executes; audit entry records approval.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** ActivityStore `activity_log` record with approved status.
- **Related Finding / Test:** `tests/apps/telegram/test_bot.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Reset policy store.

---

## 7. Model Context Protocol (MCP) and Tool Discovery

### TC-MCP-01: MCP Stdio Connector Handshake and Tool Listing
- **Test ID:** TC-MCP-01
- **Feature/Module:** MCP Client (`sutra/connectors/mcp/client.py`, `stdio.py`)
- **Objective:** Verify stdio MCP transport connects to local test server and lists tools.
- **Preconditions:** `python tests/mocks/mcp_stdio_test_server.py` available.
- **Test Data / Environment:** Stdio connector configuration pointing to mock server script.
- **Steps:**
  1. Register connector: `python -m sutra connectors add mock-stdio --endpoint "python tests/mocks/mcp_stdio_test_server.py"`.
  2. Test connector: `python -m sutra connectors test mock-stdio`.
  3. List tools: `python -m sutra connectors tools mock-stdio`.
- **Expected Result:** Step 2 reports handshake OK; Step 3 lists available mock tools (`echo`, `calculate`) with parameter schemas.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Terminal output for `test` and `tools` commands.
- **Related Finding / Test:** `tests/test_mcp_stdio.py`
- **Priority:** P1 (Demo Blocker)
- **Cleanup Requirements:** Run `python -m sutra connectors remove mock-stdio`.

---

## 8. Graceful Shutdown and Resource Cleanup

### TC-SHT-01: SIGINT / Ctrl+C Graceful Termination
- **Test ID:** TC-SHT-01
- **Feature/Module:** Runtime Lifecycle (`sutra/runtime.py`)
- **Objective:** Verify runtime cleans up active child processes and closes SQLite connections on interrupt.
- **Preconditions:** Runtime running in foreground terminal.
- **Test Data / Environment:** Terminal console.
- **Steps:**
  1. Start `python -m sutra local`.
  2. Let runtime initialize (status panel displays).
  3. Press `Ctrl+C`.
- **Expected Result:** Runtime intercepts `KeyboardInterrupt`, cleanly shuts down tasks, closes database connections, and exits with code 0 without unhandled asyncio tracebacks.
- **Actual Result:** NOT RUN
- **Status:** NOT RUN
- **Evidence to Capture:** Shutdown console logs.
- **Related Finding / Test:** `tests/test_local_runtime.py`
- **Priority:** P2 (Important)
- **Cleanup Requirements:** Verify no dangling python processes.
