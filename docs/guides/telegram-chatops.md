# Telegram ChatOps Manual

AgentBridge provides allowlisted Telegram menus for runtime pairing, workspace browsing, and selected control-plane operations. The current Telegram Agents screen does not start or stream local coding-agent processes.

The Telegram ChatOps integration is implemented as an isolated modular application in [`sutra/apps/telegram/`](../../sutra/apps/telegram).

---

## 1. Security & Configuration

- **Configuration Namespace**: Configured via `SUTRA_TELEGRAM_*` (with legacy fallback to `TELEGRAM_*`):
  - `SUTRA_TELEGRAM_BOT_TOKEN`: Telegram bot API token from @BotFather.
  - `SUTRA_TELEGRAM_ALLOWED_USER_IDS`: Comma-separated allowlisted numeric user IDs.
  - `SUTRA_TELEGRAM_POLLING_MODE`: `long_polling` (default) or `webhook`.
  - `SUTRA_TELEGRAM_WEBHOOK_URL` & `SUTRA_TELEGRAM_WEBHOOK_SECRET`: Required for webhook mode.
- **Allowlist Enforced**: Only Telegram `user_id`s defined in `SUTRA_TELEGRAM_ALLOWED_USER_IDS` are handled. Unauthorized users are ignored.
- **Secret Redaction**: Existing command and connector paths use [`redact_secrets()`](../../sutra/redaction.py) where wired. Agent CLI output is not currently streamed to Telegram.
- **Message Chunking**: Implemented message formatting applies to supported Telegram responses; it is not an agent-process output channel.

---

## 2. Slash Commands

| Command | Description | Example |
| :--- | :--- | :--- |
| `/start` | Open the main interactive navigation menu | `/start` |
| `/menu` | Open the main navigation menu | `/menu` |
| `/help` | Show supported compatibility commands | `/help` |
| `/pair CODE` | Complete runtime pairing | `/pair 123456` |
| `/devices` | Show paired runtime devices | `/devices` |
| `/status [DEVICE_ID]` | Show runtime status | `/status` |
| `/workspaces` | Show registered workspace roots | `/workspaces` |
| `/projects` | Browse discovered projects | `/projects` |
| `/revoke DEVICE_ID` | Revoke a paired runtime | `/revoke AB-PC-ABC123` |

There is no `/run`, agent prompt, or agent stop command in the current Telegram handler.

---

## 3. Interactive Menu Navigation

The bot features interactive inline keyboard screens:

### 🏠 Home Menu
- **📱 Devices**: View connected local runtimes, latency, and status.
- **📂 Projects**: Browse approved workspace directories and Git repositories.
- **🤖 Agents**: Shows local placeholder descriptors. Start, stop, status, and prompt buttons are not connected to the paired runtime.
- **🔐 Permissions**: Shows permission requests created by existing Telegram operations and persistent policy information. It does not receive pending approvals from coding-agent CLI processes.
- **⚙️ Settings**: Manage approved access roots and session settings.

---

## 4. Permission Approval Workflows

The permission engine applies to operations that explicitly call it in the existing Telegram code paths. A provider CLI's own permission prompt is not correlated with this UI. Antigravity's headless mode follows its own configured permission policy and may soft-deny actions that require an interactive approval; AgentBridge does not turn those events into Telegram decisions.

```
⚠️ PERMISSION REQUIRED
Agent: Antigravity
Project: SampleApp
Action: bash_exec
Command: pytest tests/test_core.py
```

Where an existing AgentBridge operation presents approval controls, the selected decision is recorded by the central policy engine. This does not resume or inject input into a coding-agent CLI process.

## 5. Agent CLI integration status

- Antigravity's default adapter launch uses the documented stream-json stdin/stdout protocol, captures response deltas and final response metadata, and accepts follow-up prompts in the same process. Its headless permission decisions remain provider-policy decisions, not Telegram approvals.
- Cursor and Codex adapters currently launch headless one-shot commands. Their active sessions do not provide verified Telegram follow-up input or approval routing.
- Jules is represented by a local simulation adapter, not a live Jules service connection.
- Generic CLI uses ordinary subprocess pipes. Input semantics depend on the specific child command; it is not a PTY or universal approval interface.

The Telegram-to-CLI lifecycle, live output, completion summary, changed-file report, and next-prompt workflow remain incomplete.
