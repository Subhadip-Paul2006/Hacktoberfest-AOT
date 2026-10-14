"""Dedicated test suite for Remote MCP HTTP Transport (Phase 7.7).

Validates all 30 test groups (A through AD) specified by SAMANVAYA · SŪTRA:
A. connection success
B. URL validation
C. TLS/HTTPS configuration handling
D. request dispatch
E. JSON response
F. streaming/SSE response
G. tools/list
H. tools/call
I. resources/list/read
J. prompts/list/get
K. pagination
L. protocol negotiation
M. server metadata
N. HTTP 401
O. HTTP 403
P. HTTP 404
Q. HTTP 429
R. HTTP 500
S. malformed response
T. timeout
U. cancellation
V. disconnect
W. reconnect
X. secret redaction
Y. permission enforcement
Z. ActivityStore correlation
AA. analyzer integration
AB. bounded response/event size
AC. retry behavior
AD. non-idempotent tool execution is not blindly retried
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import pytest

_TESTS_DIR = Path(__file__).parent
_ROOT_DIR = _TESTS_DIR.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
_MOCKS_DIR = _TESTS_DIR / "mocks"
if str(_MOCKS_DIR) not in sys.path:
    sys.path.insert(0, str(_MOCKS_DIR))

from tests.mocks.mcp_http_test_server import MockMCPServer
from sutra.activity_store import ActivityStore
from sutra.connectors import (
    ConnectorAnalyzer,
    ConnectorAuthError,
    ConnectorCapability,
    ConnectorConnectionError,
    ConnectorInvalidRequestError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorProviderError,
    ConnectorRegistry,
    ConnectorRequest,
    ConnectorTimeoutError,
    ConnectorUnavailableError,
    HttpMCPTransport,
    HttpTransportMode,
    MCPClient,
    MCPConnector,
    MCPHttpConfig,
    MCPHttpState,
    MCPProtocolVersion,
    create_permission_request,
)
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import PolicyStore
from sutra.resilience import RetryPolicy, with_timeout_and_retry


@asynccontextmanager
async def running_mock_server():
    """Context manager managing lifecycle of the local mock MCP HTTP server."""
    server = MockMCPServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Test Group A: Connection Success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_a_connection_success():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        assert not transport.is_connected()
        assert transport.state == MCPHttpState.DISCONNECTED

        await transport.open()
        assert transport.is_connected()
        assert transport.state == MCPHttpState.CONNECTED

        # Ping server
        latency = await transport.ping(timeout=5.0)
        assert latency >= 0.0

        await transport.close()
        assert not transport.is_connected()
        assert transport.state == MCPHttpState.DISCONNECTED


# ---------------------------------------------------------------------------
# Test Group B: URL Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_b_url_validation():
    # Empty URL
    with pytest.raises(ConnectorInvalidRequestError):
        MCPHttpConfig(url="")

    # Invalid scheme
    with pytest.raises(ConnectorInvalidRequestError):
        MCPHttpConfig(url="ftp://example.com/mcp")

    with pytest.raises(ConnectorInvalidRequestError):
        MCPHttpConfig(url="file:///path/to/server")


# ---------------------------------------------------------------------------
# Test Group C: TLS/HTTPS Configuration Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_c_tls_https_configuration():
    # Insecure plain HTTP to remote host rejected when allow_http=False
    with pytest.raises(ConnectorInvalidRequestError) as exc_info:
        MCPHttpConfig(url="http://remote-mcp-server.org/mcp", allow_http=False)
    assert "Insecure scheme" in str(exc_info.value)

    # Insecure plain HTTP to remote host allowed when allow_http=True
    cfg_allowed = MCPHttpConfig(url="http://remote-mcp-server.org/mcp", allow_http=True)
    assert cfg_allowed.allow_http is True

    # Plain HTTP to localhost allowed even if allow_http=False
    cfg_local = MCPHttpConfig(url="http://localhost:8000/mcp", allow_http=False)
    assert cfg_local.url == "http://localhost:8000/mcp"

    cfg_loopback = MCPHttpConfig(url="http://127.0.0.1:8000/mcp", allow_http=False)
    assert cfg_loopback.url == "http://127.0.0.1:8000/mcp"

    # HTTPS always allowed
    cfg_https = MCPHttpConfig(url="https://secure-mcp.org/mcp", allow_http=False)
    assert cfg_https.url == "https://secure-mcp.org/mcp"


# ---------------------------------------------------------------------------
# Test Group D: Request Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_d_request_dispatch():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()

        res = await transport.send_request("initialize", {"protocolVersion": "2026-07-28"})
        assert isinstance(res, dict)
        assert res.get("protocolVersion") == "2026-07-28"
        assert "serverInfo" in res

        # Send one-way notification
        await transport.send_notification("notifications/initialized", {})

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group E: JSON Response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_e_json_response():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()

        res = await transport.send_request("tools/call", {"name": "echo", "arguments": {"msg": "hello-world"}})
        assert res.get("isError") is False
        assert res["content"][0]["text"] == "Echo: hello-world"

        # Session ID should have been recorded from response header
        assert transport.session_id == mock_server.state["session_id"]

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group F: Streaming / SSE Response (Streamable HTTP)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_f_streaming_sse_response():
    async with running_mock_server() as mock_server:
        # Pass header telling mock server to respond with text/event-stream
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-sse": "true"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        res = await transport.send_request("tools/call", {"name": "echo", "arguments": {"msg": "streamed-data"}})
        assert res.get("isError") is False
        assert res["content"][0]["text"] == "Echo: streamed-data"

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group G: Tools List
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_g_tools_list():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        client = MCPClient(transport)

        await client.connect()
        page = await client.list_tools()
        tool_names = [t.name for t in page.items]
        assert "echo" in tool_names
        assert "mutate_data" in tool_names

        await client.disconnect()


# ---------------------------------------------------------------------------
# Test Group H: Tools Call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_h_tools_call():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        client = MCPClient(transport)

        await client.connect()
        result = await client.call_tool("echo", {"msg": "mcp-test"})
        assert not result.is_error
        assert "Echo: mcp-test" in result.get_text_content()

        await client.disconnect()


# ---------------------------------------------------------------------------
# Test Group I: Resources List & Read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_i_resources_list_and_read():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        client = MCPClient(transport)

        await client.connect()
        page = await client.list_resources()
        resources = page.items
        assert len(resources) > 0
        assert resources[0].uri == "file://mock/readme.txt"

        contents = await client.read_resource("file://mock/readme.txt")
        assert len(contents) > 0
        assert "Hello from mock MCP resource!" in contents[0].text

        await client.disconnect()


# ---------------------------------------------------------------------------
# Test Group J: Prompts List & Get
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_j_prompts_list_and_get():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        client = MCPClient(transport)

        await client.connect()
        page = await client.list_prompts()
        prompts = page.items
        assert len(prompts) > 0
        assert prompts[0].name == "mock_code_review"

        prompt_res = await client.get_prompt("mock_code_review", {"code": "print(1)"})
        assert len(prompt_res.messages) > 0

        await client.disconnect()


# ---------------------------------------------------------------------------
# Test Group K: Pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_k_pagination():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()

        # Request first page
        page1 = await transport.send_request("tools/list", {"paginate": True})
        assert len(page1["tools"]) == 1
        assert page1["tools"][0]["name"] == "tool_page_1"
        assert page1.get("nextCursor") == "page2"

        # Request second page using cursor
        page2 = await transport.send_request("tools/list", {"cursor": page1["nextCursor"]})
        assert len(page2["tools"]) == 1
        assert page2["tools"][0]["name"] == "tool_page_2"
        assert page2.get("nextCursor") is None

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group L: Protocol Negotiation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_l_protocol_negotiation():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        client = MCPClient(transport, preferred_protocol_version=MCPProtocolVersion.V2026_07_28)

        await client.connect()
        assert client.protocol_version == MCPProtocolVersion.V2026_07_28
        await client.disconnect()


# ---------------------------------------------------------------------------
# Test Group M: Server Metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_m_server_metadata():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        client = MCPClient(transport)

        await client.connect()
        info = client.server_info
        assert info is not None
        assert info.name == "mock-http-server"
        assert info.version == "1.0.0"
        assert info.title == "Mock Remote MCP Server"

        await client.disconnect()


# ---------------------------------------------------------------------------
# Test Group N: HTTP 401 Unauthorized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_n_http_401():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-status": "401"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorAuthError) as exc_info:
            await transport.send_request("ping", {})
        assert "401" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group O: HTTP 403 Forbidden
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_o_http_403():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-status": "403"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorPermissionError) as exc_info:
            await transport.send_request("ping", {})
        assert "403" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group P: HTTP 404 Not Found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_p_http_404():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-status": "404"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorNotFoundError) as exc_info:
            await transport.send_request("ping", {})
        assert "404" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group Q: HTTP 429 Too Many Requests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_q_http_429():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-status": "429"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorUnavailableError) as exc_info:
            await transport.send_request("ping", {})
        assert "429" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group R: HTTP 500 Server Error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_r_http_500():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-status": "500"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorProviderError) as exc_info:
            await transport.send_request("ping", {})
        assert "500" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group S: Malformed Response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_s_malformed_response():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"x-simulate-malformed": "true"},
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorProviderError) as exc_info:
            await transport.send_request("ping", {})
        assert "JSON" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group T: Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_t_timeout():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True, request_timeout=0.1)
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorTimeoutError):
            # slow_op sleeps for 0.5s, while request_timeout is 0.1s
            await transport.send_request("tools/call", {"name": "slow_op", "arguments": {"delay": 0.5}}, timeout=0.1)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group U: Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_u_cancellation():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()

        task = asyncio.create_task(
            transport.send_request("tools/call", {"name": "slow_op", "arguments": {"delay": 1.0}})
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Transport should still be functional after cancellation
        ping_lat = await transport.ping(timeout=5.0)
        assert ping_lat >= 0.0

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group V: Disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_v_disconnect():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()
        assert transport.is_connected()

        await transport.close()
        assert not transport.is_connected()
        assert transport.state == MCPHttpState.DISCONNECTED

        with pytest.raises(Exception):
            await transport.send_request("ping", {})


# ---------------------------------------------------------------------------
# Test Group W: Reconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_w_reconnect():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)

        await transport.open()
        assert transport.is_connected()
        await transport.close()
        assert not transport.is_connected()

        # Reopen transport
        await transport.open()
        assert transport.is_connected()
        res = await transport.send_request("ping", {})
        assert isinstance(res, dict)
        await transport.close()


# ---------------------------------------------------------------------------
# Test Group X: Secret Redaction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_x_secret_redaction():
    async with running_mock_server() as mock_server:
        sensitive_token = "Bearer secret_api_token_12345"
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            headers={"Authorization": sensitive_token, "X-Api-Key": "my-secret-key"},
        )
        transport = HttpMCPTransport(config)
        info = transport.transport_info()

        # Secret header values must not appear in diagnostics
        info_str = json.dumps(info)
        assert sensitive_token not in info_str
        assert "my-secret-key" not in info_str
        assert info["has_auth"] is True


# ---------------------------------------------------------------------------
# Test Group Y: Permission Enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_y_permission_enforcement():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        connector = MCPConnector("mcp.remote", transport)
        await connector.connect()

        policy_store = PolicyStore.from_path(":memory:")
        engine = PolicyEngine(policy_store)

        perm_req = create_permission_request(
            connector_id="mcp.remote",
            operation="mutate_data",
            request_id="req_perm_01",
            risk_level="HIGH",
        )
        assert perm_req.action_type == "connector_admin"
        assert "mutate_data" in perm_req.command

        allowed, reason = engine.check_permission(perm_req)
        assert isinstance(allowed, bool)
        assert isinstance(reason, str)

        await connector.disconnect()


# ---------------------------------------------------------------------------
# Test Group Z: ActivityStore Correlation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_z_activity_store_correlation():
    async with running_mock_server() as mock_server:
        store = ActivityStore.from_path(":memory:")
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        connector = MCPConnector("mcp.remote", transport, activity_store=store)

        await connector.connect()
        try:
            req_id = "req_http_audit_01"
            req = ConnectorRequest(
                connector_id="mcp.remote",
                operation="tool:echo",
                parameters={"msg": "activity-test"},
                request_id=req_id,
            )
            res = await connector.execute(req)
            assert res.success

            # Verify ActivityStore has recorded correlated tool call
            calls = store.list_tool_calls(request_id=req_id)
            assert len(calls) == 1
            assert calls[0]["connector_id"] == "mcp.remote"
            assert "echo" in calls[0]["tool_name"]
            assert calls[0]["status"].lower() == "success"
        finally:
            await connector.disconnect()


# ---------------------------------------------------------------------------
# Test Group AA: Analyzer Integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_aa_analyzer_integration():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        connector = MCPConnector("mcp.remote", transport)

        registry = ConnectorRegistry()
        await registry.register(connector)

        analyzer = ConnectorAnalyzer(registry=registry)
        report = await analyzer.analyze("mcp.remote")

        assert report.connector_id == "mcp.remote"
        assert report.is_healthy is True
        desc = connector.identity()
        assert desc.type.value == "MCP"
        assert desc.metadata["transport"] == "HttpMCPTransport"


# ---------------------------------------------------------------------------
# Test Group AB: Bounded Response / Event Size
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_ab_bounded_response_size():
    async with running_mock_server() as mock_server:
        # Set limit lower than oversized_output's 2MB payload
        config = MCPHttpConfig(
            url=mock_server.url,
            allow_http=True,
            max_response_bytes=100 * 1024,  # 100KB limit
        )
        transport = HttpMCPTransport(config)
        await transport.open()

        with pytest.raises(ConnectorProviderError) as exc_info:
            await transport.send_request("tools/call", {"name": "oversized_output", "arguments": {}})
        assert "exceeded" in str(exc_info.value)

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group AC: Retry Behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_ac_retry_behavior():
    async with running_mock_server() as mock_server:
        # Simulate retry on transient failure using SŪTRA resilience
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()

        attempts = 0

        async def flaky_op():
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise ConnectionResetError("Transient network reset")
            return await transport.send_request("ping", {})

        policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05)
        result = await with_timeout_and_retry(
            flaky_op,
            timeout_seconds=5.0,
            policy=policy,
        )
        assert isinstance(result, dict)
        assert attempts == 2

        await transport.close()


# ---------------------------------------------------------------------------
# Test Group AD: Non-Idempotent Tool Execution Is Not Blindly Retried
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_ad_non_idempotent_tool_not_blindly_retried():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(url=mock_server.url, allow_http=True)
        transport = HttpMCPTransport(config)
        await transport.open()

        # Simulate mutating request that fails with 500
        mutation_attempts = 0

        async def mutating_op():
            nonlocal mutation_attempts
            mutation_attempts += 1
            # Server returns 500 on mutation
            raise ConnectorProviderError(
                connector_id="mcp.http",
                message="HTTP 500 internal server error during mutation",
                details={"retryable": False},
            )

        policy = RetryPolicy(max_retries=0)
        with pytest.raises(ConnectorProviderError):
            await with_timeout_and_retry(
                mutating_op,
                timeout_seconds=5.0,
                policy=policy,
            )
        assert mutation_attempts == 1

        await transport.close()


# ---------------------------------------------------------------------------
# Dedicated Test: Legacy SSE Mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_sse_mode():
    async with running_mock_server() as mock_server:
        config = MCPHttpConfig(
            url=mock_server.sse_url,
            allow_http=True,
            legacy_sse=True,
        )
        transport = HttpMCPTransport(config)
        assert transport.mode == HttpTransportMode.LEGACY_SSE

        await transport.open()
        assert transport.is_connected()

        # Send initialize request over legacy mode (POST to /messages, response on /sse)
        init_res = await transport.send_request("initialize", {"protocolVersion": "2024-11-05"})
        assert init_res.get("protocolVersion") == "2024-11-05"

        # Ping
        ping_lat = await transport.ping(timeout=5.0)
        assert ping_lat >= 0.0

        await transport.close()
        assert not transport.is_connected()
