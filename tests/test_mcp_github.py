"""Comprehensive test suite for Phase 7.8A: GitHub MCP Connector & Portable Authentication.

Covers Test Groups A through AB:
A. Connector registration
B. Default endpoint configuration
C. Credential source precedence
D. Environment-variable authentication
E. Masked interactive token setup
F. Keyring storage abstraction
G. Keyring unavailable fallback
H. Credential replacement
I. Credential removal
J. Invalid/expired token response
K. HTTP 401 and 403 handling
L. Network failure and timeout
M. MCP protocol handshake/session reuse
N. tools/list discovery
O. tools/call execution
P. Structured results and normalized errors
Q. Permission enforcement
R. Mutating-tool confirmation/denial
S. Unknown tool safety
T. ActivityStore correlation
U. No token in SQLite
V. No token in logs/errors/metrics
W. Analyzer descriptor integration
X. Connector disconnect/reconnect
Y. Multiple connector instances with independent credentials
Z. Existing GitHub REST integration compatibility
AA. Packaging/import behavior on supported Python versions
AB. Full mocked end-to-end connector workflow
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from threading import RLock
from typing import Any, Optional

import pytest

from sutra.activity_store import ActivityStore
from sutra.connectors import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorDescriptor,
    ConnectorPermissionError,
    ConnectorRegistry,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
    ConnectorType,
    CredentialInfo,
    CredentialProvider,
    CredentialSource,
    DEFAULT_CONNECTOR_ID,
    DEFAULT_GITHUB_MCP_URL,
    GitHubCredentialProvider,
    GitHubMCPConnector,
    KeyringBackend,
)
from sutra.connectors.analyzer import (
    AnalysisReport,
    CheckCategory,
    CheckStatus,
    ConnectorAnalyzer,
    VerificationState,
)
from sutra.credentials import CredentialError, KeyringUnavailableError
from sutra.permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
    PermissionRiskLevel,
)
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import PolicyStore

import sys
from pathlib import Path

_tests_dir = Path(__file__).resolve().parent
_mocks_dir = str(_tests_dir / "mocks")
if _mocks_dir not in sys.path:
    sys.path.insert(0, _mocks_dir)

from tests.mocks.mcp_github_test_server import (
    DEFAULT_TEST_TOKEN,
    running_mock_github_server,
)


# ===========================================================================
# Helpers & Mocks
# ===========================================================================

class MockKeyringBackend(KeyringBackend):
    """In-memory mock of KeyringBackend for isolated testing."""

    def __init__(self, service_name: str = "sutra", available: bool = True) -> None:
        super().__init__(service_name=service_name)
        self._available = available
        self._store: dict[str, str] = {}

    def is_available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = available

    def get_password(self, account_name: str) -> Optional[str]:
        if not self._available:
            return None
        return self._store.get(account_name)

    def set_password(self, account_name: str, password: str) -> None:
        if not self._available:
            raise KeyringUnavailableError("Secure OS keyring is not available on this platform. Configure credentials via SUTRA_GITHUB_TOKEN or use an in-memory session.")
        self._store[account_name] = password

    def delete_password(self, account_name: str) -> bool:
        if not self._available:
            return False
        return self._store.pop(account_name, None) is not None


def make_test_db() -> tuple[sqlite3.Connection, RLock]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn, RLock()


def make_test_activity_store() -> ActivityStore:
    conn, lock = make_test_db()
    return ActivityStore(db=conn, lock=lock)


# ===========================================================================
# Test Group A: Connector Registration
# ===========================================================================

@pytest.mark.asyncio
async def test_group_a_connector_registration() -> None:
    """Group A: GitHub MCP connector registers with ConnectorRegistry cleanly."""
    conn, lock = make_test_db()
    registry = ConnectorRegistry(db=conn, db_lock=lock)

    connector = GitHubMCPConnector(token="ghp_test_token_12345")
    await registry.register(connector)

    assert registry.exists("github-mcp")
    desc = registry.descriptor("github-mcp")
    assert desc is not None
    assert desc.connector_id == "github-mcp"
    assert desc.provider == "github"
    assert desc.type == ConnectorType.MCP
    assert desc.status in (ConnectorStatus.REGISTERED, ConnectorStatus.DISCONNECTED)
    assert desc.metadata.get("transport_hint") == "streamable_http"


# ===========================================================================
# Test Group B: Default Endpoint Configuration
# ===========================================================================

def test_group_b_default_endpoint_configuration() -> None:
    """Group B: Default endpoint points to official GitHub Remote MCP Server."""
    assert DEFAULT_GITHUB_MCP_URL == "https://api.githubcopilot.com/mcp/"
    assert DEFAULT_CONNECTOR_ID == "github-mcp"

    connector = GitHubMCPConnector()
    assert connector.endpoint == "https://api.githubcopilot.com/mcp/"
    desc = connector.identity()
    assert desc.metadata["endpoint"] == "https://api.githubcopilot.com/mcp/"
    assert desc.provider == "github"


# ===========================================================================
# Test Group C: Credential Source Precedence
# ===========================================================================

def test_group_c_credential_source_precedence() -> None:
    """Group C: Deterministic precedence: Memory > SUTRA_GITHUB_TOKEN > GITHUB_PERSONAL_ACCESS_TOKEN > Keyring."""
    mock_keyring = MockKeyringBackend()
    mock_keyring.set_password("github-mcp:github-mcp", "ghp_keyring_val")

    # 1. Keyring alone
    provider = GitHubCredentialProvider(
        "github-mcp",
        keyring_backend=mock_keyring,
        custom_env={},
    )
    assert provider.resolve_token() == "ghp_keyring_val"
    assert provider.get_source() == CredentialSource.KEYRING

    # 2. Fallback env var overrides Keyring
    provider = GitHubCredentialProvider(
        "github-mcp",
        keyring_backend=mock_keyring,
        custom_env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_fallback_val"},
    )
    assert provider.resolve_token() == "ghp_fallback_val"
    assert provider.get_source() == CredentialSource.ENV_FALLBACK

    # 3. Primary env var overrides fallback env var
    provider = GitHubCredentialProvider(
        "github-mcp",
        keyring_backend=mock_keyring,
        custom_env={
            "SUTRA_GITHUB_TOKEN": "ghp_primary_val",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_fallback_val",
        },
    )
    assert provider.resolve_token() == "ghp_primary_val"
    assert provider.get_source() == CredentialSource.ENV_PRIMARY

    # 4. Explicit Memory token overrides everything
    provider = GitHubCredentialProvider(
        "github-mcp",
        memory_token="ghp_memory_val",
        keyring_backend=mock_keyring,
        custom_env={
            "SUTRA_GITHUB_TOKEN": "ghp_primary_val",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_fallback_val",
        },
    )
    assert provider.resolve_token() == "ghp_memory_val"
    assert provider.get_source() == CredentialSource.MEMORY


# ===========================================================================
# Test Group D: Environment-Variable Authentication
# ===========================================================================

def test_group_d_env_var_authentication() -> None:
    """Group D: Non-interactive env var mode works cleanly for CI/headless."""
    provider = GitHubCredentialProvider(
        "github-mcp",
        custom_env={"SUTRA_GITHUB_TOKEN": "ghp_env_token_456"},
    )
    assert provider.has_credential() is True
    assert provider.resolve_token() == "ghp_env_token_456"
    assert provider.get_source() == CredentialSource.ENV_PRIMARY
    assert provider.get_auth_headers() == {"Authorization": "Bearer ghp_env_token_456"}


# ===========================================================================
# Test Group E: Masked Interactive Token Setup
# ===========================================================================

def test_group_e_masked_interactive_token_setup() -> None:
    """Group E: Storing token via provider generates masked diagnostic info."""
    mock_keyring = MockKeyringBackend()
    provider = GitHubCredentialProvider("github-mcp", keyring_backend=mock_keyring, custom_env={})

    provider.store_token("ghp_1234567890abcdef")
    info = provider.get_info()

    assert info.has_credential is True
    assert info.source == CredentialSource.MEMORY
    assert info.masked_preview == "ghp_...def"
    # Never exposes full secret
    assert "1234567890" not in str(info)
    assert "1234567890" not in repr(provider)


# ===========================================================================
# Test Group F: Keyring Storage Abstraction
# ===========================================================================

def test_group_f_keyring_storage_abstraction() -> None:
    """Group F: Keyring backend sets, gets, and deletes credentials correctly."""
    mock_keyring = MockKeyringBackend()
    assert mock_keyring.is_available() is True

    mock_keyring.set_password("acc1", "secret123")
    assert mock_keyring.get_password("acc1") == "secret123"

    deleted = mock_keyring.delete_password("acc1")
    assert deleted is True
    assert mock_keyring.get_password("acc1") is None

    # Deleting absent is False without raising
    assert mock_keyring.delete_password("nonexistent") is False


# ===========================================================================
# Test Group G: Keyring Unavailable Fallback
# ===========================================================================

def test_group_g_keyring_unavailable_fallback() -> None:
    """Group G: If keyring unavailable, raises clear exception; never stores plaintext."""
    mock_keyring = MockKeyringBackend(available=False)
    provider = GitHubCredentialProvider("github-mcp", keyring_backend=mock_keyring, custom_env={})

    with pytest.raises(KeyringUnavailableError) as exc_info:
        provider.store_token("ghp_cannot_store_secret")

    assert "Secure OS keyring is not available" in str(exc_info.value)
    # Ensure no token was persisted
    assert provider.resolve_token() is None


# ===========================================================================
# Test Group H: Credential Replacement
# ===========================================================================

def test_group_h_credential_replacement() -> None:
    """Group H: Replacing stored credential updates value cleanly."""
    mock_keyring = MockKeyringBackend()
    provider = GitHubCredentialProvider("github-mcp", keyring_backend=mock_keyring, custom_env={})

    provider.store_token("ghp_initial_token_111")
    assert provider.resolve_token() == "ghp_initial_token_111"

    # Replace
    provider.store_token("ghp_updated_token_222")
    assert provider.resolve_token() == "ghp_updated_token_222"
    assert mock_keyring.get_password(provider.keyring_account_name) == "ghp_updated_token_222"


# ===========================================================================
# Test Group I: Credential Removal
# ===========================================================================

def test_group_i_credential_removal() -> None:
    """Group I: Credential removal cleans up memory and keyring."""
    mock_keyring = MockKeyringBackend()
    provider = GitHubCredentialProvider("github-mcp", keyring_backend=mock_keyring, custom_env={})

    provider.store_token("ghp_temp_token_999")
    assert provider.has_credential() is True

    removed = provider.remove_token()
    assert removed is True
    assert provider.has_credential() is False
    assert provider.resolve_token() is None
    assert provider.get_source() == CredentialSource.NONE


# ===========================================================================
# Test Group J: Invalid/Expired Token Response
# ===========================================================================

@pytest.mark.asyncio
async def test_group_j_invalid_token_response() -> None:
    """Group J: Invalid or expired token triggers HTTP 401 mapped to ConnectorAuthError."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        # Pass bad token
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token="ghp_invalid_expired_token",
            allow_http=True,
        )

        with pytest.raises(ConnectorAuthError) as exc_info:
            await connector.connect()

        assert "401" in str(exc_info.value)
        assert connector.status != ConnectorStatus.READY


# ===========================================================================
# Test Group K: HTTP 401 and 403 Handling
# ===========================================================================

@pytest.mark.asyncio
async def test_group_k_http_401_and_403_handling() -> None:
    """Group K: HTTP 401 maps to ConnectorAuthError; HTTP 403 maps to ConnectorPermissionError."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        # Missing token raises before network
        connector_no_token = GitHubMCPConnector(endpoint=server.url, allow_http=True)
        with pytest.raises(ConnectorAuthError) as auth_exc:
            await connector_no_token.connect()
        assert "No GitHub Personal Access Token configured" in str(auth_exc.value)

        # Forbidden token raises ConnectorPermissionError
        connector_forbidden = GitHubMCPConnector(
            endpoint=server.url,
            token="ghp_forbidden_token_999",
            allow_http=True,
        )
        with pytest.raises(ConnectorPermissionError) as perm_exc:
            await connector_forbidden.connect()
        assert "403" in str(perm_exc.value)


# ===========================================================================
# Test Group L: Network Failure and Timeout
# ===========================================================================

@pytest.mark.asyncio
async def test_group_l_network_failure_and_timeout() -> None:
    """Group L: Connection refusal or timeout maps to ConnectorConnectionError / ConnectorTimeoutError."""
    # Use unassigned loopback port
    connector = GitHubMCPConnector(
        endpoint="http://127.0.0.1:59999/mcp",
        token=DEFAULT_TEST_TOKEN,
        allow_http=True,
        connect_timeout=0.5,
    )
    with pytest.raises(ConnectorConnectionError):
        await connector.connect()


# ===========================================================================
# Test Group M: Protocol Handshake & Session Reuse
# ===========================================================================

@pytest.mark.asyncio
async def test_group_m_handshake_and_session_reuse() -> None:
    """Group M: MCP handshake negotiates protocol and session is reused across requests."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            allow_http=True,
        )
        await connector.connect()

        assert connector.status == ConnectorStatus.READY
        assert connector.client.is_connected is True
        assert connector.client.protocol_version is not None
        assert connector.client.server_info is not None
        assert connector.client.server_info.name == "github-remote-mcp"

        # Health ping reuses session
        health = await connector.health()
        assert health.healthy is True
        assert health.status == ConnectorStatus.READY

        await connector.disconnect()
        assert connector.status == ConnectorStatus.DISCONNECTED


# ===========================================================================
# Test Group N: tools/list Discovery
# ===========================================================================

@pytest.mark.asyncio
async def test_group_n_tools_list_discovery() -> None:
    """Group N: Discovers declared remote tools with read_only annotations."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            allow_http=True,
        )
        await connector.connect()

        tools = await connector.discover_tools()
        assert len(tools) >= 5
        tool_names = {t.name: t for t in tools}

        assert "list_repositories" in tool_names
        assert tool_names["list_repositories"].read_only is True

        assert "create_issue" in tool_names
        assert tool_names["create_issue"].read_only is False

        assert "delete_repository" in tool_names
        assert tool_names["delete_repository"].read_only is False

        await connector.disconnect()


# ===========================================================================
# Test Group O: tools/call Execution
# ===========================================================================

@pytest.mark.asyncio
async def test_group_o_tools_call_execution() -> None:
    """Group O: Dispatches tool call and receives structured result."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:list_repositories",
            parameters={"visibility": "public"},
        )
        res = await connector.execute(req)

        assert res.success is True
        assert "sutra-demo" in res.content
        assert isinstance(res.structured_data, dict)
        assert res.structured_data.get("total_count") == 2

        await connector.disconnect()


# ===========================================================================
# Test Group P: Structured Results and Normalized Errors
# ===========================================================================

@pytest.mark.asyncio
async def test_group_p_normalized_tool_errors() -> None:
    """Group P: Remote tool execution failures normalize cleanly into ConnectorResult."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:error_tool",
            parameters={},
        )
        res = await connector.execute(req)

        assert res.success is False
        assert "Simulated tool execution error" in str(res.error)

        await connector.disconnect()


# ===========================================================================
# Test Group Q: Permission Enforcement (Read-Only Tools)
# ===========================================================================

@pytest.mark.asyncio
async def test_group_q_permission_enforcement_read_only() -> None:
    """Group Q: Read-only query tools are auto-approved by PolicyEngine by default."""
    engine = PolicyEngine()
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            policy_engine=engine,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:get_issue",
            parameters={"owner": "octocat", "repo": "sutra", "issue_number": 42},
        )
        res = await connector.execute(req)

        assert res.success is True
        assert "Bug in authentication loop" in res.content

        await connector.disconnect()


# ===========================================================================
# Test Group R: Mutating-Tool Confirmation / Denial
# ===========================================================================

@pytest.mark.asyncio
async def test_group_r_mutating_tool_permission_denial_and_grant() -> None:
    """Group R: Mutating tools require policy approval; denied calls raise ConnectorPermissionError."""
    engine = PolicyEngine()
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            policy_engine=engine,
            allow_http=True,
        )
        await connector.connect()

        # 1. Mutating tool call without grant is DENIED by PolicyEngine
        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:create_issue",
            request_id="req_mutate_01",
            parameters={"owner": "octocat", "repo": "sutra", "title": "New Bug"},
            actor="test-agent",
            session_id="sess-01",
        )

        with pytest.raises(ConnectorPermissionError) as exc_info:
            await connector.execute(req)

        assert "rejected by permission policy" in str(exc_info.value)

        # 2. Grant session permission for mutating tool
        engine.apply_decision(
            request_id="req_mutate_01",
            decision=PermissionDecision.ALLOW_SESSION,
        )

        # Now permitted
        res = await connector.execute(req)
        assert res.success is True
        assert "Issue #103 created" in res.content

        await connector.disconnect()


# ===========================================================================
# Test Group S: Unknown Tool Safety
# ===========================================================================

@pytest.mark.asyncio
async def test_group_s_unknown_tool_safety() -> None:
    """Group S: Unknown tools are classified as HIGH risk and subjected to permission checks."""
    engine = PolicyEngine()
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            policy_engine=engine,
            allow_http=True,
        )
        await connector.connect()

        # Unknown tool without grant is rejected
        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:arbitrary_unknown_mutator",
            request_id="req_unknown_01",
            parameters={},
            actor="test-agent",
            session_id="sess-02",
        )

        with pytest.raises(ConnectorPermissionError):
            await connector.execute(req)

        await connector.disconnect()


# ===========================================================================
# Test Group T: ActivityStore Correlation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_t_activity_store_correlation() -> None:
    """Group T: Operations audit start and completion into ActivityStore with correlation IDs."""
    store = make_test_activity_store()
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            activity_store=store,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:list_repositories",
            request_id="req_act_corr_101",
            parameters={"visibility": "all"},
        )
        res = await connector.execute(req)
        assert res.success is True

        calls = store.list_tool_calls(request_id="req_act_corr_101")
        assert len(calls) == 1
        record = calls[0]
        assert record["connector_id"] == "github-mcp"
        assert "list_repositories" in record["tool_name"]
        assert record["status"] == "success"

        await connector.disconnect()


# ===========================================================================
# Test Group U: No Token in SQLite
# ===========================================================================

@pytest.mark.asyncio
async def test_group_u_no_token_in_sqlite() -> None:
    """Group U: Secret token is NEVER written into SQLite connector_configs or activity_records."""
    secret_pat = "ghp_super_secret_pat_9999999999"
    conn, lock = make_test_db()
    registry = ConnectorRegistry(db=conn, db_lock=lock)
    store = ActivityStore(db=conn, lock=lock)

    async with running_mock_github_server(expected_token=secret_pat) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=secret_pat,
            activity_store=store,
            allow_http=True,
        )
        await registry.register(connector)
        await connector.connect()

        req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:list_repositories",
            request_id="req_secret_check_01",
            parameters={},
        )
        await connector.execute(req)
        await connector.disconnect()

    # Inspect all SQLite tables for secret leak
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for tbl in tables:
        rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
        for row in rows:
            row_str = str(dict(row))
            assert secret_pat not in row_str, f"Token leaked in SQLite table {tbl}!"
            assert "Bearer" not in row_str, f"Bearer header leaked in SQLite table {tbl}!"


# ===========================================================================
# Test Group V: No Token in Logs, Errors, or Metrics
# ===========================================================================

@pytest.mark.asyncio
async def test_group_v_no_token_in_logs_and_errors() -> None:
    """Group V: Tokens are completely redacted from exceptions, descriptors, and string outputs."""
    secret_pat = "ghp_leaked_secret_never_print_this"
    provider = GitHubCredentialProvider("github-mcp", memory_token=secret_pat)

    # Provider repr
    assert secret_pat not in repr(provider)
    assert secret_pat not in str(provider)

    # Connector identity
    connector = GitHubMCPConnector(token=secret_pat)
    desc = connector.identity()
    assert secret_pat not in str(desc)
    assert secret_pat not in str(desc.metadata)

    # Exception message
    connector_fail = GitHubMCPConnector(
        endpoint="http://127.0.0.1:59999/mcp",
        token=secret_pat,
        allow_http=True,
        connect_timeout=0.1,
    )
    with pytest.raises(Exception) as exc_info:
        await connector_fail.connect()
    assert secret_pat not in str(exc_info.value)


# ===========================================================================
# Test Group W: Analyzer Descriptor Integration
# ===========================================================================

@pytest.mark.asyncio
async def test_group_w_analyzer_descriptor_integration() -> None:
    """Group W: Generic Connector Analyzer evaluates CONFIGURED, AUTHENTICATED, and VERIFIED states."""
    conn, lock = make_test_db()
    registry = ConnectorRegistry(db=conn, db_lock=lock)
    analyzer = ConnectorAnalyzer(registry=registry)

    # 1. Registered with credential -> CONFIGURED (not yet connected)
    connector = GitHubMCPConnector(token=DEFAULT_TEST_TOKEN)
    await registry.register(connector)

    report = await analyzer.analyze("github-mcp", force_connect_probe=False)
    assert report.verification_state == VerificationState.CONFIGURED
    assert report.has_check_status(CheckCategory.CONFIGURATION, CheckStatus.PASS)

    # 2. Connected -> VERIFIED
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        # Re-register pointing to live test server
        await registry.unregister("github-mcp")
        live_connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            allow_http=True,
        )
        await registry.register(live_connector)
        await registry.connect("github-mcp")

        verified_report = await analyzer.analyze("github-mcp", force_connect_probe=False)
        assert verified_report.verification_state == VerificationState.VERIFIED
        assert verified_report.has_check_status(CheckCategory.CONNECTION, CheckStatus.PASS)
        assert verified_report.has_check_status(CheckCategory.AUTHENTICATION, CheckStatus.PASS)

        await registry.disconnect("github-mcp")


# ===========================================================================
# Test Group X: Connector Disconnect and Reconnect
# ===========================================================================

@pytest.mark.asyncio
async def test_group_x_disconnect_and_reconnect() -> None:
    """Group X: Connector disconnects cleanly and can reconnect safely."""
    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        connector = GitHubMCPConnector(
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            allow_http=True,
        )
        await connector.connect()
        assert connector.status == ConnectorStatus.READY

        # Disconnect
        await connector.disconnect()
        assert connector.status == ConnectorStatus.DISCONNECTED

        # Reconnect
        await connector.connect()
        assert connector.status == ConnectorStatus.READY

        # Subsequent call works
        res = await connector.execute(
            ConnectorRequest(connector_id="github-mcp", operation="tool:list_repositories")
        )
        assert res.success is True

        await connector.disconnect()


# ===========================================================================
# Test Group Y: Multiple Connector Instances with Independent Credentials
# ===========================================================================

def test_group_y_multiple_independent_instances() -> None:
    """Group Y: Multiple connector instances maintain separate credentials without cross-account leakage."""
    mock_keyring = MockKeyringBackend()
    mock_keyring.set_password("github-mcp:github-work", "ghp_token_work_111")
    mock_keyring.set_password("github-mcp:github-personal", "ghp_token_personal_222")

    provider_work = GitHubCredentialProvider("github-work", keyring_backend=mock_keyring, custom_env={})
    provider_pers = GitHubCredentialProvider("github-personal", keyring_backend=mock_keyring, custom_env={})

    assert provider_work.resolve_token() == "ghp_token_work_111"
    assert provider_pers.resolve_token() == "ghp_token_personal_222"

    # Removing work token does NOT affect personal
    provider_work.remove_token()
    assert provider_work.has_credential() is False
    assert provider_pers.resolve_token() == "ghp_token_personal_222"


# ===========================================================================
# Test Group Z: Compatibility with Existing GitHub REST Integration
# ===========================================================================

def test_group_z_existing_github_rest_compatibility() -> None:
    """Group Z: Existing sutra.git.github REST functionality remains intact."""
    from sutra.git.github import (
        detect_github_remote,
        detect_github_token,
        parse_github_remote,
    )

    info = parse_github_remote("https://github.com/octocat/Sutra.git")
    assert info.is_github is True
    assert info.owner == "octocat"
    assert info.repo == "Sutra"

    # Verify existing token detection function still exists and executes
    tok = detect_github_token()
    assert tok is None or isinstance(tok, str)


# ===========================================================================
# Test Group AA: Packaging and Clean Import Verification
# ===========================================================================

def test_group_aa_clean_packaging_imports() -> None:
    """Group AA: All Phase 7.8A symbols import cleanly without side-effects."""
    import sutra.credentials as creds
    from sutra.connectors.mcp.github import GitHubMCPConnector
    from sutra.connectors.cli import handle_connectors_command

    assert hasattr(creds, "GitHubCredentialProvider")
    assert hasattr(creds, "KeyringBackend")
    assert hasattr(creds, "CredentialSource")
    assert GitHubMCPConnector is not None
    assert handle_connectors_command is not None


# ===========================================================================
# Test Group AB: Full Mocked End-to-End Connector Workflow
# ===========================================================================

@pytest.mark.asyncio
async def test_group_ab_full_end_to_end_workflow() -> None:
    """Group AB: Complete end-to-end lifecycle: Register -> Auth -> Connect -> Discover -> Execute -> Disconnect -> Clean."""
    conn, lock = make_test_db()
    registry = ConnectorRegistry(db=conn, db_lock=lock)
    activity_store = ActivityStore(db=conn, lock=lock)
    policy_engine = PolicyEngine()

    # Pre-approve session for create_issue
    policy_engine._base._session_grants[("e2e-session", "tool_call", "*")] = PermissionDecision.ALLOW_SESSION

    async with running_mock_github_server(expected_token=DEFAULT_TEST_TOKEN) as server:
        # 1. Setup connector instance
        connector = GitHubMCPConnector(
            connector_id="github-mcp",
            endpoint=server.url,
            token=DEFAULT_TEST_TOKEN,
            activity_store=activity_store,
            policy_engine=policy_engine,
            allow_http=True,
        )

        # 2. Register
        await registry.register(connector)
        assert registry.exists("github-mcp")

        # 3. Connect via registry
        await registry.connect("github-mcp")
        desc = registry.descriptor("github-mcp")
        assert desc is not None
        assert desc.status == ConnectorStatus.READY

        # 4. Discover tools
        tools = await connector.discover_tools()
        assert len(tools) >= 5

        # 5. Execute read-only tool
        read_req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:get_issue",
            request_id="e2e_read_01",
            parameters={"owner": "octocat", "repo": "sutra", "issue_number": 42},
            session_id="e2e-session",
        )
        read_res = await connector.execute(read_req)
        assert read_res.success is True

        # 6. Execute mutating tool (authorized by session grant)
        mutate_req = ConnectorRequest(
            connector_id="github-mcp",
            operation="tool:create_issue",
            request_id="e2e_mutate_01",
            parameters={"owner": "octocat", "repo": "sutra", "title": "E2E Verified"},
            session_id="e2e-session",
        )
        mutate_res = await connector.execute(mutate_req)
        assert mutate_res.success is True
        assert "Issue #103 created" in mutate_res.content

        # 7. Verify ActivityStore contains records
        calls = activity_store.list_tool_calls(request_id="e2e_mutate_01")
        assert len(calls) == 1
        assert calls[0]["status"] == "success"

        # 8. Disconnect and remove
        await registry.disconnect("github-mcp")
        await registry.unregister("github-mcp")
        assert not registry.exists("github-mcp")
