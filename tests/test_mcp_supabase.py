"""Comprehensive test suite for Phase 7.8C: Supabase Remote MCP Connector & Authentication.

Covers Test Groups A through Z:
A. Connector registration
B. Default endpoint and URL construction
C. Project-reference validation
D. Read-only default configuration
E. Feature-group filtering
F. Authentication missing
G. Successful mocked authentication
H. OAuth state/PKCE validation
I. OAuth denial/cancellation
J. Token refresh and expiry handling
K. Headless/environment-variable authentication
L. HTTP 401 and 403 errors
M. Network timeout and connection failure
N. MCP session negotiation
O. tools/list
P. tools/call
Q. Structured data responses
R. Mutating operation denied by default
S. Unknown tool treated as high risk
T. Project-instance isolation
U. ActivityStore correlation
V. Zero secret persistence
W. Analyzer integration
X. Disconnect/reconnect
Y. GitHub MCP compatibility
Z. Full mocked end-to-end connector workflow
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
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorRegistry,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
    ConnectorTimeoutError,
    ConnectorType,
    CredentialInfo,
    CredentialProvider,
    CredentialSource,
    DEFAULT_GITHUB_MCP_URL,
    DEFAULT_SUPABASE_MCP_URL,
    GitHubMCPConnector,
    HttpMCPTransport,
    KeyringBackend,
    MCPOAuthClient,
    OAuthFlowConfig,
    OAuthToken,
    SupabaseCredentialProvider,
    SupabaseMCPConnector,
    build_supabase_mcp_url,
    validate_project_ref,
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

from tests.mocks.mcp_supabase_test_server import (
    DEFAULT_TEST_PROJECT_REF,
    DEFAULT_TEST_SUPABASE_TOKEN,
    running_mock_supabase_server,
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
            raise KeyringUnavailableError("Secure OS keyring is not available on this platform.")
        self._store[account_name] = password

    def delete_password(self, account_name: str) -> bool:
        if not self._available:
            return False
        if account_name in self._store:
            del self._store[account_name]
            return True
        return False


# ===========================================================================
# A. Connector Registration
# ===========================================================================

def test_group_a_connector_registration() -> None:
    """Verify Supabase connector registration, identity, and type descriptor."""
    keyring = MockKeyringBackend()
    provider = SupabaseCredentialProvider("supabase-test", keyring_backend=keyring)
    connector = SupabaseMCPConnector(
        connector_id="supabase-test",
        project_ref="myproject123",
        credential_provider=provider,
    )

    desc = connector.identity()
    assert desc.connector_id == "supabase-test"
    assert desc.provider == "supabase"
    assert desc.type == ConnectorType.MCP
    assert desc.metadata["project_ref"] == "myproject123"
    assert desc.metadata["read_only"] is True
    assert "transport_hint" in desc.metadata


# ===========================================================================
# B. Default Endpoint and URL Construction
# ===========================================================================

def test_group_b_default_endpoint_and_url_construction() -> None:
    """Verify official hosted endpoint default and safe query parameter construction."""
    assert DEFAULT_SUPABASE_MCP_URL == "https://mcp.supabase.com/mcp"

    url = build_supabase_mcp_url(
        endpoint=DEFAULT_SUPABASE_MCP_URL,
        project_ref="proj-xyz-789",
        read_only=True,
        features=("docs", "database"),
    )
    assert url.startswith("https://mcp.supabase.com/mcp?")
    assert "project_ref=proj-xyz-789" in url
    assert "read_only=true" in url
    assert "features=docs%2Cdatabase" in url or "features=docs,database" in url


# ===========================================================================
# C. Project-Reference Validation
# ===========================================================================

def test_group_c_project_reference_validation() -> None:
    """Verify project_ref validation rejects traversal, invalid chars, and empty strings."""
    assert validate_project_ref("valid-ref-123_45") == "valid-ref-123_45"

    with pytest.raises(ValueError, match="cannot be empty"):
        validate_project_ref("   ")

    with pytest.raises(ValueError, match="alphanumeric"):
        validate_project_ref("invalid/ref/path")

    with pytest.raises(ValueError, match="alphanumeric"):
        validate_project_ref("proj?query=1&evil=true")

    with pytest.raises(ValueError, match="alphanumeric"):
        validate_project_ref("proj;drop table users")


# ===========================================================================
# D. Read-Only Default Configuration
# ===========================================================================

def test_group_d_read_only_default_configuration() -> None:
    """Verify default read_only is True, and url construction honors it."""
    connector = SupabaseMCPConnector(
        connector_id="supabase-ro-check",
        credential_provider=SupabaseCredentialProvider("supabase-ro-check", memory_token="dummy"),
    )
    assert connector.read_only is True
    assert "read_only=true" in connector.endpoint

    custom_rw = SupabaseMCPConnector(
        connector_id="supabase-rw-check",
        read_only=False,
        credential_provider=SupabaseCredentialProvider("supabase-rw-check", memory_token="dummy"),
    )
    assert custom_rw.read_only is False
    assert "read_only=false" in custom_rw.endpoint


# ===========================================================================
# E. Feature-Group Filtering
# ===========================================================================

def test_group_e_feature_group_filtering() -> None:
    """Verify minimum feature set default and custom filtering."""
    connector = SupabaseMCPConnector(
        connector_id="supabase-feat-check",
        credential_provider=SupabaseCredentialProvider("supabase-feat-check", memory_token="dummy"),
    )
    assert "docs" in connector.features
    assert "database" in connector.features

    custom_feat = SupabaseMCPConnector(
        connector_id="supabase-docs-only",
        features=("docs",),
        credential_provider=SupabaseCredentialProvider("supabase-docs-only", memory_token="dummy"),
    )
    assert custom_feat.features == ("docs",)
    assert "features=docs" in custom_feat.endpoint


# ===========================================================================
# F. Authentication Missing
# ===========================================================================

@pytest.mark.asyncio
async def test_group_f_authentication_missing() -> None:
    """Verify connect() raises ConnectorAuthError when no credentials exist."""
    keyring = MockKeyringBackend()
    provider = SupabaseCredentialProvider("supabase-missing-auth", keyring_backend=keyring, custom_env={})
    connector = SupabaseMCPConnector(
        connector_id="supabase-missing-auth",
        credential_provider=provider,
    )

    with pytest.raises(ConnectorAuthError) as exc_info:
        await connector.connect()
    assert "No Supabase Access Token configured" in str(exc_info.value)


# ===========================================================================
# G. Successful Mocked Authentication
# ===========================================================================

@pytest.mark.asyncio
async def test_group_g_successful_mocked_authentication() -> None:
    """Verify successful connect and handshake against mock server with valid PAT."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider(
            "supabase-test-auth",
            memory_token=DEFAULT_TEST_SUPABASE_TOKEN,
        )
        connector = SupabaseMCPConnector(
            connector_id="supabase-test-auth",
            endpoint=f"{base_url}/mcp",
            project_ref=DEFAULT_TEST_PROJECT_REF,
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()
        assert connector.client.is_connected
        assert connector.project_access_verified is True
        await connector.disconnect()


# ===========================================================================
# H. OAuth State / PKCE Validation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_h_oauth_state_pkce_validation() -> None:
    """Verify PKCE challenge calculation, state verification, and token exchange."""
    import httpx

    async def simulate_browser_visit(url: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.get(url, follow_redirects=True)

    async with running_mock_supabase_server() as (base_url, state):
        oauth_cfg = OAuthFlowConfig(
            authorization_endpoint=f"{base_url}/v1/oauth/authorize",
            token_endpoint=f"{base_url}/v1/oauth/token",
            client_id="sutra-test-client",
            loopback_port=9988,
            timeout_seconds=5.0,
        )
        oauth_client = MCPOAuthClient(oauth_cfg)

        token = await oauth_client.authenticate_interactive(
            open_browser=False,
            browser_opener=simulate_browser_visit,
        )
        assert token.access_token == "sbp_oauth_access_token_valid"
        assert token.refresh_token == "sbp_oauth_refresh_token_valid"
        assert not token.is_expired()


# ===========================================================================
# I. OAuth Denial / Cancellation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_i_oauth_denial_cancellation() -> None:
    """Verify OAuth denial maps cleanly to OAuthCancelledError."""
    import httpx
    from sutra.connectors.mcp.oauth import OAuthCancelledError

    async def simulate_browser_visit(url: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.get(url, follow_redirects=True)

    async with running_mock_supabase_server() as (base_url, state):
        state["simulate_oauth_denial"] = True
        oauth_cfg = OAuthFlowConfig(
            authorization_endpoint=f"{base_url}/v1/oauth/authorize",
            token_endpoint=f"{base_url}/v1/oauth/token",
            client_id="sutra-test-client",
            loopback_port=9989,
            timeout_seconds=5.0,
        )
        oauth_client = MCPOAuthClient(oauth_cfg)

        with pytest.raises(OAuthCancelledError, match="cancelled or denied"):
            await oauth_client.authenticate_interactive(
                open_browser=False,
                browser_opener=simulate_browser_visit,
            )


# ===========================================================================
# J. Token Refresh and Expiry Handling
# ===========================================================================

@pytest.mark.asyncio
async def test_group_j_token_refresh_and_expiry() -> None:
    """Verify OAuth token refresh and auto-refresh in SupabaseCredentialProvider."""
    async with running_mock_supabase_server() as (base_url, state):
        oauth_cfg = OAuthFlowConfig(
            authorization_endpoint=f"{base_url}/v1/oauth/authorize",
            token_endpoint=f"{base_url}/v1/oauth/token",
            client_id="sutra-test-client",
        )
        oauth_client = MCPOAuthClient(oauth_cfg)

        # Refresh token directly
        refreshed = await oauth_client.refresh_token("sbp_oauth_refresh_token_valid")
        assert refreshed.access_token == "sbp_oauth_access_token_refreshed"

        # Auto-refresh via provider
        expired_token = OAuthToken(
            access_token="sbp_old_token",
            refresh_token="sbp_oauth_refresh_token_valid",
            expires_at=time.time() - 100,  # Expired
        )
        keyring = MockKeyringBackend()
        provider = SupabaseCredentialProvider(
            "supabase-refresh-test",
            keyring_backend=keyring,
            oauth_client=oauth_client,
        )
        provider.store_oauth_token(expired_token)

        new_tok = await provider.refresh_if_needed()
        assert new_tok == "sbp_oauth_access_token_refreshed"
        assert provider.resolve_token() == "sbp_oauth_access_token_refreshed"


# ===========================================================================
# K. Headless / Environment-Variable Authentication
# ===========================================================================

def test_group_k_environment_variable_authentication() -> None:
    """Verify deterministic precedence: Memory > Instance Env > Primary Env > Fallback Env > Keyring."""
    keyring = MockKeyringBackend()
    keyring.set_password("supabase-mcp:test-inst", "keyring_token")

    env_map = {
        "SUTRA_SUPABASE_ACCESS_TOKEN_TEST_INST": "inst_env_token",
        "SUTRA_SUPABASE_ACCESS_TOKEN": "primary_env_token",
        "SUPABASE_ACCESS_TOKEN": "fallback_env_token",
    }
    provider = SupabaseCredentialProvider("test-inst", keyring_backend=keyring, custom_env=env_map)

    # 1. In-memory override
    provider.set_memory_token("memory_token")
    assert provider.resolve_token() == "memory_token"
    assert provider.get_source() == CredentialSource.MEMORY

    # 2. Instance env
    provider.set_memory_token(None)
    assert provider.resolve_token() == "inst_env_token"
    assert provider.get_source() == CredentialSource.ENV_PRIMARY

    # 3. Primary env
    del env_map["SUTRA_SUPABASE_ACCESS_TOKEN_TEST_INST"]
    assert provider.resolve_token() == "primary_env_token"
    assert provider.get_source() == CredentialSource.ENV_PRIMARY

    # 4. Fallback env
    del env_map["SUTRA_SUPABASE_ACCESS_TOKEN"]
    assert provider.resolve_token() == "fallback_env_token"
    assert provider.get_source() == CredentialSource.ENV_FALLBACK

    # 5. Keyring
    del env_map["SUPABASE_ACCESS_TOKEN"]
    assert provider.resolve_token() == "keyring_token"
    assert provider.get_source() == CredentialSource.KEYRING


# ===========================================================================
# L. HTTP 401 and 403 Errors
# ===========================================================================

@pytest.mark.asyncio
async def test_group_l_http_401_and_403_errors() -> None:
    """Verify HTTP 401 and 403 normalization to ConnectorAuthError and ConnectorPermissionError."""
    async with running_mock_supabase_server() as (base_url, state):
        # Invalid token (401)
        bad_prov = SupabaseCredentialProvider("test-401", memory_token="invalid_token")
        c401 = SupabaseMCPConnector(
            connector_id="test-401",
            endpoint=f"{base_url}/mcp",
            credential_provider=bad_prov,
            allow_http=True,
        )
        with pytest.raises(ConnectorAuthError):
            await c401.connect()

        # Forbidden token (403)
        forbidden_prov = SupabaseCredentialProvider("test-403", memory_token="sbp_forbidden_token_999")
        c403 = SupabaseMCPConnector(
            connector_id="test-403",
            endpoint=f"{base_url}/mcp",
            credential_provider=forbidden_prov,
            allow_http=True,
        )
        with pytest.raises(ConnectorPermissionError):
            await c403.connect()


# ===========================================================================
# M. Network Timeout and Connection Failure
# ===========================================================================

@pytest.mark.asyncio
async def test_group_m_network_timeout_and_connection_failure() -> None:
    """Verify unreachable endpoint maps cleanly to ConnectorConnectionError."""
    prov = SupabaseCredentialProvider("test-unreachable", memory_token="dummy_token")
    connector = SupabaseMCPConnector(
        connector_id="test-unreachable",
        endpoint="http://127.0.0.1:54321/nonexistent",
        credential_provider=prov,
        connect_timeout=0.2,
        request_timeout=0.2,
        allow_http=True,
    )
    with pytest.raises(ConnectorConnectionError):
        await connector.connect()


# ===========================================================================
# N. MCP Session Negotiation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_n_mcp_session_negotiation() -> None:
    """Verify protocol version negotiation and health ping."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-neg", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-neg",
            endpoint=f"{base_url}/mcp",
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()
        assert connector.client.protocol_version is not None
        health = await connector.health()
        assert health.healthy is True
        assert health.latency_ms is not None
        await connector.disconnect()


# ===========================================================================
# O. tools/list Discovery
# ===========================================================================

@pytest.mark.asyncio
async def test_group_o_tools_list_discovery() -> None:
    """Verify tool discovery returns properly annotated ConnectorToolDefinitions."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-tools", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-tools",
            endpoint=f"{base_url}/mcp",
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()
        tools = await connector.discover_tools()
        assert len(tools) >= 4
        names = {t.name for t in tools}
        assert "get_project_url" in names
        assert "list_tables" in names
        assert "search_docs" in names
        await connector.disconnect()


# ===========================================================================
# P. tools/call Execution
# ===========================================================================

@pytest.mark.asyncio
async def test_group_p_tools_call_execution() -> None:
    """Verify tool invocation execution through connector boundary."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-call", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-call",
            endpoint=f"{base_url}/mcp",
            project_ref="my-sample-proj",
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="test-call",
            operation="tool:get_project_url",
            parameters={},
            request_id="req_test_call_001",
        )
        res = await connector.execute(req)
        assert res.success is True
        assert "https://my-sample-proj.supabase.co" in res.content
        await connector.disconnect()


# ===========================================================================
# Q. Structured Data Responses
# ===========================================================================

@pytest.mark.asyncio
async def test_group_q_structured_data_responses() -> None:
    """Verify structured data is returned and preserved."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-struct", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-struct",
            endpoint=f"{base_url}/mcp",
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="test-struct",
            operation="tool:list_tables",
            parameters={},
            request_id="req_test_struct_001",
        )
        res = await connector.execute(req)
        assert res.success is True
        assert isinstance(res.structured_data, dict)
        assert "tables" in res.structured_data
        await connector.disconnect()


# ===========================================================================
# R. Mutating Operation Denied by Default
# ===========================================================================

@pytest.mark.asyncio
async def test_group_r_mutating_operation_denied_by_default() -> None:
    """Verify mutating operations (apply_migration) are rejected by read_only default."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-ro-enforce", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-ro-enforce",
            endpoint=f"{base_url}/mcp",
            read_only=True,  # Default
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="test-ro-enforce",
            operation="tool:apply_migration",
            parameters={"sql": "CREATE TABLE test (id int);"},
            request_id="req_test_ro_001",
        )
        with pytest.raises(ConnectorPermissionError, match="read-only mode"):
            await connector.execute(req)

        await connector.disconnect()


# ===========================================================================
# S. Unknown Tool Treated as High Risk
# ===========================================================================

@pytest.mark.asyncio
async def test_group_s_unknown_tool_high_risk() -> None:
    """Verify unknown tool is classified as non-read-only and rejected under read_only=True."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-unknown-tool", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-unknown-tool",
            endpoint=f"{base_url}/mcp",
            read_only=True,
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="test-unknown-tool",
            operation="tool:custom_unknown_action",
            parameters={},
            request_id="req_test_unknown_001",
        )
        # Treated as mutating -> rejected under read_only mode
        with pytest.raises(ConnectorPermissionError):
            await connector.execute(req)

        await connector.disconnect()


# ===========================================================================
# T. Project-Instance Isolation
# ===========================================================================

def test_group_t_project_instance_isolation() -> None:
    """Verify independent instances have isolated credential namespaces and project references."""
    keyring = MockKeyringBackend()
    p1 = SupabaseCredentialProvider("supabase-proj-alpha", keyring_backend=keyring)
    p2 = SupabaseCredentialProvider("supabase-proj-beta", keyring_backend=keyring)

    p1.store_token("token_alpha_111")
    p2.store_token("token_beta_222")

    assert p1.resolve_token() == "token_alpha_111"
    assert p2.resolve_token() == "token_beta_222"

    c1 = SupabaseMCPConnector(
        connector_id="supabase-proj-alpha",
        project_ref="alpha-ref",
        credential_provider=p1,
    )
    c2 = SupabaseMCPConnector(
        connector_id="supabase-proj-beta",
        project_ref="beta-ref",
        credential_provider=p2,
    )

    assert c1.project_ref == "alpha-ref"
    assert c2.project_ref == "beta-ref"
    assert "project_ref=alpha-ref" in c1.endpoint
    assert "project_ref=beta-ref" in c2.endpoint


# ===========================================================================
# U. ActivityStore Correlation
# ===========================================================================

@pytest.mark.asyncio
async def test_group_u_activity_store_correlation(tmp_path: Path) -> None:
    """Verify tool execution events are recorded in ActivityStore with request correlation."""
    db_path = tmp_path / "activity_test.db"
    conn = sqlite3.connect(db_path)
    store = ActivityStore(db=conn, lock=RLock())

    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-activity", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-activity",
            endpoint=f"{base_url}/mcp",
            credential_provider=provider,
            activity_store=store,
            allow_http=True,
        )
        await connector.connect()

        req = ConnectorRequest(
            connector_id="test-activity",
            operation="tool:get_project_url",
            parameters={},
            request_id="req_correlate_999",
        )
        res = await connector.execute(req)
        assert res.success is True

        # Verify event in store
        calls = store.list_tool_calls(request_id="req_correlate_999")
        assert len(calls) >= 1
        assert any(c["request_id"] == "req_correlate_999" for c in calls)
        assert calls[0]["tool_name"] in ("get_project_url", "test-activity.get_project_url")

        await connector.disconnect()


# ===========================================================================
# V. Zero Secret Persistence
# ===========================================================================

def test_group_v_zero_secret_persistence() -> None:
    """Verify tokens are never echoed in identity, metadata, repr, or strings."""
    raw_secret = "sbp_super_secret_token_never_leak_xyz"
    provider = SupabaseCredentialProvider("test-zero-leak", memory_token=raw_secret)
    connector = SupabaseMCPConnector(
        connector_id="test-zero-leak",
        credential_provider=provider,
    )

    desc = connector.identity()
    meta_str = str(desc.metadata)
    assert raw_secret not in meta_str
    assert raw_secret not in repr(provider)
    assert raw_secret not in str(provider)
    assert raw_secret not in repr(connector)

    info = provider.get_info()
    assert info.masked_preview is not None
    assert raw_secret not in info.masked_preview


# ===========================================================================
# W. Analyzer Integration
# ===========================================================================

@pytest.mark.asyncio
async def test_group_w_analyzer_integration(tmp_path: Path) -> None:
    """Verify Generic Connector Analyzer verifies Supabase connector without custom branching."""
    db_path = tmp_path / "analyzer_test.db"
    conn = sqlite3.connect(db_path)
    registry = ConnectorRegistry(db=conn, db_lock=RLock())

    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("supabase-analyzer", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="supabase-analyzer",
            endpoint=f"{base_url}/mcp",
            project_ref=DEFAULT_TEST_PROJECT_REF,
            credential_provider=provider,
            allow_http=True,
        )
        await registry.register(connector)

        analyzer = ConnectorAnalyzer(registry=registry)
        report = await analyzer.analyze("supabase-analyzer")

        assert report.connector_id == "supabase-analyzer"
        assert report.verification_state in (VerificationState.VERIFIED, VerificationState.PARTIALLY_VERIFIED)
        assert report.has_check_status(CheckCategory.CONFIGURATION, CheckStatus.PASS)
        assert report.has_check_status(CheckCategory.CONNECTION, CheckStatus.PASS)


# ===========================================================================
# X. Disconnect / Reconnect
# ===========================================================================

@pytest.mark.asyncio
async def test_group_x_disconnect_reconnect() -> None:
    """Verify clean disconnect, status reset, and reconnection."""
    async with running_mock_supabase_server() as (base_url, state):
        provider = SupabaseCredentialProvider("test-reconnect", memory_token=DEFAULT_TEST_SUPABASE_TOKEN)
        connector = SupabaseMCPConnector(
            connector_id="test-reconnect",
            endpoint=f"{base_url}/mcp",
            credential_provider=provider,
            allow_http=True,
        )
        await connector.connect()
        assert connector.client.is_connected
        await connector.disconnect()
        assert not connector.client.is_connected

        # Reconnect
        await connector.connect()
        assert connector.client.is_connected
        await connector.disconnect()


# ===========================================================================
# Y. GitHub MCP Compatibility
# ===========================================================================

def test_group_y_github_mcp_compatibility() -> None:
    """Verify GitHub MCP connector and existing architecture remain fully intact."""
    from sutra.credentials import GitHubCredentialProvider

    gh_provider = GitHubCredentialProvider("github-mcp-compat", memory_token="ghp_test_token")
    gh_connector = GitHubMCPConnector(
        connector_id="github-mcp-compat",
        credential_provider=gh_provider,
    )
    desc = gh_connector.identity()
    assert desc.provider == "github"
    assert desc.type == ConnectorType.MCP
    assert "https://api.githubcopilot.com/mcp/" in gh_connector.endpoint


# ===========================================================================
# Z. Full Mocked End-to-End Connector Workflow
# ===========================================================================

@pytest.mark.asyncio
async def test_group_z_full_mocked_end_to_end_workflow(tmp_path: Path) -> None:
    """Verify full end-to-end flow: register -> authenticate -> discover -> inspect -> execute -> disconnect."""
    db_path = tmp_path / "e2e_state.db"
    conn = sqlite3.connect(db_path)
    registry = ConnectorRegistry(db=conn, db_lock=RLock())

    async with running_mock_supabase_server() as (base_url, state):
        keyring = MockKeyringBackend()
        provider = SupabaseCredentialProvider("supabase-e2e", keyring_backend=keyring)
        provider.store_token(DEFAULT_TEST_SUPABASE_TOKEN)

        connector = SupabaseMCPConnector(
            connector_id="supabase-e2e",
            endpoint=f"{base_url}/mcp",
            project_ref="my-e2e-project",
            read_only=True,
            features=("docs", "database"),
            credential_provider=provider,
            allow_http=True,
        )
        await registry.register(connector)

        # Connect
        await connector.connect()
        assert connector.project_access_verified is True

        # Discover
        tools = await connector.discover_tools()
        assert len(tools) >= 4

        # Execute read-only tool
        req = ConnectorRequest(
            connector_id="supabase-e2e",
            operation="tool:get_project_url",
            parameters={},
            request_id="req_e2e_01",
        )
        res = await connector.execute(req)
        assert res.success is True
        assert "my-e2e-project.supabase.co" in res.content

        # Disconnect
        await connector.disconnect()
        assert not connector.client.is_connected
