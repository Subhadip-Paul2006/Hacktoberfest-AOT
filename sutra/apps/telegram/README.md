# AgentBridge Telegram ChatOps Application

## Status
**Status:** Implemented for pairing, workspace browsing, and supported ChatOps actions; coding-agent CLI control is partial.

## Architecture
The Telegram application is an isolated ChatOps module providing an interactive menu and command control plane for AgentBridge.

### Structure
- **`bot.py`**: Defines `TelegramBot`, lifecycle methods, connection pooling, and request/update orchestration.
- **`handlers.py`**: Dispatches text commands (`/start`, `/help`, `/pair`, `/status`, etc.) and manages update deduplication.
- **`callbacks.py`**: Handles inline keyboard callback query routing, session verification, permission approval callbacks, and access root management.
- **`keyboards.py`**: Declares menu layouts, persistent navigation keyboards, and short-lived target button state allocations.
- **`formatting.py`**: Renders dynamic screens, status dashboards, file browser views, and security credential redaction filters.

The current Agents screen uses the Telegram bot's local descriptor registry; it is not connected to the paired runtime's agent sessions. Telegram does not yet receive CLI output or genuine provider approval events, and its permission callbacks do not inject answers into a CLI process. Antigravity's local adapter supports documented stream-json prompt continuation, but that capability is not exposed through this Telegram UI. See the [ChatOps guide](../../../docs/guides/telegram-chatops.md) for adapter-specific limits.

## Configuration
Isolated under the `SUTRA_TELEGRAM_*` namespace (with legacy `TELEGRAM_*` fallback):
- `SUTRA_TELEGRAM_BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN`)
- `SUTRA_TELEGRAM_ALLOWED_USER_IDS` (or `TELEGRAM_ALLOWED_USER_IDS`)
- `SUTRA_TELEGRAM_POLLING_MODE` (or `TELEGRAM_POLLING_MODE`)
- `SUTRA_TELEGRAM_WEBHOOK_URL` (or `TELEGRAM_WEBHOOK_URL`)
- `SUTRA_TELEGRAM_WEBHOOK_SECRET` (or `TELEGRAM_WEBHOOK_SECRET`)
