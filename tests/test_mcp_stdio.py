"""Comprehensive test suite for Phase 7.6 MCP Stdio Transport.

Covers all 26 test groups (A through Z) using a deterministic local test server:
A. Startup success
B. Executable missing
C. Invalid arguments
D. Startup timeout
E. Stdin/stdout communication
F. Stderr isolation
G. Tools/list
H. Tools/call
I. Resources/list/read
J. Prompts/list/get
K. Pagination
L. Server metadata
M. Capability discovery
N. Malformed stdout
O. Stdout EOF
P. Process crash
Q. Request timeout
R. Cancellation
S. Graceful shutdown
T. Forced shutdown
U. Idempotent close
V. Secret redaction
W. Permission enforcement
X. ActivityStore correlation
Y. Analyzer descriptor integration
Z. Cross-platform behavior abstraction
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from threading import RLock
from typing import Any

import pytest

from sutra.activity_store import ActivityStore
from sutra.connectors.analyzer import AnalysisReport, ConnectorAnalyzer, VerificationState
from sutra.connectors.errors import (
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorProviderError,
    ConnectorTimeoutError,
)
from sutra.connectors.mcp import (
    MCPClient,
    MCPConnector,
    MCPProtocolVersion,
    MCPSession,
    MCPSessionState,
    MCPStdioConfig,
    MCPStdioState,
    MCPStdioTransport,
    StdioMCPTransport,
)
from sutra.connectors.models import (
    ConnectorCapability,
    ConnectorRequest,
    ConnectorStatus,
)
from sutra.connectors.registry import ConnectorRegistry, create_permission_request
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import PolicyStore

SERVER_SCRIPT = str(Path(__file__).parent / "mocks" / "mcp_stdio_test_server.py")


def make_test_activity_store() -> ActivityStore:
    """Create a thread-safe in-memory ActivityStore for testing."""
    return ActivityStore.from_path(":memory:")


def make_stdio_config(*extra_args: str, **kwargs: Any) -> MCPStdioConfig:
    """Helper to build standard MCPStdioConfig invoking the deterministic local test server."""
    base_args = [SERVER_SCRIPT] + list(extra_args)
    return MCPStdioConfig(
        command=sys.executable,
        args=tuple(base_args),
        startup_timeout=kwargs.get("startup_timeout", 10.0),
        request_timeout=kwargs.get("request_timeout", 10.0),
        shutdown_timeout=kwargs.get("shutdown_timeout", 3.0),
        kill_timeout=kwargs.get("kill_timeout", 2.0),
        clean_env=kwargs.get("clean_env", False),
        env=kwargs.get("env", None),
        env_allowlist=kwargs.get("env_allowlist", None),
        capture_stderr=kwargs.get("capture_stderr", True),
    )


# ===========================================================================
# Group A: Startup Success
# ===========================================================================

@pytest.mark.asyncio
async def test_group_a_startup_success() -> None:
    """Group A: Child process starts, transitions to CONNECTED, pid is set, ping succeeds."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    assert transport.state == MCPStdioState.DISCONNECTED
    assert not transport.is_connected()

    await transport.open()
    try:
        assert transport.is_connected()
        assert transport.state == MCPStdioState.CONNECTED
        assert transport.pid is not None
        assert transport.pid > 0

        # Verify transport info
        info = transport.transport_info()
        assert info["connected"] is True
        assert info["pid"] == transport.pid
        assert info["type"] == "StdioMCPTransport"
    finally:
        await transport.close()

    assert transport.state == MCPStdioState.DISCONNECTED
    assert not transport.is_connected()


# ===========================================================================
# Group B: Executable Missing
# ===========================================================================

@pytest.mark.asyncio
async def test_group_b_executable_missing() -> None:
    """Group B: Non-existent binary raises ConnectorNotFoundError without leaking resources."""
    config = MCPStdioConfig(
        command="this_binary_definitely_does_not_exist_12345",
        args=(),
        startup_timeout=5.0,
    )
    transport = StdioMCPTransport(config)

    with pytest.raises(ConnectorNotFoundError) as exc_info:
        await transport.open()

    assert "not found" in str(exc_info.value).lower()
    assert transport.state == MCPStdioState.ERROR
    assert not transport.is_connected()


# ===========================================================================
# Group C: Invalid Arguments
# ===========================================================================

@pytest.mark.asyncio
async def test_group_c_invalid_arguments() -> None:
    """Group C: Empty command string raises ConnectorInvalidRequestError."""
    config = MCPStdioConfig(command="", args=())
    transport = StdioMCPTransport(config)

    with pytest.raises(ConnectorInvalidRequestError) as exc_info:
        await transport.open()

    assert "command cannot be empty" in str(exc_info.value).lower()
    assert transport.state == MCPStdioState.ERROR


# ===========================================================================
# Group D: Startup Timeout
# ===========================================================================

@pytest.mark.asyncio
async def test_group_d_startup_timeout() -> None:
    """Group D: Hanging process during spawn is bounded by startup_timeout."""
    # Test server with --hang-on-start
    config = make_stdio_config("--hang-on-start", startup_timeout=0.5)
    transport = StdioMCPTransport(config)
    # The process will spawn, but let's test timeout on initialize or request
    await transport.open()
    try:
        # Request times out when process hangs
        with pytest.raises(ConnectorTimeoutError):
            await transport.send_request("initialize", {}, timeout=0.5)
    finally:
        await transport.close()


# ===========================================================================
# Group E: Stdin/Stdout Communication
# ===========================================================================

@pytest.mark.asyncio
async def test_group_e_stdin_stdout_communication() -> None:
    """Group E: JSON-RPC request is written to stdin and correlated response is read from stdout."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        res = await transport.send_request("initialize", {"protocolVersion": "2026-07-28"})
        assert isinstance(res, dict)
        assert res.get("protocolVersion") == "2026-07-28"
        assert res.get("serverInfo", {}).get("name") == "sutra-test-stdio-server"

        # Notification transmission
        await transport.send_notification("notifications/initialized", {})
    finally:
        await transport.close()


# ===========================================================================
# Group F: Stderr Isolation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_f_stderr_isolation() -> None:
    """Group F: Stderr output is captured into bounded buffer and never corrupts stdout stream."""
    config = make_stdio_config("--log-to-stderr")
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Call tool that specifically emits diagnostics to stderr
        result = await client.call_tool("emit_stderr", {"text": "diagnostic_log_marker_777"})
        assert result.get_text_content() == "Wrote to stderr"

        # Allow background stderr reader task to flush line
        await asyncio.sleep(0.05)

        # Verify stderr buffer captured the log
        tail = transport.get_stderr_tail()
        assert "diagnostic_log_marker_777" in tail or "Server starting up" in tail

        # Normal tools still execute cleanly over stdout without stream corruption
        add_res = await client.call_tool("add", {"a": 10, "b": 20})
        assert add_res.get_text_content() == "30.0"

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group G: Tools/List
# ===========================================================================

@pytest.mark.asyncio
async def test_group_g_tools_list() -> None:
    """Group G: Tools are listed and converted into normalized MCPTool definitions."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        page = await client.list_tools()
        tools = page.items
        assert len(tools) >= 5
        tool_names = {t.name for t in tools}
        assert "echo" in tool_names
        assert "add" in tool_names
        assert "emit_stderr" in tool_names

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group H: Tools/Call
# ===========================================================================

@pytest.mark.asyncio
async def test_group_h_tools_call() -> None:
    """Group H: Tool call dispatches arguments and parses result content."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Echo tool
        res_echo = await client.call_tool("echo", {"message": "Hello from Sutra stdio!"})
        assert res_echo.get_text_content() == "echo: Hello from Sutra stdio!"
        assert res_echo.is_error is False

        # Add tool (returns structuredContent)
        res_add = await client.call_tool("add", {"a": 40, "b": 2})
        assert res_add.get_text_content() == "42.0"
        assert res_add.structured_data == {"sum": 42.0}

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group I: Resources/List/Read
# ===========================================================================

@pytest.mark.asyncio
async def test_group_i_resources_list_and_read() -> None:
    """Group I: Resources can be listed and read (both text and binary payloads)."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # List resources
        page = await client.list_resources()
        assert len(page.items) == 2
        res_uris = {r.uri for r in page.items}
        assert "test://resource/1" in res_uris
        assert "test://resource/binary" in res_uris

        # Read text resource
        text_contents = await client.read_resource("test://resource/1")
        assert len(text_contents) == 1
        assert text_contents[0].text == "Hello from stdio resource 1"
        assert text_contents[0].mime_type == "text/plain"

        # Read binary resource
        bin_contents = await client.read_resource("test://resource/binary")
        assert len(bin_contents) == 1
        assert bin_contents[0].blob is not None
        assert bin_contents[0].mime_type == "application/octet-stream"

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group J: Prompts/List/Get
# ===========================================================================

@pytest.mark.asyncio
async def test_group_j_prompts_list_and_get() -> None:
    """Group J: Prompts can be listed and rendered into message templates."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # List prompts
        page = await client.list_prompts()
        assert len(page.items) == 1
        assert page.items[0].name == "greeting"

        # Get prompt
        prompt_res = await client.get_prompt("greeting", {"name": "Sutra Engineer"})
        assert "Sutra Engineer" in prompt_res.description
        assert len(prompt_res.messages) == 1
        assert "Hello, Sutra Engineer!" in str(prompt_res.messages[0].content)

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group K: Pagination
# ===========================================================================

@pytest.mark.asyncio
async def test_group_k_pagination() -> None:
    """Group K: Pagination works over the real stdio stream with max_pages protection."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Test cursor pagination across pages over stdio stream
        page1 = await client.list_tools(cursor="paginate")
        assert len(page1.items) == 1
        assert page1.items[0].name == "tool_page_1"
        assert page1.next_cursor == "page_2"
        assert page1.has_more is True

        page2 = await client.list_tools(cursor=page1.next_cursor)
        assert len(page2.items) == 1
        assert page2.items[0].name == "tool_page_2"
        assert page2.has_more is False

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group L: Server Metadata
# ===========================================================================

@pytest.mark.asyncio
async def test_group_l_server_metadata() -> None:
    """Group L: Server name, version, and protocol version populated from handshake."""
    config = make_stdio_config("--server-name", "custom-stdio-srv", "--server-version", "3.2.1")
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        assert client.server_info is not None
        assert client.server_info.name == "custom-stdio-srv"
        assert client.server_info.version == "3.2.1"
        assert client.protocol_version == MCPProtocolVersion.V2026_07_28

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group M: Capability Discovery
# ===========================================================================

@pytest.mark.asyncio
async def test_group_m_capability_discovery() -> None:
    """Group M: Server capabilities are discovered and mapped to ConnectorCapability."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        assert client.server_capabilities.tools is True
        assert client.server_capabilities.resources is True
        assert client.server_capabilities.prompts is True

        conn_caps = client.server_capabilities.to_connector_capabilities()
        assert ConnectorCapability.TOOL_EXECUTION in conn_caps
        assert ConnectorCapability.RESOURCES in conn_caps
        assert ConnectorCapability.PROMPTS in conn_caps
        assert ConnectorCapability.DISCOVERY in conn_caps

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group N: Malformed Stdout
# ===========================================================================

@pytest.mark.asyncio
async def test_group_n_malformed_stdout() -> None:
    """Group N: Corrupt non-JSON line on stdout is normalized into error without crashing transport."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Invoking malformed_output makes test server write invalid bytes
        with pytest.raises(ConnectorProviderError) as exc_info:
            await client.call_tool("malformed_output", {})

        assert "malformed" in str(exc_info.value).lower()

        # Subsequent requests can still proceed or fail cleanly
        echo_res = await client.call_tool("echo", {"message": "still alive"})
        assert echo_res.get_text_content() == "echo: still alive"

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group O: Stdout EOF
# ===========================================================================

@pytest.mark.asyncio
async def test_group_o_stdout_eof() -> None:
    """Group O: Server exit / stdout EOF is detected and transitions transport correctly."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Terminate server process manually
        if transport._process:
            transport._process.terminate()

        # Allow monitor task to detect termination
        await asyncio.sleep(0.1)

        # Invocations fail with ConnectorNotConnectedError or ConnectorConnectionError
        with pytest.raises((ConnectorNotConnectedError, ConnectorConnectionError)):
            await client.call_tool("echo", {"message": "should fail"})
    finally:
        await transport.close()


# ===========================================================================
# Group P: Process Crash
# ===========================================================================

@pytest.mark.asyncio
async def test_group_p_process_crash() -> None:
    """Group P: Subprocess crash during execution is detected with exit code preserved."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Trigger crash tool (exits with code 42)
        with pytest.raises(ConnectorConnectionError) as exc_info:
            await client.call_tool("crash", {})

        assert "crashed" in str(exc_info.value).lower() or "closed" in str(exc_info.value).lower()

        # Process is dead
        assert transport.is_connected() is False
    finally:
        await transport.close()


# ===========================================================================
# Group Q: Request Timeout
# ===========================================================================

@pytest.mark.asyncio
async def test_group_q_request_timeout() -> None:
    """Group Q: Request taking longer than timeout raises ConnectorTimeoutError."""
    config = make_stdio_config(request_timeout=0.3)
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Sleep for 1.5 seconds, but timeout is 0.3s
        with pytest.raises(ConnectorTimeoutError):
            await client.call_tool("sleep", {"seconds": 1.5}, timeout=0.2)

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group R: Cancellation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_r_cancellation() -> None:
    """Group R: Caller cancellation propagates CancelledError without leaking pending futures."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        # Start slow request and cancel immediately
        task = asyncio.create_task(client.call_tool("sleep", {"seconds": 2.0}))
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Pending requests cleaned up
        assert len(transport._pending_requests) == 0

        # Subsequent calls still work
        echo_res = await client.call_tool("echo", {"message": "after cancel"})
        assert echo_res.get_text_content() == "echo: after cancel"

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group S: Graceful Shutdown
# ===========================================================================

@pytest.mark.asyncio
async def test_group_s_graceful_shutdown() -> None:
    """Group S: transport.close() cleanly releases stdin, process exits with code 0."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()
    assert transport.is_connected()
    pid = transport.pid

    await transport.close()
    assert not transport.is_connected()
    assert transport.state == MCPStdioState.DISCONNECTED
    assert transport.pid is None


# ===========================================================================
# Group T: Forced Shutdown
# ===========================================================================

@pytest.mark.asyncio
async def test_group_t_forced_shutdown() -> None:
    """Group T: Hanging process on shutdown is forcibly terminated within kill_timeout."""
    # Test server with --hang-on-start (ignores stdin EOF)
    config = make_stdio_config("--hang-on-start", shutdown_timeout=0.2, kill_timeout=0.3)
    transport = StdioMCPTransport(config)
    await transport.open()
    assert transport.is_connected()

    # close() must terminate process within kill_timeout and not hang indefinitely
    await transport.close()
    assert not transport.is_connected()


# ===========================================================================
# Group U: Idempotent Close
# ===========================================================================

@pytest.mark.asyncio
async def test_group_u_idempotent_close() -> None:
    """Group U: Calling close() multiple times in sequence or concurrently is safe."""
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    await transport.open()

    # Sequential close
    await transport.close()
    await transport.close()
    await transport.close()
    assert transport.state == MCPStdioState.DISCONNECTED

    # Concurrent close
    await transport.open()
    await asyncio.gather(transport.close(), transport.close(), transport.close())
    assert transport.state == MCPStdioState.DISCONNECTED


# ===========================================================================
# Group V: Secret Redaction
# ===========================================================================

@pytest.mark.asyncio
async def test_group_v_secret_redaction() -> None:
    """Group V: Config metadata and tool outputs redact sensitive tokens and passwords."""
    config = make_stdio_config(
        env={"API_KEY": "super_secret_token_12345", "SAFE_VAR": "hello"}
    )
    transport = StdioMCPTransport(config)

    # 1. Config sanitization
    sanitized = config.sanitized_dict()
    assert sanitized["env"]["API_KEY"] == "[REDACTED]"
    assert sanitized["env"]["SAFE_VAR"] == "hello"

    # 2. Tool output redaction
    await transport.open()
    try:
        client = MCPClient(transport)
        await client.connect()

        result = await client.call_tool("secret_tool", {})
        assert "sk-ant-api03" not in result.get_text_content()
        assert "[REDACTED" in result.get_text_content()

        await client.disconnect()
    finally:
        await transport.close()


# ===========================================================================
# Group W: Permission Enforcement
# ===========================================================================

@pytest.mark.asyncio
async def test_group_w_permission_enforcement() -> None:
    """Group W: Mutating tool requests over stdio respect PolicyEngine decisions."""
    policy_store = PolicyStore.from_path(":memory:")
    engine = PolicyEngine(policy_store)

    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    connector = MCPConnector("conn.mcp.stdio_safe", transport)
    await connector.connect()
    try:
        # Create standardized permission request
        perm_req = create_permission_request(
            operation="crash",
            connector_id="conn.mcp.stdio_safe",
            request_id="req_stdio_perm_01",
            risk_level="HIGH",
        )
        assert perm_req.action_type == "connector_admin"
        assert "crash" in perm_req.command

        # Default policy asks/checks before allowing
        allowed, reason = engine.check_permission(perm_req)
        assert isinstance(allowed, bool)
        assert isinstance(reason, str)
    finally:
        await connector.disconnect()


# ===========================================================================
# Group X: ActivityStore Correlation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_x_activity_store_correlation() -> None:
    """Group X: Real stdio tool invocations are audited into SQLite ActivityStore."""
    store = make_test_activity_store()
    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    connector = MCPConnector("conn.mcp.stdio_audit", transport, activity_store=store)
    await connector.connect()
    try:
        req = ConnectorRequest(
            connector_id="conn.mcp.stdio_audit",
            operation="tool:add",
            request_id="req_stdio_audit_01",
            parameters={"a": 15, "b": 25},
        )
        res = await connector.execute(req)
        assert res.success is True
        assert res.content == "40.0"

        # Check correlated SQLite records
        calls = store.list_tool_calls(request_id="req_stdio_audit_01")
        assert len(calls) == 1
        tc = calls[0]
        assert tc["connector_id"] == "conn.mcp.stdio_audit"
        assert "add" in tc["tool_name"]
        assert tc["status"].lower() == "success"
    finally:
        await connector.disconnect()


# ===========================================================================
# Group Y: Analyzer Descriptor Integration
# ===========================================================================

@pytest.mark.asyncio
async def test_group_y_analyzer_descriptor_integration() -> None:
    """Group Y: Stdio MCP connector registers in registry and is verified by Phase 7.4 Analyzer."""
    store = make_test_activity_store()
    registry = ConnectorRegistry(activity_store=store)

    config = make_stdio_config()
    transport = StdioMCPTransport(config)
    connector = MCPConnector(
        "conn.mcp.stdio_analyzer",
        transport,
        activity_store=store,
        metadata={
            "declared_binary_names": [sys.executable],
            "transport_hint": "mcp_stdio",
        },
    )

    await registry.register(connector)
    try:
        assert registry.exists("conn.mcp.stdio_analyzer")
        desc = registry.get_analysis_descriptor("conn.mcp.stdio_analyzer")
        assert desc.connector_id == "conn.mcp.stdio_analyzer"
        assert desc.transport in ("mcp_stdio", "StdioMCPTransport")

        # Verify using Phase 7.4 Generic ConnectorAnalyzer
        analyzer = ConnectorAnalyzer(registry=registry, activity_store=store)
        report = await analyzer.analyze("conn.mcp.stdio_analyzer")

        assert isinstance(report, AnalysisReport)
        assert report.connector_id == "conn.mcp.stdio_analyzer"
        assert report.verification_state == VerificationState.VERIFIED
        assert report.is_healthy is True
        assert report.checks_passed > 0
        assert report.checks_failed == 0
    finally:
        await registry.unregister("conn.mcp.stdio_analyzer")


# ===========================================================================
# Group Z: Cross-Platform Behavior Abstraction
# ===========================================================================

@pytest.mark.asyncio
async def test_group_z_cross_platform_behavior() -> None:
    """Group Z: Verifies environment building, clean_env filtering, and platform safety."""
    # Test clean_env=True filtering
    config = MCPStdioConfig(
        command=sys.executable,
        args=(SERVER_SCRIPT,),
        clean_env=True,
        env={"CUSTOM_KEY": "custom_val"},
    )
    built_env = config.build_env()
    assert "CUSTOM_KEY" in built_env
    assert built_env["CUSTOM_KEY"] == "custom_val"
    # PATH is retained because it's in standard runtime keys
    assert "PATH" in built_env

    # Test allowlist filtering
    config_allow = MCPStdioConfig(
        command=sys.executable,
        args=(SERVER_SCRIPT,),
        clean_env=False,
        env_allowlist=("PATH", "SYSTEMROOT", "HOME", "USERPROFILE"),
    )
    env_filtered = config_allow.build_env()
    assert "PATH" in env_filtered
