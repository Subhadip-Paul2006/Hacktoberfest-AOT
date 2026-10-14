"""Terminal Connector package for SŪTRA."""
from __future__ import annotations

from .connector import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONCURRENCY_LIMIT,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TERMINAL_CONNECTOR_ID,
    TerminalConnector,
)
from .models import (
    CommandProfile,
    PROJECT_TEST_PROFILE,
    SAFE_READ_ONLY_PROFILE,
    TerminalCommandRequest,
    TerminalCommandResult,
)
from .security import (
    FORBIDDEN_SHELL_TOKENS,
    build_controlled_environment,
    kill_process_tree,
    validate_argument_paths,
    validate_command_safety,
    validate_working_directory,
)

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_CONCURRENCY_LIMIT",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TERMINAL_CONNECTOR_ID",
    "CommandProfile",
    "FORBIDDEN_SHELL_TOKENS",
    "PROJECT_TEST_PROFILE",
    "SAFE_READ_ONLY_PROFILE",
    "TerminalCommandRequest",
    "TerminalCommandResult",
    "TerminalConnector",
    "build_controlled_environment",
    "kill_process_tree",
    "validate_argument_paths",
    "validate_command_safety",
    "validate_working_directory",
]
