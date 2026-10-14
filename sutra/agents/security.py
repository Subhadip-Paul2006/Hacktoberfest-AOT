"""Security guards for project scope validation and safe process execution."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..security import is_path_allowed
from .errors import InvalidProjectScopeError

# Common sensitive environment variable name patterns to redact or exclude from subprocesses
SENSITIVE_ENV_PATTERNS = re.compile(
    r"(?i)(token|secret|password|key|cred|auth|bearer|cert|private|passphrase)"
)

# Forbidden shell chaining tokens when inspecting process argument lists
FORBIDDEN_SHELL_TOKENS = {";", "&&", "||", "|", ">", ">>", "<"}

from ..redaction import (
    PRIVATE_KEY_BLOCK,
    SECRET_ASSIGNMENT,
    TOKEN_SIGNATURES as SECRET_PREFIX,
    redact_secrets,
)


def validate_project_scope(project_root: str | Path, authorized_roots: Sequence[str | Path]) -> Path:
    """Validate that a project directory exists, is canonicalized, and strictly resides within authorized workspace roots.
    
    Prevents directory traversal, symlink escapes, and bare drive root targeting.
    """
    if not project_root:
        raise InvalidProjectScopeError("Project root cannot be empty")

    try:
        candidate = Path(project_root).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise InvalidProjectScopeError(f"Project path inaccessible or not found: {exc}") from exc

    if not candidate.is_dir():
        raise InvalidProjectScopeError(f"Project path is not a directory: {candidate}")

    # Prevent targeting the root of a drive directly (e.g. D:\ or /)
    if candidate.parent == candidate:
        raise InvalidProjectScopeError(
            f"Bare drive or filesystem root '{candidate}' cannot be targeted as a project scope"
        )

    # Check that candidate path is contained within at least one authorized access root
    if not is_path_allowed(candidate, authorized_roots):
        raise InvalidProjectScopeError(
            f"Project root '{candidate}' is not contained within any registered workspace root"
        )

    return candidate


@dataclass
class SafeProcessConfig:
    """Configuration for launching subprocesses with strict arguments, path containment, and environment sanitization."""
    args: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Enforce command execution security rules."""
        if not isinstance(self.args, list) or not self.args:
            raise ValueError("Subprocess args must be a non-empty list of strings")

        for idx, arg in enumerate(self.args):
            if not isinstance(arg, str):
                raise TypeError(f"Argument at index {idx} is not a string: {type(arg).__name__}")
            if any(token in arg.split() for token in FORBIDDEN_SHELL_TOKENS):
                raise ValueError(f"Argument at index {idx} contains forbidden shell chaining operators: {arg!r}")

        if not isinstance(self.cwd, Path):
            self.cwd = Path(self.cwd)

        if not self.cwd.is_dir():
            raise ValueError(f"Subprocess working directory is not a directory: {self.cwd}")

        if not 0.1 <= self.timeout_seconds <= 3600.0:
            raise ValueError(f"Subprocess timeout out of bounds (0.1 to 3600s): {self.timeout_seconds}")

    def build_sanitized_env(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        """Produce an environment dict stripped of local SŪTRA credentials and secrets."""
        source = dict(base_env if base_env is not None else os.environ)
        # Strip out sensitive patterns
        sanitized = {
            k: v for k, v in source.items()
            if not SENSITIVE_ENV_PATTERNS.search(k)
        }
        # Overlay explicitly configured environment variables
        sanitized.update(self.env)
        return sanitized
