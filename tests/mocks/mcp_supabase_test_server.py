"""Deterministic local mock Supabase Remote MCP and OAuth server for SAMANVAYA · SŪTRA tests.

Simulates:
1. Official Supabase Remote MCP Server (https://mcp.supabase.com/mcp):
   - Streamable HTTP JSON-RPC 2.0 protocol
   - Query parameter parsing: project_ref, read_only, features
   - Token authentication (Bearer PAT and OAuth token validation)
   - Read-only enforcement at server level (rejecting mutating SQL / actions)
   - Realistic tools for docs, database inspection, and migration management
   - Configurable error injection (401, 403, 404, 429, 500, timeouts)
2. Supabase OAuth 2.0 PKCE Endpoints (https://api.supabase.com/v1/oauth/):
   - /v1/oauth/authorize (code_challenge & state verification, simulation of user grant/denial)
   - /v1/oauth/token (authorization_code exchange with PKCE verification and refresh_token grant)

Zero external internet dependencies; runs on 127.0.0.1 on ephemeral ports.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn

log = logging.getLogger(__name__)

DEFAULT_TEST_SUPABASE_TOKEN = "sbp_test_valid_pat_token_12345"
DEFAULT_TEST_PROJECT_REF = "test-proj-ref-12345"


def create_mock_supabase_mcp_app(
    expected_token: str = DEFAULT_TEST_SUPABASE_TOKEN,
    valid_project_ref: str = DEFAULT_TEST_PROJECT_REF,
) -> tuple[FastAPI, dict[str, Any]]:
    """Create a FastAPI app simulating both the remote MCP server and the OAuth server."""
    app = FastAPI(title="MockSupabaseRemoteMCPServer", docs_url=None, redoc_url=None)
    state: dict[str, Any] = {
        "call_count": 0,
        "authenticated_calls": 0,
        "last_received_token": None,
        "last_query_params": {},
        "expected_token": expected_token,
        "valid_project_ref": valid_project_ref,
        "protocol_version": "2026-07-28",
        "simulate_status": None,
        "simulate_oauth_denial": False,
        "pending_auth_codes": {},
    }

    # =======================================================================
    # Remote MCP POST Endpoint (/mcp and /mcp/)
    # =======================================================================
    @app.post("/mcp")
    @app.post("/mcp/")
    async def handle_supabase_mcp(
        request: Request,
        authorization: Optional[str] = Header(None),
        x_simulate_status: Optional[int] = Header(None, alias="x-simulate-status"),
    ) -> Response:
        state["call_count"] += 1
        query_params = dict(request.query_params)
        state["last_query_params"] = query_params

        # 1. Error simulation
        sim_status = x_simulate_status or state.get("simulate_status")
        if sim_status:
            if sim_status == 429:
                return Response(
                    content=json.dumps({"error": "Rate limit exceeded"}),
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": "2"},
                )
            return Response(
                content=json.dumps({"error": f"Simulated HTTP {sim_status}"}),
                status_code=sim_status,
                media_type="application/json",
            )

        # 2. Token Authentication Verification
        if not authorization:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Missing authorization header"},
            )

        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"error": "Bad credentials: Bearer scheme required"},
            )

        received_token = parts[1]
        state["last_received_token"] = received_token

        if received_token == "sbp_forbidden_token_999":
            return JSONResponse(
                status_code=403,
                content={"error": "Forbidden: Access denied to Supabase project"},
            )

        if received_token not in (
            state["expected_token"],
            "sbp_oauth_access_token_valid",
            "sbp_oauth_access_token_refreshed",
        ):
            return JSONResponse(
                status_code=401,
                content={"error": "Bad credentials: Token invalid or expired"},
            )

        # 3. Project Scoping Verification
        proj_ref = query_params.get("project_ref")
        if proj_ref and proj_ref == "nonexistent-project-999":
            return JSONResponse(
                status_code=404,
                content={"error": f"Supabase project '{proj_ref}' not found"},
            )

        state["authenticated_calls"] += 1

        # 4. Parse JSON-RPC Payload
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

        result_data = await _dispatch_supabase_mcp(method, params, query_params, state)

        response_body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result_data,
        }
        return JSONResponse(
            status_code=200,
            content=response_body,
            headers={
                "mcp-session-id": "mock-sb-session-001",
                "Content-Type": "application/json",
            },
        )

    # =======================================================================
    # OAuth Endpoints (/v1/oauth/authorize and /v1/oauth/token)
    # =======================================================================
    @app.get("/v1/oauth/authorize")
    async def handle_oauth_authorize(
        response_type: str = Query(...),
        client_id: str = Query(...),
        redirect_uri: str = Query(...),
        state_param: str = Query(..., alias="state"),
        code_challenge: str = Query(...),
        code_challenge_method: str = Query("S256"),
    ) -> Response:
        if state["simulate_oauth_denial"]:
            target_url = f"{redirect_uri}?error=access_denied&error_description=User+denied+access&state={state_param}"
            return RedirectResponse(target_url, status_code=302)

        auth_code = f"mock_sb_code_{state_param[:8]}"
        state["pending_auth_codes"][auth_code] = {
            "code_challenge": code_challenge,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }

        target_url = f"{redirect_uri}?code={auth_code}&state={state_param}"
        return RedirectResponse(target_url, status_code=302)

    @app.post("/v1/oauth/token")
    async def handle_oauth_token(request: Request) -> Response:
        form = await request.form()
        grant_type = form.get("grant_type")
        client_id = form.get("client_id")

        if grant_type == "authorization_code":
            code = form.get("code")
            code_verifier = form.get("code_verifier")

            if not code or code not in state["pending_auth_codes"]:
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "Invalid code"})

            stored = state["pending_auth_codes"][code]
            expected_challenge = stored["code_challenge"]

            # Compute S256 challenge from verifier
            if not code_verifier:
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "Missing code_verifier"})

            sha256_hash = hashlib.sha256(code_verifier.encode("ascii")).digest()
            computed_challenge = base64.urlsafe_b64encode(sha256_hash).decode("ascii").rstrip("=")

            if computed_challenge != expected_challenge:
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "PKCE verification failed"})

            # Code consumed
            del state["pending_auth_codes"][code]

            return JSONResponse(
                status_code=200,
                content={
                    "access_token": "sbp_oauth_access_token_valid",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "sbp_oauth_refresh_token_valid",
                    "scope": "all",
                },
            )

        elif grant_type == "refresh_token":
            refresh_tok = form.get("refresh_token")
            if refresh_tok != "sbp_oauth_refresh_token_valid":
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "Invalid refresh token"})

            return JSONResponse(
                status_code=200,
                content={
                    "access_token": "sbp_oauth_access_token_refreshed",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "sbp_oauth_refresh_token_valid",
                    "scope": "all",
                },
            )

        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

    return app, state


async def _dispatch_supabase_mcp(
    method: str,
    params: dict[str, Any],
    query_params: dict[str, str],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch MCP JSON-RPC methods for Supabase server."""
    read_only_param = query_params.get("read_only", "true").lower() == "true"
    features_param = set(query_params.get("features", "docs,database").split(","))

    if method == "initialize":
        client_version = params.get("protocolVersion", "2026-07-28")
        negotiated = client_version if client_version in ("2026-07-28", "2025-03-26", "2024-11-05") else "2026-07-28"
        state["protocol_version"] = negotiated
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {
                "name": "mock-supabase-mcp-server",
                "version": "1.0.0",
            },
        }

    elif method == "ping":
        return {}

    elif method == "tools/list":
        tools: list[dict[str, Any]] = []

        # Docs feature tools
        if "docs" in features_param:
            tools.append({
                "name": "search_docs",
                "description": "Search official Supabase documentation",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "metadata": {"read_only": True},
            })

        # Database feature tools
        if "database" in features_param:
            tools.extend([
                {
                    "name": "get_project_url",
                    "description": "Get project API URL",
                    "inputSchema": {"type": "object", "properties": {}},
                    "metadata": {"read_only": True},
                },
                {
                    "name": "list_tables",
                    "description": "List all database tables in public schema",
                    "inputSchema": {"type": "object", "properties": {"schema": {"type": "string", "default": "public"}}},
                    "metadata": {"read_only": True},
                },
                {
                    "name": "get_schema",
                    "description": "Describe database table schema and column types",
                    "inputSchema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]},
                    "metadata": {"read_only": True},
                },
                {
                    "name": "execute_sql",
                    "description": "Execute raw SQL query against project database",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                    "metadata": {"read_only": False},
                },
                {
                    "name": "apply_migration",
                    "description": "Apply DDL migration script to database",
                    "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
                    "metadata": {"read_only": False},
                },
            ])

        # Branching feature tools
        if "branching" in features_param:
            tools.extend([
                {
                    "name": "list_branches",
                    "description": "List all preview branches for project",
                    "inputSchema": {"type": "object", "properties": {}},
                    "metadata": {"read_only": True},
                },
                {
                    "name": "create_branch",
                    "description": "Create a new database branch",
                    "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                    "metadata": {"read_only": False},
                },
            ])

        return {"tools": tools}

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        proj_ref = query_params.get("project_ref", "default-ref")

        if tool_name == "get_project_url":
            return {
                "content": [{"type": "text", "text": f"https://{proj_ref}.supabase.co"}],
                "structuredData": {"url": f"https://{proj_ref}.supabase.co", "project_ref": proj_ref},
                "isError": False,
            }

        elif tool_name == "search_docs":
            q = arguments.get("query", "")
            return {
                "content": [{"type": "text", "text": f"Found documentation matches for: {q}"}],
                "structuredData": {"query": q, "results": [{"title": "Auth Guide", "url": "https://supabase.com/docs/auth"}]},
                "isError": False,
            }

        elif tool_name == "list_tables":
            return {
                "content": [{"type": "text", "text": "Tables: users, profiles, audit_logs"}],
                "structuredData": {"tables": [{"name": "users"}, {"name": "profiles"}, {"name": "audit_logs"}]},
                "isError": False,
            }

        elif tool_name == "get_schema":
            table = arguments.get("table", "users")
            return {
                "content": [{"type": "text", "text": f"Schema for table '{table}': id (uuid), name (text), created_at (timestamptz)"}],
                "structuredData": {
                    "table": table,
                    "columns": [
                        {"name": "id", "type": "uuid", "nullable": False},
                        {"name": "name", "type": "text", "nullable": True},
                        {"name": "created_at", "type": "timestamptz", "nullable": False},
                    ],
                },
                "isError": False,
            }

        elif tool_name == "execute_sql":
            sql = arguments.get("query", "")
            is_mutating = any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"))
            if read_only_param and is_mutating:
                return {
                    "content": [{"type": "text", "text": "permission denied: cannot execute mutating query in read-only mode"}],
                    "isError": True,
                }
            return {
                "content": [{"type": "text", "text": f"Executed SQL: {sql} (1 row returned)"}],
                "structuredData": {"rows": [{"count": 42}]},
                "isError": False,
            }

        elif tool_name == "apply_migration":
            if read_only_param:
                return {
                    "content": [{"type": "text", "text": "permission denied: migration cannot be applied in read-only mode"}],
                    "isError": True,
                }
            return {
                "content": [{"type": "text", "text": "Migration applied successfully"}],
                "structuredData": {"applied": True, "version": "20261009120000"},
                "isError": False,
            }

        elif tool_name == "create_branch":
            if read_only_param:
                return {
                    "content": [{"type": "text", "text": "permission denied: branch creation disabled in read-only mode"}],
                    "isError": True,
                }
            branch_name = arguments.get("name", "preview-1")
            return {
                "content": [{"type": "text", "text": f"Branch '{branch_name}' created"}],
                "structuredData": {"branch": branch_name, "status": "active"},
                "isError": False,
            }

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: '{tool_name}'"}],
                "isError": True,
            }

    elif method == "resources/list":
        return {"resources": []}

    return {}


def _get_free_port() -> int:
    """Find an available port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def running_mock_supabase_server(
    expected_token: str = DEFAULT_TEST_SUPABASE_TOKEN,
    valid_project_ref: str = DEFAULT_TEST_PROJECT_REF,
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    """Async context manager running the mock Supabase MCP and OAuth server on localhost."""
    app, state = create_mock_supabase_mcp_app(
        expected_token=expected_token,
        valid_project_ref=valid_project_ref,
    )
    port = _get_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    base_url = f"http://127.0.0.1:{port}"

    # Wait for server to become responsive
    max_retries = 50
    for _ in range(max_retries):
        if server.started:
            break
        await asyncio.sleep(0.05)

    try:
        yield base_url, state
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
