"""Centralized message chunking and output formatting layer for Telegram and remote streams."""
from __future__ import annotations

import re
from typing import Sequence

TELEGRAM_MAX_MESSAGE_CHARS = 4000
DEFAULT_CHUNK_SIZE = 3800  # Leave room for continuation markers and closing fences

# Pattern detecting markdown code fences: ```language\n or ```
CODE_FENCE_PATTERN = re.compile(r"^```([a-zA-Z0-9_\-\+]*)\s*$", re.MULTILINE)


def chunk_message(
    text: str,
    max_chunk_size: int = DEFAULT_CHUNK_SIZE,
    header: str | None = None,
    parse_mode: str | None = "markdown",
) -> list[str]:
    """Split long text safely into ordered chunks conforming to max_chunk_size.
    
    Preserves:
    - Code fences: safely closes open code blocks at chunk boundaries and re-opens in next chunk.
    - Safe line boundaries: splits on newlines and paragraphs before falling back to spaces.
    - Chunk order and non-duplication.
    """
    if not text:
        return [header] if header else [""]

    max_size = max(100, min(max_chunk_size, TELEGRAM_MAX_MESSAGE_CHARS))
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []

    current_chunk_lines: list[str] = []
    current_size = len(header + "\n\n") if header else 0
    in_code_block = False
    current_code_lang = ""

    for line in lines:
        line_len = len(line)
        fence_match = CODE_FENCE_PATTERN.match(line.strip())

        # If a single line exceeds max_size, break it hard
        if line_len > max_size:
            # First flush what we have
            if current_chunk_lines:
                chunk_str = "".join(current_chunk_lines)
                if in_code_block and parse_mode == "markdown":
                    chunk_str += "\n```"
                chunks.append(chunk_str)
                current_chunk_lines = [f"```{current_code_lang}\n"] if in_code_block and parse_mode == "markdown" else []
                current_size = len(current_chunk_lines[0]) if current_chunk_lines else 0

            # Split large line into substrings
            start = 0
            while start < len(line):
                sub = line[start:start + (max_size - 100)]
                start += len(sub)
                chunk_str = sub
                if in_code_block and parse_mode == "markdown":
                    chunk_str = f"```{current_code_lang}\n" + chunk_str + "\n```"
                chunks.append(chunk_str)
            continue

        # Check if adding this line would exceed chunk budget
        overhead = 6 if in_code_block and parse_mode == "markdown" else 0
        if current_size + line_len + overhead > max_size and current_chunk_lines:
            chunk_str = "".join(current_chunk_lines)
            if in_code_block and parse_mode == "markdown":
                chunk_str += "\n```"
            chunks.append(chunk_str)

            # Next chunk resumes with code fence if in code block
            current_chunk_lines = [f"```{current_code_lang}\n"] if in_code_block and parse_mode == "markdown" else []
            current_size = len(current_chunk_lines[0]) if current_chunk_lines else 0

        # Update code block tracking
        if fence_match:
            if not in_code_block:
                in_code_block = True
                current_code_lang = fence_match.group(1) or ""
            else:
                in_code_block = False
                current_code_lang = ""

        current_chunk_lines.append(line)
        current_size += line_len

    if current_chunk_lines:
        chunk_str = "".join(current_chunk_lines)
        if in_code_block and parse_mode == "markdown":
            chunk_str += "\n```"
        chunks.append(chunk_str)

    # Prefix header to first chunk if specified
    if header and chunks:
        chunks[0] = f"{header}\n\n{chunks[0]}"

    # Add chunk indicators if multi-chunk
    if len(chunks) > 1:
        total = len(chunks)
        for i in range(total):
            prefix = f"[{i+1}/{total}]\n"
            chunks[i] = prefix + chunks[i]

    return chunks
