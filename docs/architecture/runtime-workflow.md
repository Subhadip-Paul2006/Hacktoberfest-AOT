# AgentBridge — Runtime Command & File Access Workflow

## Architecture Flow

The AgentBridge runtime workflow bridges user interactions from ChatOps interfaces (such as Telegram) through the remote Control Plane to local desktop workspaces.

```text
Operator (Telegram)
        │  /projects, Click folder, Click file, [Preview], [Jump], [Search], [Symbols]
        ▼
TelegramBotApp (sutra/apps/telegram)
        │  Idempotent update handling & active file context routing
        ▼
ControlPlane (sutra/control_plane.py)
        │  Operator session authorization, rate limiting, and command dispatch
        ▼  [WSS Frame: TLS Encrypted & Frame Nonce Protected]
LocalRuntime (sutra/runtime.py)
        │  _validated_project_path()
        │  Enforces project boundary & denies sensitive/hidden items
        ▼
FileAccessLayer (sutra/file_access.py)
        │
        ├── list_directory_paginated()   → Lazy paging, directories first
        ├── get_file_info()             → 64KB buffered line counting & metadata
        ├── preview_file_bounded()      → Default 35-line streaming window
        ├── search_file_text()          → Bounded regex/case-insensitive search
        ├── extract_file_symbols()      → AST parser for Python, regex for polyglot
        └── redact_secrets()            → Token, JWT, & credential redaction
        ▼
Telegram Response Formatting
        │  Markdown code blocks with line numbers, chunking & inline keyboards
        ▼
Operator View (Telegram)
```

---

## Command Dispatch Protocol

All explorer requests use standardized command strings over the bidirectional WSS tunnel:

| Command | Allowed Request Parameters | Response Payload |
|---|---|---|
| `project.list` | *(empty)* | List of discovered projects within authorized workspace roots. |
| `file.list` | `project_root`, `path`, `page`, `page_size`, `search`, `recursive` | Paginated directory entries: `items`, `page`, `total_pages`, `has_more`, `has_prev`. |
| `file.info` | `project_root`, `path`, `allow_binary`, `metadata` | File metadata: `name`, `relative_path`, `is_binary`, `language`, `total_lines`, `size_bytes`, `modified_at`. |
| `file.preview` | `project_root`, `path`, `start_line`, `end_line`, `max_lines`, `expected_version`, `line_preview` | Bounded slice: `content` (redacted), `start_line`, `end_line`, `total_lines`, `file_version`, `version_changed`. |
| `file.search` | `project_root`, `path`, `query`, `case_sensitive`, `max_results` | Match list: `matches` (line numbers + snippets), `total_matches`, `has_more`. |
| `file.symbols` | `project_root`, `path` | AST/regex symbols: `classes` (name + line), `functions` (name + line), `language`. |
| `file.download` | `project_root`, `path` | Export payload: `name`, `content_base64` (redacted for text, raw for allowed binaries). |

---

## Security Invariants

- **Sandbox Guarding**: Paths are resolved to their canonical realpaths and verified against authorized roots. Relative `..` escape attempts and symlink traversal outside authorized roots are forbidden.
- **Sensitive File Exclusion**: Sensitive files such as `.env`, `credentials.json`, `.pem`, `.key`, and private keys cannot be listed, previewed, searched, or downloaded.
- **Secret Redaction**: Every line emitted from previews and searches is processed by the secret redaction engine before transmission over the network.
