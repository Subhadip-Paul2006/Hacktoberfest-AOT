"""Centralized secret detection, redaction, and sensitive file filtering."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Sensitive File Rules
# ---------------------------------------------------------------------------

BLOCKED_FILE_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pkcs12", ".pfx", ".sqlite", ".db"
})

BLOCKED_FILE_PARTS = (
    ".env", "secret", "credential", "password", "token",
    "private_key", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"
)

HIDDEN_DIRECTORY_NAMES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"
})

# ---------------------------------------------------------------------------
# Secret Pattern Regexes
# ---------------------------------------------------------------------------

PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.MULTILINE,
)

# Assignments: key = value or "key": "value"
SECRET_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:api[_-]?key|access[_-]?key|client[_-]?secret|auth[_-]?token|token|secret|password|passwd|passphrase|bearer)\b\s*[:=]\s*[\"']?)([^\r\n\"',;}{]+)([\"']?)",
)

# Known Token Signatures
TOKEN_SIGNATURES = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_-]{12,}|"                  # OpenAI / generic AI key
    r"gh[pousr]_[A-Za-z0-9_-]{20,}|"           # GitHub personal/oauth token
    r"AKIA[0-9A-Z]{16}|"                       # AWS Access Key ID
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"           # Slack token
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}" # JWT token
    r")\b"
)

# HTTP Authorization Headers
AUTH_HEADER = re.compile(
    r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)([A-Za-z0-9_\-\.\+/=]{8,})"
)


def is_sensitive_file(name_or_path: str | Path) -> bool:
    """Return True if a file name or path matches known sensitive file patterns."""
    p = Path(name_or_path)
    lowered_name = p.name.casefold()

    # Exact or variation of .env
    if lowered_name == ".env" or lowered_name.startswith(".env.") or lowered_name.endswith(".env"):
        return True

    # Suffix check
    if p.suffix.casefold() in BLOCKED_FILE_SUFFIXES:
        return True

    # Check parts of name
    if any(part in lowered_name for part in BLOCKED_FILE_PARTS):
        return True

    # Check path elements
    for part in p.parts[:-1]:
        part_cf = part.casefold()
        if part_cf in HIDDEN_DIRECTORY_NAMES:
            return True
        if part_cf == ".env" or any(bp in part_cf for bp in BLOCKED_FILE_PARTS):
            return True

    return False


def redact_secrets(content: str) -> str:
    """Centralized sanitization preventing secret leakage in logs, outputs, and previews."""
    if not content or not isinstance(content, str):
        return content

    # 1. Redact Private Key blocks
    content = PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", content)

    # 2. Redact Authorization headers
    content = AUTH_HEADER.sub(r"\1[REDACTED AUTH TOKEN]", content)

    # 3. Redact Well-known tokens
    content = TOKEN_SIGNATURES.sub("[REDACTED TOKEN]", content)

    # 4. Redact Key/Secret assignments
    content = SECRET_ASSIGNMENT.sub(r"\1[REDACTED]\3", content)

    return content


def contains_secrets(content: str) -> bool:
    """Check if content contains suspected secrets without modifying it."""
    if not content or not isinstance(content, str):
        return False
    return bool(
        PRIVATE_KEY_BLOCK.search(content)
        or AUTH_HEADER.search(content)
        or TOKEN_SIGNATURES.search(content)
        or SECRET_ASSIGNMENT.search(content)
    )
