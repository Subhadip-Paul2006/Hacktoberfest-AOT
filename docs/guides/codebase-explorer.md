# Codebase Explorer & Mobile Code Review User Guide

The **AgentBridge Codebase Explorer** allows engineers to browse repositories, review code, inspect symbols, and search codebases directly from Telegram on mobile devices.

---

## 1. Getting Started

1. Open your paired AgentBridge Telegram bot.
2. Tap **📁 Projects** on the persistent keyboard (or send `/projects`).
3. Tap on your registered project (e.g. `📁 SampleApp`).
4. Tap **📂 Files** to open the root directory explorer.

---

## 2. Browsing Directories

Directories are listed lazily and sorted with folders first, followed by files:

```text
📂 SampleApp · src · Page 1/2 (28 items)

[📁 api]
[📁 core]
[📁 services]
[📄 app.py]
[📄 config.py]

[Next ➡️]
[⬅️ Back to project]  [🏠 Home]
```

- Tap any folder button (`📁 ...`) to navigate deeper into the tree.
- Tap `[⬅️ Back]` to return to the parent directory.
- Use `[⬅️ Previous]` and `[Next ➡️]` to page through large directories.

---

## 3. Viewing File Information

Selecting a file displays a compact metadata summary without loading the whole file into memory:

```text
📄 auth.py
Path: src/auth.py
Type: Python
Lines: 5,243 lines
Size: 118.4 KB
Modified: 2026-10-06 17:30 UTC

Read-only codebase explorer.

[👁 Preview]           [🧩 Symbols]
[🔍 Search]            [📥 Download]
[⬅️ Back to folder]    [🏠 Home]
```

For binary files (images, PDFs, archives):
- The bot displays MIME type, category, and size.
- Text preview is disabled.
- A secure `[📥 Download]` button is provided (if within the 10 MB download threshold).

---

## 4. Code Preview & Pagination

Tapping **[👁 Preview]** opens a bounded 35-line window of source code:

```text
📄 auth.py
Lines 1–35 of 5243

```python
import jwt
import hashlib
from typing import Optional

class AuthManager:
    def __init__(self, secret: str):
        self.secret = secret
...
```

[⬅️ Prev]             [Next ➡️]
[🔢 Jump]             [🔍 Search]           [🧩 Symbols]
[📥 Download]
[⬅️ Back to file]     [🏠 Home]
```

- Tap `[Next ➡️]` to advance by 35 lines.
- Tap `[⬅️ Prev]` to page backward.
- Exact line numbers are maintained throughout navigation.

---

## 5. Jump to Line

To jump to a specific location in a large file:
- Tap **[🔢 Jump]** to see quick presets: `[Top (L1)]`, `[Middle (L2621)]`, `[End (L5209)]`.
- Or simply reply in chat with:
  ```text
  /jump 1720
  ```
- AgentBridge streams lines 1,720 through 1,754 directly without loading preceding lines.

---

## 6. Text Search

To search for text within the active file:
- Tap **[🔍 Search]** or send:
  ```text
  /search authenticate
  ```
- AgentBridge scans the file line-by-line and returns matching line numbers with sanitized snippets:
  ```text
  🔍 Search: "authenticate" in auth.py

  2 match(es) found:

    42  def authenticate_user(user, password):
   180  def authenticate_token(token):

  [Line 42]  [Line 180]
  [⬅️ Back to preview]  [🏠 Home]
  ```
- Tap any `[Line X]` button to jump directly to the match line.

---

## 7. Symbol Discovery

Tap **[🧩 Symbols]** on any source file to inspect its structure:

```text
🧩 Symbols · auth.py (Python)

Classes:
• AuthManager (line 5)
• TokenVerifier (line 88)

Functions:
• authenticate_user() (line 42)
• generate_jwt() (line 120)
• refresh_session() (line 210)

[🏛️ AuthManager (L5)]  [🏛️ TokenVerifier (L88)]
[⚡ authenticate_user (L42)]  [⚡ generate_jwt (L120)]
[⬅️ Back to file]  [🏠 Home]
```

- Deterministic AST parsing identifies classes, methods, and functions with exact line numbers.
- Polyglot languages (JavaScript, TypeScript, Go, Rust) use deterministic regex parsers.
- Tapping a symbol button opens the preview directly at that symbol's line.

---

## 8. Stale View Protection

If a file is edited on disk while you are browsing:
- AgentBridge detects the change via file metadata tags (`st_mtime` + `st_size`).
- The bot displays:
  ```text
  ⚠️ File changed since this preview was opened.
  The file on disk has been modified. Reload to inspect the updated contents.

  [🔄 Reload Preview]
  [⬅️ Back to file]  [🏠 Home]
  ```
- This prevents mixing pages across incompatible versions of code.

---

---

## 9. Secure Downloads & Deterministic Multi-Part Export

AgentBridge handles file downloads based on transport boundaries:

### Single Direct Download (Files ≤ 10 MB)
- Tap **[📥 Download]** to receive a file attachment directly in Telegram.
- Embedded secrets and private API keys are automatically redacted in text files.
- The file is exported with `.redacted.txt` extension (or binary MIME format).

### Deterministic Multi-Part Export (Files > 10 MB up to 200 MB)
For files exceeding a single 10 MB transport chunk:
- Tapping **[📦 Export Parts]** prepares a deterministic multi-part export plan:
  ```text
  📦 Large File Export · report.py

  Size: 42.0 MB (44,040,192 bytes)
  Parts: 5 chunk(s)
  SHA-256: `a1b2c3d4e5f6...`

  Select a part below to export:
  [ 📦 Part 1/5 ]  [ 📦 Part 2/5 ]
  [ 📦 Part 3/5 ]  [ 📦 Part 4/5 ]
  [ 📦 Part 5/5 ]
  [ ⬅️ Back to file ]  [ 🏠 Home ]
  ```
- Every part has an exact byte range, size, and verifiable SHA-256 hash.
- Parts can be reconstructed on your machine (`cat report.py.part-* > report.py` or equivalent) matching the exact whole-file SHA-256 checksum.

---

## 10. Large Local File Preview (50 MB, 100 MB, 120 MB+)

Local files are **never** blocked from previewing simply because of total file size:
- A 120 MB text log or generated report can be previewed for any requested 35-line window.
- The preview engine uses sparse line checkpoints to jump directly to target lines without buffering the file into memory.
- If a line range exceeds the configured byte window limit (64 KB), the view is cleanly bounded with:
  `⚠️ Preview truncated by size limit.`
- Local preview is 100% independent of GitHub file limits or Git LFS.

