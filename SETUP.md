# Comprehensive Setup & Installation Guide (SETUP) — SAMANVAYA · SŪTRA

> **File:** `SETUP.md`  
> **Purpose:** Authoritative, step-by-step setup guide for cloning, configuring, authenticating, and running the SŪTRA runtime on developer workstations.  
> **Audience:** Developers, evaluators, and system operators.  
> **Status:** Maintained — Verified against the `sutra` CLI and package configuration.  

---

## Table of Contents

- [1. Purpose & Supported Environments](#1-purpose--supported-environments)
- [2. Prerequisites & Verification](#2-prerequisites--verification)
- [3. Obtain the Project](#3-obtain-the-project)
- [4. Create an Isolated Virtual Environment](#4-create-an-isolated-virtual-environment)
- [5. Install Dependencies](#5-install-dependencies)
- [6. Configure Environment Variables](#6-configure-environment-variables)
- [7. Configure the Telegram Bot (Optional)](#7-configure-the-telegram-bot-optional)
- [8. Configure Local AI Models & Ollama](#8-configure-local-ai-models--ollama)
- [9. Configure Connectors (Terminal, GitHub, Supabase)](#9-configure-connectors-terminal-github-supabase)
- [10. Generate Development Certificates & Initialize State](#10-generate-development-certificates--initialize-state)
- [11. Register Approved Workspaces](#11-register-approved-workspaces)
- [12. Start the Control Plane & Local Runtime](#12-start-the-control-plane--local-runtime)
- [13. First-Run Smoke Test](#13-first-run-smoke-test)
- [14. Troubleshooting Guide](#14-troubleshooting-guide)
- [15. Shutdown, Reset & Cleanup](#15-shutdown-reset--cleanup)
- [16. Documentation Links & Next Steps](#16-documentation-links--next-steps)

---

## 1. Purpose & Supported Environments

This guide provides end-to-end instructions for installing and running **SAMANVAYA · SŪTRA** on a local developer workstation.

### Deployment Architecture
- **Native Local Execution:** SŪTRA executes natively as a standard Python process. It accesses your approved local workspaces and development tools directly. **Docker is not required.**
- **Supported Operating Systems:**
  - **Windows:** Windows 10 / 11 (tested on Windows PowerShell 5.1 and PowerShell 7)
  - **macOS:** macOS 12 (Monterey) or newer (Apple Silicon and Intel)
  - **Linux:** Ubuntu 22.04 LTS+, Debian 12+, Fedora 38+, Arch Linux

---

## 2. Prerequisites & Verification

Before installing SŪTRA, verify that the following tools are installed on your workstation:

### A. Python 3.11 or Newer
SŪTRA requires Python 3.11+ for modern async language primitives and strict path resolution.
```bash
python --version
# Expected: Python 3.11.x, 3.12.x, or 3.13.x
```
*If not installed, download from [python.org](https://www.python.org/downloads/) or install via your OS package manager (`winget install Python.Python.3.11`, `brew install python@3.11`, or `apt install python3.11`).*

### B. Git Version Control
Required for Git-backed workspace features and repository operations.
```bash
git --version
# Expected: git version 2.30.0 or newer
```

### C. OS Keyring Backend (Recommended)
SŪTRA uses the Python `keyring` library to store credentials in native OS secret stores:
- **Windows:** Windows Credential Manager (built-in)
- **macOS:** Apple Keychain (built-in)
- **Linux:** Secret Service API (requires `gnome-keyring` or `keepassxc`)

---

## 3. Obtain the Project

Clone the repository to your local drive and change into the project root:

```bash
# Clone the repository
git clone https://github.com/Subhadip-Paul2006/NotForYou.git

# Enter the project root
cd NotForYou
```

---

## 4. Create an Isolated Virtual Environment

Always run SŪTRA within a dedicated Python virtual environment to avoid package conflicts.

### Windows (PowerShell)
```powershell
# Create virtual environment
python -m venv .venv

# Activate virtual environment
.venv\Scripts\Activate.ps1
```
*(If PowerShell execution policy prevents activation, run: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process`)*

### macOS / Linux (bash/zsh)
```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate
```

Verify that the active interpreter points to the virtual environment:
```bash
# Windows
Get-Command python | Select-Object -ExpandProperty Source

# Linux / macOS
which python
```

---

## 5. Install Dependencies

Install SŪTRA in editable mode with development dependencies:

```bash
# Upgrade pip, setuptools, and wheel
python -m pip install --upgrade pip setuptools wheel

# Install SŪTRA in editable mode with development packages
python -m pip install -e ".[dev]"
```

Verify the installation:
```bash
python -m sutra --help
```
*You should see the SŪTRA CLI help menu listing commands: `local`, `control-plane`, `setup`, `status`, `workspace`, `projects`, `doctor`, `connectors`.*

---

## 6. Configure Environment Variables

SŪTRA configuration is loaded from OS environment variables and an optional `.env` file in the project root.

Create your `.env` file from the example template:
```bash
# Windows
Copy-Item .env.example .env

# macOS / Linux
cp .env.example .env
```

### Complete Environment Variable Reference

| Variable Name | Required | Default Value | Subsystem | Secret? | Description |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `SUTRA_HOME` | No | `~/.sutra` | Runtime / State | No | Directory where SQLite databases and runtime state reside. |
| `SUTRA_CONTROL_PLANE_URL` | No | `""` | Runtime Client | No | WebSocket URL for the control plane (e.g., `wss://127.0.0.1:8766`). |
| `SUTRA_CONTROL_PLANE_TOKEN` | Yes (for CP) | `""` | Control Plane | **YES** | Random operator token ($\ge 32$ chars) protecting the control plane. |
| `SUTRA_TLS_CERTFILE` | Yes (for CP) | `""` | Control Plane | No | Filesystem path to the control plane TLS certificate. |
| `SUTRA_TLS_KEYFILE` | Yes (for CP) | `""` | Control Plane | **YES** | Filesystem path to the control plane TLS private key. |
| `SUTRA_TELEGRAM_BOT_TOKEN` | Optional | `""` | Telegram Bot | **YES** | Bot API token obtained from `@BotFather`. |
| `SUTRA_TELEGRAM_ALLOWED_USER_IDS` | Optional | `""` | Telegram Bot | No | Comma-separated numeric Telegram User IDs authorized to interact. |
| `SUTRA_TELEGRAM_POLLING_MODE` | No | `long_polling` | Telegram Bot | No | Delivery mode: `long_polling` (default) or `webhook`. |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Optional | `""` | GitHub MCP / REST | **YES** | GitHub personal access token with `repo` and `workflow` scopes. |
| `SUPABASE_ACCESS_TOKEN` | Optional | `""` | Supabase MCP | **YES** | Supabase personal access token for schema inspection. |
| `LOG_LEVEL` | No | `INFO` | Logging | No | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

> [!CAUTION]
> **Never commit `.env` to Git.** SŪTRA's pre-staging sanitizer will block `.env` files, but ensure your `.gitignore` contains `.env` and `.env.*`.

---

## 7. Configure the Telegram Bot (Optional)

If you plan to supervise SŪTRA remotely from your mobile device, set up a Telegram bot:

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the prompts to choose a bot name and username.
3. Copy the HTTP API token provided by BotFather (e.g., `7123456789:ABCdefGHIjklMNOpqrsTUVwxyz`).
4. Find your numeric Telegram User ID using `@userinfobot` (e.g., `123456789`).
5. Add these values to your `.env` file:
   ```env
   SUTRA_TELEGRAM_BOT_TOKEN=7123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   SUTRA_TELEGRAM_ALLOWED_USER_IDS=123456789
   SUTRA_TELEGRAM_POLLING_MODE=long_polling
   ```

---

## 8. Configure Local AI Models & Ollama

SŪTRA can coordinate local open-weight models without incurring cloud API costs:

### 1. Install & Verify Ollama
1. Download Ollama from [ollama.ai](https://ollama.ai/download).
2. Start the Ollama server:
   ```bash
   ollama serve
   ```
3. In a separate terminal, verify that the service is running:
   ```bash
   curl http://127.0.0.1:11434/api/tags
   ```

### 2. Pull a Supported Model
Pull Google Gemma 2 or Meta Llama 3:
```bash
ollama pull gemma2:9b
# or
ollama pull llama3:8b
```

### 3. Configure Provider
Set your provider configuration in `.env` if using local models:
```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=gemma2:9b
```

---

## 9. Configure Connectors (Terminal, GitHub, Supabase)

SŪTRA includes a dedicated CLI for managing connectors (`python -m sutra connectors`):

### Terminal Connector
The guarded terminal connector executes local shell commands safely within an authorized workspace root:
```bash
# Register the terminal connector with an authorized workspace
python -m sutra connectors add terminal --provider terminal --workspace d:\practice-file

# Test terminal connector connectivity
python -m sutra connectors test terminal
```

### GitHub Remote MCP Connector
Connects to GitHub's streamable MCP endpoint:
```bash
# Register the GitHub MCP connector
python -m sutra connectors add github-mcp --provider github

# Store token securely in OS Keyring (prompts securely for token)
python -m sutra connectors auth github-mcp

# Test connectivity and tool discovery
python -m sutra connectors test github-mcp

# List discovered MCP tools
python -m sutra connectors tools github-mcp
```

### Supabase Remote MCP Connector
Connects to Supabase for schema and database inspection:
```bash
# Register the Supabase MCP connector
python -m sutra connectors add supabase-mcp --provider supabase

# Store token securely in OS Keyring
python -m sutra connectors auth supabase-mcp

# Test connectivity
python -m sutra connectors test supabase-mcp
```

---

## 10. Generate Development Certificates & Initialize State

Before launching the control plane, generate a development TLS certificate and initialize the `~/.sutra` home directory:

```bash
# Run the automated setup command
python -m sutra setup --output-dir .dev-certs
```

This creates:
- `~/.sutra/` (runtime state directory)
- `.dev-certs/localhost.crt` (development TLS certificate)
- `.dev-certs/localhost.key` (TLS private key)

Export the certificate paths to your environment or configure them in `.env`:
```env
SUTRA_TLS_CERTFILE=.dev-certs/localhost.crt
SUTRA_TLS_KEYFILE=.dev-certs/localhost.key
SUTRA_CONTROL_PLANE_TOKEN=3a9b1c7d2e4f8a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b
```

---

## 11. Register Approved Workspaces

To protect your personal files and unrelated codebases, SŪTRA requires explicitly approving workspace directories before any file or Git operations can be performed:

```bash
# Register an approved repository workspace
python -m sutra workspace add d:\practice-file

# List all registered workspaces
python -m sutra workspace list

# Discover projects within registered workspaces
python -m sutra projects
```

---

## 12. Start the Control Plane & Local Runtime

SŪTRA operates with two lightweight processes:

### Terminal 1: Start the Local TLS Control Plane
```bash
# Activate your virtual environment first
python -m sutra control-plane
```
*Expected Output:*
```text
INFO:     Started server process [PID]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on https://127.0.0.1:8766 (Press CTRL+C to quit)
```

### Terminal 2: Start the SŪTRA Local Runtime
In a second terminal, configure the control plane URL and start the runtime:
```powershell
$env:SUTRA_CONTROL_PLANE_URL = "wss://127.0.0.1:8766"
$env:SUTRA_TLS_CA_FILE = ".dev-certs/localhost.crt"
python -m sutra local
```
*The runtime will initialize, render the modern Rich startup dashboard, establish the outbound WebSocket session, and await operator commands.*

---

## 13. First-Run Smoke Test

Perform this safe sequence to confirm complete operational readiness:

### Step 1: Check Subsystem Diagnostics
```bash
python -m sutra doctor
```
*Verifies Python version, database access, TLS certificates, and registered workspaces.*

### Step 2: Query Subsystem Status
```bash
python -m sutra status
```

### Step 3: Test Telegram Bot Responsiveness
From Telegram, send `/start` to your bot.
- If authorized, the bot replies with the SŪTRA main menu.
- If unauthorized, the bot drops the message without echoing private information.

### Step 4: Verify Workspace Confinement
Attempting to access a directory outside registered workspaces (e.g., `C:\Windows`) will immediately raise a `PermissionError` and be blocked.

---

## 14. Troubleshooting Guide

| Symptom | Likely Cause | Diagnosis & Safe Fix |
| :--- | :--- | :--- |
| `SystemExit: Set SUTRA_CONTROL_PLANE_TOKEN to at least 32 characters.` | Missing or short token. | Generate a random 64-char token in `.env`: `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `SystemExit: TLS certificate and key are required.` | Certificate files not found. | Run `python -m sutra setup` to generate development certificates. |
| `I/O operation on closed pipe` or transport warning on Windows. | Normal async cleanup noise on Windows Proactor event loop. | Harmless warning during subprocess termination. |
| Telegram bot does not respond. | User ID not in allowlist or invalid bot token. | Verify numeric ID with `@userinfobot` and ensure `TELEGRAM_ALLOWED_USER_IDS` matches. |
| Connector authentication fails. | OS Keyring backend missing. | Run `python -c "import keyring; print(keyring.get_keyring())"`. On headless Linux, install `keyrings.alt`. |
| `PermissionError: Path escapes workspace root.` | Path traversal or external symlink. | SŪTRA security defense working as intended. Register the parent root with `sutra workspace add`. |

---

## 15. Shutdown, Reset & Cleanup

### Stopping the Services
Press `Ctrl+C` in each terminal. SŪTRA cleanly terminates child processes, closes WebSocket sessions, and flushes SQLite databases.

### Revoking Device Authorization
To immediately revoke all paired devices and invalidate session tokens:
```bash
# Purge runtime credentials
Remove-Item -Recurse -Force ~/.sutra/state.sqlite3
```

---

## 16. Documentation Links & Next Steps

- [USAGE.md](USAGE.md) — Comprehensive command reference for the CLI and Telegram bot.
- [README.md](README.md) — Architectural overview and quick reference.
- [PRD.md](PRD.md) — Product requirements and acceptance criteria.
- [TRD.md](TRD.md) — Detailed technical specifications and protocols.
- [SECURITY.md](SECURITY.md) — Security model and threat mitigation matrix.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — Contributor community standards and behavioral guidelines.
- [docs/architecture/ai-architecture.md](docs/architecture/ai-architecture.md) — Deep architecture blueprint.
