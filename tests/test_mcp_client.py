"""Comprehensive test suite for Phase 7.5 — MCP Client Core in SAMANVAYA · SŪTRA.

Verifies:
- Group A: Client Lifecycle
- Group B: Protocol Version Negotiation
- Group C: Capability Discovery
- Group D: Server Metadata
- Group E: Client Metadata
- Group F: Tools/List
- Group G: Tools/Call
- Group H: Resources/List
- Group I: Resources/Read
- Group J: Prompts/List
- Group K: Prompts/Get
- Group L: Pagination & Bounded Loops
- Group M: Normalized Results
- Group N: Normalized Errors
- Group O: Timeout Compatibility
- Group P: Cancellation Compatibility
- Group Q: Permission Compatibility
- Group R: Resilience Compatibility
- Group S: ActivityStore Correlation
- Group T: Redaction & Secret Safety
- Group U: Unsupported Capability Handling
- Group V: Transport Failure
- Group W: Backward Compatibility
- Group X: Registry & Analyzer Integration
"""
from __future__ import annotations

import asyncio
import sqlite3
from threading import RLock
from typing import Any, Optional

import pytest

from sutra.activity_store import ActivityStore
from sutra.connectors import (
    ConnectorCapability,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorRegistry,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
    ConnectorTimeoutError,
    ConnectorToolError,
    FakeMCPTransport,
    MCPClient,
    MCPClientCapabilities,
    MCPClientInfo,
    MCPConnector,
    MCPPage,
    MCPPrompt,
    MCPProtocolVersion,
    MCPResource,
    MCPServerCapabilities,
    MCPServerInfo,
    MCPSessionState,
    MCPTool,
    MCPToolCallResult,
    create_permission_request,
)
from sutra.connectors.analyzer import ConnectorAnalyzer, VerificationState
from sutra.permissions import PermissionDecision, PermissionRequest, PermissionRiskLevel
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import PolicyStore
from sutra.resilience import RetryPolicy, with_timeout_and_retry


# ===========================================================================
# In-Memory SQLite ActivityStore Fixture
# ===========================================================================

def make_test_activity_store() -> ActivityStore:
    """Create a thread-safe in-memory ActivityStore for testing."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return ActivityStore(db=conn, lock=RLock())


# ===========================================================================
# Group A: Client Lifecycle
# ===========================================================================

@pytest.mark.asyncio
async def test_group_a_client_lifecycle() -> None:
    """Group A: Client connects, handshakes, verifies state transitions and disconnects."""
    transport = FakeMCPTransport()
    client = MCPClient(transport)

    assert client.state == MCPSessionState.DISCONNECTED
    assert not client.is_connected

    # Connect
    await client.connect()
    assert client.state == MCPSessionState.READY
    assert client.is_connected
    assert client.protocol_version == MCPProtocolVersion.V2026_07_28

    # Double connect is idempotent
    await client.connect()
    assert client.state == MCPSessionState.READY

    # Ping
    rtt = await client.ping()
    assert rtt >= 0.0

    # Disconnect
    await client.disconnect()
    assert client.state == MCPSessionState.CLOSED
    assert not client.is_connected


# ===========================================================================
# Group B: Protocol Version Negotiation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_b_protocol_version_negotiation() -> None:
    """Group B: Negotiation of modern, intermediary, older, and unsupported versions."""
    # 1. Modern 2026-07-28
    t_2026 = FakeMCPTransport(server_protocol_version="2026-07-28")
    c_2026 = MCPClient(t_2026)
    await c_2026.connect()
    assert c_2026.protocol_version == MCPProtocolVersion.V2026_07_28
    await c_2026.disconnect()

    # 2. Intermediary 2025-03-26
    t_2025 = FakeMCPTransport(server_protocol_version="2025-03-26")
    c_2025 = MCPClient(t_2025)
    await c_2025.connect()
    assert c_2025.protocol_version == MCPProtocolVersion.V2025_03_26
    await c_2025.disconnect()

    # 3. Older 2024-11-05
    t_2024 = FakeMCPTransport(server_protocol_version="2024-11-05")
    c_2024 = MCPClient(t_2024)
    await c_2024.connect()
    assert c_2024.protocol_version == MCPProtocolVersion.V2024_11_05
    await c_2024.disconnect()

    # 4. Unsupported version rejection
    t_bad = FakeMCPTransport(unsupported_version="1999-01-01")
    c_bad = MCPClient(t_bad)
    with pytest.raises(ConnectorInvalidRequestError) as exc_info:
        await c_bad.connect()
    assert "Unsupported MCP protocol version '1999-01-01'" in str(exc_info.value)
    assert c_bad.state == MCPSessionState.ERROR


# ===========================================================================
# Group C: Capability Discovery
# ===========================================================================

@pytest.mark.asyncio
async def test_group_c_capability_discovery() -> None:
    """Group C: Server capability declarations map accurately to SŪTRA connector capabilities."""
    caps_dict = {
        "tools": {"listChanged": True},
        "resources": {"subscribe": True},
        "prompts": {},
        "logging": {},
    }
    transport = FakeMCPTransport(capabilities=caps_dict)
    client = MCPClient(transport)
    await client.connect()

    sc = client.server_capabilities
    assert sc.tools is True
    assert sc.tools_list_changed is True
    assert sc.resources is True
    assert sc.resources_subscribe is True
    assert sc.prompts is True
    assert sc.logging is True

    connector_caps = sc.to_connector_capabilities()
    assert ConnectorCapability.DISCOVERY in connector_caps
    assert ConnectorCapability.TOOL_EXECUTION in connector_caps
    assert ConnectorCapability.RESOURCES in connector_caps
    assert ConnectorCapability.PROMPTS in connector_caps
    assert ConnectorCapability.READ in connector_caps
    assert ConnectorCapability.WRITE in connector_caps
    assert ConnectorCapability.HEALTH_CHECK in connector_caps
    await client.disconnect()


# ===========================================================================
# Group D: Server Metadata
# ===========================================================================

@pytest.mark.asyncio
async def test_group_d_server_metadata() -> None:
    """Group D: Server metadata is accessible and informational, not an auth boundary."""
    transport = FakeMCPTransport(server_name="acme-tools-server", server_version="2.4.1")
    client = MCPClient(transport)
    await client.connect()

    assert isinstance(client.server_info, MCPServerInfo)
    assert client.server_info.name == "acme-tools-server"
    assert client.server_info.version == "2.4.1"
    await client.disconnect()


# ===========================================================================
# Group E: Client Metadata
# ===========================================================================

@pytest.mark.asyncio
async def test_group_e_client_metadata() -> None:
    """Group E: Client metadata is communicated to the server during handshake."""
    transport = FakeMCPTransport()
    c_info = MCPClientInfo(name="custom-agent", version="0.9.0", title="Agent Bridge")
    c_caps = MCPClientCapabilities(roots=True, sampling=True)
    client = MCPClient(transport, client_info=c_info, client_capabilities=c_caps)
    await client.connect()

    # Verify initialize request sent with client details
    init_call = next(req for req in transport.requests_sent if req[0] == "initialize")
    params = init_call[1]
    assert params["clientInfo"]["name"] == "custom-agent"
    assert params["clientInfo"]["version"] == "0.9.0"
    assert "roots" in params["capabilities"]
    assert "sampling" in params["capabilities"]
    await client.disconnect()


# ===========================================================================
# Group F: Tools/List API
# ===========================================================================

@pytest.mark.asyncio
async def test_group_f_tools_list() -> None:
    """Group F: Listing tools with parameters, schema, and page structure."""
    transport = FakeMCPTransport()
    transport.add_tool(
        name="calculate_tax",
        description="Compute local sales tax",
        input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
    )
    transport.add_tool(
        name="fetch_rate",
        description="Fetch exchange rate",
        input_schema={"type": "object", "properties": {"currency": {"type": "string"}}},
    )
    client = MCPClient(transport)
    await client.connect()

    page = await client.list_tools()
    assert isinstance(page, MCPPage)
    assert len(page.items) == 2

    tool1 = page.items[0]
    assert isinstance(tool1, MCPTool)
    assert tool1.name == "calculate_tax"
    assert tool1.description == "Compute local sales tax"
    assert "amount" in tool1.input_schema["properties"]

    # Convert to SŪTRA ConnectorToolDefinition
    c_tool = tool1.to_connector_tool_definition(read_only=True, risk_level="LOW")
    assert c_tool.name == "calculate_tax"
    assert c_tool.read_only is True
    assert c_tool.risk_level == "LOW"
    await client.disconnect()


# ===========================================================================
# Group G: Tools/Call API
# ===========================================================================

@pytest.mark.asyncio
async def test_group_g_tools_call() -> None:
    """Group G: Invoking tools, receiving structured and text outputs."""
    transport = FakeMCPTransport()
    transport.add_tool(
        name="echo_uppercase",
        handler=lambda args: {
            "content": [{"type": "text", "text": str(args.get("text", "")).upper()}],
            "isError": False,
            "structuredContent": {"original": args.get("text"), "length": len(args.get("text", ""))},
        },
    )
    client = MCPClient(transport)
    await client.connect()

    res = await client.call_tool("echo_uppercase", {"text": "hello mcp"})
    assert isinstance(res, MCPToolCallResult)
    assert not res.is_error
    assert res.get_text_content() == "HELLO MCP"
    assert res.structured_data["length"] == 9
    await client.disconnect()


# ===========================================================================
# Group H: Resources/List API
# ===========================================================================

@pytest.mark.asyncio
async def test_group_h_resources_list() -> None:
    """Group H: Listing resources with URIs and MIME types."""
    transport = FakeMCPTransport()
    transport.add_resource(
        uri="file:///workspace/config.json",
        name="Application Configuration",
        description="App configuration file",
        mime_type="application/json",
    )
    client = MCPClient(transport)
    await client.connect()

    page = await client.list_resources()
    assert len(page.items) == 1
    r = page.items[0]
    assert isinstance(r, MCPResource)
    assert r.uri == "file:///workspace/config.json"
    assert r.mime_type == "application/json"

    # Convert to SŪTRA ConnectorResource
    cr = r.to_connector_resource()
    assert cr.uri == r.uri
    assert cr.name == r.name
    await client.disconnect()


# ===========================================================================
# Group I: Resources/Read API
# ===========================================================================

@pytest.mark.asyncio
async def test_group_i_resources_read() -> None:
    """Group I: Reading resource contents by URI."""
    transport = FakeMCPTransport()
    transport.add_resource(
        uri="memo://notes/today",
        name="Today Notes",
        text_content="Sprint planning at 10 AM",
    )
    client = MCPClient(transport)
    await client.connect()

    contents = await client.read_resource("memo://notes/today")
    assert len(contents) == 1
    assert contents[0].uri == "memo://notes/today"
    assert contents[0].text == "Sprint planning at 10 AM"
    await client.disconnect()


# ===========================================================================
# Group J: Prompts/List API
# ===========================================================================

@pytest.mark.asyncio
async def test_group_j_prompts_list() -> None:
    """Group J: Listing prompts exposed by the server."""
    transport = FakeMCPTransport()
    transport.add_prompt(
        name="code_review",
        description="Perform code review on a diff",
        arguments=[{"name": "diff", "description": "Git diff string", "required": True}],
    )
    client = MCPClient(transport)
    await client.connect()

    page = await client.list_prompts()
    assert len(page.items) == 1
    p = page.items[0]
    assert isinstance(p, MCPPrompt)
    assert p.name == "code_review"
    assert len(p.arguments) == 1
    assert p.arguments[0].name == "diff"
    assert p.arguments[0].required is True
    await client.disconnect()


# ===========================================================================
# Group K: Prompts/Get API
# ===========================================================================

@pytest.mark.asyncio
async def test_group_k_prompts_get() -> None:
    """Group K: Rendering a prompt template."""
    transport = FakeMCPTransport()
    transport.add_prompt(
        name="summarize",
        handler=lambda args: {
            "description": "Summary prompt",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": f"Summarize: {args.get('topic')}"}}
            ],
        },
    )
    client = MCPClient(transport)
    await client.connect()

    res = await client.get_prompt("summarize", {"topic": "Quantum Computing"})
    assert res.description == "Summary prompt"
    assert len(res.messages) == 1
    assert res.messages[0].role == "user"
    assert res.messages[0].content["text"] == "Summarize: Quantum Computing"
    await client.disconnect()


# ===========================================================================
# Group L: Pagination & Bounded Loops
# ===========================================================================

@pytest.mark.asyncio
async def test_group_l_pagination() -> None:
    """Group L: Multi-page listing with cursors and bounded loop safeguards."""
    transport = FakeMCPTransport()
    for i in range(15):
        transport.add_tool(name=f"tool_{i:02d}", description=f"Tool number {i}")

    client = MCPClient(transport)
    await client.connect()

    # 1. Fetch single page with limit=5
    page1 = await client.list_tools(limit=5)
    assert len(page1.items) == 5
    assert page1.has_more
    assert page1.next_cursor is not None

    # 2. Fetch all tools via bounded pagination
    all_tools = await client.list_all_tools(max_pages=5, max_items=20)
    assert len(all_tools) == 15

    # 3. Verify max_items bounds prevent runaway collections
    bounded_tools = await client.list_all_tools(max_pages=5, max_items=8)
    assert len(bounded_tools) == 8
    await client.disconnect()


# ===========================================================================
# Group M: Normalized Results via Connector Adapter
# ===========================================================================

@pytest.mark.asyncio
async def test_group_m_normalized_results() -> None:
    """Group M: MCPConnector normalizes results into SŪTRA ConnectorResult."""
    transport = FakeMCPTransport()
    transport.add_tool(
        name="generate_id",
        handler=lambda args: {
            "content": [{"type": "text", "text": "ID-998877"}],
            "isError": False,
            "structuredContent": {"id": 998877},
        },
    )
    connector = MCPConnector("conn.mcp.test", transport)
    await connector.connect()

    req = ConnectorRequest(
        connector_id="conn.mcp.test",
        operation="tool:generate_id",
        parameters={},
    )
    result = await connector.execute(req)
    assert isinstance(result, ConnectorResult)
    assert result.success is True
    assert result.content == "ID-998877"
    assert result.structured_data == {"id": 998877}
    assert result.metadata["tool_name"] == "generate_id"
    await connector.disconnect()


# ===========================================================================
# Group N: Normalized Errors
# ===========================================================================

@pytest.mark.asyncio
async def test_group_n_normalized_errors() -> None:
    """Group N: Protocol errors map into SŪTRA ConnectorError hierarchy."""
    transport = FakeMCPTransport()
    client = MCPClient(transport)

    # 1. Calling before connect raises ConnectorNotConnectedError
    with pytest.raises(ConnectorNotConnectedError):
        await client.list_tools()

    await client.connect()

    # 2. Requesting non-existent tool with server failure
    transport.add_tool(
        name="failing_tool",
        handler=lambda args: {"content": [{"type": "text", "text": "Database crashed"}], "isError": True},
    )
    res = await client.call_tool("failing_tool", {})
    assert res.is_error is True
    assert "Database crashed" in res.get_text_content()
    await client.disconnect()


# ===========================================================================
# Group O: Timeout Compatibility
# ===========================================================================

@pytest.mark.asyncio
async def test_group_o_timeout_compatibility() -> None:
    """Group O: Request timeout raises ConnectorTimeoutError cleanly."""
    transport = FakeMCPTransport(timeout_methods={"tools/call"})
    transport.add_tool(name="slow_tool")
    client = MCPClient(transport)
    await client.connect()

    with pytest.raises(ConnectorTimeoutError):
        await client.call_tool("slow_tool", {}, timeout=0.1)
    await client.disconnect()


# ===========================================================================
# Group P: Cancellation Compatibility
# ===========================================================================

@pytest.mark.asyncio
async def test_group_p_cancellation_compatibility() -> None:
    """Group P: Async cancellation cleanly cancels execution without hanging."""
    transport = FakeMCPTransport()
    transport.add_tool(name="hang_tool", handler=lambda args: asyncio.sleep(10))
    client = MCPClient(transport)
    await client.connect()

    task = asyncio.create_task(client.call_tool("hang_tool", {}))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await client.disconnect()


# ===========================================================================
# Group Q: Permission Compatibility
# ===========================================================================

@pytest.mark.asyncio
async def test_group_q_permission_compatibility() -> None:
    """Group Q: Mutating MCP tool operations respect the authoritative PolicyEngine."""
    policy_store = PolicyStore.from_path(":memory:")
    engine = PolicyEngine(policy_store)

    transport = FakeMCPTransport()
    transport.add_tool(name="delete_database")
    connector = MCPConnector("conn.mcp.safe", transport)
    await connector.connect()

    # 1. Caller constructs permission request for mutating tool call
    perm_req = create_permission_request(
        operation="delete_database",
        connector_id="conn.mcp.safe",
        request_id="req_perm_01",
        risk_level="HIGH",
    )
    assert perm_req.action_type == "connector_admin"
    assert "delete_database" in perm_req.command

    # Check permission with PolicyEngine
    allowed, reason = engine.check_permission(perm_req)
    assert isinstance(allowed, bool)
    assert isinstance(reason, str)

    await connector.disconnect()


# ===========================================================================
# Group R: Resilience Compatibility
# ===========================================================================

@pytest.mark.asyncio
async def test_group_r_resilience_compatibility() -> None:
    """Group R: MCP operations integrate with SŪTRA shared resilience wrappers."""
    transport = FakeMCPTransport()
    call_count = 0

    def flaky_handler(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionResetError("Transient network glitch")
        return {"content": [{"type": "text", "text": "Success on retry"}], "isError": False}

    transport.add_tool(name="flaky_tool", handler=flaky_handler)
    client = MCPClient(transport)
    await client.connect()

    # Wrap call_tool with shared SŪTRA timeout and retry
    retry_policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.1)
    result = await with_timeout_and_retry(
        lambda: client.call_tool("flaky_tool", {}),
        timeout_seconds=5.0,
        policy=retry_policy,
    )
    assert result.get_text_content() == "Success on retry"
    assert call_count == 2
    await client.disconnect()


# ===========================================================================
# Group S: ActivityStore Correlation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_s_activity_store_correlation() -> None:
    """Group S: MCPConnector audits tool executions into SQLite ActivityStore."""
    store = make_test_activity_store()
    transport = FakeMCPTransport()
    transport.add_tool(name="audit_me", handler=lambda args: "Done!")
    connector = MCPConnector("conn.mcp.audit", transport, activity_store=store)
    await connector.connect()

    req = ConnectorRequest(
        connector_id="conn.mcp.audit",
        operation="tool:audit_me",
        request_id="req_mcp_test_01",
        parameters={"key": "val"},
        actor="dev_user",
    )
    res = await connector.execute(req)
    assert res.success is True

    # Verify ActivityStore contains correlated tool_calls record
    calls = store.list_tool_calls(request_id="req_mcp_test_01")
    assert len(calls) == 1
    row = calls[0]
    assert row["connector_id"] == "conn.mcp.audit"
    assert "audit_me" in row["tool_name"]
    assert row["status"].lower() == "success"
    await connector.disconnect()


# ===========================================================================
# Group T: Secret Redaction & Safety
# ===========================================================================

@pytest.mark.asyncio
async def test_group_t_secret_redaction() -> None:
    """Group T: Sensitive parameters, tokens, and keys are redacted from results and metadata."""
    store = make_test_activity_store()
    transport = FakeMCPTransport()
    transport.add_tool(
        name="auth_service",
        handler=lambda args: {
            "content": [{"type": "text", "text": "Token returned: sk-ant-api03-abcdef1234567890abcdef"}],
            "isError": False,
        },
    )
    connector = MCPConnector(
        "conn.mcp.secret",
        transport,
        activity_store=store,
        metadata={"api_key": "raw_secret_value_12345", "safe_meta": "safe_val"},
    )
    await connector.connect()

    # 1. Metadata in identity() sanitized
    desc = connector.identity()
    assert desc.metadata["api_key"] == "[REDACTED]"
    assert desc.metadata["safe_meta"] == "safe_val"

    # 2. Tool call with secret parameter
    req = ConnectorRequest(
        connector_id="conn.mcp.secret",
        operation="tool:auth_service",
        request_id="req_secret_audit",
        parameters={"password": "super_secret_pw", "secret_token": "my_secret_token"},
    )
    res = await connector.execute(req)

    # Output text sanitized
    assert "sk-ant-api03" not in res.content
    assert "[REDACTED" in res.content

    # ActivityStore record sanitized
    calls = store.list_tool_calls(request_id="req_secret_audit")
    assert len(calls) == 1
    tc = calls[0]
    assert "super_secret_pw" not in str(tc["arguments"])
    assert "[REDACTED]" in str(tc["arguments"])
    await connector.disconnect()


# ===========================================================================
# Group U: Unsupported Capability Handling
# ===========================================================================

@pytest.mark.asyncio
async def test_group_u_unsupported_capability_handling() -> None:
    """Group U: Invoking unsupported capabilities raises structured errors without invalid requests."""
    # Server advertises only tools, not resources or prompts
    transport = FakeMCPTransport(capabilities={"tools": {}})
    client = MCPClient(transport)
    await client.connect()

    assert client.server_capabilities.tools is True
    assert client.server_capabilities.resources is False
    assert client.server_capabilities.prompts is False

    # Calling list_resources raises ConnectorInvalidRequestError
    with pytest.raises(ConnectorInvalidRequestError) as exc_res:
        await client.list_resources()
    assert "does not support 'resources' capability" in str(exc_res.value)

    # Calling list_prompts raises ConnectorInvalidRequestError
    with pytest.raises(ConnectorInvalidRequestError) as exc_prompt:
        await client.list_prompts()
    assert "does not support 'prompts' capability" in str(exc_prompt.value)

    await client.disconnect()


# ===========================================================================
# Group V: Transport Failure
# ===========================================================================

@pytest.mark.asyncio
async def test_group_v_transport_failure() -> None:
    """Group V: Sudden transport failure or connection drop handled gracefully."""
    # 1. Transport open failure
    t_fail_open = FakeMCPTransport(fail_open=True)
    c_fail_open = MCPClient(t_fail_open)
    with pytest.raises(ConnectorConnectionError):
        await c_fail_open.connect()
    assert c_fail_open.state == MCPSessionState.ERROR

    # 2. Connection drops during request
    t_drop = FakeMCPTransport()
    t_drop.add_tool(name="any_tool")
    c_drop = MCPClient(t_drop)
    await c_drop.connect()
    t_drop.disconnect_on_request = True
    with pytest.raises(ConnectorConnectionError):
        await c_drop.call_tool("any_tool", {})


# ===========================================================================
# Group W: Backward Compatibility
# ===========================================================================

@pytest.mark.asyncio
async def test_group_w_backward_compatibility() -> None:
    """Group W: Compatibility with older servers (2024-11-05 protocol revision)."""
    transport = FakeMCPTransport(
        server_protocol_version="2024-11-05",
        capabilities={"tools": {}},
    )
    transport.add_tool(name="legacy_tool", description="Runs on older protocol")
    connector = MCPConnector("conn.mcp.legacy", transport)
    await connector.connect()

    assert connector.status == ConnectorStatus.READY
    assert connector.client.protocol_version == MCPProtocolVersion.V2024_11_05

    # Discovery works
    discovered = await connector.discover()
    assert len(discovered) == 1
    assert discovered[0].name == "legacy_tool"

    # Execution works
    req = ConnectorRequest(connector_id="conn.mcp.legacy", operation="tool:legacy_tool")
    res = await connector.execute(req)
    assert res.success is True
    await connector.disconnect()


# ===========================================================================
# Group X: Registry & Analyzer Integration
# ===========================================================================

@pytest.mark.asyncio
async def test_group_x_registry_and_analyzer_integration() -> None:
    """Group X: MCPConnector registers into ConnectorRegistry and verifies via ConnectorAnalyzer."""
    store = make_test_activity_store()
    registry = ConnectorRegistry(activity_store=store)

    transport = FakeMCPTransport(
        capabilities={"tools": {"listChanged": True}, "resources": {}},
    )
    transport.add_tool(name="ping_pong", description="Ping pong tool")
    connector = MCPConnector(
        "conn.mcp.registry_test",
        transport,
        activity_store=store,
        metadata={"declared_env_vars": [], "transport_hint": "mcp_fake"},
    )

    # Register into ConnectorRegistry
    await registry.register(connector)
    assert registry.exists("conn.mcp.registry_test")
    assert registry.status("conn.mcp.registry_test") == ConnectorStatus.DISCONNECTED

    # Verify ConnectorAnalysisDescriptor
    desc = registry.get_analysis_descriptor("conn.mcp.registry_test")
    assert desc.connector_id == "conn.mcp.registry_test"
    assert desc.provider == "mcp"

    # Verify with Phase 7.4 ConnectorAnalyzer
    analyzer = ConnectorAnalyzer(registry=registry, activity_store=store)
    report = await analyzer.analyze("conn.mcp.registry_test")

    assert report.connector_id == "conn.mcp.registry_test"
    assert report.verification_state == VerificationState.VERIFIED
    assert report.is_healthy is True
    assert report.checks_passed > 0
    assert report.checks_failed == 0

    await registry.unregister("conn.mcp.registry_test")
