"""MCP stdio transport implementation for SAMANVAYA · SŪTRA.

Enables secure, transport-level communication with a local MCP server process
via standard input and standard output streams:
    child process
        ├── stdin  ← SŪTRA writes JSON-RPC protocol messages
        ├── stdout → SŪTRA reads JSON-RPC protocol messages
        └── stderr → SŪTRA captures diagnostic logs (isolated from protocol)

Invariants:
1. Pure transport layer: Does NOT duplicate MCPClient, MCPSession, or Connector logic.
2. Secure process launch: No shell=True, arguments array preserved, no shell expansion.
3. Isolated stderr: Stderr is captured in bounded memory and never pollutes stdout.
4. Clean lifecycle: Graceful shutdown with forced termination fallback, zombie prevention.
5. Cancellation and timeout compatible: Propagates cancellation without leaking processes.
6. Secret redaction: No plaintext secrets in logs, ActivityStore, or metrics.
"""
from __future__ import annotations

import asyncio
import collections
import enum
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from sutra.activity_store import ActivityStore
from sutra.metrics import Metrics, get_metrics
from sutra.redaction import redact_secrets
from ..errors import (
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorProviderError,
    ConnectorTimeoutError,
)
from ..models import sanitize_metadata
from .transport import MCPTransport

log = logging.getLogger(__name__)

# Standard environment variables necessary for core language runtimes to function
_STANDARD_SYSTEM_ENV_VARS = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP",
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "HOME", "USERPROFILE",
    "USERNAME", "USER", "LANG", "LC_ALL", "TERM", "PATHEXT",
})


class MCPStdioState(str, enum.Enum):
    """Lifecycle states of the MCP stdio transport."""
    DISCONNECTED = "DISCONNECTED"
    STARTING = "STARTING"
    CONNECTED = "CONNECTED"
    CLOSING = "CLOSING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class MCPStdioConfig:
    """Strongly typed process configuration for local MCP stdio transport.
    
    Attributes:
        command: The explicit executable command to spawn.
        args: Command-line arguments passed as an array (preserves argv boundaries).
        env: Explicit environment variables to supply to the process.
        cwd: Optional working directory for the process.
        startup_timeout: Maximum seconds to wait for initial process startup.
        request_timeout: Default per-request timeout in seconds.
        shutdown_timeout: Grace period in seconds for graceful process exit on stdin EOF.
        kill_timeout: Timeout before sending SIGKILL if process ignores graceful termination.
        max_stderr_lines: Maximum number of recent stderr lines to retain in memory.
        max_stderr_bytes: Maximum total stderr bytes to retain.
        max_frame_bytes: Maximum allowed bytes for a single line on stdout (framing protection).
        clean_env: If True, strips inherited environment down to essential runtime keys.
        env_allowlist: Optional allowlist of environment variable names to pass through.
        capture_stderr: If True, reads and buffers stderr asynchronously.
    """
    command: str
    args: tuple[str, ...] = field(default_factory=tuple)
    env: Optional[Mapping[str, str]] = None
    cwd: Optional[Union[str, Path]] = None
    startup_timeout: float = 10.0
    request_timeout: float = 30.0
    shutdown_timeout: float = 5.0
    kill_timeout: float = 2.0
    max_stderr_lines: int = 200
    max_stderr_bytes: int = 65536
    max_frame_bytes: int = 10 * 1024 * 1024  # 10MB
    clean_env: bool = False
    env_allowlist: Optional[tuple[str, ...]] = None
    capture_stderr: bool = True

    def build_env(self) -> dict[str, str]:
        """Construct the sanitized environment dictionary for subprocess execution."""
        base_env: dict[str, str] = {}
        if self.clean_env:
            # Only keep essential system variables or allowlisted variables
            for k, v in os.environ.items():
                k_upper = k.upper()
                if k_upper in _STANDARD_SYSTEM_ENV_VARS or (self.env_allowlist and k in self.env_allowlist):
                    base_env[k] = v
        else:
            base_env = dict(os.environ)
            if self.env_allowlist is not None:
                allow_set = set(self.env_allowlist) | _STANDARD_SYSTEM_ENV_VARS
                base_env = {k: v for k, v in base_env.items() if k.upper() in allow_set or k in allow_set}

        # Apply explicitly configured environment overrides
        if self.env:
            base_env.update(self.env)

        return base_env

    def sanitized_dict(self) -> dict[str, Any]:
        """Return a safe representation of configuration without leaking sensitive secrets."""
        sanitized_env = sanitize_metadata(dict(self.env)) if self.env else {}
        return {
            "command": self.command,
            "args": list(self.args),
            "cwd": str(self.cwd) if self.cwd else None,
            "env": sanitized_env,
            "startup_timeout": self.startup_timeout,
            "request_timeout": self.request_timeout,
            "shutdown_timeout": self.shutdown_timeout,
            "kill_timeout": self.kill_timeout,
            "max_stderr_lines": self.max_stderr_lines,
            "clean_env": self.clean_env,
        }


async def _safe_terminate_process(
    proc: asyncio.subprocess.Process,
    shutdown_timeout: float = 5.0,
    kill_timeout: float = 2.0,
) -> int:
    """Platform-neutral graceful process termination and process-tree cleanup.
    
    1. Closes stdin to signal EOF to the process.
    2. Waits up to shutdown_timeout for natural process exit.
    3. If still active, invokes terminate() (SIGTERM on POSIX, TerminateProcess on Windows).
    4. Waits up to kill_timeout.
    5. If still active, invokes kill() (SIGKILL on POSIX) or process-tree taskkill on Windows.
    6. Ensures proc.wait() completes to reap zombie processes.
    """
    if proc.returncode is not None:
        return proc.returncode

    # 1. Signal EOF on stdin
    if proc.stdin and not proc.stdin.is_closing():
        try:
            proc.stdin.close()
            await proc.stdin.wait_closed()
        except Exception:
            pass

    # 2. Wait for natural exit
    try:
        return await asyncio.wait_for(proc.wait(), timeout=shutdown_timeout)
    except asyncio.TimeoutError:
        pass

    # 3. Request graceful termination
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            return 0
        except Exception as exc:
            log.debug("Process terminate() signal failed: %s", exc)

    try:
        return await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
    except asyncio.TimeoutError:
        pass

    # 4. Forced kill fallback
    if proc.returncode is None:
        try:
            if sys.platform == "win32":
                # On Windows, try taskkill to clean up any spawned child processes
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
            log.warning("Forced kill of process PID %s failed: %s", proc.pid, exc)

    # 5. Reap process
    try:
        return await proc.wait()
    except Exception:
        return proc.returncode or -1


class StdioMCPTransport(MCPTransport):
    """Concrete Model Context Protocol stdio transport over child process streams.
    
    Manages process startup, line-delimited JSON-RPC message framing, bounded
    stderr diagnostics, cancellation, timeout detection, and graceful teardown.
    """

    def __init__(
        self,
        config: MCPStdioConfig,
        *,
        connector_id: str = "mcp.stdio",
        activity_store: Optional[ActivityStore] = None,
        metrics: Optional[Metrics] = None,
        on_notification: Optional[Callable[[str, Optional[dict[str, Any]]], None]] = None,
    ) -> None:
        self._config = config
        self._connector_id = connector_id
        self._activity_store = activity_store
        self._metrics = metrics or get_metrics()
        self._on_notification = on_notification

        self._state = MCPStdioState.DISCONNECTED
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_counter: int = 0
        self._pending_requests: dict[Union[int, str], asyncio.Future[dict[str, Any]]] = {}

        self._stdout_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._monitor_task: Optional[asyncio.Task[None]] = None

        self._stderr_buffer: collections.deque[str] = collections.deque(maxlen=config.max_stderr_lines)
        self._stderr_total_bytes: int = 0
        self._exit_code: Optional[int] = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> MCPStdioConfig:
        return self._config

    @property
    def state(self) -> MCPStdioState:
        return self._state

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process else None

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    # -----------------------------------------------------------------------
    # MCPTransport Interface Implementation
    # -----------------------------------------------------------------------

    async def open(self) -> None:
        """Spawn the configured MCP server process and establish I/O pipelines."""
        async with self._lock:
            if self._state == MCPStdioState.CONNECTED and self._process is not None:
                return  # Idempotent double open

            self._metrics.record("mcp_stdio_start_total", 1)
            self._state = MCPStdioState.STARTING

            if not self._config.command or not self._config.command.strip():
                self._state = MCPStdioState.ERROR
                self._metrics.record("mcp_stdio_start_error_total", 1)
                raise ConnectorInvalidRequestError(
                    connector_id=self._connector_id,
                    message="Cannot start MCP process: command cannot be empty",
                )

            env = self._config.build_env()
            cwd = str(self._config.cwd) if self._config.cwd else None

            log.info(
                "Starting MCP stdio process for '%s': cmd='%s' args=%s cwd=%s",
                self._connector_id,
                self._config.command,
                self._config.args,
                cwd,
            )

            try:
                # Spawn subprocess with explicit args (never shell=True)
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        self._config.command,
                        *self._config.args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE if self._config.capture_stderr else asyncio.subprocess.DEVNULL,
                        env=env,
                        cwd=cwd,
                    ),
                    timeout=self._config.startup_timeout,
                )
                self._process = proc
                self._exit_code = None

            except FileNotFoundError as exc:
                self._state = MCPStdioState.ERROR
                self._metrics.record("mcp_stdio_start_error_total", 1)
                raise ConnectorNotFoundError(
                    connector_id=self._connector_id,
                    message=f"Executable '{self._config.command}' not found in PATH or directory",
                ) from exc

            except PermissionError as exc:
                self._state = MCPStdioState.ERROR
                self._metrics.record("mcp_stdio_start_error_total", 1)
                raise ConnectorConnectionError(
                    connector_id=self._connector_id,
                    message=f"Permission denied executing '{self._config.command}'",
                    cause=exc,
                ) from exc

            except asyncio.TimeoutError as exc:
                self._state = MCPStdioState.ERROR
                self._metrics.record("mcp_stdio_start_error_total", 1)
                self._metrics.record("mcp_stdio_timeout_total", 1)
                raise ConnectorTimeoutError(
                    connector_id=self._connector_id,
                    message=f"Timed out starting MCP process '{self._config.command}' after {self._config.startup_timeout}s",
                ) from exc

            except Exception as exc:
                self._state = MCPStdioState.ERROR
                self._metrics.record("mcp_stdio_start_error_total", 1)
                raise ConnectorConnectionError(
                    connector_id=self._connector_id,
                    message=f"Failed to spawn MCP process '{self._config.command}': {exc}",
                    cause=exc,
                ) from exc

            # Start reader loops and process exit monitor
            loop = asyncio.get_running_loop()
            self._stdout_task = loop.create_task(self._read_stdout_loop(), name=f"mcp-stdout-{self._connector_id}")
            if self._config.capture_stderr and self._process.stderr:
                self._stderr_task = loop.create_task(self._read_stderr_loop(), name=f"mcp-stderr-{self._connector_id}")
            self._monitor_task = loop.create_task(self._monitor_process_exit(), name=f"mcp-monitor-{self._connector_id}")

            self._state = MCPStdioState.CONNECTED
            self._metrics.record("mcp_stdio_start_success_total", 1)
            log.debug("MCP stdio process started successfully: PID=%s", self._process.pid)

    async def close(self) -> None:
        """Gracefully terminate child process and release all I/O streams."""
        async with self._lock:
            if self._state == MCPStdioState.DISCONNECTED and self._process is None:
                return  # Idempotent close

            self._state = MCPStdioState.CLOSING

            # Fail any in-flight pending requests
            for req_id, fut in list(self._pending_requests.items()):
                if not fut.done():
                    fut.set_exception(
                        ConnectorConnectionError(
                            connector_id=self._connector_id,
                            message="MCP stdio transport closed while request was in-flight",
                        )
                    )
            self._pending_requests.clear()

            # Terminate child process gracefully
            if self._process is not None:
                self._exit_code = await _safe_terminate_process(
                    self._process,
                    shutdown_timeout=self._config.shutdown_timeout,
                    kill_timeout=self._config.kill_timeout,
                )
                self._process = None

            # Cancel background tasks
            for task in (self._stdout_task, self._stderr_task, self._monitor_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

            self._stdout_task = None
            self._stderr_task = None
            self._monitor_task = None

            self._state = MCPStdioState.DISCONNECTED
            self._metrics.record("mcp_stdio_shutdown_total", 1)
            log.info("MCP stdio transport closed for '%s'", self._connector_id)

    def is_connected(self) -> bool:
        """Return True if the transport is open and the child process is active."""
        return (
            self._state == MCPStdioState.CONNECTED
            and self._process is not None
            and self._process.returncode is None
        )

    async def send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC 2.0 request over stdin and await correlated response from stdout."""
        if not self.is_connected() or self._process is None or self._process.stdin is None:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message=f"Cannot send '{method}': stdio transport is not connected (state={self._state.value})",
            )

        self._request_counter += 1
        req_id = self._request_counter
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        # Serialize line-delimited JSON-RPC (must not contain embedded newlines)
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")

        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._pending_requests.pop(req_id, None)
            self._state = MCPStdioState.ERROR
            tail = self.get_stderr_tail(10)
            err_msg = f"Failed writing to MCP stdin: {exc}" + (f" (stderr: {tail})" if tail else "")
            raise ConnectorConnectionError(
                connector_id=self._connector_id,
                message=err_msg,
                cause=exc,
            ) from exc

        effective_timeout = timeout if timeout is not None else self._config.request_timeout

        try:
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError as exc:
            self._pending_requests.pop(req_id, None)
            self._metrics.record("mcp_stdio_timeout_total", 1)
            tail = self.get_stderr_tail(10)
            err_msg = f"MCP stdio request '{method}' timed out after {effective_timeout:.1f}s"
            if tail:
                err_msg += f" (stderr: {tail})"
            raise ConnectorTimeoutError(
                connector_id=self._connector_id,
                message=err_msg,
            ) from exc
        except asyncio.CancelledError:
            self._pending_requests.pop(req_id, None)
            log.debug("MCP request '%s' (id=%s) cancelled by caller", method, req_id)
            raise

    async def send_notification(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        """Transmit a one-way notification without waiting for a response."""
        if not self.is_connected() or self._process is None or self._process.stdin is None:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message=f"Cannot send notification '{method}': stdio transport is not connected",
            )

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")

        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._state = MCPStdioState.ERROR
            raise ConnectorConnectionError(
                connector_id=self._connector_id,
                message=f"Failed writing notification '{method}' to MCP stdin: {exc}",
                cause=exc,
            ) from exc

    def transport_info(self) -> dict[str, Any]:
        """Return diagnostic info regarding transport configuration and process state."""
        return {
            "type": "StdioMCPTransport",
            "connected": self.is_connected(),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "config": self._config.sanitized_dict(),
            "stderr_lines_captured": len(self._stderr_buffer),
        }

    def get_stderr_tail(self, max_lines: int = 50) -> str:
        """Retrieve recent stderr lines joined into a sanitized string."""
        if not self._stderr_buffer:
            return ""
        lines = list(self._stderr_buffer)[-max_lines:]
        raw = "\n".join(lines).strip()
        return redact_secrets(raw)

    # -----------------------------------------------------------------------
    # Internal Asynchronous Stream Readers
    # -----------------------------------------------------------------------

    async def _read_stdout_loop(self) -> None:
        """Consume line-delimited JSON-RPC messages from stdout."""
        if not self._process or not self._process.stdout:
            return

        stdout = self._process.stdout
        while True:
            try:
                line_bytes = await stdout.readline()
            except (asyncio.CancelledError, Exception):
                break

            if not line_bytes:
                # stdout reached EOF (process terminated or closed stream)
                log.debug("MCP stdout reached EOF for '%s'", self._connector_id)
                break

            if len(line_bytes) > self._config.max_frame_bytes:
                log.error("MCP stdout frame exceeded max limit (%d bytes)", len(line_bytes))
                self._handle_framing_error("Stdout frame size exceeded maximum limit")
                break

            raw_str = line_bytes.decode("utf-8", errors="replace").strip()
            if not raw_str:
                continue

            try:
                msg = json.loads(raw_str)
            except Exception as exc:
                log.warning("Received malformed JSON on MCP stdout: '%s' (%s)", raw_str[:120], exc)
                self._handle_malformed_stdout(raw_str)
                continue

            if not isinstance(msg, dict):
                log.warning("Ignoring non-dict JSON message on stdout: %s", type(msg))
                continue

            # Route message by type
            if "id" in msg and msg["id"] is not None:
                self._dispatch_response(msg)
            elif "method" in msg:
                self._dispatch_incoming_notification(msg)

    def _dispatch_response(self, msg: dict[str, Any]) -> None:
        """Match response by ID and resolve correlated future."""
        req_id = msg.get("id")
        fut = self._pending_requests.pop(req_id, None)
        if fut is None or fut.done():
            log.debug("Received response for unknown or already completed request id=%s", req_id)
            return

        if "error" in msg:
            err = msg["error"]
            err_msg = err.get("message", "Unknown error") if isinstance(err, dict) else str(err)
            err_code = err.get("code") if isinstance(err, dict) else None
            fut.set_exception(
                ConnectorProviderError(
                    connector_id=self._connector_id,
                    message=f"MCP server error (code {err_code}): {err_msg}",
                    details={"error": err},
                )
            )
        else:
            fut.set_result(msg.get("result", {}))

    def _dispatch_incoming_notification(self, msg: dict[str, Any]) -> None:
        """Handle server-to-client notifications."""
        method = msg.get("method", "")
        params = msg.get("params")
        log.debug("Received MCP notification '%s' from '%s'", method, self._connector_id)
        if self._on_notification:
            try:
                self._on_notification(method, params)
            except Exception as exc:
                log.warning("Error in notification handler for '%s': %s", method, exc)

    async def _read_stderr_loop(self) -> None:
        """Asynchronously buffer and redact stderr diagnostic output."""
        if not self._process or not self._process.stderr:
            return

        stderr = self._process.stderr
        while True:
            try:
                line_bytes = await stderr.readline()
            except (asyncio.CancelledError, Exception):
                break

            if not line_bytes:
                break

            line_str = line_bytes.decode("utf-8", errors="replace").rstrip()
            if line_str:
                self._stderr_total_bytes += len(line_bytes)
                self._stderr_buffer.append(line_str)
                log.debug("[MCP %s stderr] %s", self._connector_id, redact_secrets(line_str))

    async def _monitor_process_exit(self) -> None:
        """Monitor process lifecycle; handle unexpected crashes or termination."""
        if not self._process:
            return

        try:
            code = await self._process.wait()
            self._exit_code = code
            log.info("MCP stdio process PID %s exited with code %s", self._process.pid, code)
        except asyncio.CancelledError:
            return

        # If process exited while transport expected to be connected, handle crash
        if self._state == MCPStdioState.CONNECTED:
            self._state = MCPStdioState.ERROR
            self._metrics.record("mcp_stdio_crash_total", 1)
            tail = self.get_stderr_tail(20)
            err_msg = f"MCP process crashed with exit code {code}"
            if tail:
                err_msg += f"\nStderr tail:\n{tail}"

            # Fail all waiting requests with ConnectionError
            for req_id, fut in list(self._pending_requests.items()):
                if not fut.done():
                    fut.set_exception(
                        ConnectorConnectionError(
                            connector_id=self._connector_id,
                            message=err_msg,
                        )
                    )
            self._pending_requests.clear()

    def _handle_malformed_stdout(self, line: str) -> None:
        """Handle non-JSON output on stdout."""
        # Fail any single in-flight request if exactly one request is pending
        if len(self._pending_requests) == 1:
            req_id, fut = next(iter(self._pending_requests.items()))
            if not fut.done():
                self._pending_requests.pop(req_id, None)
                fut.set_exception(
                    ConnectorProviderError(
                        connector_id=self._connector_id,
                        message=f"Received malformed protocol response on stdout: '{line[:100]}'",
                    )
                )

    def _handle_framing_error(self, message: str) -> None:
        """Handle fatal framing violation."""
        self._state = MCPStdioState.ERROR
        for req_id, fut in list(self._pending_requests.items()):
            if not fut.done():
                fut.set_exception(
                    ConnectorConnectionError(
                        connector_id=self._connector_id,
                        message=message,
                    )
                )
        self._pending_requests.clear()


# Canonical alias for compatibility
MCPStdioTransport = StdioMCPTransport
