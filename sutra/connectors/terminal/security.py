"""Security boundaries, shell injection blockers, path guards, and process cleanup for TerminalConnector."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from sutra.redaction import redact_secrets
from sutra.security import _canonical, is_path_allowed

log = logging.getLogger(__name__)

# Forbidden shell chaining and redirection tokens
FORBIDDEN_SHELL_TOKENS = {
    ";", "&&", "||", "|", ">", ">>", "<", "&",
    "`", "$(", "${", "\n", "\r",
}

# Regex to detect dangerous subshell or redirection constructs inside an argument
FORBIDDEN_ARGUMENT_REGEX = re.compile(
    r"(?:[;&|<>`]|(\$\([^\)]*\))|(\$\{[^\}]*\})|(\r?\n))"
)

# Sensitive environment variable name patterns
SENSITIVE_ENV_PATTERNS = re.compile(
    r"(?i)(token|secret|password|key|cred|auth|bearer|cert|private|passphrase|sutra_|github_)"
)

# Standard baseline environment variables permitted by platform
SAFE_ENV_WHITELIST_NT = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "WINDIR",
})

SAFE_ENV_WHITELIST_POSIX = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM",
})

# Known sensitive filesystem directories that should not be targeted as argument destinations
SENSITIVE_TARGET_DIRS = (
    ".ssh", ".aws", ".gnupg", ".docker", ".kube",
    "id_rsa", "id_ed25519", "credentials", "config.json",
)


def validate_command_safety(executable: str, args: Sequence[str]) -> None:
    """Enforce strict shell-less argument boundary validation.
    
    Rejects:
    1. Shell chaining operators (;, &&, ||, |, >, <)
    2. Embedded newlines or command substitutions ($(), `)
    3. Shell interpreters being passed inline execution flags (bash -c, cmd /c, powershell -c)
    """
    if not executable or not executable.strip():
        raise ValueError("Executable cannot be empty")

    exe_clean = executable.strip()
    exe_name = Path(exe_clean).name.lower()

    # Reject shell interpreters used as wrappers for arbitrary shell strings
    if exe_name in ("bash", "sh", "zsh", "dash", "ksh") and any(a in ("-c", "-s") for a in args):
        raise ValueError(f"Direct execution of shell strings via '{exe_name} -c' is prohibited in TerminalConnector")

    if exe_name in ("cmd", "cmd.exe") and any(a.lower() in ("/c", "/k") for a in args):
        raise ValueError("Direct execution of command strings via 'cmd /c' is prohibited in TerminalConnector")

    if exe_name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        for a in args:
            a_lower = a.lower()
            if a_lower in ("-c", "-command", "-encodedcommand"):
                raise ValueError("Direct execution of shell strings via PowerShell is prohibited in TerminalConnector")

    # Check executable itself for forbidden tokens
    for token in FORBIDDEN_SHELL_TOKENS:
        if token in exe_clean:
            raise ValueError(f"Executable contains forbidden shell token '{token}': {executable!r}")

    # Check each argument individually
    for idx, arg in enumerate(args):
        if not isinstance(arg, str):
            raise TypeError(f"Argument at index {idx} must be a string, got {type(arg).__name__}")

        # Check for forbidden operators
        for token in FORBIDDEN_SHELL_TOKENS:
            if token in arg:
                raise ValueError(
                    f"Argument at index {idx} contains forbidden shell token '{token}': {arg!r}"
                )

        if FORBIDDEN_ARGUMENT_REGEX.search(arg):
            raise ValueError(
                f"Argument at index {idx} contains forbidden shell chaining or substitution syntax: {arg!r}"
            )


def validate_working_directory(cwd: Path | str, authorized_workspaces: Sequence[Path | str]) -> Path:
    """Ensure working directory exists, is canonicalized, and resides strictly inside authorized workspaces."""
    if not cwd:
        raise ValueError("Working directory cannot be empty")

    try:
        resolved_cwd = _canonical(cwd)
    except Exception as exc:
        raise ValueError(f"Working directory path could not be resolved safely: {cwd} ({exc})") from exc

    if not resolved_cwd.exists():
        raise FileNotFoundError(f"Working directory does not exist: {resolved_cwd}")

    if not resolved_cwd.is_dir():
        raise NotADirectoryError(f"Working directory is not a directory: {resolved_cwd}")

    # Prevent targeting root filesystem directly
    if resolved_cwd.parent == resolved_cwd:
        raise ValueError(f"Filesystem root '{resolved_cwd}' cannot be used as terminal working directory")

    if not is_path_allowed(resolved_cwd, list(authorized_workspaces)):
        raise PermissionError(
            f"Working directory '{resolved_cwd}' is outside authorized workspace roots: "
            f"{[str(w) for w in authorized_workspaces]}"
        )

    return resolved_cwd


def validate_argument_paths(
    args: Sequence[str],
    cwd: Path,
    authorized_workspaces: Sequence[Path | str],
) -> None:
    """Inspect arguments for path traversal escapes or attempts to target sensitive credential files."""
    for idx, arg in enumerate(args):
        # Check for targeting known sensitive credential stores
        arg_lower = arg.lower()
        for sensitive in SENSITIVE_TARGET_DIRS:
            if sensitive in arg_lower:
                # If it looks like a path targeting sensitive credentials
                if (
                    arg_lower.endswith(sensitive)
                    or f"/{sensitive}" in arg_lower
                    or f"\\{sensitive}" in arg_lower
                ):
                    raise PermissionError(
                        f"Argument at index {idx} targets sensitive credential or key resource '{sensitive}': {arg!r}"
                    )

        # Check for directory traversal attempts
        if ".." in arg:
            # Test if resolving this relative path from cwd escapes the authorized workspace
            candidate: Optional[Path] = None
            try:
                candidate = (cwd / arg).resolve(strict=False)
            except Exception:
                candidate = None
            if candidate is not None and not is_path_allowed(candidate, list(authorized_workspaces)):
                raise PermissionError(
                    f"Argument at index {idx} traverses outside authorized workspaces: {arg!r}"
                )


def build_controlled_environment(
    custom_env: Optional[dict[str, str]] = None,
    allowed_extra_keys: Optional[Sequence[str]] = None,
) -> dict[str, str]:
    """Produce a sanitized subprocess environment stripped of parent secrets and credentials."""
    whitelist = SAFE_ENV_WHITELIST_NT if sys.platform == "win32" else SAFE_ENV_WHITELIST_POSIX
    allowed_extras = {k.upper() for k in (allowed_extra_keys or ())}

    controlled: dict[str, str] = {}
    for k, v in os.environ.items():
        k_upper = k.upper()
        # Allow standard system variables
        if k_upper in whitelist or k_upper in allowed_extras:
            # Double check it does not match secret patterns
            if not SENSITIVE_ENV_PATTERNS.search(k):
                controlled[k] = v

    # Overlay user-supplied custom environment variables
    if custom_env:
        for k, v in custom_env.items():
            if SENSITIVE_ENV_PATTERNS.search(k):
                log.warning("Excluding sensitive key '%s' from subprocess environment", k)
                continue
            controlled[k] = str(v)

    return controlled


async def kill_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    term_timeout: float = 1.0,
    kill_timeout: float = 1.5,
) -> int:
    """Safely terminate child process and all descendants, avoiding leaked or zombie processes."""
    if proc.returncode is not None:
        return proc.returncode

    # 1. Graceful terminate
    try:
        proc.terminate()
    except ProcessLookupError:
        return 0
    except Exception as exc:
        log.debug("Process terminate failed: %s", exc)

    try:
        return await asyncio.wait_for(proc.wait(), timeout=term_timeout)
    except asyncio.TimeoutError:
        pass

    # 2. Forceful kill
    if proc.returncode is None:
        try:
            if sys.platform == "win32":
                # On Windows, taskkill /F /T kills the entire process tree cleanly
                try:
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(proc.pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(killer.wait(), timeout=2.0)
                except Exception:
                    proc.kill()
            else:
                proc.kill()
        except ProcessLookupError:
            return 0
        except Exception as exc:
            log.warning("Force kill of process PID %s failed: %s", proc.pid, exc)

    # 3. Final wait
    try:
        return await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
    except Exception:
        return proc.returncode if proc.returncode is not None else -1
