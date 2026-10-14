"""Comprehensive tests for Phase 7.2: Connector Abstraction & Contract.

Validates:
1. Connector model creation and identity
2. Connector type representation (MCP, REST, CLI, CUSTOM)
3. Capability model and requirement enforcement
4. Lifecycle states (REGISTERED, CONNECTING, READY, DEGRADED, DISCONNECTED, ERROR)
5. Normalized ConnectorRequest with universal request_id correlation
6. Normalized ConnectorResult (ok, fail, truncation, structured data)
7. Error normalization, categories, and conversion to ConnectorResult
8. Async interface contracts (connect, disconnect, health, discover, execute)
9. request_id, project_id, and session_id propagation
10. Secret-safety of connector metadata and content redaction
11. Compatibility with ActivityStore (connector_events, tool_calls, timeline)
12. Compatibility with PermissionEngine (PermissionRequest from ConnectorRequest)
13. Compatibility with shared resilience (with_timeout_and_retry, circuit breaker)
14. Invalid input handling and boundary constraints
"""
from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any, Optional, Sequence, Union

from sutra.activity_store import ActivityStore
from sutra.connectors import (
    Connector,
    ConnectorAuthError,
    ConnectorCapability,
    ConnectorConnectionError,
    ConnectorDescriptor,
    ConnectorError,
    ConnectorErrorCode,
    ConnectorHealth,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorProviderError,
    ConnectorRequest,
    ConnectorResource,
    ConnectorResult,
    ConnectorStatus,
    ConnectorTimeoutError,
    ConnectorToolDefinition,
    ConnectorToolError,
    ConnectorType,
    ConnectorUnavailableError,
    sanitize_metadata,
)
from sutra.permissions import (
    PermissionDecision,
    PermissionRequest,
    PermissionRiskLevel,
)
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import PolicyStore
from sutra.resilience import RetryPolicy, with_timeout_and_retry


# ---------------------------------------------------------------------------
# Test Double: In-Memory Fake Connector
# ---------------------------------------------------------------------------

class FakeConnector(Connector):
    """Minimal in-memory fake connector used exclusively for testing the contract."""

    def __init__(
        self,
        connector_id: str = "fake.test",
        name: str = "Fake Test Connector",
        connector_type: ConnectorType = ConnectorType.MCP,
        capabilities: Optional[set[ConnectorCapability]] = None,
        initial_status: ConnectorStatus = ConnectorStatus.REGISTERED,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._connector_id = connector_id
        self._name = name
        self._type = connector_type
        self._capabilities = frozenset(capabilities or {
            ConnectorCapability.DISCOVERY,
            ConnectorCapability.TOOL_EXECUTION,
            ConnectorCapability.HEALTH_CHECK,
            ConnectorCapability.READ,
        })
        self._status = initial_status
        self._metadata = metadata or {}
        self.connect_count = 0
        self.disconnect_count = 0
        self.fail_next_connect = False
        self.fail_next_execute = False

    def identity(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=self._type,
            version="1.2.0",
            description="Fake connector for contract verification",
            capabilities=self._capabilities,
            status=self._status,
            metadata=self._metadata,
        )

    async def connect(self) -> None:
        self.connect_count += 1
        if self.fail_next_connect:
            self._status = ConnectorStatus.ERROR
            raise ConnectorConnectionError(self.connector_id, "Simulated handshake failure")
        self._status = ConnectorStatus.READY

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        self._status = ConnectorStatus.DISCONNECTED

    async def health(self) -> ConnectorHealth:
        is_ready = self._status == ConnectorStatus.READY
        return ConnectorHealth(
            status=self._status,
            healthy=is_ready,
            latency_ms=1.5 if is_ready else None,
            message="Operational" if is_ready else "Not ready",
            details={"connections": 1 if is_ready else 0},
        )

    async def discover(self) -> Sequence[Union[ConnectorToolDefinition, ConnectorResource]]:
        self.require_capability(ConnectorCapability.DISCOVERY)
        return [
            ConnectorToolDefinition(
                name="echo",
                description="Echo input text",
                parameters_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
                read_only=True,
                risk_level="LOW",
            ),
            ConnectorResource(
                uri="resource://config",
                name="Configuration",
                description="Connector configuration parameters",
                mime_type="application/json",
            ),
        ]

    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        self.require_capability(ConnectorCapability.TOOL_EXECUTION)
        if self._status != ConnectorStatus.READY:
            raise ConnectorNotConnectedError(self.connector_id)

        if self.fail_next_execute:
            raise ConnectorToolError(self.connector_id, "Simulated tool execution crash")

        if request.operation == "echo":
            msg = request.parameters.get("msg", "")
            return ConnectorResult.ok(content=f"ECHO: {msg}", structured_data={"echo": msg})

        if request.operation == "add":
            a = request.parameters.get("a", 0)
            b = request.parameters.get("b", 0)
            return ConnectorResult.ok(content=str(a + b), structured_data={"result": a + b})

        raise ConnectorNotFoundError(self.connector_id, f"Tool '{request.operation}' not recognized")


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestConnectorDomainModels(unittest.TestCase):
    """Test identity, capability, lifecycle, request, and result domain models."""

    def test_descriptor_creation_and_identity(self) -> None:
        """Verify ConnectorDescriptor fields and immutability."""
        desc = ConnectorDescriptor(
            connector_id="mcp.github",
            name="GitHub MCP Server",
            type=ConnectorType.MCP,
            version="2.0.0",
            description="GitHub integration",
            capabilities=frozenset({ConnectorCapability.TOOL_EXECUTION, ConnectorCapability.DISCOVERY}),
            status=ConnectorStatus.REGISTERED,
            metadata={"transport": "stdio"},
        )
        self.assertEqual(desc.connector_id, "mcp.github")
        self.assertEqual(desc.name, "GitHub MCP Server")
        self.assertEqual(desc.type, ConnectorType.MCP)
        self.assertEqual(desc.version, "2.0.0")
        self.assertTrue(desc.has_capability(ConnectorCapability.TOOL_EXECUTION))
        self.assertFalse(desc.has_capability(ConnectorCapability.WRITE))

    def test_descriptor_validation_rejects_empty_id(self) -> None:
        """ConnectorDescriptor must reject empty connector_id or empty name."""
        with self.assertRaises(ValueError):
            ConnectorDescriptor(connector_id="", name="Valid Name", type=ConnectorType.REST)
        with self.assertRaises(ValueError):
            ConnectorDescriptor(connector_id="valid.id", name="  ", type=ConnectorType.CLI)

    def test_connector_types(self) -> None:
        """Verify supported and extensible connector types."""
        self.assertEqual(ConnectorType.MCP.value, "MCP")
        self.assertEqual(ConnectorType.REST.value, "REST")
        self.assertEqual(ConnectorType.CLI.value, "CLI")
        self.assertEqual(ConnectorType.CUSTOM.value, "CUSTOM")

    def test_capability_model(self) -> None:
        """Verify structured capability enums."""
        expected = {
            "discovery", "tool_execution", "health_check",
            "resources", "prompts", "read", "write",
        }
        actual = {c.value for c in ConnectorCapability}
        self.assertTrue(expected.issubset(actual))

    def test_lifecycle_states(self) -> None:
        """Verify required lifecycle states."""
        expected_states = {"REGISTERED", "CONNECTING", "READY", "DEGRADED", "DISCONNECTED", "ERROR"}
        actual_states = {s.value for s in ConnectorStatus}
        self.assertTrue(expected_states.issubset(actual_states))

    def test_connector_request_creation_and_correlation(self) -> None:
        """Verify ConnectorRequest initializes with universal request_id and correlation attributes."""
        req = ConnectorRequest(
            connector_id="rest.weather",
            operation="get_forecast",
            request_id="req-custom-123",
            parameters={"city": "Tokyo"},
            project_id="proj-alpha",
            session_id="sess-beta",
            actor="telegram:9876",
            source="telegram",
            metadata={"client": "sutra-core"},
        )
        self.assertEqual(req.request_id, "req-custom-123")
        self.assertEqual(req.connector_id, "rest.weather")
        self.assertEqual(req.operation, "get_forecast")
        self.assertEqual(req.project_id, "proj-alpha")
        self.assertEqual(req.session_id, "sess-beta")
        self.assertEqual(req.actor, "telegram:9876")
        self.assertEqual(req.parameters["city"], "Tokyo")

    def test_connector_request_validation(self) -> None:
        """ConnectorRequest must reject blank connector_id or operation."""
        with self.assertRaises(ValueError):
            ConnectorRequest(connector_id="", operation="op")
        with self.assertRaises(ValueError):
            ConnectorRequest(connector_id="valid", operation=" ")

    def test_normalized_connector_result_success(self) -> None:
        """Verify ConnectorResult.ok factory initializes clean normalized response."""
        res = ConnectorResult.ok(
            content="Result text",
            structured_data={"key": "val"},
            metadata={"cached": False},
        )
        self.assertTrue(res.success)
        self.assertEqual(res.content, "Result text")
        self.assertEqual(res.structured_data, {"key": "val"})
        self.assertIsNone(res.error)
        self.assertIsNone(res.error_code)
        self.assertFalse(res.truncated)

    def test_normalized_connector_result_failure(self) -> None:
        """Verify ConnectorResult.fail factory initializes failure payload."""
        res = ConnectorResult.fail(
            error="Connection timed out",
            error_code=ConnectorErrorCode.TIMEOUT.value,
            metadata={"attempts": 3},
        )
        self.assertFalse(res.success)
        self.assertIsNone(res.content)
        self.assertIsNone(res.structured_data)
        self.assertEqual(res.error, "Connection timed out")
        self.assertEqual(res.error_code, "TIMEOUT")
        self.assertEqual(res.metadata["attempts"], 3)


class TestSecurityAndRedaction(unittest.TestCase):
    """Verify connectors enforce strict secret sanitization across all layers."""

    def test_metadata_sanitization(self) -> None:
        """Sensitive keys in metadata are automatically redacted."""
        raw_meta = {
            "api_key": "secret_12345",
            "auth_token": "bearer_99999",
            "password": "super_secret_pw",
            "client_name": "sutra_agent",
            "nested": {"oauth_secret": "xyz123", "normal": "ok"},
        }
        clean = sanitize_metadata(raw_meta)
        self.assertEqual(clean["api_key"], "[REDACTED]")
        self.assertEqual(clean["auth_token"], "[REDACTED]")
        self.assertEqual(clean["password"], "[REDACTED]")
        self.assertEqual(clean["client_name"], "sutra_agent")
        self.assertEqual(clean["nested"]["oauth_secret"], "[REDACTED]")
        self.assertEqual(clean["nested"]["normal"], "ok")

    def test_descriptor_metadata_secret_safety(self) -> None:
        """ConnectorDescriptor automatically sanitizes incoming metadata."""
        desc = ConnectorDescriptor(
            connector_id="test.sec",
            name="Secured Connector",
            type=ConnectorType.REST,
            metadata={"github_token": "ghp_1234567890abcdef1234567890abcdef1234"},
        )
        self.assertEqual(desc.metadata["github_token"], "[REDACTED]")

    def test_connector_result_redaction(self) -> None:
        """ConnectorResult automatically redacts credentials from output text and error messages."""
        leak_text = "Failed authenticating with token ghp_1234567890abcdef1234567890abcdef1234"
        res = ConnectorResult.fail(error=leak_text)
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef1234", res.error or "")
        self.assertIn("[REDACTED", res.error or "")

        res_ok = ConnectorResult.ok(content="Connected using Authorization: Bearer sec_tok_99999999")
        self.assertNotIn("sec_tok_99999999", res_ok.content or "")


class TestConnectorErrorHierarchy(unittest.TestCase):
    """Verify provider-neutral error models and conversion to ConnectorResult."""

    def test_error_hierarchy_and_codes(self) -> None:
        """Verify all error classes map to corresponding ConnectorErrorCode."""
        cases = [
            (ConnectorNotConnectedError("c1"), ConnectorErrorCode.NOT_CONNECTED),
            (ConnectorConnectionError("c1", "fail"), ConnectorErrorCode.CONNECTION_FAILED),
            (ConnectorTimeoutError("c1"), ConnectorErrorCode.TIMEOUT),
            (ConnectorAuthError("c1"), ConnectorErrorCode.AUTHENTICATION_ERROR),
            (ConnectorPermissionError("c1"), ConnectorErrorCode.AUTHORIZATION_ERROR),
            (ConnectorInvalidRequestError("c1", "bad"), ConnectorErrorCode.INVALID_REQUEST),
            (ConnectorNotFoundError("c1", "missing"), ConnectorErrorCode.NOT_FOUND),
            (ConnectorProviderError("c1", "500"), ConnectorErrorCode.PROVIDER_ERROR),
            (ConnectorUnavailableError("c1"), ConnectorErrorCode.UNAVAILABLE),
            (ConnectorToolError("c1", "crash"), ConnectorErrorCode.TOOL_ERROR),
        ]
        for err, expected_code in cases:
            self.assertEqual(err.code, expected_code)
            self.assertEqual(err.connector_id, "c1")

    def test_error_to_result_conversion(self) -> None:
        """Verify ConnectorError.to_result produces normalized ConnectorResult."""
        err = ConnectorTimeoutError("mcp.db", "Query timed out after 30s", details={"table": "users"})
        res = err.to_result()
        self.assertFalse(res.success)
        self.assertEqual(res.error, "Query timed out after 30s")
        self.assertEqual(res.error_code, "TIMEOUT")
        self.assertEqual(res.metadata["connector_id"], "mcp.db")
        self.assertEqual(res.metadata["table"], "users")


class TestAsyncConnectorInterface(unittest.TestCase):
    """Verify async execution, discovery, and lifecycle contracts using FakeConnector."""

    def setUp(self) -> None:
        self.conn = FakeConnector(connector_id="fake.async")

    def test_async_connect_and_disconnect(self) -> None:
        """Connector transitions between REGISTERED, READY, and DISCONNECTED states."""
        async def _run() -> None:
            self.assertEqual(self.conn.status, ConnectorStatus.REGISTERED)
            await self.conn.connect()
            self.assertEqual(self.conn.status, ConnectorStatus.READY)
            self.assertEqual(self.conn.connect_count, 1)

            await self.conn.disconnect()
            self.assertEqual(self.conn.status, ConnectorStatus.DISCONNECTED)
            self.assertEqual(self.conn.disconnect_count, 1)

        asyncio.run(_run())

    def test_async_health_diagnostics(self) -> None:
        """Health diagnostics reflect current connector lifecycle status."""
        async def _run() -> None:
            # When registered/not connected:
            h1 = await self.conn.health()
            self.assertFalse(h1.healthy)

            # When ready:
            await self.conn.connect()
            h2 = await self.conn.health()
            self.assertTrue(h2.healthy)
            self.assertIsNotNone(h2.latency_ms)
            self.assertEqual(h2.status, ConnectorStatus.READY)

        asyncio.run(_run())

    def test_async_discovery(self) -> None:
        """Discover returns sequence of declared tools and resources."""
        async def _run() -> None:
            items = await self.conn.discover()
            self.assertEqual(len(items), 2)
            tools = [item for item in items if isinstance(item, ConnectorToolDefinition)]
            resources = [item for item in items if isinstance(item, ConnectorResource)]
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0].name, "echo")
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0].uri, "resource://config")

        asyncio.run(_run())

    def test_missing_capability_enforcement(self) -> None:
        """Attempting an operation without declared capability raises ConnectorInvalidRequestError."""
        # Create connector with NO discovery capability
        limited_conn = FakeConnector(
            connector_id="fake.limited",
            capabilities={ConnectorCapability.TOOL_EXECUTION},
        )

        async def _run() -> None:
            with self.assertRaises(ConnectorInvalidRequestError) as ctx:
                await limited_conn.discover()
            self.assertIn("does not support required capability 'discovery'", str(ctx.exception))

        asyncio.run(_run())

    def test_async_execution_success(self) -> None:
        """Execute processes request parameters and returns normalized result."""
        async def _run() -> None:
            await self.conn.connect()
            req = ConnectorRequest(
                connector_id=self.conn.connector_id,
                operation="add",
                request_id="req-add-1",
                parameters={"a": 10, "b": 32},
            )
            res = await self.conn.execute(req)
            self.assertTrue(res.success)
            self.assertEqual(res.content, "42")
            self.assertEqual(res.structured_data, {"result": 42})

        asyncio.run(_run())

    def test_async_execution_requires_connected_state(self) -> None:
        """Execution before connect() raises ConnectorNotConnectedError."""
        async def _run() -> None:
            req = ConnectorRequest(
                connector_id=self.conn.connector_id,
                operation="echo",
                parameters={"msg": "hi"},
            )
            with self.assertRaises(ConnectorNotConnectedError):
                await self.conn.execute(req)

        asyncio.run(_run())

    def test_unknown_operation_raises_not_found(self) -> None:
        """Requesting non-existent tool raises ConnectorNotFoundError."""
        async def _run() -> None:
            await self.conn.connect()
            req = ConnectorRequest(
                connector_id=self.conn.connector_id,
                operation="non_existent_tool",
            )
            with self.assertRaises(ConnectorNotFoundError):
                await self.conn.execute(req)

        asyncio.run(_run())


class TestCrossSystemCompatibility(unittest.TestCase):
    """Verify Connector abstraction seamlessly interlinks with ActivityStore, PermissionEngine, and Resilience."""

    def test_activity_store_correlation_compatibility(self) -> None:
        """Verify a ConnectorRequest and ConnectorResult map directly into ActivityStore events."""
        store = ActivityStore.from_path(":memory:")
        req = ConnectorRequest(
            connector_id="fake.mcp",
            operation="echo",
            request_id="req-compat-42",
            parameters={"msg": "Hello Sutra"},
            project_id="proj-x",
            session_id="sess-y",
        )

        # 1. Record connector event (e.g. invocation started)
        ce_id = store.record_connector_event(
            connector_id=req.connector_id,
            event_type="invoked",
            status="running",
            request_id=req.request_id,
            metadata={"operation": req.operation},
        )
        self.assertIsNotNone(ce_id)

        # 2. Record tool call start
        tc_id = store.record_tool_call_start(
            request_id=req.request_id,
            connector_id=req.connector_id,
            tool_name=req.operation,
            arguments=req.parameters,
        )
        self.assertIsNotNone(tc_id)

        # 3. Simulate execution and record completion
        res = ConnectorResult.ok(content="ECHO: Hello Sutra", structured_data={"echo": "Hello Sutra"})
        store.record_tool_call_complete(
            tool_call_id=tc_id,
            status="completed" if res.success else "failed",
            result=res.structured_data,
            duration_ms=12.5,
        )

        # 4. Verify correlation via get_timeline
        timeline = store.get_timeline(req.request_id)
        self.assertEqual(timeline["request_id"], req.request_id)
        self.assertEqual(len(timeline["connector_events"]), 1)
        self.assertEqual(len(timeline["tool_calls"]), 1)
        self.assertEqual(timeline["tool_calls"][0]["tool_name"], "echo")
        self.assertEqual(timeline["tool_calls"][0]["status"], "completed")

        store.close()

    def test_permission_engine_context_compatibility(self) -> None:
        """Verify ConnectorRequest can construct a valid PermissionRequest for PolicyEngine evaluation."""
        pol_store = PolicyStore.from_path(":memory:")
        engine = PolicyEngine(pol_store)

        req = ConnectorRequest(
            connector_id="mcp.github",
            operation="create_issue",
            request_id="req-perm-check",
            parameters={"title": "Bug found"},
            project_id="proj-my-app",
            session_id="sess-abc",
        )

        # Map to PermissionRequest
        perm_req = PermissionRequest(
            request_id=req.request_id,
            action_type="tool_call",
            resource_path=req.connector_id,
            command=f"{req.connector_id}.{req.operation}",
            agent_id="agent-sutra",
            session_id=req.session_id or "default",
            risk_level=PermissionRiskLevel.LOW,
            description="Create an issue on GitHub",
        )

        # Evaluate against PolicyEngine
        allowed, reason = engine.check_permission(perm_req)
        self.assertIsInstance(allowed, bool)
        self.assertIsInstance(reason, str)

        pol_store.close()

    def test_resilience_boundary_compatibility(self) -> None:
        """Verify Connector execution can be cleanly wrapped in centralized with_timeout_and_retry."""
        conn = FakeConnector(connector_id="fake.resilience")

        async def _run() -> None:
            await conn.connect()
            conn.fail_next_execute = True

            req = ConnectorRequest(
                connector_id=conn.connector_id,
                operation="echo",
                parameters={"msg": "retry test"},
            )

            # Simulate caller wrapped in standard resilience
            attempts = 0

            async def _attempt() -> ConnectorResult:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    conn.fail_next_execute = False
                    raise ConnectionResetError("Transient network drop")
                return await conn.execute(req)

            policy = RetryPolicy(max_retries=2, base_delay=0.01)
            result = await with_timeout_and_retry(
                _attempt,
                timeout_seconds=2.0,
                policy=policy,
            )
            self.assertTrue(result.success)
            self.assertEqual(attempts, 2)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
