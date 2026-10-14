"""Bounded context retrieval for large codebases: symbol lookup, stack-trace extraction, and keyword search."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Sequence

from .context_budget import ContextBudgetManager, ContextPriority
from .file_access import detect_binary_file
from .redaction import is_sensitive_file, redact_secrets, HIDDEN_DIRECTORY_NAMES

# Regex to detect stack trace file paths and line numbers: File "foo.py", line 42, or foo.py:42
STACK_TRACE_LOC = re.compile(
    r'(?:File\s+["\']([^"\']+)["\'],\s+line\s+(\d+))|(?:([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+):(\d+))'
)

# Regex for common function and class definitions
DEF_PATTERN = re.compile(
    r'^\s*(?:async\s+def|def|class|function|export\s+function|export\s+class)\s+([a-zA-Z0-9_]+)',
    re.MULTILINE,
)

MAX_RETRIEVAL_FILES = 15
MAX_LINES_PER_SNIPPET = 40


class BoundedContextRetriever:
    """Retrieves relevant code snippets and context from a project directory without scanning the full project."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def _is_accessible_text_file(self, path: Path) -> bool:
        if not path.is_file() or is_sensitive_file(path):
            return False
        if any(part.casefold() in HIDDEN_DIRECTORY_NAMES for part in path.parts):
            return False
        is_bin, _, _ = detect_binary_file(path)
        return not is_bin

    def extract_from_trace(self, trace_or_error: str, context_lines: int = 5) -> list[dict[str, Any]]:
        """Extract referenced files and exact lines from a stack trace or error log."""
        snippets: list[dict[str, Any]] = []
        if not trace_or_error:
            return snippets

        for match in STACK_TRACE_LOC.finditer(trace_or_error):
            raw_path = match.group(1) or match.group(3)
            raw_line = match.group(2) or match.group(4)
            if not raw_path or not raw_line:
                continue

            try:
                line_no = int(raw_line)
                candidate = Path(raw_path)
                if not candidate.is_absolute():
                    candidate = (self.project_root / candidate).resolve()
                else:
                    candidate = candidate.resolve()

                if not candidate.is_relative_to(self.project_root) or not self._is_accessible_text_file(candidate):
                    continue

                snippet = self._extract_lines(candidate, line_no - context_lines, line_no + context_lines)
                if snippet:
                    snippets.append({
                        "file": str(candidate.relative_to(self.project_root)),
                        "line": line_no,
                        "content": snippet,
                        "source": "stack_trace",
                    })
            except (ValueError, OSError):
                continue

        return snippets

    def search_symbols_or_keywords(
        self,
        keywords: Sequence[str],
        max_results: int = 5,
        context_lines: int = 4,
    ) -> list[dict[str, Any]]:
        """Search text/symbols across top project source files and return bounded code snippets."""
        results: list[dict[str, Any]] = []
        cleaned_keywords = [k.strip().casefold() for k in keywords if len(k.strip()) >= 3]
        if not cleaned_keywords:
            return results

        scanned = 0
        for root, dirs, files in os.walk(str(self.project_root)):
            dirs[:] = [d for d in dirs if d.casefold() not in HIDDEN_DIRECTORY_NAMES and not is_sensitive_file(d)]
            for file_name in files:
                file_path = Path(root) / file_name
                if not self._is_accessible_text_file(file_path):
                    continue

                scanned += 1
                if scanned > 100:  # Bound files scanned
                    break

                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        file_lines = f.readlines()

                    for idx, line in enumerate(file_lines):
                        line_cf = line.casefold()
                        if any(kw in line_cf for kw in cleaned_keywords):
                            start = max(0, idx - context_lines)
                            end = min(len(file_lines), idx + context_lines + 1)
                            snippet_text = "".join(file_lines[start:end])
                            rel_path = str(file_path.relative_to(self.project_root))
                            results.append({
                                "file": rel_path,
                                "line": idx + 1,
                                "content": redact_secrets(snippet_text),
                                "source": "keyword_match",
                            })
                            if len(results) >= max_results:
                                return results
                except (OSError, PermissionError):
                    continue

        return results

    def _extract_lines(self, path: Path, start_line: int, end_line: int) -> str:
        start_line = max(1, start_line)
        end_line = max(start_line, end_line)
        lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f, start=1):
                    if idx >= start_line and idx <= end_line:
                        lines.append(f"{idx}: {line.rstrip()}")
                    elif idx > end_line:
                        break
        except (OSError, PermissionError):
            return ""
        return redact_secrets("\n".join(lines))

    def retrieve_context_for_prompt(
        self,
        prompt: str,
        budget_manager: ContextBudgetManager,
        error_context: str | None = None,
    ) -> dict[str, Any]:
        """Orchestrate bounded retrieval and feed into ContextBudgetManager."""
        budget_manager.add_item(
            item_id="user_prompt",
            content=f"User Request:\n{prompt}",
            priority=ContextPriority.USER_PROMPT,
        )

        if error_context:
            budget_manager.add_item(
                item_id="error_context",
                content=f"Recent Error/Trace:\n{redact_secrets(error_context)}",
                priority=ContextPriority.ERRORS_AND_FAILURES,
            )
            trace_snippets = self.extract_from_trace(error_context)
            for snip in trace_snippets:
                budget_manager.add_item(
                    item_id=f"trace_{snip['file']}_{snip['line']}",
                    content=f"Code Context from Stack Trace ({snip['file']} around line {snip['line']}):\n```\n{snip['content']}\n```",
                    priority=ContextPriority.RELEVANT_CODE_SNIPPETS,
                )

        # Extract potential identifier keywords from prompt
        words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", prompt)
        keyword_snippets = self.search_symbols_or_keywords(words, max_results=4)
        for snip in keyword_snippets:
            budget_manager.add_item(
                item_id=f"symbol_{snip['file']}_{snip['line']}",
                content=f"Relevant Reference ({snip['file']} line {snip['line']}):\n```\n{snip['content']}\n```",
                priority=ContextPriority.RELEVANT_CODE_SNIPPETS,
            )

        return budget_manager.assemble_context()
