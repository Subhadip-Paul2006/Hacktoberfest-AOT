"""Domain models, command profiles, and requests for the Local Terminal Connector."""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from sutra.redaction import redact_secrets
from ..models import sanitize_metadata


@dataclass(frozen=True)
class CommandProfile:
    """Pre-configured authorization and constraint profile for terminal commands."""
    name: str
    description: str = ""
    allowed_executables: frozenset[str] = field(default_factory=frozenset)
    allowed_argument_patterns: tuple[re.Pattern, ...] = ()
    denied_argument_patterns: tuple[re.Pattern, ...] = ()
    default_timeout: float = 30.0
    allowed_env_vars: frozenset[str] = field(default_factory=frozenset)
    read_only: bool = False

    def is_executable_allowed(self, executable: str) -> bool:
        """Check if an executable is allowed by this profile."""
        if not self.allowed_executables:
            return True
        exe_name = Path(executable).name.lower()
        allowed_lower = {e.lower() for e in self.allowed_executables}
        return exe_name in allowed_lower or executable.lower() in allowed_lower

    def validate_arguments(self, args: Sequence[str]) -> tuple[bool, str]:
        """Validate argument list against allowed and denied patterns."""
        cmd_line = " ".join(args)
        for pattern in self.denied_argument_patterns:
            if pattern.search(cmd_line):
                return False, f"Arguments match denied pattern: {pattern.pattern}"

        if self.allowed_argument_patterns:
            matched = any(pattern.search(cmd_line) for pattern in self.allowed_argument_patterns)
            if not matched:
                return False, "Arguments do not match any allowed pattern in profile"

        return True, ""


# ---------------------------------------------------------------------------
# Built-in Command Profiles
# ---------------------------------------------------------------------------

SAFE_READ_ONLY_PROFILE = CommandProfile(
    name="safe_read_only",
    description="Read-only diagnostic, version query, and directory inspection commands",
    allowed_executables=frozenset({
        "git", "python", "python3", "node", "npm", "cargo", "go",
        "ls", "dir", "echo", "pwd", "whoami",
    }),
    denied_argument_patterns=(
        re.compile(r"\bpush\b", re.IGNORECASE),
        re.compile(r"\bcommit\b", re.IGNORECASE),
        re.compile(r"\bmerge\b", re.IGNORECASE),
        re.compile(r"\brebase\b", re.IGNORECASE),
        re.compile(r"\breset\b", re.IGNORECASE),
        re.compile(r"\binstall\b", re.IGNORECASE),
        re.compile(r"-c\b", re.IGNORECASE),
        re.compile(r"-e\b", re.IGNORECASE),
    ),
    default_timeout=15.0,
    read_only=True,
)

PROJECT_TEST_PROFILE = CommandProfile(
    name="project_test",
    description="Standard test suite execution within the project",
    allowed_executables=frozenset({
        "pytest", "python", "python3", "npm", "cargo", "go",
    }),
    default_timeout=120.0,
    read_only=False,
)


@dataclass
class TerminalCommandRequest:
    """Strongly typed execution request for a local terminal command."""
    executable: str
    args: Sequence[str] = field(default_factory=tuple)
    cwd: Path | str = "."
    request_id: str = field(default_factory=lambda: f"req_term_{uuid.uuid4().hex[:10]}")
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    profile_id: Optional[str] = None
    actor: str = "system"
    session_id: str = "session-system"
    lock_workspace: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.executable or not str(self.executable).strip():
            raise ValueError("Executable name or path cannot be empty")
        self.executable = str(self.executable).strip()
        self.args = tuple(str(a) for a in self.args)
        if isinstance(self.cwd, str):
            self.cwd = Path(self.cwd)
        if self.timeout <= 0:
            raise ValueError("Timeout must be a positive number")
        self.metadata = sanitize_metadata(self.metadata)

    def full_command_line(self) -> str:
        """Render a readable command representation for logging and authorization."""
        parts = [self.executable] + list(self.args)
        return " ".join(parts)


@dataclass
class TerminalCommandResult:
    """Structured execution result of a local terminal command."""
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stdout:
            self.stdout = redact_secrets(self.stdout)
        if self.stderr:
            self.stderr = redact_secrets(self.stderr)
        if self.error:
            self.error = redact_secrets(self.error)
        self.metadata = sanitize_metadata(self.metadata)

    @property
    def success(self) -> bool:
        """Check if command completed normally with exit code 0."""
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def to_dict(self) -> dict[str, Any]:
        """Serialize structured execution summary without secrets."""
        return {
            "exit_code": self.exit_code,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "stdout_len": len(self.stdout),
            "stderr_len": len(self.stderr),
            "error": self.error,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
        }
