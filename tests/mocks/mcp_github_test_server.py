"""Deterministic local mock GitHub Remote MCP server for SAMANVAYA · SŪTRA tests.

Simulates the official GitHub Remote MCP Server (https://api.githubcopilot.com/mcp/):
- Token authentication validation (HTTP 401 for missing/invalid, 403 for forbidden)
- MCP Streamable HTTP JSON-RPC 2.0 protocol (initialize, initialized, ping)
- Realistic GitHub tools (read-only queries and mutating actions)
- Configurable error injection (401, 403, 429, 500, timeouts)
- Zero internet dependency (runs on 127.0.0.1 on ephemeral ports)
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

log = logging.getLogger(__name__)

DEFAULT_TEST_TOKEN = "ghp_test_valid_mock_token_12345"


def create_mock_github_mcp_app(
    expected_token: str = DEFAULT_TEST_TOKEN,
) -> tuple[FastAPI, dict[str, Any]]:
    """Create a FastAPI application mimicking the official GitHub Remote MCP Server."""
    app = FastAPI(title="MockGitHubRemoteMCPServer", docs_url=None, redoc_url=None)
    state: dict[str, Any] = {
        "call_count": 0,
        "authenticated_calls": 0,
        "last_received_token": None,
        "expected_token": expected_token,
        "protocol_version": "2026-07-28",
    }

    @app.post("/mcp")
    @app.post("/mcp/")
    async def handle_github_mcp(
        request: Request,
        authorization: Optional[str] = Header(None),
        x_simulate_status: Optional[int] = Header(None, alias="x-simulate-status"),
    ) -> Response:
        state["call_count"] += 1

        # 1. Error simulation
        if x_simulate_status:
            if x_simulate_status == 429:
                return Response(
                    content=json.dumps({"error": "API rate limit exceeded for user"}),
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": "2"},
                )
            return Response(
                content=json.dumps({"error": f"Simulated HTTP {x_simulate_status}"}),
                status_code=x_simulate_status,
                media_type="application/json",
            )

        # 2. Token Authentication Verification
        if not authorization:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Requires authentication token"},
            )

        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"error": "Bad credentials: Bearer scheme required"},
            )

        received_token = parts[1]
        state["last_received_token"] = received_token

        if received_token == "ghp_forbidden_token_999":
            return JSONResponse(
                status_code=403,
                content={"error": "Resource not accessible by personal access token"},
            )

        if received_token != state["expected_token"]:
            return JSONResponse(
                status_code=401,
                content={"error": "Bad credentials: Token invalid or expired"},
            )

        state["authenticated_calls"] += 1

        # 3. Parse JSON-RPC Payload
        body_bytes = await request.body()
        try:
            rpc_req = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
            )

        req_id = rpc_req.get("id")
        method = rpc_req.get("method")
        params = rpc_req.get("params") or {}

        # Notification handling
        if req_id is None:
            return Response(status_code=204)

        result_data = await _dispatch_github_mcp(method, params, state)

        response_body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result_data,
        }
        return JSONResponse(
            status_code=200,
            content=response_body,
            headers={
                "mcp-session-id": "mock-gh-session-001",
                "Content-Type": "application/json",
            },
        )

    return app, state


async def _dispatch_github_mcp(method: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Dispatch MCP JSON-RPC methods for GitHub server."""
    if method == "initialize":
        client_version = params.get("protocolVersion", "2026-07-28")
        negotiated = client_version if client_version in ("2026-07-28", "2025-03-26", "2024-11-05") else "2026-07-28"
        state["protocol_version"] = negotiated
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": "github-remote-mcp",
                "version": "1.0.0",
            },
        }

    elif method == "ping":
        return {}

    elif method == "tools/list":
        return {
            "tools": [
                {
                    "name": "list_repositories",
                    "description": "List repositories for the authenticated user",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "visibility": {"type": "string", "enum": ["all", "public", "private"]},
                        },
                    },
                    "readOnly": True,
                },
                {
                    "name": "get_issue",
                    "description": "Get details of a specific GitHub issue",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "issue_number": {"type": "integer"},
                        },
                        "required": ["owner", "repo", "issue_number"],
                    },
                    "readOnly": True,
                },
                {
                    "name": "search_code",
                    "description": "Search code across GitHub repositories",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    "readOnly": True,
                },
                {
                    "name": "create_issue",
                    "description": "Create a new issue in a repository",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["owner", "repo", "title"],
                    },
                    "readOnly": False,
                },
                {
                    "name": "create_pull_request",
                    "description": "Create a pull request",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "title": {"type": "string"},
                            "head": {"type": "string"},
                            "base": {"type": "string"},
                        },
                        "required": ["owner", "repo", "title", "head", "base"],
                    },
                    "readOnly": False,
                },
                {
                    "name": "delete_repository",
                    "description": "Permanently delete a repository (CRITICAL)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                        },
                        "required": ["owner", "repo"],
                    },
                    "readOnly": False,
                },
                {
                    "name": "error_tool",
                    "description": "Tool that triggers an execution error",
                    "inputSchema": {"type": "object"},
                    "readOnly": True,
                },
            ]
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        if tool_name == "list_repositories":
            repos = [
                {"id": 101, "name": "sutra-demo", "owner": "octocat", "private": False},
                {"id": 102, "name": "sutra-docs", "owner": "octocat", "private": True},
            ]
            return {
                "content": [{"type": "text", "text": json.dumps(repos, indent=2)}],
                "structuredData": {"repositories": repos, "total_count": len(repos)},
                "isError": False,
            }

        elif tool_name == "get_issue":
            issue = {
                "number": args.get("issue_number", 42),
                "title": "Bug in authentication loop",
                "state": "open",
                "author": "octocat",
            }
            return {
                "content": [{"type": "text", "text": json.dumps(issue, indent=2)}],
                "structuredData": issue,
                "isError": False,
            }

        elif tool_name == "create_issue":
            issue = {
                "number": 103,
                "title": args.get("title", "Sample Issue"),
                "body": args.get("body", ""),
                "state": "open",
            }
            return {
                "content": [{"type": "text", "text": f"Issue #103 created: {issue['title']}"}],
                "structuredData": issue,
                "isError": False,
            }

        elif tool_name == "create_pull_request":
            pr = {
                "number": 201,
                "title": args.get("title", "Feature PR"),
                "head": args.get("head", "feature"),
                "base": args.get("base", "main"),
                "state": "open",
            }
            return {
                "content": [{"type": "text", "text": f"PR #201 created: {pr['title']}"}],
                "structuredData": pr,
                "isError": False,
            }

        elif tool_name == "delete_repository":
            return {
                "content": [{"type": "text", "text": f"Repository {args.get('owner')}/{args.get('repo')} deleted"}],
                "structuredData": {"deleted": True},
                "isError": False,
            }

        elif tool_name == "error_tool":
            return {
                "content": [{"type": "text", "text": "Simulated tool execution error on GitHub"}],
                "isError": True,
            }

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

    elif method == "resources/list":
        return {
            "resources": [
                {
                    "uri": "github://octocat/sutra-demo/README.md",
                    "name": "README.md",
                    "description": "Repository documentation",
                    "mimeType": "text/markdown",
                }
            ]
        }

    elif method == "resources/read":
        uri = params.get("uri", "")
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/markdown",
                    "text": "# SUTRA Demo Repository\nWelcome to the mock GitHub MCP repository.",
                }
            ]
        }

    return {"error": {"code": -32601, "message": f"Method '{method}' not found"}}


class MockGitHubServerContext:
    def __init__(self, port: int, state: dict[str, Any]) -> None:
        self.port = port
        self.state = state
        self.url = f"http://127.0.0.1:{port}/mcp"


@asynccontextmanager
async def running_mock_github_server(
    expected_token: str = DEFAULT_TEST_TOKEN,
) -> AsyncGenerator[MockGitHubServerContext, None]:
    """Launch local mock GitHub Remote MCP server on ephemeral loopback port."""
    # Find free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app, state = create_mock_github_mcp_app(expected_token=expected_token)
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())

    # Wait for startup
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)

    ctx = MockGitHubServerContext(port=port, state=state)
    try:
        yield ctx
    finally:
        server.should_exit = True
        await server_task
