"""Hardened file and directory access: bounded streaming, line-based pagination, binary detection, search, and deterministic chunked export."""
from __future__ import annotations

import ast
import hashlib
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import is_sensitive_file, redact_secrets, HIDDEN_DIRECTORY_NAMES

# ---------------------------------------------------------------------------
# Configuration & Limits
# ---------------------------------------------------------------------------

DEFAULT_PREVIEW_LINES = 35
MAX_PREVIEW_LINES = 200
MAX_PREVIEW_BYTES = int(os.environ.get("SUTRA_MAX_PREVIEW_BYTES", 64 * 1024))  # 64 KB per preview window

# Operation budget (Scanning, searching, AST parsing):
# Maximum data permitted for ONE bounded scanning/search/processing operation.
# Does NOT limit total file size for preview!
MAX_SINGLE_SCAN_BYTES = int(os.environ.get("SUTRA_MAX_SINGLE_SCAN_BYTES", 50 * 1024 * 1024))  # 50 MB operation budget

# Backward-compatible alias for MAX_SINGLE_SCAN_BYTES
MAX_STREAM_BYTES = MAX_SINGLE_SCAN_BYTES

# Transport download chunk size and overall export limits:
MAX_DOWNLOAD_CHUNK_BYTES = int(os.environ.get("SUTRA_MAX_DOWNLOAD_CHUNK_BYTES", 10 * 1024 * 1024))  # 10 MB per download chunk
MAX_SINGLE_DOWNLOAD_CHUNK_BYTES = MAX_DOWNLOAD_CHUNK_BYTES

# Backward-compatible alias for MAX_DOWNLOAD_CHUNK_BYTES
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_CHUNK_BYTES

# Upper bound for total multi-part file export:
MAX_TOTAL_EXPORT_BYTES = int(os.environ.get("SUTRA_MAX_TOTAL_EXPORT_BYTES", 200 * 1024 * 1024))  # 200 MB max total export

SAMPLE_BINARY_BYTES = 8192

DEFAULT_DIR_PAGE_SIZE = 30
MAX_DIR_PAGE_SIZE = 100
MAX_SEARCH_DEPTH = 4
MAX_SEARCH_ENTRIES = 2000

# Binary extension mappings
BINARY_CATEGORIES: dict[str, set[str]] = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".svg"},
    "pdf": {".pdf"},
    "archive": {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz"},
    "executable": {".exe", ".dll", ".so", ".dylib", ".bin", ".app"},
    "database": {".db", ".sqlite", ".sqlite3", ".parquet", ".mdf", ".ldf"},
    "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv"},
    "compiled": {".pyc", ".class", ".o", ".obj", ".wasm"},
}

PROGRAMMING_LANGUAGES: dict[str, str] = {
    ".py": "Python",
    ".pyw": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (React)",
    ".jsx": "JavaScript (React)",
    ".json": "JSON",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Bash",
    ".zsh": "Zsh",
    ".ps1": "PowerShell",
    ".rs": "Rust",
    ".go": "Go",
    ".c": "C",
    ".h": "C Header",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++ Header",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".lua": "Lua",
    ".r": "R",
    ".dart": "Dart",
    ".txt": "Plain Text",
}


# ---------------------------------------------------------------------------
# Multipart Export Data Models
# ---------------------------------------------------------------------------

@dataclass
class ExportPart:
    """Represents a single deterministic chunk of an exported file."""
    index: int
    filename: str
    byte_start: int
    byte_end: int
    size: int
    sha256: str
    line_start: int | None = None
    line_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "index": self.index,
            "filename": self.filename,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.line_start is not None:
            d["line_start"] = self.line_start
        if self.line_end is not None:
            d["line_end"] = self.line_end
        return d


@dataclass
class ExportManifest:
    """Deterministic manifest documenting a multi-part file export."""
    schema_version: int = 1
    original_filename: str = ""
    original_size: int = 0
    whole_file_sha256: str = ""
    part_count: int = 0
    parts: list[ExportPart] = None  # type: ignore
    is_binary: bool = False
    created_at: float = 0.0
    file_version: str = ""

    def __post_init__(self) -> None:
        if self.parts is None:
            self.parts = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "original_filename": self.original_filename,
            "original_size": self.original_size,
            "whole_file_sha256": self.whole_file_sha256,
            "part_count": self.part_count,
            "is_binary": self.is_binary,
            "created_at": self.created_at,
            "file_version": self.file_version,
            "parts": [p.to_dict() for p in self.parts],
        }


# ---------------------------------------------------------------------------
# Sparse Line Index for Fast Random Access in Large Files
# ---------------------------------------------------------------------------

class SparseLineIndex:
    """Lightweight in-memory sparse checkpoint index for fast random access in large files."""
    def __init__(
        self,
        file_path: str,
        file_version: str,
        checkpoints: list[tuple[int, int]],
        total_lines: int,
        total_bytes: int,
    ) -> None:
        self.file_path = file_path
        self.file_version = file_version
        self.checkpoints = checkpoints  # [(1, 0), (1001, offset1), (2001, offset2), ...]
        self.total_lines = total_lines
        self.total_bytes = total_bytes
        self.created_at = time.time()

    def find_nearest_checkpoint(self, target_line: int) -> tuple[int, int]:
        """Binary search for the highest checkpoint line <= target_line."""
        if not self.checkpoints or target_line <= 1:
            return 1, 0
        low, high = 0, len(self.checkpoints) - 1
        best = self.checkpoints[0]
        while low <= high:
            mid = (low + high) // 2
            line, offset = self.checkpoints[mid]
            if line <= target_line:
                best = self.checkpoints[mid]
                low = mid + 1
            else:
                high = mid - 1
        return best


_SPARSE_INDEX_CACHE: dict[str, SparseLineIndex] = {}


def get_or_build_sparse_index(target: Path, step_lines: int = 1000) -> SparseLineIndex:
    """Retrieve or build a sparse line index for fast seeking without repeated full scans."""
    stat = target.stat()
    version = f"{int(stat.st_mtime)}-{stat.st_size}"
    cache_key = str(target.resolve())

    cached = _SPARSE_INDEX_CACHE.get(cache_key)
    if cached and cached.file_version == version:
        return cached

    checkpoints: list[tuple[int, int]] = [(1, 0)]
    current_line = 1
    total_bytes = stat.st_size
    last_byte = b""

    with open(target, "rb") as f:
        offset = 0
        buf = f.read(65536)
        while buf:
            last_byte = buf[-1:]
            nl_idx = 0
            while True:
                next_nl = buf.find(b"\n", nl_idx)
                if next_nl == -1:
                    break
                current_line += 1
                nl_offset = offset + next_nl + 1
                if current_line % step_lines == 1 or step_lines == 1:
                    checkpoints.append((current_line, nl_offset))
                nl_idx = next_nl + 1
            offset += len(buf)
            buf = f.read(65536)

    # Compute total lines matching count_file_lines convention
    if total_bytes == 0:
        total_lines = 0
    elif last_byte == b"\n":
        total_lines = current_line - 1
    else:
        total_lines = current_line

    index = SparseLineIndex(
        file_path=cache_key,
        file_version=version,
        checkpoints=checkpoints,
        total_lines=total_lines,
        total_bytes=total_bytes,
    )

    if len(_SPARSE_INDEX_CACHE) >= 64:
        # Evict oldest entry
        oldest_key = next(iter(_SPARSE_INDEX_CACHE))
        _SPARSE_INDEX_CACHE.pop(oldest_key, None)

    _SPARSE_INDEX_CACHE[cache_key] = index
    return index


# ---------------------------------------------------------------------------
# Binary Detection
# ---------------------------------------------------------------------------

def detect_binary_file(target: Path) -> tuple[bool, str, str]:
    """Inspect file extension and the first 8 KB to determine if it is binary.

    Never loads the entire file. Returns (is_binary, category, mime_type).
    """
    suffix = target.suffix.casefold()
    mime_type, _ = mimetypes.guess_type(target.name)
    mime_str = mime_type or "application/octet-stream"

    # 1. Match known binary categories by extension
    for category, suffixes in BINARY_CATEGORIES.items():
        if suffix in suffixes:
            return True, category, mime_str

    # 2. Inspect first SAMPLE_BINARY_BYTES for NUL byte or high control-character ratio
    try:
        with open(target, "rb") as f:
            chunk = f.read(SAMPLE_BINARY_BYTES)
    except (OSError, PermissionError):
        return True, "inaccessible", mime_str

    if not chunk:
        # Empty file can be treated as text
        return False, "text", mime_type or "text/plain"

    if b"\x00" in chunk:
        return True, "binary", mime_str

    # Non-text byte ratio check (excluding standard whitespace \t, \n, \r)
    control_bytes = sum(1 for b in chunk if b < 9 or (13 < b < 32))
    if control_bytes > max(1, len(chunk) // 10):
        return True, "binary", mime_str

    return False, "text", mime_type or "text/plain"


def get_binary_metadata(target: Path) -> dict[str, Any]:
    """Generate safe structured metadata for a binary file without reading contents."""
    stat = target.stat()
    is_bin, category, mime_type = detect_binary_file(target)
    export_available = stat.st_size <= MAX_TOTAL_EXPORT_BYTES
    is_multipart = stat.st_size > MAX_DOWNLOAD_CHUNK_BYTES and export_available
    return {
        "name": target.name,
        "is_binary": True,
        "category": category,
        "mime_type": mime_type,
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
        "download_available": stat.st_size <= MAX_DOWNLOAD_CHUNK_BYTES,
        "export_available": export_available,
        "is_multipart": is_multipart,
        "export_chunk_size": MAX_DOWNLOAD_CHUNK_BYTES,
        "message": f"Binary file ({category}) - text preview unavailable. Size: {stat.st_size} bytes.",
    }


# ---------------------------------------------------------------------------
# Helper: Safe UTF-8 Truncation & Boundaries
# ---------------------------------------------------------------------------

def _truncate_utf8_string(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes without splitting UTF-8 characters."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    sliced = encoded[:max_bytes]
    return sliced.decode("utf-8", errors="ignore")


def _find_safe_utf8_boundary(chunk: bytes) -> int:
    """Find the byte index in chunk that does not split a multi-byte UTF-8 sequence."""
    if not chunk:
        return 0
    idx = len(chunk)
    steps = 0
    while steps < 4 and idx > 0 and (chunk[idx - 1] & 0xC0) == 0x80:
        idx -= 1
        steps += 1
    if idx == 0:
        return len(chunk)
    lead = chunk[idx - 1]
    if (lead & 0x80) == 0:
        return len(chunk)  # 1-byte ASCII
    elif (lead & 0xE0) == 0xC0:
        expected = 2
    elif (lead & 0xF0) == 0xE0:
        expected = 3
    elif (lead & 0xF8) == 0xF0:
        expected = 4
    else:
        return len(chunk)
    actual = steps + 1
    if actual == expected:
        return len(chunk)
    return idx - 1


# ---------------------------------------------------------------------------
# Bounded Streaming Line-Based File Preview (Phase 5 / Hardened)
# ---------------------------------------------------------------------------

def preview_file_bounded(
    target: Path,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = DEFAULT_PREVIEW_LINES,
    max_preview_bytes: int = MAX_PREVIEW_BYTES,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Stream and preview a slice of a source file without loading it completely into memory.

    Preview window is strictly bounded by BOTH max_lines and max_preview_bytes.
    Large local files (50 MB, 100 MB, 120 MB+) are fully previewable for bounded requests.
    """
    if is_sensitive_file(target):
        raise ValueError("file is unavailable for safe text transfer")

    if not target.is_file():
        raise ValueError("file is not available")

    stat = target.stat()

    # Check for binary
    is_bin, category, mime_type = detect_binary_file(target)
    if is_bin:
        return get_binary_metadata(target)

    # Compute lightweight version token (mtime + size)
    file_version = f"{int(stat.st_mtime)}-{stat.st_size}"
    version_changed = expected_version is not None and expected_version != file_version

    # Validate and bound line requests
    if start_line < 1:
        start_line = 1

    bounded_max_lines = min(max(1, max_lines), MAX_PREVIEW_LINES)
    if end_line is None:
        target_end_line = start_line + bounded_max_lines - 1
    else:
        if end_line < start_line:
            target_end_line = start_line + bounded_max_lines - 1
        else:
            target_end_line = min(end_line, start_line + bounded_max_lines - 1)

    # Use sparse line index for fast checkpoint seek
    index = get_or_build_sparse_index(target)
    total_lines = index.total_lines
    chk_line, chk_offset = index.find_nearest_checkpoint(start_line)

    collected_lines: list[str] = []
    accumulated_bytes = 0
    truncated_by_bytes = False
    has_more = False

    with open(target, "r", encoding="utf-8", errors="replace") as f:
        f.seek(chk_offset)
        current_line_no = chk_line - 1
        for line in f:
            current_line_no += 1
            if current_line_no < start_line:
                continue
            if current_line_no <= target_end_line:
                line_str = line.rstrip("\r\n")
                line_bytes_len = len(line_str.encode("utf-8", errors="replace"))

                if accumulated_bytes + line_bytes_len > max_preview_bytes:
                    truncated_by_bytes = True
                    has_more = True
                    if not collected_lines:
                        # Pathological case: first line itself exceeds byte limit
                        safe_slice = _truncate_utf8_string(line_str, max_preview_bytes)
                        collected_lines.append(safe_slice + " ... [line truncated by size limit]")
                    break

                collected_lines.append(line_str)
                accumulated_bytes += line_bytes_len
            else:
                has_more = True
                break

    if not has_more and current_line_no < total_lines:
        has_more = True

    actual_end_line = start_line + len(collected_lines) - 1 if collected_lines else start_line

    # Redact any secrets line-by-line
    sanitized_content = "\n".join(redact_secrets(ln) for ln in collected_lines)

    return {
        "name": target.name,
        "is_binary": False,
        "size_bytes": stat.st_size,
        "total_lines": total_lines,
        "start_line": start_line,
        "end_line": actual_end_line,
        "lines_returned": len(collected_lines),
        "has_more": has_more,
        "has_prev": start_line > 1,
        "file_version": file_version,
        "version_changed": version_changed,
        "content": sanitized_content,
        "truncated_by_bytes": truncated_by_bytes,
        "truncation_reason": "size_limit" if truncated_by_bytes else None,
        "message": "⚠️ Preview truncated by size limit." if truncated_by_bytes else None,
    }


# ---------------------------------------------------------------------------
# Paginated Directory Listing & Path Search
# ---------------------------------------------------------------------------

def list_directory_paginated(
    target: Path,
    page: int = 0,
    page_size: int = DEFAULT_DIR_PAGE_SIZE,
    search: str | None = None,
    recursive: bool = False,
    max_depth: int = MAX_SEARCH_DEPTH,
) -> dict[str, Any]:
    """Return paginated directory entries with deterministic ordering and optional search."""
    if not target.is_dir():
        raise ValueError("folder unavailable")

    page_size = min(max(1, page_size), MAX_DIR_PAGE_SIZE)
    page = max(0, page)

    search_term = search.strip().casefold() if search else None
    all_entries: list[dict[str, Any]] = []

    def _should_skip(entry_name: str) -> bool:
        lowered = entry_name.casefold()
        return lowered in HIDDEN_DIRECTORY_NAMES or is_sensitive_file(entry_name)

    if not recursive:
        try:
            raw_entries = sorted(
                target.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.casefold())
            )
            for item in raw_entries:
                if item.is_symlink() or _should_skip(item.name):
                    continue
                if not item.is_dir() and not item.is_file():
                    continue

                if search_term and search_term not in item.name.casefold():
                    continue

                all_entries.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "path": str(item),
                    "size_bytes": item.stat().st_size if item.is_file() else None,
                })
        except (OSError, PermissionError) as exc:
            raise ValueError(f"failed to read directory: {exc}") from exc
    else:
        count = 0
        target_depth = len(target.parts)
        for root, dirs, files in os.walk(str(target)):
            root_path = Path(root)
            current_depth = len(root_path.parts) - target_depth
            if current_depth > max_depth:
                dirs.clear()
                continue

            dirs[:] = [d for d in dirs if not _should_skip(d) and not (root_path / d).is_symlink()]
            dirs.sort(key=lambda s: s.casefold())

            for d in dirs:
                if search_term and search_term in d.casefold():
                    dir_full = root_path / d
                    all_entries.append({
                        "name": d,
                        "type": "directory",
                        "path": str(dir_full),
                        "size_bytes": None,
                    })
                    count += 1
                    if count >= MAX_SEARCH_ENTRIES:
                        break

            if count >= MAX_SEARCH_ENTRIES:
                break

            sorted_files = sorted(files, key=lambda s: s.casefold())
            for f in sorted_files:
                if _should_skip(f):
                    continue
                file_full = root_path / f
                if file_full.is_symlink():
                    continue

                if search_term is None or search_term in f.casefold() or search_term in str(file_full.relative_to(target)).casefold():
                    all_entries.append({
                        "name": f,
                        "type": "file",
                        "path": str(file_full),
                        "size_bytes": file_full.stat().st_size if file_full.exists() else None,
                    })
                    count += 1
                    if count >= MAX_SEARCH_ENTRIES:
                        break

            if count >= MAX_SEARCH_ENTRIES:
                break

    total_items = len(all_entries)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total_items)
    paged_items = all_entries[start_idx:end_idx] if start_idx < total_items else []

    return {
        "items": paged_items,
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_more": (page + 1) < total_pages,
        "has_prev": page > 0,
    }


# ---------------------------------------------------------------------------
# File Metadata & Line Counting
# ---------------------------------------------------------------------------

def count_file_lines(target: Path) -> int:
    """Quickly count lines in a text file using 64 KB streaming buffers without loading full file."""
    lines = 0
    last_byte = b""
    try:
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                lines += chunk.count(b"\n")
                last_byte = chunk[-1:]
        if target.stat().st_size > 0 and last_byte != b"\n":
            lines += 1
    except (OSError, PermissionError):
        return 0
    return lines


def get_file_info(target: Path, relative_to: Path | None = None) -> dict[str, Any]:
    """Return compact metadata for a file without loading the entire file."""
    if is_sensitive_file(target):
        raise ValueError("file is unavailable for safe text transfer")
    if not target.is_file():
        raise ValueError("file is not available")

    stat = target.stat()
    rel_path = str(target.relative_to(relative_to)) if relative_to and target.is_relative_to(relative_to) else target.name
    is_bin, category, mime = detect_binary_file(target)

    file_version = f"{int(stat.st_mtime)}-{stat.st_size}"
    suffix = target.suffix.casefold()
    language = PROGRAMMING_LANGUAGES.get(suffix, "Text")

    export_available = stat.st_size <= MAX_TOTAL_EXPORT_BYTES
    is_multipart = stat.st_size > MAX_DOWNLOAD_CHUNK_BYTES and export_available

    if is_bin:
        meta = get_binary_metadata(target)
        meta["relative_path"] = rel_path
        meta["file_type"] = f"Binary ({category})"
        meta["file_version"] = file_version
        return meta

    total_lines = count_file_lines(target)
    return {
        "name": target.name,
        "relative_path": rel_path,
        "is_binary": False,
        "category": "text",
        "mime_type": mime,
        "file_type": language,
        "language": language,
        "size_bytes": stat.st_size,
        "total_lines": total_lines,
        "modified_at": stat.st_mtime,
        "file_version": file_version,
        "download_available": stat.st_size <= MAX_DOWNLOAD_CHUNK_BYTES,
        "export_available": export_available,
        "is_multipart": is_multipart,
        "export_chunk_size": MAX_DOWNLOAD_CHUNK_BYTES,
    }


# ---------------------------------------------------------------------------
# Bounded Text Search with Operation Budget
# ---------------------------------------------------------------------------

def search_file_text(
    target: Path,
    query: str,
    max_results: int = 20,
    case_sensitive: bool = False,
    max_scan_bytes: int = MAX_SINGLE_SCAN_BYTES,
) -> dict[str, Any]:
    """Search for matching lines within a file using bounded line streaming.

    Stops scanning when max_scan_bytes operation budget is reached without refusing the search.
    """
    if is_sensitive_file(target):
        raise ValueError("file is unavailable for safe text transfer")
    if not target.is_file():
        raise ValueError("file is not available")

    is_bin, _, _ = detect_binary_file(target)
    if is_bin:
        raise ValueError("binary file search is unavailable")

    if not query or not query.strip():
        return {
            "name": target.name,
            "query": query,
            "matches": [],
            "total_matches": 0,
            "has_more": False,
            "scan_budget_exceeded": False,
        }

    clean_query = query.strip()
    search_term = clean_query if case_sensitive else clean_query.casefold()
    max_results = min(max(1, max_results), 100)

    matches: list[dict[str, Any]] = []
    total_matches = 0
    bytes_scanned = 0
    scan_budget_exceeded = False

    with open(target, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw_line in enumerate(f, start=1):
            bytes_scanned += len(raw_line.encode("utf-8", errors="replace"))
            if bytes_scanned > max_scan_bytes:
                scan_budget_exceeded = True
                break

            check_line = raw_line if case_sensitive else raw_line.casefold()
            if search_term in check_line:
                total_matches += 1
                if len(matches) < max_results:
                    stripped = raw_line.strip()
                    sanitized = redact_secrets(stripped)
                    snippet = (sanitized[:117] + "...") if len(sanitized) > 120 else sanitized
                    matches.append({
                        "line": line_no,
                        "snippet": snippet,
                    })

    has_more = (total_matches > len(matches)) or scan_budget_exceeded
    message = (
        f"Search stopped after reaching operation scan budget ({bytes_scanned} bytes). Showing matches found so far."
        if scan_budget_exceeded else None
    )

    return {
        "name": target.name,
        "query": clean_query,
        "case_sensitive": case_sensitive,
        "matches": matches,
        "total_matches": total_matches,
        "max_results": max_results,
        "has_more": has_more,
        "scan_budget_exceeded": scan_budget_exceeded,
        "bytes_scanned": bytes_scanned,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Deterministic Symbol Discovery with Operation Budget
# ---------------------------------------------------------------------------

def extract_file_symbols(
    target: Path,
    max_scan_bytes: int = MAX_SINGLE_SCAN_BYTES,
) -> dict[str, Any]:
    """Deterministically discover functions, methods, and classes in source files."""
    if is_sensitive_file(target):
        raise ValueError("file is unavailable for safe text transfer")
    if not target.is_file():
        raise ValueError("file is not available")

    is_bin, _, _ = detect_binary_file(target)
    if is_bin:
        raise ValueError("binary file symbol extraction is unavailable")

    stat = target.stat()
    if stat.st_size > max_scan_bytes:
        raise ValueError(f"symbol extraction exceeds operation scan budget ({stat.st_size} bytes > {max_scan_bytes} bytes limit)")

    suffix = target.suffix.casefold()
    language = PROGRAMMING_LANGUAGES.get(suffix, "Text")

    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    # 1. Python AST deterministic parsing
    if suffix in (".py", ".pyw"):
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                tree = ast.parse(f.read(), filename=target.name)

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    class_methods: list[dict[str, Any]] = []
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            class_methods.append({
                                "name": item.name,
                                "line": item.lineno,
                            })
                    classes.append({
                        "name": node.name,
                        "line": node.lineno,
                        "methods": class_methods,
                    })
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append({
                        "name": node.name,
                        "line": node.lineno,
                        "is_async": isinstance(node, ast.AsyncFunctionDef),
                    })
            return {
                "name": target.name,
                "language": language,
                "classes": classes,
                "functions": functions,
                "total_symbols": len(classes) + len(functions),
            }
        except (SyntaxError, ValueError):
            pass

    # 2. Deterministic regex parsing for JS/TS/Go/Rust/Java/Python fallback
    func_pattern: re.Pattern[str] | None = None
    class_pattern: re.Pattern[str] | None = None

    if suffix in (".py", ".pyw"):
        func_pattern = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\(")
        class_pattern = re.compile(r"^\s*class\s+([A-Za-z0-9_]+)")
    elif suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        func_pattern = re.compile(r"(?:function\s+([A-Za-z0-9_$]+)|(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)")
        class_pattern = re.compile(r"class\s+([A-Za-z0-9_$]+)")
    elif suffix == ".go":
        func_pattern = re.compile(r"func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\(")
        class_pattern = re.compile(r"type\s+([A-Za-z0-9_]+)\s+struct")
    elif suffix == ".rs":
        func_pattern = re.compile(r"(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*\(")
        class_pattern = re.compile(r"(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z0-9_]+)")
    elif suffix in (".java", ".kt", ".cs"):
        func_pattern = re.compile(r"(?:public|protected|private|static|\s)+[\w<>\[\]]+\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?:\{|throws)")
        class_pattern = re.compile(r"(?:class|interface|record)\s+([A-Za-z0-9_]+)")

    if func_pattern or class_pattern:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                if class_pattern:
                    m = class_pattern.search(line)
                    if m:
                        name = next((g for g in m.groups() if g), None)
                        if name:
                            classes.append({"name": name, "line": line_no, "methods": []})
                            continue
                if func_pattern:
                    m = func_pattern.search(line)
                    if m:
                        name = next((g for g in m.groups() if g), None)
                        if name:
                            functions.append({"name": name, "line": line_no})

    return {
        "name": target.name,
        "language": language,
        "classes": classes,
        "functions": functions,
        "total_symbols": len(classes) + len(functions),
    }


# ---------------------------------------------------------------------------
# Deterministic Multipart File Export Subsystem
# ---------------------------------------------------------------------------

def build_export_plan(
    target: Path,
    chunk_size: int = MAX_DOWNLOAD_CHUNK_BYTES,
) -> ExportManifest:
    """Generate a deterministic multi-part export plan and manifest for a file.

    Text files prefer splitting on newline boundaries while preserving UTF-8 integrity.
    Binary files use exact byte slicing.
    """
    if is_sensitive_file(target):
        raise ValueError("file is unavailable for safe export")
    if not target.is_file():
        raise ValueError("file is not available")

    stat = target.stat()
    if stat.st_size > MAX_TOTAL_EXPORT_BYTES:
        raise ValueError(f"file size ({stat.st_size} bytes) exceeds maximum export limit ({MAX_TOTAL_EXPORT_BYTES} bytes)")

    is_bin, _, _ = detect_binary_file(target)
    file_version = f"{int(stat.st_mtime)}-{stat.st_size}"

    parts: list[ExportPart] = []
    whole_hasher = hashlib.sha256()

    if stat.st_size == 0:
        empty_sha = hashlib.sha256(b"").hexdigest()
        parts.append(ExportPart(
            index=1,
            filename=f"{target.name}.part-0001",
            byte_start=0,
            byte_end=0,
            size=0,
            sha256=empty_sha,
            line_start=0 if not is_bin else None,
            line_end=0 if not is_bin else None,
        ))
        return ExportManifest(
            schema_version=1,
            original_filename=target.name,
            original_size=0,
            whole_file_sha256=empty_sha,
            part_count=1,
            parts=parts,
            is_binary=is_bin,
            created_at=time.time(),
            file_version=file_version,
        )

    current_byte_offset = 0
    current_line = 1
    part_index = 1

    with open(target, "rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break

            if is_bin or len(buf) < chunk_size:
                split_len = len(buf)
            else:
                last_nl = buf.rfind(b"\n")
                if last_nl != -1 and last_nl > 0:
                    split_len = last_nl + 1
                else:
                    split_len = _find_safe_utf8_boundary(buf)
                    if split_len == 0:
                        split_len = len(buf)

            chunk_bytes = buf[:split_len]
            unconsumed = len(buf) - split_len
            if unconsumed > 0:
                f.seek(-unconsumed, os.SEEK_CUR)

            part_size = len(chunk_bytes)
            whole_hasher.update(chunk_bytes)
            part_sha = hashlib.sha256(chunk_bytes).hexdigest()
            byte_start = current_byte_offset
            byte_end = current_byte_offset + part_size - 1

            line_start = None
            line_end = None
            if not is_bin:
                line_start = current_line
                lines_in_chunk = chunk_bytes.count(b"\n")
                current_line += lines_in_chunk
                line_end = max(line_start, current_line if not chunk_bytes.endswith(b"\n") else current_line - 1)

            parts.append(ExportPart(
                index=part_index,
                filename=f"{target.name}.part-{part_index:04d}",
                byte_start=byte_start,
                byte_end=byte_end,
                size=part_size,
                sha256=part_sha,
                line_start=line_start,
                line_end=line_end,
            ))
            current_byte_offset += part_size
            part_index += 1

    whole_sha = whole_hasher.hexdigest()
    total_parts_size = sum(p.size for p in parts)
    if total_parts_size != stat.st_size:
        raise ValueError(f"export plan integrity failure: parts size ({total_parts_size}) != file size ({stat.st_size})")

    return ExportManifest(
        schema_version=1,
        original_filename=target.name,
        original_size=stat.st_size,
        whole_file_sha256=whole_sha,
        part_count=len(parts),
        parts=parts,
        is_binary=is_bin,
        created_at=time.time(),
        file_version=file_version,
    )


def get_export_part_bytes(
    target: Path,
    manifest: ExportManifest,
    part_index: int,
) -> tuple[ExportPart, bytes]:
    """Retrieve exact bytes for a part specified in manifest and verify part integrity."""
    if is_sensitive_file(target):
        raise ValueError("file is unavailable for safe export")
    if not target.is_file():
        raise ValueError("file is not available")

    stat = target.stat()
    current_version = f"{int(stat.st_mtime)}-{stat.st_size}"
    if manifest.file_version and manifest.file_version != current_version:
        raise ValueError("file was modified during export: version token mismatch")

    if part_index < 1 or part_index > len(manifest.parts):
        raise IndexError(f"part index {part_index} out of range (1..{len(manifest.parts)})")

    part = manifest.parts[part_index - 1]

    with open(target, "rb") as f:
        f.seek(part.byte_start)
        data = f.read(part.size)

    if len(data) != part.size:
        raise ValueError(f"failed to read expected part size ({len(data)} != {part.size})")

    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != part.sha256:
        raise ValueError(f"part integrity failure: checksum mismatch on part {part_index}")

    return part, data


def verify_export_integrity(
    manifest: ExportManifest,
    parts_data: dict[int, bytes],
) -> tuple[bool, str]:
    """Verify that collected parts match manifest and reconstruct whole file with correct SHA-256."""
    if len(parts_data) != manifest.part_count:
        return False, f"missing parts: received {len(parts_data)} of {manifest.part_count} parts"

    total_size = 0
    whole_hasher = hashlib.sha256()

    for i in range(1, manifest.part_count + 1):
        if i not in parts_data:
            return False, f"missing part index {i}"
        part = manifest.parts[i - 1]
        data = parts_data[i]
        if len(data) != part.size:
            return False, f"part {i} size mismatch ({len(data)} != {part.size})"
        actual_part_sha = hashlib.sha256(data).hexdigest()
        if actual_part_sha != part.sha256:
            return False, f"part {i} checksum mismatch: corrupted data"
        whole_hasher.update(data)
        total_size += len(data)

    if total_size != manifest.original_size:
        return False, f"reconstructed size mismatch ({total_size} != {manifest.original_size})"

    reconstructed_sha = whole_hasher.hexdigest()
    if reconstructed_sha != manifest.whole_file_sha256:
        return False, f"whole-file checksum mismatch ({reconstructed_sha} != {manifest.whole_file_sha256})"

    return True, "Integrity verified: all parts match original file SHA-256"


def reconstruct_file_from_parts(
    manifest: ExportManifest,
    parts_data: dict[int, bytes],
    output_path: Path,
) -> bool:
    """Reconstruct original file from parts and verify full integrity."""
    valid, reason = verify_export_integrity(manifest, parts_data)
    if not valid:
        raise ValueError(f"cannot reconstruct file: {reason}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        for i in range(1, manifest.part_count + 1):
            f.write(parts_data[i])

    return True


def export_file_to_directory(
    target: Path,
    output_dir: Path,
    chunk_size: int = MAX_DOWNLOAD_CHUNK_BYTES,
) -> ExportManifest:
    """Materialize export parts and manifest.json into output_dir with integrity verification."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_export_plan(target, chunk_size=chunk_size)

    parts_data: dict[int, bytes] = {}
    for part in manifest.parts:
        part_obj, data = get_export_part_bytes(target, manifest, part.index)
        parts_data[part.index] = data
        part_path = output_dir / part.filename
        part_path.write_bytes(data)

    valid, reason = verify_export_integrity(manifest, parts_data)
    if not valid:
        raise ValueError(f"export verification failed: {reason}")

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    return manifest


def cleanup_export_directory(output_dir: Path) -> None:
    """Clean up temporary export directory."""
    if output_dir.exists() and output_dir.is_dir():
        for item in output_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            output_dir.rmdir()
        except OSError:
            pass
