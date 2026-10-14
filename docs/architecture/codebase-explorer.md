# AgentBridge — Sandboxed Codebase Explorer Architecture

## Overview

The **Sandboxed Codebase Explorer & Mobile Code Review** subsystem (Phase 5) enables remote engineers to inspect, navigate, and review project source trees securely through Telegram ChatOps. It enforces an airtight workspace boundary, zero arbitrary filesystem access, deterministic symbol discovery, and bounded memory streaming for multi-thousand line files.

```
Telegram Client (Mobile ChatOps)
             │
             ▼
    TelegramBotApp (sutra/apps/telegram)
             │  • Idempotency & Rate Limiting
             │  • Active File Context Tracking
             │  • Screen Formatting & Pagination
             ▼
      ControlPlane (sutra/control_plane.py)
             │  • TLS Session Authentication
             │  • Command Schema Bounds Enforcement
             ▼  [WSS Frame: file.list | file.info | file.preview | file.search | file.symbols]
      LocalRuntime (sutra/runtime.py)
             │  • Discovered Project Scope Validation
             │  • Sandbox Boundary Verification (sutra/security.py)
             ▼
     File Access Engine (sutra/file_access.py)
      ├── list_directory_paginated()       [Lazy directory paging]
      ├── get_file_info()                  [Fast line count & metadata]
      ├── preview_file_bounded()           [Streaming 35-line window]
      ├── search_file_text()               [Bounded line scanner]
      ├── extract_file_symbols()           [AST / deterministic parser]
      └── redact_secrets()                 [Secret & token sanitization]
```

---

## Core Guarantees & Invariants

1. **Workspace Sandboxing Invariant**:
   $$\text{canonical}(\text{resolved\_target}) \subseteq \text{canonical}(\text{authorized\_workspace\_root})$$
   Any attempt to traverse via `..`, escape via symlinks, or supply paths outside discovered projects immediately raises a `PermissionError` or `ValueError`.

2. **Bounded Memory & Output Consumption**:
   - Directory listings are lazy and paginated (default 20–30 items, maximum 100).
   - Source code preview streams a bounded window (default 35 lines, maximum 200) strictly bounded by byte limits (`MAX_PREVIEW_BYTES = 64 KB`).
   - Large local files (50 MB, 100 MB, 120 MB+) are fully previewable via `SparseLineIndex` checkpoints (indexed every 1,000 lines) without buffering entire files into memory.
   - Message payloads are constrained to Telegram limits (4,096 chars) with automatic size truncation notices (`⚠️ Preview truncated by size limit.`).

3. **Deterministic Multi-Part File Export**:
   - Small files (`<= 10 MB`) download directly as a single transport unit.
   - Large files (`> 10 MB` up to `200 MB`) are split into deterministic parts (`.part-0001`, `.part-0002`, ...) with an `ExportManifest`.
   - Text files align to newline boundaries without bisecting multi-byte UTF-8 sequences.
   - Every part records SHA-256 and the manifest records whole-file SHA-256 for exact reconstruction.

4. **Secret Redaction by Default**:
   All text exposed via directory listings, file previews, searches, or downloads passes through the centralized `redact_secrets()` filter, masking API keys, JWTs, and private tokens before transmission.

5. **Stale View & Concurrent Modification Protection**:
   Previews generate a version tag `file_version = f"{int(stat.st_mtime)}-{stat.st_size}"`. When navigating pages, if `expected_version` differs from the on-disk state, the user is notified with an explicit warning banner (`⚠️ File changed since this preview was opened.`) with a `[🔄 Reload Preview]` button.

6. **Deterministic Symbol Extraction & Operation Budget**:
   Symbol discovery and text search operate within an operation scan budget (`MAX_SINGLE_SCAN_BYTES = 50 MB`). Operations that reach this budget report partial results cleanly without failing or claiming the file is too large.

7. **GitHub Limit Independence**:
   GitHub repository file size constraints (100 MB limit) do **not** govern local preview. Large local files, build artifacts, and Git LFS objects can be previewed locally without requiring GitHub interaction.

---

## Component Responsibilities

| Component | Module | Responsibility |
|---|---|---|
| **Telegram Formatting** | `sutra/apps/telegram/formatting.py` | Generates compact inline keyboards for folder navigation, file metadata cards, bounded previews, jump menus, search results, and multi-part export selectors. |
| **Callback Router** | `sutra/apps/telegram/callbacks.py` | Routes inline button presses safely, protects against expired or foreign-session buttons, and clears active file context upon project switching. |
| **Command Router** | `sutra/apps/telegram/handlers.py` | Interprets `/jump <line>` and `/search <query>` commands in the context of the user's active file. |
| **Control Plane** | `sutra/control_plane.py` | Dispatches authenticated commands over WSS and validates payload schemas. |
| **Local Runtime** | `sutra/runtime.py` | Dispatches `file.list`, `file.info`, `file.preview`, `file.download`, `file.search`, `file.symbols` through sandbox validators and coordinates multi-part export plans. |
| **File Access Engine** | `sutra/file_access.py` | Implements bounded line streaming, sparse checkpoint indexing, binary detection, deterministic multi-part export planner, and integrity verification. |
| **Security Layer** | `sutra/security.py` & `redaction.py` | Filters hidden directories, excludes `.env` / credentials / private keys, and sanitizes secrets. |
