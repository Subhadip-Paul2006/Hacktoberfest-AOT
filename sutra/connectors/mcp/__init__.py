"""Model Context Protocol (MCP) Client Core for SAMANVAYA · SŪTRA.

Provides a transport-agnostic client, session lifecycle manager, capability discovery,
and Connector adapter for integrating MCP servers.

Invariants:
- Decoupled from transport specifics (stdio in Phase 7.6, HTTP in Phase 7.7).
- Reuses shared SŪTRA resilience, secret redaction, and ActivityStore auditing.
- Never bypasses PermissionEngine.
"""
from __future__ import annotations

from .client import MCPClient
from .connector import MCPConnector
from .models import (
    MCPClientCapabilities,
    MCPClientInfo,
    MCPPage,
    MCPPaginationParams,
    MCPPrompt,
    MCPPromptArgument,
    MCPPromptMessage,
    MCPPromptResult,
    MCPProtocolVersion,
    MCPResource,
    MCPResourceContent,
    MCPResourceTemplate,
    MCPServerCapabilities,
    MCPServerInfo,
    MCPSessionState,
    MCPTool,
    MCPToolCallResult,
)
from .http import (
    HttpMCPTransport,
    HttpTransportMode,
    MCPHttpConfig,
    MCPHttpState,
    MCPHttpTransport,
)
from .github import (
    DEFAULT_CONNECTOR_ID,
    DEFAULT_GITHUB_MCP_URL,
    GitHubMCPConnector,
)
from .oauth import (
    MCPOAuthClient,
    OAuthFlowConfig,
    OAuthLoopbackServer,
    OAuthToken,
    generate_code_challenge,
    generate_code_verifier,
    generate_pkce_pair,
)
from .supabase import (
    DEFAULT_FEATURES,
    DEFAULT_SUPABASE_MCP_URL,
    RECOGNIZED_FEATURES,
    SupabaseMCPConnector,
    build_supabase_mcp_url,
    validate_project_ref,
)
from .session import MCPSession
from .stdio import MCPStdioConfig, MCPStdioState, MCPStdioTransport, StdioMCPTransport
from .testing import FakeMCPTransport
from .transport import MCPTransport

__all__ = [
    # Core Client & Adapter
    "MCPClient",
    "MCPSession",
    "MCPConnector",
    "GitHubMCPConnector",
    "DEFAULT_GITHUB_MCP_URL",
    "DEFAULT_CONNECTOR_ID",
    "SupabaseMCPConnector",
    "DEFAULT_SUPABASE_MCP_URL",
    "DEFAULT_FEATURES",
    "RECOGNIZED_FEATURES",
    "build_supabase_mcp_url",
    "validate_project_ref",
    "MCPOAuthClient",
    "OAuthFlowConfig",
    "OAuthLoopbackServer",
    "OAuthToken",
    "generate_pkce_pair",
    "generate_code_verifier",
    "generate_code_challenge",
    "MCPTransport",
    "StdioMCPTransport",
    "MCPStdioTransport",
    "MCPStdioConfig",
    "MCPStdioState",
    "HttpMCPTransport",
    "MCPHttpTransport",
    "MCPHttpConfig",
    "MCPHttpState",
    "HttpTransportMode",
    "FakeMCPTransport",
    # Protocol & Session Models
    "MCPProtocolVersion",
    "MCPSessionState",
    "MCPClientInfo",
    "MCPServerInfo",
    "MCPClientCapabilities",
    "MCPServerCapabilities",
    # Tools, Resources & Prompts
    "MCPTool",
    "MCPToolCallResult",
    "MCPResource",
    "MCPResourceTemplate",
    "MCPResourceContent",
    "MCPPrompt",
    "MCPPromptArgument",
    "MCPPromptMessage",
    "MCPPromptResult",
    "MCPPage",
    "MCPPaginationParams",
]
