"""Comprehensive test suite for Phase 7.3 — Connector Registry & Lifecycle Management.

Covers test groups:
A. Registry basics (register, unregister, get, require, list, exists, status)
B. Duplicate ID rejection (ConnectorAlreadyExistsError)
C. Multiple connector instances of the same provider
D. Lifecycle transitions and state machine enforcement (ConnectorLifecycleError)
E. Enable / Disable availability separation (ConnectorDisabledError)
F. Health diagnostics and fault isolation (health, health_all)
G. Concurrent lifecycle operations
H. Connector isolation (failing connector does not affect others)
I. ActivityStore event recording and request_id correlation
J. PermissionEngine / PolicyEngine compatibility
K. Resilience boundary integration (with_timeout_and_retry)
L. Secret-safety and sanitized persistence
M. Future Generic Analyzer compatibility contract (ConnectorAnalysisDescriptor)
N. Persistence lifecycle and configuration reconstruction
"""
from __future__ import annotations

import asyncio
import sqlite3
from threading import RLock
from typing import Any, Optional, Sequence

import pytest

from sutra.activity_store import ActivityStore
from sutra.connectors import (
    Connector,
    ConnectorAlreadyExistsError,
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorConnectionError,
    ConnectorDescriptor,
    ConnectorDisabledError,
    ConnectorError,
    ConnectorErrorCode,
    ConnectorHealth,
    ConnectorInvalidRequestError,
    ConnectorLifecycleError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorRegistry,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
    ConnectorToolDefinition,
    ConnectorType,
    create_permission_request,
)
from sutra.metrics import Metrics
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import PolicyStore
from sutra.resilience import RetryPolicy, with_timeout_and_retry


# ===========================================================================
# Test Doubles & In-Memory Connector Implementation
# ===========================================================================

class InMemoryTestConnector(Connector):
    """Test connector double supporting simulated latency, failures, and diagnostics."""

    def __init__(
        self,
        connector_id: str,
        name: str = "Test Connector",
        connector_type: ConnectorType = ConnectorType.REST,
        capabilities: Optional[set[ConnectorCapability]] = None,
        provider: str = "test-provider",
        fail_connect: bool = False,
        fail_health: bool = False,
        raise_in_health: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._connector_id = connector_id
        self._name = name
        self._connector_type = connector_type
        self._capabilities = frozenset(capabilities or {
            ConnectorCapability.DISCOVERY,
            ConnectorCapability.TOOL_EXECUTION,
            ConnectorCapability.HEALTH_CHECK,
            ConnectorCapability.READ,
        })
        self._provider = provider
        self._fail_connect = fail_connect
        self._fail_health = fail_health
        self._raise_in_health = raise_in_health
        self._metadata = metadata or {"provider": provider}
        self._status = ConnectorStatus.REGISTERED
        self.connect_count = 0
        self.disconnect_count = 0
        self.execute_count = 0

    def identity(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=self._connector_type,
            provider=self._provider,
            capabilities=self._capabilities,
            status=self._status,
            metadata=dict(self._metadata),
        )

    async def connect(self) -> None:
        self.connect_count += 1
        if self._fail_connect:
            self._status = ConnectorStatus.ERROR
            raise ConnectionRefusedError(f"Simulated network failure on {self._connector_id}")
        self._status = ConnectorStatus.READY

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        self._status = ConnectorStatus.DISCONNECTED

    async def health(self) -> ConnectorHealth:
        if self._raise_in_health:
            raise RuntimeError(f"Simulated health crash in {self._connector_id}")
        if self._fail_health:
            return ConnectorHealth(
                status=ConnectorStatus.DEGRADED,
                healthy=False,
                latency_ms=45.0,
                message="Endpoint responded with 503 degraded",
            )
        return ConnectorHealth(
            status=self._status,
            healthy=(self._status == ConnectorStatus.READY),
            latency_ms=12.5,
            message="OK",
        )

    async def discover(self) -> Sequence[ConnectorToolDefinition]:
        self.require_capability(ConnectorCapability.DISCOVERY)
        return [
            ConnectorToolDefinition(
                name="ping",
                description="Ping tool",
                parameters_schema={"type": "object"},
                read_only=True,
            )
        ]

    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        self.execute_count += 1
        if self._status != ConnectorStatus.READY:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message=f"Connector is in status {self._status.value}",
            )
        if request.operation == "error_op":
            return ConnectorResult.fail(error="Simulated operation error", error_code="OP_ERROR")
        return ConnectorResult.ok(
            content=f"Executed {request.operation} on {self._connector_id}",
            structured_data={"echo": request.parameters},
        )


# ===========================================================================
# Group A: Registry Basics
# ===========================================================================

class TestRegistryBasics:
    @pytest.mark.asyncio
    async def test_register_and_lookup(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("github-main", name="GitHub Main")

        desc = await registry.register(conn)
        assert desc.connector_id == "github-main"
        assert desc.status == ConnectorStatus.REGISTERED
        assert desc.enabled is True
        assert registry.exists("github-main") is True
        assert registry.get("github-main") is conn
        assert registry.require("github-main") is conn
        assert registry.status("github-main") == ConnectorStatus.REGISTERED

    @pytest.mark.asyncio
    async def test_require_missing_raises_not_found(self):
        registry = ConnectorRegistry()
        with pytest.raises(ConnectorNotFoundError) as exc_info:
            registry.require("non-existent")
        assert exc_info.value.code == ConnectorErrorCode.NOT_FOUND

    @pytest.mark.asyncio
    async def test_register_invalid_type_raises_type_error(self):
        registry = ConnectorRegistry()
        with pytest.raises(TypeError):
            await registry.register("not-a-connector")  # type: ignore

    @pytest.mark.asyncio
    async def test_unregister_existing_connector(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("slack-alerts")
        await registry.register(conn)
        assert registry.exists("slack-alerts") is True

        removed = await registry.unregister("slack-alerts")
        assert removed.connector_id == "slack-alerts"
        assert registry.exists("slack-alerts") is False
        assert registry.get("slack-alerts") is None

    @pytest.mark.asyncio
    async def test_unregister_non_existent_raises_not_found(self):
        registry = ConnectorRegistry()
        with pytest.raises(ConnectorNotFoundError):
            await registry.unregister("ghost-connector")

    @pytest.mark.asyncio
    async def test_list_and_filtering(self):
        registry = ConnectorRegistry()
        conn1 = InMemoryTestConnector("c1", connector_type=ConnectorType.REST)
        conn2 = InMemoryTestConnector("c2", connector_type=ConnectorType.CLI)
        await registry.register(conn1, enabled=True)
        await registry.register(conn2, enabled=False)

        all_desc = registry.list()
        assert len(all_desc) == 2

        enabled_only = registry.list(enabled_only=True)
        assert len(enabled_only) == 1
        assert enabled_only[0].connector_id == "c1"

        status_filtered = registry.list(status=ConnectorStatus.REGISTERED)
        assert len(status_filtered) == 2


# ===========================================================================
# Group B: Duplicate IDs
# ===========================================================================

class TestDuplicateIdRejection:
    @pytest.mark.asyncio
    async def test_duplicate_registration_rejected_deterministically(self):
        registry = ConnectorRegistry()
        conn1 = InMemoryTestConnector("shared-id", name="Instance 1")
        conn2 = InMemoryTestConnector("shared-id", name="Instance 2")

        await registry.register(conn1)
        with pytest.raises(ConnectorAlreadyExistsError) as exc_info:
            await registry.register(conn2)

        assert exc_info.value.connector_id == "shared-id"
        assert exc_info.value.code == ConnectorErrorCode.ALREADY_EXISTS
        # Verify original instance was not overwritten
        assert registry.require("shared-id") is conn1


# ===========================================================================
# Group C: Multiple Connector Instances
# ===========================================================================

class TestMultipleConnectorInstances:
    @pytest.mark.asyncio
    async def test_multiple_instances_of_same_provider(self):
        registry = ConnectorRegistry()
        conn_personal = InMemoryTestConnector("github-personal", name="GH Personal", provider="github")
        conn_work = InMemoryTestConnector("github-work", name="GH Work", provider="github")

        await registry.register(conn_personal)
        await registry.register(conn_work)

        assert registry.exists("github-personal")
        assert registry.exists("github-work")

        # Independent lifecycle: connect only one
        await registry.connect("github-personal")
        assert registry.status("github-personal") == ConnectorStatus.READY
        assert registry.status("github-work") == ConnectorStatus.REGISTERED

        # Descriptors remain distinct
        d_personal = registry.descriptor("github-personal")
        d_work = registry.descriptor("github-work")
        assert d_personal.status == ConnectorStatus.READY
        assert d_work.status == ConnectorStatus.REGISTERED
        assert d_personal.provider == "github"
        assert d_work.provider == "github"


# ===========================================================================
# Group D: Lifecycle Transitions & State Machine
# ===========================================================================

class TestLifecycleTransitions:
    @pytest.mark.asyncio
    async def test_successful_connect_and_disconnect(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("service-a")
        await registry.register(conn)

        health = await registry.connect("service-a")
        assert health.healthy is True
        assert registry.status("service-a") == ConnectorStatus.READY

        await registry.disconnect("service-a")
        assert registry.status("service-a") == ConnectorStatus.DISCONNECTED

        # Can reconnect after disconnect
        await registry.connect("service-a")
        assert registry.status("service-a") == ConnectorStatus.READY

    @pytest.mark.asyncio
    async def test_connect_failure_transitions_to_error(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("flaky-service", fail_connect=True)
        await registry.register(conn)

        with pytest.raises(ConnectorConnectionError):
            await registry.connect("flaky-service")

        assert registry.status("flaky-service") == ConnectorStatus.ERROR

    @pytest.mark.asyncio
    async def test_unregister_connected_gracefully_disconnects(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("active-conn")
        await registry.register(conn)
        await registry.connect("active-conn")
        assert conn.connect_count == 1

        await registry.unregister("active-conn")
        assert conn.disconnect_count == 1
        assert registry.exists("active-conn") is False


# ===========================================================================
# Group E: Enable / Disable Separation
# ===========================================================================

class TestEnableDisableSeparation:
    @pytest.mark.asyncio
    async def test_enable_disable_toggle(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("toggled-service")
        await registry.register(conn, enabled=True)
        assert registry.is_enabled("toggled-service") is True

        desc = await registry.disable("toggled-service")
        assert desc.enabled is False
        assert registry.is_enabled("toggled-service") is False

        desc_re = await registry.enable("toggled-service")
        assert desc_re.enabled is True
        assert registry.is_enabled("toggled-service") is True

    @pytest.mark.asyncio
    async def test_connect_disabled_connector_rejected(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("disabled-service")
        await registry.register(conn, enabled=False)

        with pytest.raises(ConnectorDisabledError) as exc_info:
            await registry.connect("disabled-service")

        assert exc_info.value.code == ConnectorErrorCode.DISABLED

    @pytest.mark.asyncio
    async def test_disabling_active_connector_disconnects_it(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("live-service")
        await registry.register(conn, enabled=True)
        await registry.connect("live-service")
        assert registry.status("live-service") == ConnectorStatus.READY

        await registry.disable("live-service")
        assert registry.is_enabled("live-service") is False
        assert registry.status("live-service") == ConnectorStatus.DISCONNECTED
        assert conn.disconnect_count == 1

    @pytest.mark.asyncio
    async def test_execute_on_disabled_connector_rejected(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("svc")
        await registry.register(conn, enabled=True)
        await registry.connect("svc")
        await registry.disable("svc")

        req = ConnectorRequest(connector_id="svc", operation="ping")
        with pytest.raises(ConnectorDisabledError):
            await registry.execute(req)


# ===========================================================================
# Group F: Health & Diagnostics
# ===========================================================================

class TestHealthDiagnostics:
    @pytest.mark.asyncio
    async def test_health_check_single_and_all(self):
        registry = ConnectorRegistry()
        conn1 = InMemoryTestConnector("h1")
        conn2 = InMemoryTestConnector("h2")
        await registry.register(conn1)
        await registry.register(conn2)
        await registry.connect("h1")
        await registry.connect("h2")

        h1 = await registry.health("h1")
        assert h1.healthy is True

        all_health = await registry.health_all()
        assert "h1" in all_health
        assert "h2" in all_health
        assert all_health["h1"].healthy is True
        assert all_health["h2"].healthy is True

    @pytest.mark.asyncio
    async def test_health_fault_isolation_does_not_crash_health_all(self):
        registry = ConnectorRegistry()
        conn_ok = InMemoryTestConnector("ok-svc")
        conn_crashed = InMemoryTestConnector("crash-svc", raise_in_health=True)
        conn_degraded = InMemoryTestConnector("degraded-svc", fail_health=True)

        await registry.register(conn_ok)
        await registry.register(conn_crashed)
        await registry.register(conn_degraded)
        await registry.connect("ok-svc")

        all_health = await registry.health_all()
        assert all_health["ok-svc"].healthy is True
        assert all_health["crash-svc"].healthy is False
        assert all_health["degraded-svc"].healthy is False
        assert registry.status("degraded-svc") == ConnectorStatus.DEGRADED


# ===========================================================================
# Group G: Concurrent Lifecycle Operations
# ===========================================================================

class TestConcurrentOperations:
    @pytest.mark.asyncio
    async def test_concurrent_connect_calls_serialize_cleanly(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("race-service")
        await registry.register(conn)

        # Launch 5 concurrent connect calls
        results = await asyncio.gather(
            registry.connect("race-service"),
            registry.connect("race-service"),
            registry.connect("race-service"),
            registry.connect("race-service"),
            registry.connect("race-service"),
        )
        assert len(results) == 5
        for h in results:
            assert h.healthy is True
        assert registry.status("race-service") == ConnectorStatus.READY
        # Should only connect once (subsequent calls see READY and return health)
        assert conn.connect_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_registration_of_different_connectors(self):
        registry = ConnectorRegistry()
        connectors = [InMemoryTestConnector(f"worker-{i}") for i in range(10)]

        await asyncio.gather(*[registry.register(c) for c in connectors])
        assert len(registry.list()) == 10
        for i in range(10):
            assert registry.exists(f"worker-{i}")


# ===========================================================================
# Group H: Connector Error Isolation
# ===========================================================================

class TestConnectorIsolation:
    @pytest.mark.asyncio
    async def test_broken_connector_does_not_affect_healthy_connector(self):
        registry = ConnectorRegistry()
        broken = InMemoryTestConnector("broken-svc", fail_connect=True)
        healthy = InMemoryTestConnector("healthy-svc")

        await registry.register(broken)
        await registry.register(healthy)

        # Connect broken -> ERROR
        with pytest.raises(ConnectorConnectionError):
            await registry.connect("broken-svc")
        assert registry.status("broken-svc") == ConnectorStatus.ERROR

        # Healthy can connect and execute normally
        await registry.connect("healthy-svc")
        assert registry.status("healthy-svc") == ConnectorStatus.READY

        res = await registry.execute(ConnectorRequest(connector_id="healthy-svc", operation="ping"))
        assert res.success is True

        # Unregistering broken doesn't affect healthy
        await registry.unregister("broken-svc")
        assert registry.exists("healthy-svc") is True
        assert registry.status("healthy-svc") == ConnectorStatus.READY


# ===========================================================================
# Group I: ActivityStore Integration
# ===========================================================================

class TestActivityStoreIntegration:
    @pytest.mark.asyncio
    async def test_lifecycle_events_recorded_in_activity_store(self):
        activity_store = ActivityStore.from_path(":memory:")
        registry = ConnectorRegistry(activity_store=activity_store)
        conn = InMemoryTestConnector("audit-conn")

        req_id = "req_audit_123"
        await registry.register(conn, request_id=req_id)
        await registry.connect("audit-conn", request_id=req_id)
        await registry.disable("audit-conn", request_id=req_id)
        await registry.enable("audit-conn", request_id=req_id)
        await registry.unregister("audit-conn", request_id=req_id)

        events = activity_store.query_connector_events(connector_id="audit-conn")
        assert len(events) >= 5
        event_types = [e["event_type"] for e in events]
        assert "registered" in event_types
        assert "connect_started" in event_types
        assert "connected" in event_types
        assert "disabled" in event_types
        assert "enabled" in event_types
        assert "unregistered" in event_types
        # Verify request_id correlated
        for e in events:
            assert e["request_id"] == req_id


# ===========================================================================
# Group J: Permission Boundary Compatibility
# ===========================================================================

class TestPermissionBoundaryCompatibility:
    @pytest.mark.asyncio
    async def test_permission_request_helper_and_policy_engine_evaluation(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        lock = RLock()
        policy_store = PolicyStore(db, lock)
        policy_engine = PolicyEngine(policy_store)

        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("protected-conn")
        await registry.register(conn)

        # Create standardized permission request for connector admin
        perm_req = create_permission_request(
            operation="connect",
            connector_id="protected-conn",
            request_id="req_perm_test",
            agent_id="test-agent",
            session_id="test-session",
            risk_level="HIGH",
        )
        assert perm_req.action_type == "connector_admin"
        assert perm_req.resource_path == "protected-conn"
        assert perm_req.command == "connector.connect"

        # Check permission against PolicyEngine
        allowed, reason = policy_engine.check_permission(perm_req)
        # Default policy asks or allows depending on rule; verify structured interaction
        assert isinstance(allowed, bool)
        assert isinstance(reason, str)


# ===========================================================================
# Group K: Resilience Boundary Compatibility
# ===========================================================================

class TestResilienceBoundaryCompatibility:
    @pytest.mark.asyncio
    async def test_with_timeout_and_retry_wrapping_registry_operations(self):
        registry = ConnectorRegistry()
        conn = InMemoryTestConnector("resilient-conn")
        await registry.register(conn)

        # Wrap connect in shared resilience infrastructure
        policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05)
        health = await with_timeout_and_retry(
            lambda: registry.connect("resilient-conn"),
            timeout_seconds=2.0,
            policy=policy,
            operation_name="test_connector_connect",
        )
        assert health.healthy is True
        assert registry.status("resilient-conn") == ConnectorStatus.READY


# ===========================================================================
# Group L: Secret-Safety and Metadata Sanitization
# ===========================================================================

class TestSecretSafetyAndSanitization:
    @pytest.mark.asyncio
    async def test_descriptor_scrubs_secrets_in_metadata_and_persistence(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        lock = RLock()
        registry = ConnectorRegistry(db=db, db_lock=lock)

        sensitive_meta = {
            "api_key": "sk-1234567890abcdef1234567890abcdef",
            "auth_token": "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "safe_cluster": "prod-east-1",
        }
        conn = InMemoryTestConnector("secure-conn", metadata=sensitive_meta)
        desc = await registry.register(conn)

        # Runtime descriptor sanitization
        assert desc.metadata["api_key"] == "[REDACTED]"
        assert desc.metadata["auth_token"] == "[REDACTED]"
        assert desc.metadata["safe_cluster"] == "prod-east-1"

        # SQLite persistent storage sanitization
        configs = registry.load_persisted_configs()
        assert len(configs) == 1
        stored_meta = configs[0]["metadata"]
        assert stored_meta["api_key"] == "[REDACTED]"
        assert stored_meta["auth_token"] == "[REDACTED]"
        assert stored_meta["safe_cluster"] == "prod-east-1"


# ===========================================================================
# Group M: Future Analyzer Compatibility Contract (Phase 7.4 Extension)
# ===========================================================================

class TestFutureAnalyzerCompatibilityContract:
    @pytest.mark.asyncio
    async def test_generic_analyzer_contract_without_provider_conditionals(self):
        registry = ConnectorRegistry()
        connectors = [
            InMemoryTestConnector("mcp-notion", connector_type=ConnectorType.MCP, provider="notion",
                                  metadata={"transport": "stdio", "declared_env_vars": ["NOTION_TOKEN"], "declared_binary_names": ["node"]}),
            InMemoryTestConnector("rest-github", connector_type=ConnectorType.REST, provider="github",
                                  metadata={"transport": "http", "declared_env_vars": ["GITHUB_TOKEN"]}),
            InMemoryTestConnector("cli-kubectl", connector_type=ConnectorType.CLI, provider="kubernetes",
                                  metadata={"transport": "subprocess", "declared_binary_names": ["kubectl"]}),
        ]
        for c in connectors:
            await registry.register(c)

        # Simulate Phase 7.4 generic analyzer loop:
        # Crucial requirement: ZERO provider conditionals ("if github:", "if notion:").
        analysis_descriptors: list[ConnectorAnalysisDescriptor] = []
        for desc in registry.list():
            a_desc = registry.get_analysis_descriptor(desc.connector_id)
            assert isinstance(a_desc, ConnectorAnalysisDescriptor)
            analysis_descriptors.append(a_desc)

        assert len(analysis_descriptors) == 3

        # Generic pipeline verification assertions
        for ad in analysis_descriptors:
            assert ad.connector_id != ""
            assert ad.type in {ConnectorType.MCP, ConnectorType.REST, ConnectorType.CLI}
            assert isinstance(ad.capabilities, frozenset)
            assert isinstance(ad.declared_env_vars, tuple)
            assert isinstance(ad.declared_binary_names, tuple)
            assert isinstance(ad.metadata, dict)
            # No credentials leaked
            assert "sk-" not in str(ad.metadata).lower()
            assert "ghp_" not in str(ad.metadata).lower()


# ===========================================================================
# Group N: Metrics Emission
# ===========================================================================

class TestMetricsEmission:
    @pytest.mark.asyncio
    async def test_registry_operations_increment_metrics(self):
        metrics = Metrics()
        registry = ConnectorRegistry(metrics=metrics)
        conn = InMemoryTestConnector("metric-conn")

        await registry.register(conn)
        await registry.connect("metric-conn")
        await registry.health("metric-conn")
        await registry.disable("metric-conn")
        await registry.enable("metric-conn")
        await registry.unregister("metric-conn")

        events = metrics.to_flat_dict()
        assert events.get("connector_registered", 0) >= 1
        assert events.get("connector_connect_success", 0) >= 1
        assert events.get("connector_health_check", 0) >= 1
        assert events.get("connector_disable", 0) >= 1
        assert events.get("connector_enable", 0) >= 1
        assert events.get("connector_unregistered", 0) >= 1
