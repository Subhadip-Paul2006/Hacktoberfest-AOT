"""Secure Local Terminal Connector conforming to the canonical SŪTRA Connector contract."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from sutra.activity_store import ActivityStore
from sutra.metrics import Metrics
from sutra.permissions import (
    CRITICAL_COMMAND_PATTERNS,
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
    PermissionRiskLevel,
)
from sutra.policy_engine import PolicyEngine
from sutra.redaction import redact_secrets
from sutra.security import _canonical
from sutra.workspace_lock import LockMode, WorkspaceLockManager

from ..errors import (
    ConnectorConnectionError,
    ConnectorError,
    ConnectorErrorCode,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorTimeoutError,
)
from ..interface import Connector
from ..models import (
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorHealth,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
    ConnectorToolDefinition,
    ConnectorType,
    sanitize_metadata,
)
from .models import (
    CommandProfile,
    SAFE_READ_ONLY_PROFILE,
    TerminalCommandRequest,
    TerminalCommandResult,
)
from .security import (
    build_controlled_environment,
    kill_process_tree,
    validate_argument_paths,
    validate_command_safety,
    validate_working_directory,
)

log = logging.getLogger(__name__)


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    max_bytes: int,
) -> tuple[bytes, bool]:
    """Drain a process stream while retaining at most the configured output size."""
    captured = bytearray()
    truncated = False
    while chunk := await stream.read(64 * 1024):
        available = max(0, max_bytes - len(captured))
        if available:
            captured.extend(chunk[:available])
        if len(chunk) > available:
            truncated = True
    return bytes(captured), truncated


DEFAULT_TERMINAL_CONNECTOR_ID = "terminal-local"
DEFAULT_CONCURRENCY_LIMIT = 5
DEFAULT_COMMAND_TIMEOUT = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576  # 1MB


class TerminalConnector(Connector):
    """Secure local terminal command execution connector for SŪTRA.
    
    Guarantees:
    1. NEVER shell=True. Always native asynchronous argument array execution.
    2. Strict working directory confinement within authorized workspace roots.
    3. Traversal, symlink escapes, and sensitive credential targeting rejected.
    4. PolicyEngine is the authoritative boundary for all command authorizations.
    5. Controlled minimum environment; secret-bearing parent variables scrubbed.
    6. Complete process tree termination on timeout or cancellation.
    7. Universal ActivityStore correlation and secret-redacted diagnostics.
    """

    def __init__(
        self,
        connector_id: str = DEFAULT_TERMINAL_CONNECTOR_ID,
        name: str = "Local Terminal",
        workspace_roots: Optional[Sequence[Path | str]] = None,
        default_cwd: Optional[Path | str] = None,
        allowed_executables: Optional[Sequence[str]] = None,
        command_profiles: Optional[dict[str, CommandProfile]] = None,
        concurrency_limit: int = DEFAULT_CONCURRENCY_LIMIT,
        default_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        policy_engine: Optional[Union[PolicyEngine, PermissionEngine]] = None,
        activity_store: Optional[ActivityStore] = None,
        workspace_lock_manager: Optional[WorkspaceLockManager] = None,
        metrics: Optional[Metrics] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._connector_id = connector_id.strip() or DEFAULT_TERMINAL_CONNECTOR_ID
        self._name = name
        self._concurrency_limit = max(1, concurrency_limit)
        self._semaphore = asyncio.Semaphore(self._concurrency_limit)
        self._default_timeout = default_timeout
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._status = ConnectorStatus.DISCONNECTED

        # Authorized workspace roots
        raw_roots = list(workspace_roots or [Path.cwd()])
        self._workspace_roots: list[Path] = [_canonical(r) for r in raw_roots]
        self._default_cwd = _canonical(default_cwd) if default_cwd else self._workspace_roots[0]

        # Allowed executables and command profiles
        self._allowed_executables = set(allowed_executables) if allowed_executables is not None else None
        self._command_profiles: dict[str, CommandProfile] = {
            "safe_read_only": SAFE_READ_ONLY_PROFILE,
            **(command_profiles or {}),
        }

        # Subsystem integrations
        self._policy_engine = policy_engine
        self._activity_store = activity_store
        self._lock_manager = workspace_lock_manager
        self._metrics = metrics or Metrics()
        self._custom_metadata = sanitize_metadata(metadata or {})

        # Active processes tracking for graceful shutdown
        self._active_processes: set[asyncio.subprocess.Process] = set()
        self._lifecycle_lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # Properties & Accessors
    # -----------------------------------------------------------------------

    @property
    def workspace_roots(self) -> tuple[Path, ...]:
        """Authorized workspace root directories."""
        return tuple(self._workspace_roots)

    @property
    def default_cwd(self) -> Path:
        """Default working directory within workspace."""
        return self._default_cwd

    @property
    def concurrency_limit(self) -> int:
        """Maximum concurrent command executions."""
        return self._concurrency_limit

    # -----------------------------------------------------------------------
    # Canonical Connector Interface
    # -----------------------------------------------------------------------

    def identity(self) -> ConnectorDescriptor:
        """Return immutable descriptor detailing identity, type, capabilities, and status."""
        caps = frozenset({
            ConnectorCapability.TOOL_EXECUTION,
            ConnectorCapability.HEALTH_CHECK,
            ConnectorCapability.READ,
            ConnectorCapability.WRITE,
            ConnectorCapability.DISCOVERY,
        })
        meta = {
            **self._custom_metadata,
            "workspace_roots": [str(r) for r in self._workspace_roots],
            "default_cwd": str(self._default_cwd),
            "concurrency_limit": self._concurrency_limit,
            "default_timeout": self._default_timeout,
            "transport_hint": "cli",
        }
        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=ConnectorType.CLI,
            provider="terminal",
            version="1.0.0",
            description="Local shell-less terminal command execution connector",
            capabilities=caps,
            status=self._status,
            metadata=meta,
            enabled=True,
        )

    def analysis_descriptor(self) -> ConnectorAnalysisDescriptor:
        """Return standardized descriptor for Generic Connector Analyzer."""
        desc = self.identity()
        binaries: list[str] = []
        if self._allowed_executables:
            binaries.extend(sorted(self._allowed_executables))
        else:
            binaries.extend(["python", "git"])

        return ConnectorAnalysisDescriptor(
            connector_id=desc.connector_id,
            name=desc.name,
            type=desc.type,
            provider=desc.provider,
            transport="cli",
            enabled=desc.enabled,
            status=desc.status,
            capabilities=desc.capabilities,
            version=desc.version,
            description=desc.description,
            declared_binary_names=tuple(binaries),
            metadata=dict(desc.metadata),
        )

    async def connect(self) -> None:
        """Verify workspace root validity and transition connector to READY."""
        async with self._lifecycle_lock:
            # Verify all workspace roots exist
            missing: list[str] = []
            for root in self._workspace_roots:
                if not root.exists() or not root.is_dir():
                    missing.append(str(root))

            if missing:
                self._status = ConnectorStatus.ERROR
                raise ConnectorConnectionError(
                    connector_id=self._connector_id,
                    message=f"Authorized workspace roots do not exist or are not directories: {', '.join(missing)}",
                )

            self._status = ConnectorStatus.READY
            log.info("TerminalConnector '%s' connected and READY (workspaces=%s)", self._connector_id, self._workspace_roots)

    async def disconnect(self) -> None:
        """Gracefully terminate any active subprocesses and mark DISCONNECTED."""
        async with self._lifecycle_lock:
            self._status = ConnectorStatus.DISCONNECTED
            # Terminate any running child processes
            active = list(self._active_processes)
            for proc in active:
                try:
                    await kill_process_tree(proc, term_timeout=0.5, kill_timeout=1.0)
                except Exception as exc:
                    log.debug("Error terminating process during disconnect: %s", exc)
            self._active_processes.clear()
            log.info("TerminalConnector '%s' disconnected", self._connector_id)

    async def health(self) -> ConnectorHealth:
        """Perform health diagnostic check verifying workspace accessibility."""
        if self._status != ConnectorStatus.READY:
            return ConnectorHealth(
                status=self._status,
                healthy=False,
                message=f"Terminal connector is not ready ({self._status.value})",
            )

        t0 = time.time()
        for root in self._workspace_roots:
            if not root.exists() or not root.is_dir():
                return ConnectorHealth(
                    status=ConnectorStatus.DEGRADED,
                    healthy=False,
                    message=f"Workspace root inaccessible: {root}",
                )

        latency = (time.time() - t0) * 1000.0
        return ConnectorHealth(
            status=ConnectorStatus.READY,
            healthy=True,
            latency_ms=latency,
            message="Terminal connector workspaces verified and operational",
            details={
                "workspace_count": len(self._workspace_roots),
                "active_processes": len(self._active_processes),
            },
        )

    async def discover(self) -> Sequence[ConnectorToolDefinition]:
        """Discover tools exposed by the terminal connector."""
        self.require_capability(ConnectorCapability.DISCOVERY)
        return (
            ConnectorToolDefinition(
                name="execute",
                description="Execute an authorized command in the local workspace",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "executable": {"type": "string", "description": "Binary or script name to run"},
                        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments"},
                        "cwd": {"type": "string", "description": "Working directory path within workspace"},
                        "timeout": {"type": "number", "description": "Execution timeout in seconds"},
                    },
                    "required": ["executable"],
                },
                read_only=False,
                risk_level="HIGH",
            ),
            ConnectorToolDefinition(
                name="query_status",
                description="Query terminal connector workspace and process status",
                parameters_schema={"type": "object"},
                read_only=True,
                risk_level="LOW",
            ),
        )

    # -----------------------------------------------------------------------
    # Command Execution Flow
    # -----------------------------------------------------------------------

    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        """Execute a local terminal command with strict security and auditing."""
        self.require_capability(ConnectorCapability.TOOL_EXECUTION)
        if self._status != ConnectorStatus.READY:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message=f"Cannot execute command: connector '{self._connector_id}' is in state '{self._status.value}'",
            )

        # 1. Parse typed command parameters
        cmd_req = self._parse_request(request)

        # 2. Workspace boundary validation
        try:
            resolved_cwd = validate_working_directory(cmd_req.cwd, self._workspace_roots)
        except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            raise ConnectorInvalidRequestError(
                connector_id=self._connector_id,
                message=f"Invalid working directory '{cmd_req.cwd}': {exc}",
            ) from exc

        # 3. Argument boundary and shell injection validation
        try:
            validate_command_safety(cmd_req.executable, cmd_req.args)
            validate_argument_paths(cmd_req.args, resolved_cwd, self._workspace_roots)
        except (ValueError, TypeError, PermissionError) as exc:
            raise ConnectorPermissionError(
                connector_id=self._connector_id,
                message=f"Command validation failed: {exc}",
            ) from exc

        # 4. Check executable allowlist if configured
        if self._allowed_executables is not None:
            exe_name = Path(cmd_req.executable).name.lower()
            allowed_names = {e.lower() for e in self._allowed_executables}
            if exe_name not in allowed_names and cmd_req.executable.lower() not in allowed_names:
                raise ConnectorPermissionError(
                    connector_id=self._connector_id,
                    message=f"Executable '{cmd_req.executable}' is not in allowed executable set: {sorted(self._allowed_executables)}",
                )

        # 5. Check command profile if specified
        if cmd_req.profile_id and cmd_req.profile_id in self._command_profiles:
            profile = self._command_profiles[cmd_req.profile_id]
            if not profile.is_executable_allowed(cmd_req.executable):
                raise ConnectorPermissionError(
                    connector_id=self._connector_id,
                    message=f"Executable '{cmd_req.executable}' not permitted by profile '{cmd_req.profile_id}'",
                )
            ok_args, err_args = profile.validate_arguments(cmd_req.args)
            if not ok_args:
                raise ConnectorPermissionError(
                    connector_id=self._connector_id,
                    message=f"Arguments rejected by profile '{cmd_req.profile_id}': {err_args}",
                )

        # 6. PolicyEngine Risk Assessment & Authorization Boundary
        await self._enforce_policy(cmd_req, request)

        # 7. Audit start in ActivityStore
        tc_id: Optional[str] = None
        if self._activity_store is not None:
            try:
                tc_id = self._activity_store.record_tool_call(
                    request_id=request.request_id,
                    connector_id=self._connector_id,
                    tool_name=f"{self._connector_id}.execute",
                    arguments={
                        "executable": cmd_req.executable,
                        "args": [redact_secrets(a) for a in cmd_req.args],
                        "cwd": str(resolved_cwd),
                        "timeout": cmd_req.timeout,
                    },
                    status="running",
                )
            except Exception as exc:
                log.warning("Failed to record tool call start in ActivityStore: %s", exc)

        # 8. Dispatch subprocess through concurrency semaphore and workspace locks
        t0 = time.time()
        acquired_lock = False
        try:
            # Acquire workspace lock if requested or write operation
            if self._lock_manager is not None and cmd_req.lock_workspace:
                try:
                    await self._lock_manager.acquire(
                        target=resolved_cwd,
                        session_id=cmd_req.session_id,
                        mode=LockMode.WRITE,
                        is_dir=True,
                    )
                    acquired_lock = True
                except Exception as exc:
                    raise ConnectorError(
                        connector_id=self._connector_id,
                        message=f"Workspace lock acquisition failed on '{resolved_cwd}': {exc}",
                        code=ConnectorErrorCode.PROVIDER_ERROR,
                        cause=exc,
                    ) from exc

            async with self._semaphore:
                res = await self._run_subprocess(cmd_req, resolved_cwd)

            duration_ms = (time.time() - t0) * 1000.0
            res.duration_ms = duration_ms

            # 9. Audit completion in ActivityStore
            if self._activity_store is not None and tc_id:
                try:
                    self._activity_store.update_tool_call(
                        tool_call_id=tc_id,
                        result=res.to_dict(),
                        status="success" if res.success else "failed",
                        duration_ms=duration_ms,
                    )
                except Exception as exc:
                    log.warning("Failed to update tool call result in ActivityStore: %s", exc)

            self._metrics.record("terminal_commands_executed", 1)

            # Map to ConnectorResult
            if res.success:
                return ConnectorResult.ok(
                    content=res.stdout,
                    structured_data=res.to_dict(),
                    metadata={
                        "request_id": request.request_id,
                        "duration_ms": duration_ms,
                        "exit_code": res.exit_code,
                    },
                    truncated=res.truncated,
                )
            else:
                return ConnectorResult(
                    success=False,
                    content=res.stdout if res.stdout else None,
                    structured_data=res.to_dict(),
                    metadata={
                        "request_id": request.request_id,
                        "duration_ms": duration_ms,
                        "exit_code": res.exit_code,
                        "timed_out": res.timed_out,
                    },
                    error=res.stderr or res.error or f"Command failed with exit code {res.exit_code}",
                    truncated=res.truncated,
                )

        finally:
            if acquired_lock and self._lock_manager is not None:
                try:
                    await self._lock_manager.release(resolved_cwd, cmd_req.session_id)
                except Exception as exc:
                    log.warning("Failed to release workspace lock on %s: %s", resolved_cwd, exc)

    # -----------------------------------------------------------------------
    # Subprocess Core Execution
    # -----------------------------------------------------------------------

    async def _run_subprocess(
        self,
        cmd_req: TerminalCommandRequest,
        resolved_cwd: Path,
    ) -> TerminalCommandResult:
        """Spawn and supervise child process using native asyncio.create_subprocess_exec."""
        # Find executable path or name
        exe_path = shutil.which(cmd_req.executable) or cmd_req.executable
        if not shutil.which(cmd_req.executable) and not Path(cmd_req.executable).is_file():
            # Executable not found
            raise ConnectorNotFoundError(
                connector_id=self._connector_id,
                message=f"Executable '{cmd_req.executable}' not found on host path or filesystem",
            )

        controlled_env = build_controlled_environment(cmd_req.env)

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe_path,
                *cmd_req.args,
                cwd=str(resolved_cwd),
                env=controlled_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ConnectorNotFoundError(
                connector_id=self._connector_id,
                message=f"Executable '{cmd_req.executable}' could not be executed: {exc}",
            ) from exc
        except PermissionError as exc:
            raise ConnectorPermissionError(
                connector_id=self._connector_id,
                message=f"Permission denied executing '{cmd_req.executable}': {exc}",
            ) from exc
        except Exception as exc:
            raise ConnectorError(
                connector_id=self._connector_id,
                message=f"Failed to spawn process '{cmd_req.executable}': {exc}",
                cause=exc,
            ) from exc

        self._active_processes.add(proc)
        stdout_task: Optional[asyncio.Task[tuple[bytes, bool]]] = None
        stderr_task: Optional[asyncio.Task[tuple[bytes, bool]]] = None
        try:
            if proc.stdout is None or proc.stderr is None:
                raise ConnectorError(
                    connector_id=self._connector_id,
                    message="Subprocess output streams were not created",
                )
            stdout_task = asyncio.create_task(_read_bounded_stream(proc.stdout, self._max_stdout_bytes))
            stderr_task = asyncio.create_task(_read_bounded_stream(proc.stderr, self._max_stderr_bytes))

            # Drain both output streams concurrently so neither pipe can block the process.
            await asyncio.wait_for(
                asyncio.gather(proc.wait(), stdout_task, stderr_task),
                timeout=cmd_req.timeout,
            )
            stdout_data, stdout_truncated = stdout_task.result()
            stderr_data, stderr_truncated = stderr_task.result()

            out_text = stdout_data.decode("utf-8", errors="replace")
            err_text = stderr_data.decode("utf-8", errors="replace")

            return TerminalCommandResult(
                exit_code=proc.returncode if proc.returncode is not None else 0,
                stdout=out_text,
                stderr=err_text,
                truncated=stdout_truncated or stderr_truncated,
            )

        except asyncio.TimeoutError:
            log.warning("Command '%s' timed out after %ss; terminating process PID %s", cmd_req.executable, cmd_req.timeout, proc.pid)
            await kill_process_tree(proc)
            raise ConnectorTimeoutError(
                connector_id=self._connector_id,
                message=f"Command '{cmd_req.full_command_line()}' timed out after {cmd_req.timeout}s",
            )

        except asyncio.CancelledError:
            log.info("Command '%s' cancelled; terminating process PID %s", cmd_req.executable, proc.pid)
            await kill_process_tree(proc)
            raise

        except Exception:
            await kill_process_tree(proc)
            raise

        finally:
            for stream_task in (stdout_task, stderr_task):
                if stream_task is not None and not stream_task.done():
                    stream_task.cancel()
            pending_stream_tasks = [
                stream_task for stream_task in (stdout_task, stderr_task)
                if stream_task is not None
            ]
            if pending_stream_tasks:
                await asyncio.gather(*pending_stream_tasks, return_exceptions=True)
            self._active_processes.discard(proc)

    # -----------------------------------------------------------------------
    # Policy Evaluation Helper
    # -----------------------------------------------------------------------

    async def _enforce_policy(
        self,
        cmd_req: TerminalCommandRequest,
        raw_request: ConnectorRequest,
    ) -> None:
        """Evaluate centralized PolicyEngine permission boundary before spawning process."""
        if self._policy_engine is None:
            return

        cmd_line = cmd_req.full_command_line()

        # 1. Determine risk level
        risk_level = self._assess_command_risk(cmd_req)

        # 2. Select appropriate action_type
        is_read_only = (risk_level == PermissionRiskLevel.LOW)
        action_type = "tool_read" if is_read_only else "terminal_command"

        perm_req = PermissionRequest(
            request_id=raw_request.request_id,
            action_type=action_type,
            resource_path=f"{self._connector_id}:{cmd_req.executable}",
            command=cmd_line,
            agent_id=cmd_req.actor or "agent-system",
            session_id=cmd_req.session_id or "session-system",
            risk_level=risk_level,
            description=f"Local terminal command execution: '{cmd_line}'",
        )

        allowed, reason = self._policy_engine.check_permission(
            perm_req,
            workspace_path=str(cmd_req.cwd),
        )

        if not allowed:
            raise ConnectorPermissionError(
                connector_id=self._connector_id,
                message=f"Terminal command '{cmd_line}' rejected by permission policy: {reason}",
                details={"command": cmd_line, "risk_level": risk_level.value},
            )

    def _assess_command_risk(self, cmd_req: TerminalCommandRequest) -> PermissionRiskLevel:
        """Classify command risk level ensuring CRITICAL invariants cannot be bypassed."""
        cmd_line = cmd_req.full_command_line()

        # Check CRITICAL patterns first
        for pat in CRITICAL_COMMAND_PATTERNS:
            if pat.search(cmd_line):
                return PermissionRiskLevel.CRITICAL

        # Check if matched by safe read-only profile
        if cmd_req.profile_id == "safe_read_only":
            return PermissionRiskLevel.LOW

        # Known safe read-only queries with no mutating flags
        exe_lower = Path(cmd_req.executable).name.lower()
        args = list(cmd_req.args)
        if exe_lower in ("git", "git.exe"):
            if args and args[0] in ("status", "diff", "log", "branch", "show", "tag", "--version"):
                # Ensure no mutating subcommands or flags
                if not any(a in ("--force", "-f", "-i", "--interactive") for a in args):
                    return PermissionRiskLevel.LOW

        if exe_lower in ("python", "python.exe", "python3", "python3.exe", "node", "node.exe"):
            if len(args) == 1 and args[0] in ("--version", "-V", "-v"):
                return PermissionRiskLevel.LOW

        if exe_lower in ("pytest", "pytest.exe"):
            if len(args) == 1 and args[0] in ("--version", "-V"):
                return PermissionRiskLevel.LOW

        if exe_lower in ("ls", "dir", "echo", "pwd", "whoami") and not any(a.startswith("-r") for a in args):
            return PermissionRiskLevel.LOW

        # All other terminal commands require authorization (HIGH risk)
        return PermissionRiskLevel.HIGH

    # -----------------------------------------------------------------------
    # Request Parsing Helper
    # -----------------------------------------------------------------------

    def _parse_request(self, request: ConnectorRequest) -> TerminalCommandRequest:
        """Translate normalized ConnectorRequest into TerminalCommandRequest."""
        params = request.parameters

        # If parameters directly contain executable / args
        if "executable" in params:
            executable = str(params["executable"])
            args = list(params.get("args") or [])
        elif request.operation.startswith("tool:"):
            # e.g. tool:execute
            executable = str(params.get("executable") or "")
            args = list(params.get("args") or [])
        elif request.operation.startswith("exec:"):
            executable = request.operation[5:]
            args = list(params.get("args") or [])
        else:
            executable = request.operation
            args = list(params.get("args") or [])

        cwd = params.get("cwd") or self._default_cwd
        env = dict(params.get("env") or {})
        timeout = float(params.get("timeout") or self._default_timeout)
        profile_id = params.get("profile_id")
        lock_workspace = bool(params.get("lock_workspace", False))

        return TerminalCommandRequest(
            executable=executable,
            args=args,
            cwd=cwd,
            request_id=request.request_id,
            env=env,
            timeout=timeout,
            profile_id=profile_id,
            actor=request.actor or "system",
            session_id=request.session_id or "session-system",
            lock_workspace=lock_workspace,
            metadata=request.metadata,
        )
