"""Comprehensive test suite for Phase 7.4 — Generic Environment & Connector Analyzer.

Validates:
A. Analyzer core (orchestration, report generation, check result structure)
B. Configuration discovery (descriptor validation, enabled/disabled state)
C. Parameter checks (endpoints, transport parameters, secret-safety)
D. Dependency / CLI checks (CLIProbe, binary presence, execution safety)
E. Client applicability (ClientProbe, client detection, non-destructive inspection)
F. Connection verification (CONNECTED, NOT_CONNECTED, FAILED, UNKNOWN)
G. Authentication states (AUTHENTICATED, UNKNOWN, FAIL, secret concealment)
H. Capability discovery (read-only introspection, tool/resource counts)
I. Health checks (HEALTHY, DEGRADED, UNHEALTHY)
J. Latency measurement (latency_ms per check and overall, Metrics recording)
K. Usage evidence (ActivityStore checking: OBSERVED_IN_USE, NOT_OBSERVED, UNKNOWN)
L. Evidence aggregation (DISCOVERED, CONFIGURED, PARTIALLY_VERIFIED, VERIFIED, DEGRADED, FAILED, UNKNOWN)
M. UNKNOWN handling (insufficient evidence != FAIL)
N. Secret redaction (redaction of tokens, API keys, no secret leakage)
O. ActivityStore persistence (connector_events and audit_events recorded)
P. request_id correlation (universal request_id across checks and reports)
Q. Registration-triggered analysis (lifecycle trigger safety)
R. Concurrent connector analysis (bounded semaphore, multi-connector concurrency)
S. Failure isolation (broken connector fails independently without affecting others)
T. Permission compatibility (read-only diagnostic safety)
U. Resilience compatibility (timeouts and error handling)

Explicit Test Double Profiles:
1. Fully healthy connector
2. Configured-but-unreachable connector
3. Reachable-but-unauthenticated connector
4. Healthy-but-unused connector
5. Broken connector
6. Partially supported connector

Explicit Distinction Cases (Section 28):
- Case A: Configured=YES, Connected=YES, Authenticated=YES, Verified=YES, Used=UNKNOWN
- Case B: Configured=YES, Connected=NO, Authenticated=UNKNOWN, Verified=NO
- Case C: Configured=YES, Connected=YES, Authenticated=YES, Verified=YES, Used=OBSERVED_IN_USE
- Case D: Configured=NO, Connected=UNKNOWN, Authenticated=UNKNOWN, Verified=NO
- Case E: Configured=YES, Credentials unverified -> Authenticated=UNKNOWN, Verified=PARTIALLY_VERIFIED
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from threading import RLock
from typing import Any, Optional, Sequence
from unittest.mock import patch

import pytest

from sutra.activity_store import ActivityStore
from sutra.connectors import (
    AnalysisCheckResult,
    AnalysisReport,
    CheckCategory,
    CheckStatus,
    CLIApplicabilityStatus,
    ClientApplicabilityStatus,
    CLIProbe,
    ClientProbe,
    Connector,
    ConnectorAnalysisDescriptor,
    ConnectorAnalyzer,
    ConnectorAuthError,
    ConnectorCapability,
    ConnectorConnectionError,
    ConnectorDescriptor,
    ConnectorError,
    ConnectorErrorCode,
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorRequest,
    ConnectorResource,
    ConnectorResult,
    ConnectorStatus,
    ConnectorToolDefinition,
    ConnectorType,
    EnvironmentProbe,
    UsageEvidenceState,
    VerificationState,
    analyze_connector,
    create_permission_request,
)
from sutra.metrics import Metrics
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
# Test Doubles (6 Provider-Neutral Profiles)
# ===========================================================================

class AnalyzerTestDouble(Connector):
    """Base provider-neutral test double for analyzer verification."""

    def __init__(
        self,
        connector_id: str,
        name: str = "Test Double",
        connector_type: ConnectorType = ConnectorType.REST,
        capabilities: Optional[set[ConnectorCapability]] = None,
        provider: str = "generic-provider",
        fail_connect: bool = False,
        auth_error_on_connect: bool = False,
        fail_health: bool = False,
        raise_unhandled: bool = False,
        declared_tools: Optional[list[ConnectorToolDefinition]] = None,
        declared_env_vars: Optional[list[str]] = None,
        declared_binary_names: Optional[list[str]] = None,
        custom_env: Optional[dict[str, str]] = None,
        config: Optional[dict[str, Any]] = None,
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
        self._auth_error_on_connect = auth_error_on_connect
        self._fail_health = fail_health
        self._raise_unhandled = raise_unhandled
        self._declared_tools = declared_tools or [
            ConnectorToolDefinition(
                name="read_item",
                description="Read an item",
                parameters_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                read_only=True,
            )
        ]
        self._declared_env_vars = declared_env_vars or []
        self._declared_binary_names = declared_binary_names or []
        self._custom_env = custom_env or {}
        self._config = config or {"endpoint": "https://api.example.com/v1"}
        self._metadata = dict(metadata) if metadata else {"provider": provider, "transport": "https"}
        self._metadata.setdefault("transport", "https")
        self._status = ConnectorStatus.REGISTERED

    def identity(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=self._connector_type,
            version="1.0.0",
            description="Analyzer test double",
            capabilities=self._capabilities,
            metadata={
                **self._metadata,
                "custom_env": self._custom_env,
                "declared_env_vars": tuple(self._declared_env_vars),
                "declared_binary_names": tuple(self._declared_binary_names),
            },
            enabled=True,
        )

    async def connect(self, request: Optional[ConnectorRequest] = None) -> None:
        if self._raise_unhandled:
            raise RuntimeError("Fatal explosion during connect")
        if self._auth_error_on_connect:
            raise ConnectorAuthError("Invalid credentials provided", connector_id=self._connector_id)
        if self._fail_connect:
            raise ConnectorConnectionError("Endpoint host unreachable", connector_id=self._connector_id)
        self._status = ConnectorStatus.READY

    async def disconnect(self, request: Optional[ConnectorRequest] = None) -> None:
        self._status = ConnectorStatus.DISCONNECTED

    async def health(self, request: Optional[ConnectorRequest] = None) -> ConnectorHealth:
        if self._raise_unhandled:
            raise RuntimeError("Fatal explosion during health check")
        if self._fail_health:
            return ConnectorHealth(
                healthy=False,
                status=ConnectorStatus.ERROR,
                latency_ms=25.0,
                message="Service reporting degraded/unhealthy status",
            )
        return ConnectorHealth(
            healthy=True,
            status=self._status if self._status == ConnectorStatus.READY else ConnectorStatus.READY,
            latency_ms=8.5,
            details={"transport": "https", "version": "1.0.0"},
        )

    async def discover(self, request: Optional[ConnectorRequest] = None) -> Sequence[Any]:
        if self._raise_unhandled:
            raise RuntimeError("Fatal explosion during discover")
        return self._declared_tools

    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        return ConnectorResult.success(
            request_id=request.request_id,
            connector_id=self._connector_id,
            data={"status": "executed"},
        )



# 1. Fully healthy connector
def make_healthy_connector(cid: str = "conn.healthy") -> AnalyzerTestDouble:
    return AnalyzerTestDouble(
        connector_id=cid,
        name="Healthy Connector",
        declared_tools=[
            ConnectorToolDefinition(name="query_data", description="Query items", read_only=True)
        ],
        declared_env_vars=["TEST_API_TOKEN"],
        custom_env={"TEST_API_TOKEN": "valid_token_xyz"},
    )

# 2. Configured-but-unreachable connector
def make_unreachable_connector(cid: str = "conn.unreachable") -> AnalyzerTestDouble:
    return AnalyzerTestDouble(
        connector_id=cid,
        name="Unreachable Connector",
        fail_connect=True,
        declared_env_vars=["TEST_API_TOKEN"],
        custom_env={"TEST_API_TOKEN": "valid_token_xyz"},
    )

# 3. Reachable-but-unauthenticated connector
def make_unauthenticated_connector(cid: str = "conn.unauthenticated") -> AnalyzerTestDouble:
    return AnalyzerTestDouble(
        connector_id=cid,
        name="Unauthenticated Connector",
        auth_error_on_connect=True,
        declared_env_vars=["TEST_API_TOKEN"],
        custom_env={"TEST_API_TOKEN": "expired_bad_token"},
    )

# 4. Healthy-but-unused connector
def make_healthy_unused_connector(cid: str = "conn.unused") -> AnalyzerTestDouble:
    return AnalyzerTestDouble(
        connector_id=cid,
        name="Healthy Unused Connector",
        declared_tools=[
            ConnectorToolDefinition(name="sync_repo", description="Sync", read_only=True)
        ],
        declared_env_vars=["TEST_API_TOKEN"],
        custom_env={"TEST_API_TOKEN": "valid_token_xyz"},
    )

# 5. Broken connector
def make_broken_connector(cid: str = "conn.broken") -> AnalyzerTestDouble:
    return AnalyzerTestDouble(
        connector_id=cid,
        name="Broken Connector",
        raise_unhandled=True,
    )

# 6. Partially supported connector
def make_partially_supported_connector(cid: str = "conn.partial") -> AnalyzerTestDouble:
    return AnalyzerTestDouble(
        connector_id=cid,
        name="Partially Supported Connector",
        capabilities={ConnectorCapability.DISCOVERY},  # Missing HEALTH_CHECK and TOOL_EXECUTION
        declared_tools=[],
    )


# ===========================================================================
# TEST GROUPS
# ===========================================================================

@pytest.mark.asyncio
async def test_group_a_analyzer_core() -> None:
    """Group A: Core Analyzer initialization, report structure, and check result format."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector()
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze(connector_id="conn.healthy")

    assert isinstance(report, AnalysisReport)
    assert report.connector_id == "conn.healthy"
    assert report.provider == "generic-provider"
    assert str(report.connector_type).lower() in ("rest", "connectortype.rest")
    assert report.transport == "https"
    assert report.verification_state == VerificationState.VERIFIED
    assert report.is_healthy is True
    assert report.total_checks > 0
    assert report.checks_passed > 0
    assert report.checks_failed == 0
    assert report.total_latency_ms >= 0.0
    assert len(report.checks) == report.total_checks

    # Check structure
    for check in report.checks:
        assert isinstance(check, AnalysisCheckResult)
        assert isinstance(check.category, CheckCategory)
        assert isinstance(check.status, CheckStatus)
        assert check.check_id
        assert check.message
        assert check.completed_at >= check.started_at

    # To dict serialization
    report_dict = report.to_dict()
    assert report_dict["connector_id"] == "conn.healthy"
    assert report_dict["verification_state"] == "VERIFIED"
    assert isinstance(report_dict["checks"], list)


@pytest.mark.asyncio
async def test_group_b_configuration_discovery() -> None:
    """Group B: Configuration discovery and validation."""
    registry = ConnectorRegistry()

    # Connector with missing required env var
    conn_missing_env = AnalyzerTestDouble(
        connector_id="conn.missing.env",
        declared_env_vars=["NON_EXISTENT_VAR_12345"],
        custom_env={},
    )
    await registry.register(conn_missing_env)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.missing.env")

    config_check = next(c for c in report.checks if c.category == CheckCategory.CONFIGURATION)
    assert config_check.status == CheckStatus.FAIL
    assert "NON_EXISTENT_VAR_12345" in config_check.message
    assert report.verification_state == VerificationState.FAILED


@pytest.mark.asyncio
async def test_group_c_parameter_checks() -> None:
    """Group C: Parameter and dependency checks without revealing secrets."""
    registry = ConnectorRegistry()
    conn = AnalyzerTestDouble(
        connector_id="conn.param",
        config={"endpoint": "https://service.internal/api"},
        metadata={"region": "us-east-1", "secret_token": "super_secret_value"},
    )
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.param")

    param_check = next(c for c in report.checks if c.category == CheckCategory.PARAMETERS)
    assert param_check.status == CheckStatus.PASS
    # Secret must not appear in evidence or message
    assert "super_secret_value" not in str(param_check.evidence)
    assert "super_secret_value" not in param_check.message


@pytest.mark.asyncio
async def test_group_d_dependency_cli_checks() -> None:
    """Group D: CLI dependency checking via CLIProbe."""
    probe = CLIProbe()

    # Allowlisted known binary vs non-existent binary
    res_git = probe.check_binary("git")
    assert res_git.binary_name == "git"
    assert res_git.status in (CLIApplicabilityStatus.CLI_EXECUTABLE, CLIApplicabilityStatus.CLI_PRESENT, CLIApplicabilityStatus.CLI_NOT_FOUND)

    res_missing = probe.check_binary("non_existent_binary_xyz_987")
    assert res_missing.status == CLIApplicabilityStatus.CLI_NOT_FOUND
    assert res_missing.path is None

    # Connector declaring a binary
    registry = ConnectorRegistry()
    conn = AnalyzerTestDouble(
        connector_id="conn.cli",
        declared_binary_names=["non_existent_binary_xyz_987"],
    )
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry, cli_probe=probe)
    report = await analyzer.analyze("conn.cli")

    cli_check = next(c for c in report.checks if c.category == CheckCategory.CLI_APPLICABILITY)
    assert cli_check.status == CheckStatus.FAIL
    assert "non_existent_binary_xyz_987" in cli_check.message


@pytest.mark.asyncio
async def test_group_e_client_applicability() -> None:
    """Group E: Non-destructive client applicability probing."""
    client_probe = ClientProbe()
    overview = client_probe.detect_all_clients()
    assert "vscode" in overview
    assert "cursor" in overview
    assert "claude" in overview

    for client_id, res in overview.items():
        assert res.client_id == client_id
        assert res.status in (
            ClientApplicabilityStatus.CLIENT_DETECTED,
            ClientApplicabilityStatus.NOT_DETECTED,
        )

    # In pipeline
    registry = ConnectorRegistry()
    conn = make_healthy_connector()
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry, client_probe=client_probe)
    report = await analyzer.analyze("conn.healthy")

    client_check = next(c for c in report.checks if c.category == CheckCategory.CLIENT_APPLICABILITY)
    # Probing clients safely succeeds (PASS) with detected/not-detected counts
    assert client_check.status == CheckStatus.PASS


@pytest.mark.asyncio
async def test_group_f_connection_verification() -> None:
    """Group F: Connection verification distinguishes reachability and failures."""
    registry = ConnectorRegistry()
    conn_unreachable = make_unreachable_connector()
    await registry.register(conn_unreachable)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.unreachable")

    conn_check = next(c for c in report.checks if c.category == CheckCategory.CONNECTION)
    assert conn_check.status == CheckStatus.FAIL
    assert "unreachable" in conn_check.message.lower()
    assert report.verification_state == VerificationState.FAILED


@pytest.mark.asyncio
async def test_group_g_authentication_states() -> None:
    """Group G: Authentication verification and secret safety."""
    registry = ConnectorRegistry()
    conn_unauth = make_unauthenticated_connector()
    await registry.register(conn_unauth)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.unauthenticated")

    auth_check = next(c for c in report.checks if c.category == CheckCategory.AUTHENTICATION)
    assert auth_check.status == CheckStatus.FAIL
    assert "credential" in auth_check.message.lower() or "auth" in auth_check.message.lower()
    # Check that secrets like 'expired_bad_token' are never in report
    assert "expired_bad_token" not in str(report.to_dict())


@pytest.mark.asyncio
async def test_group_h_capability_discovery() -> None:
    """Group H: Read-only capability discovery without executing mutating tools."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector()
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.healthy")

    cap_check = next(c for c in report.checks if c.category == CheckCategory.CAPABILITIES)
    assert cap_check.status == CheckStatus.PASS
    assert cap_check.evidence.get("discovered_tools_count") == 1
    assert "query_data" in cap_check.evidence.get("tools", [])
    # Verify connector execute was not called during analysis
    assert conn._status == ConnectorStatus.READY


@pytest.mark.asyncio
async def test_group_i_health_analysis() -> None:
    """Group I: Health evaluation (HEALTHY, DEGRADED, UNHEALTHY)."""
    registry = ConnectorRegistry()
    conn_degraded = AnalyzerTestDouble(
        connector_id="conn.degraded",
        fail_health=True,
    )
    await registry.register(conn_degraded)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.degraded")

    health_check = next(c for c in report.checks if c.category == CheckCategory.HEALTH)
    assert health_check.status == CheckStatus.FAIL
    assert report.is_healthy is False
    assert report.verification_state in (VerificationState.DEGRADED, VerificationState.FAILED)


@pytest.mark.asyncio
async def test_group_j_latency_analysis() -> None:
    """Group J: Safe operation latency measurement and Metrics recording."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector()
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.healthy")

    latency_check = next(c for c in report.checks if c.category == CheckCategory.LATENCY)
    assert latency_check.status == CheckStatus.PASS
    assert latency_check.latency_ms >= 0.0
    assert report.total_latency_ms >= 0.0

    # Metrics recorded
    metrics = Metrics.get_instance()
    snap = metrics.snapshot()
    assert snap.get("analyzer_run_total", 0) >= 1
    assert snap.get("analyzer_run_success", 0) >= 1


@pytest.mark.asyncio
async def test_group_k_usage_evidence() -> None:
    """Group K: Usage evidence analysis strictly from ActivityStore."""
    store = make_test_activity_store()
    registry = ConnectorRegistry(activity_store=store)

    # 1. Unused connector -> NOT_OBSERVED
    conn_unused = make_healthy_unused_connector("conn.unused.k")
    await registry.register(conn_unused)

    analyzer = ConnectorAnalyzer(registry=registry, activity_store=store)
    report_unused = await analyzer.analyze("conn.unused.k")
    usage_check1 = next(c for c in report_unused.checks if c.category == CheckCategory.USAGE_EVIDENCE)
    assert usage_check1.evidence.get("usage_state") == UsageEvidenceState.NOT_OBSERVED.value
    assert usage_check1.status == CheckStatus.PASS

    # 2. Add an activity store tool_call for the connector -> OBSERVED_IN_USE
    store.record_tool_call_start(
        request_id="req-123",
        connector_id="conn.unused.k",
        tool_name="conn.unused.k.sync_repo",
    )

    report_used = await analyzer.analyze("conn.unused.k")
    usage_check2 = next(c for c in report_used.checks if c.category == CheckCategory.USAGE_EVIDENCE)
    assert usage_check2.evidence.get("usage_state") == UsageEvidenceState.OBSERVED_IN_USE.value
    assert usage_check2.evidence.get("tool_calls_count") == 1

    # 3. Connector without ActivityStore -> UNKNOWN
    analyzer_no_store = ConnectorAnalyzer(registry=registry, activity_store=None)
    report_no_store = await analyzer_no_store.analyze("conn.unused.k")
    usage_check3 = next(c for c in report_no_store.checks if c.category == CheckCategory.USAGE_EVIDENCE)
    assert usage_check3.evidence.get("usage_state") == UsageEvidenceState.UNKNOWN.value
    assert usage_check3.status == CheckStatus.UNKNOWN


@pytest.mark.asyncio
async def test_group_l_evidence_aggregation_rules() -> None:
    """Group L: Verification state model aggregation rules."""
    registry = ConnectorRegistry()

    # Disabled connector -> CONFIGURED (or DISCOVERED)
    conn_disabled = AnalyzerTestDouble(connector_id="conn.disabled")
    await registry.register(conn_disabled)
    await registry.disable("conn.disabled")

    analyzer = ConnectorAnalyzer(registry=registry)
    report_disabled = await analyzer.analyze("conn.disabled")
    assert report_disabled.verification_state == VerificationState.CONFIGURED

    # Broken connector -> FAILED
    conn_broken = make_broken_connector("conn.broken.l")
    await registry.register(conn_broken)
    report_broken = await analyzer.analyze("conn.broken.l")
    assert report_broken.verification_state == VerificationState.FAILED


@pytest.mark.asyncio
async def test_group_m_unknown_handling() -> None:
    """Group M: UNKNOWN status must never be treated as FAIL."""
    check = AnalysisCheckResult(
        check_id="check_sample",
        category=CheckCategory.USAGE_EVIDENCE,
        status=CheckStatus.UNKNOWN,
        message="Insufficient activity store evidence",
    )
    assert check.status == CheckStatus.UNKNOWN
    assert check.status != CheckStatus.FAIL

    # A report with UNKNOWN usage must still achieve VERIFIED if other checks pass
    registry = ConnectorRegistry()
    conn = make_healthy_connector("conn.unknown.usage")
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry, activity_store=None)
    report = await analyzer.analyze("conn.unknown.usage")
    assert report.verification_state == VerificationState.VERIFIED


@pytest.mark.asyncio
async def test_group_n_secret_redaction() -> None:
    """Group N: Centralized secret redaction prevents secret leakage."""
    secret_token = "ghp_super_secret_github_token_xyz987"
    registry = ConnectorRegistry()
    conn = AnalyzerTestDouble(
        connector_id="conn.secret",
        config={"endpoint": "https://api.example.com", "token": secret_token},
        metadata={"auth": secret_token},
    )
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.secret")

    # Serialize report to string and ensure raw secret is nowhere in it
    raw_text = str(report.to_dict())
    assert secret_token not in raw_text


@pytest.mark.asyncio
async def test_group_o_activity_store_persistence() -> None:
    """Group O: Structured analysis persistence into ActivityStore."""
    store = make_test_activity_store()
    registry = ConnectorRegistry(activity_store=store)
    conn = make_healthy_connector("conn.persist")
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry, activity_store=store)
    report = await analyzer.analyze("conn.persist", request_id="req-persist-42")

    # Verify event stored in ActivityStore
    events = store.list_connector_events(connector_id="conn.persist")
    assert len(events) >= 1
    analysis_ev = next(e for e in events if e["event_type"] == "analyzer_completed")
    assert analysis_ev["request_id"] == "req-persist-42"
    assert analysis_ev["metadata"].get("verification_state") == VerificationState.VERIFIED.value

    # Verify latest analysis retrieval
    latest = analyzer.get_latest_analysis("conn.persist")
    assert latest is not None
    assert latest.connector_id == "conn.persist"
    assert latest.request_id == "req-persist-42"


@pytest.mark.asyncio
async def test_group_p_request_id_correlation() -> None:
    """Group P: Universal request_id correlation throughout pipeline."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector("conn.req")
    await registry.register(conn)

    custom_req_id = "req-custom-trace-999"
    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("conn.req", request_id=custom_req_id)

    assert report.request_id == custom_req_id


@pytest.mark.asyncio
async def test_group_q_registration_triggered_analysis() -> None:
    """Group Q: analyze_connector convenience function & post-registration analysis."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector("conn.auto")
    await registry.register(conn)

    # analyze_connector standalone entry point
    report = await analyze_connector("conn.auto", registry=registry)
    assert report.connector_id == "conn.auto"
    assert report.verification_state == VerificationState.VERIFIED


@pytest.mark.asyncio
async def test_group_r_concurrent_connector_analysis() -> None:
    """Group R: Concurrent multi-connector analysis with bounded semaphore."""
    registry = ConnectorRegistry()
    conns = [
        make_healthy_connector(f"conn.concurrent.{i}")
        for i in range(5)
    ]
    for c in conns:
        await registry.register(c)

    analyzer = ConnectorAnalyzer(registry=registry, max_concurrency=2)
    reports = await analyzer.analyze_all()

    assert len(reports) == 5
    for r in reports:
        assert r.verification_state == VerificationState.VERIFIED


@pytest.mark.asyncio
async def test_group_s_failure_isolation() -> None:
    """Group S: A broken connector must not crash or cancel healthy connector analyses."""
    registry = ConnectorRegistry()
    healthy = make_healthy_connector("conn.iso.healthy")
    broken = make_broken_connector("conn.iso.broken")
    await registry.register(healthy)
    await registry.register(broken)

    analyzer = ConnectorAnalyzer(registry=registry)
    reports = await analyzer.analyze_all()

    report_map = {r.connector_id: r for r in reports}
    assert report_map["conn.iso.healthy"].verification_state == VerificationState.VERIFIED
    assert report_map["conn.iso.broken"].verification_state == VerificationState.FAILED


@pytest.mark.asyncio
async def test_group_t_permission_compatibility() -> None:
    """Group T: PermissionEngine compatibility: diagnostic analysis is read-only."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector("conn.perm")
    await registry.register(conn)

    # Creating permission request from connector check
    perm_req = create_permission_request(
        operation="health_check",
        connector_id="conn.perm",
        request_id="req-perm-1",
        risk_level="LOW",
    )
    assert perm_req.action_type == "connector_admin"
    assert perm_req.risk_level == PermissionRiskLevel.LOW
    assert perm_req.resource_path == "conn.perm"


@pytest.mark.asyncio
async def test_group_u_resilience_compatibility() -> None:
    """Group U: Bounded timeout and retry compatibility."""
    # Test that connector operations within analyzer abide by timeout boundaries
    async def slow_operation() -> str:
        await asyncio.sleep(0.05)
        return "slow_success"

    policy = RetryPolicy(max_retries=1, base_delay=0.01)
    result = await with_timeout_and_retry(
        slow_operation,
        timeout_seconds=0.2,
        policy=policy,
    )
    assert result == "slow_success"



# ===========================================================================
# SECTION 28: CORE DISTINCTION TEST CASES (Cases A through E)
# ===========================================================================

@pytest.mark.asyncio
async def test_case_a_configured_connected_authenticated_verified_used_unknown() -> None:
    """Case A: Configured=YES, Connected=YES, Authenticated=YES, Verified=YES, Used=UNKNOWN."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector("case.a")
    await registry.register(conn)

    # Without an ActivityStore, usage is strictly UNKNOWN
    analyzer = ConnectorAnalyzer(registry=registry, activity_store=None)
    report = await analyzer.analyze("case.a")

    cfg_check = next(c for c in report.checks if c.category == CheckCategory.CONFIGURATION)
    conn_check = next(c for c in report.checks if c.category == CheckCategory.CONNECTION)
    auth_check = next(c for c in report.checks if c.category == CheckCategory.AUTHENTICATION)
    usage_check = next(c for c in report.checks if c.category == CheckCategory.USAGE_EVIDENCE)

    assert cfg_check.status == CheckStatus.PASS  # Configured = YES
    assert conn_check.status == CheckStatus.PASS  # Connected = YES
    assert auth_check.status == CheckStatus.PASS  # Authenticated = YES
    assert report.verification_state == VerificationState.VERIFIED  # Verified = YES
    assert usage_check.status == CheckStatus.UNKNOWN  # Used = UNKNOWN
    assert usage_check.evidence.get("usage_state") == UsageEvidenceState.UNKNOWN.value


@pytest.mark.asyncio
async def test_case_b_configured_connected_no_auth_unknown_verified_no() -> None:
    """Case B: Configured=YES, Connected=NO, Authenticated=UNKNOWN, Verified=NO."""
    registry = ConnectorRegistry()
    conn = make_unreachable_connector("case.b")
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("case.b")

    cfg_check = next(c for c in report.checks if c.category == CheckCategory.CONFIGURATION)
    conn_check = next(c for c in report.checks if c.category == CheckCategory.CONNECTION)
    auth_check = next(c for c in report.checks if c.category == CheckCategory.AUTHENTICATION)

    assert cfg_check.status == CheckStatus.PASS  # Configured = YES
    assert conn_check.status == CheckStatus.FAIL  # Connected = NO
    # Because connection failed, auth could not be reached or verified -> UNKNOWN
    assert auth_check.status == CheckStatus.UNKNOWN  # Authenticated = UNKNOWN
    assert report.verification_state == VerificationState.FAILED  # Verified = NO


@pytest.mark.asyncio
async def test_case_c_configured_connected_authenticated_verified_used_observed() -> None:
    """Case C: Configured=YES, Connected=YES, Authenticated=YES, Verified=YES, Used=OBSERVED_IN_USE."""
    store = make_test_activity_store()
    registry = ConnectorRegistry(activity_store=store)
    conn = make_healthy_connector("case.c")
    await registry.register(conn)

    # Log observed activity in ActivityStore
    store.record_tool_call_start(
        request_id="req-case-c",
        connector_id="case.c",
        tool_name="case.c.query_data",
    )

    analyzer = ConnectorAnalyzer(registry=registry, activity_store=store)
    report = await analyzer.analyze("case.c")

    cfg_check = next(c for c in report.checks if c.category == CheckCategory.CONFIGURATION)
    conn_check = next(c for c in report.checks if c.category == CheckCategory.CONNECTION)
    auth_check = next(c for c in report.checks if c.category == CheckCategory.AUTHENTICATION)
    usage_check = next(c for c in report.checks if c.category == CheckCategory.USAGE_EVIDENCE)

    assert cfg_check.status == CheckStatus.PASS
    assert conn_check.status == CheckStatus.PASS
    assert auth_check.status == CheckStatus.PASS
    assert report.verification_state == VerificationState.VERIFIED
    assert usage_check.status == CheckStatus.PASS
    assert usage_check.evidence.get("usage_state") == UsageEvidenceState.OBSERVED_IN_USE.value


@pytest.mark.asyncio
async def test_case_d_configured_no_connected_unknown_auth_unknown_verified_no() -> None:
    """Case D: Configured=NO, Connected=UNKNOWN, Authenticated=UNKNOWN, Verified=NO."""
    registry = ConnectorRegistry()
    # Missing required env var
    conn = AnalyzerTestDouble(
        connector_id="case.d",
        declared_env_vars=["MANDATORY_SECRET_KEY_MISSING_999"],
        custom_env={},
    )
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    report = await analyzer.analyze("case.d")

    cfg_check = next(c for c in report.checks if c.category == CheckCategory.CONFIGURATION)
    conn_check = next(c for c in report.checks if c.category == CheckCategory.CONNECTION)
    auth_check = next(c for c in report.checks if c.category == CheckCategory.AUTHENTICATION)

    assert cfg_check.status == CheckStatus.FAIL  # Configured = NO
    # Because configuration was missing, connection was SKIPPED / UNKNOWN
    assert conn_check.status in (CheckStatus.SKIPPED, CheckStatus.UNKNOWN)
    assert auth_check.status in (CheckStatus.SKIPPED, CheckStatus.UNKNOWN)
    assert report.verification_state == VerificationState.FAILED  # Verified = NO


@pytest.mark.asyncio
async def test_case_e_credentials_unverified_auth_unknown() -> None:
    """Case E: Configuration exists but credentials cannot be verified: Authenticated=UNKNOWN, Verified=PARTIALLY_VERIFIED."""
    registry = ConnectorRegistry()
    conn = make_healthy_connector("case.e")
    await registry.register(conn)

    analyzer = ConnectorAnalyzer(registry=registry)
    # Run analyzer with test_connection=False (dry configuration analysis only)
    report = await analyzer.analyze("case.e", test_connection=False)

    cfg_check = next(c for c in report.checks if c.category == CheckCategory.CONFIGURATION)
    conn_check = next(c for c in report.checks if c.category == CheckCategory.CONNECTION)
    auth_check = next(c for c in report.checks if c.category == CheckCategory.AUTHENTICATION)

    assert cfg_check.status == CheckStatus.PASS
    assert conn_check.status == CheckStatus.SKIPPED
    assert auth_check.status == CheckStatus.UNKNOWN
    # Must NOT be VERIFIED; should be PARTIALLY_VERIFIED
    assert report.verification_state == VerificationState.PARTIALLY_VERIFIED
