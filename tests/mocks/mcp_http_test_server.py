"""Deterministic local mock HTTP MCP test server for SAMANVAYA · SŪTRA.

Implements:
1. Streamable HTTP endpoint (`/mcp`):
   - Direct JSON responses (`application/json`)
   - Streaming SSE responses (`text/event-stream`)
   - Session tracking via `mcp-session-id`
   - Configurable error injection (401, 403, 404, 429, 500, malformed)
2. Legacy SSE endpoints:
   - `GET /sse`: Initiates event stream, announces `event: endpoint\ndata: /messages?sessionId=...`
   - `POST /messages`: Receives JSON-RPC request and delivers response over the active `/sse` stream.
3. Standard MCP capabilities:
   - Tools (`echo`, `mutate`, `slow_op`, `oversized`)
   - Resources (`file://readme.txt`)
   - Prompts (`system_prompt`)
   - Multi-page cursor pagination
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
import uvicorn

log = logging.getLogger(__name__)


def create_mock_mcp_app() -> tuple[FastAPI, dict[str, Any]]:
    """Create FastAPI application modeling modern and legacy MCP HTTP servers."""
    app = FastAPI(title="MockMCPHttpServer", docs_url=None, redoc_url=None)
    state: dict[str, Any] = {
        "call_count": 0,
        "legacy_sse_queues": [],
        "session_id": "test-mcp-session-xyz",
        "protocol_version": "2026-07-28",
    }

    # -----------------------------------------------------------------------
    # Streamable HTTP (/mcp)
    # -----------------------------------------------------------------------

    @app.post("/mcp")
    async def handle_streamable_mcp(
        request: Request,
        x_simulate_status: Optional[int] = Header(None, alias="x-simulate-status"),
        x_simulate_sse: Optional[str] = Header(None, alias="x-simulate-sse"),
        x_simulate_malformed: Optional[str] = Header(None, alias="x-simulate-malformed"),
    ) -> Response:
        state["call_count"] += 1

        # 1. Error simulation via headers
        if x_simulate_status:
            if x_simulate_status == 429:
                return Response(
                    content=json.dumps({"error": "Rate limit exceeded"}),
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": "2"},
                )
            return Response(
                content=json.dumps({"error": f"Simulated HTTP {x_simulate_status}"}),
                status_code=x_simulate_status,
                media_type="application/json",
            )

        if x_simulate_malformed == "true":
            return PlainTextResponse("NOT_VALID_JSON{foo:bar}", status_code=200)

        # 2. Parse JSON-RPC request
        body_bytes = await request.body()
        try:
            rpc_req = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return Response(
                content=json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}),
                status_code=400,
                media_type="application/json",
            )

        req_id = rpc_req.get("id")
        method = rpc_req.get("method")
        params = rpc_req.get("params") or {}

        # 3. Notification handler (no 'id')
        if req_id is None:
            return Response(status_code=204)

        # 4. Generate JSON-RPC response result
        result_payload = await _dispatch_mcp_method(method, params, state)

        # 5. Check if streaming SSE response requested
        if x_simulate_sse == "true" or method == "test/streaming_sse":
            async def sse_stream() -> AsyncGenerator[str, None]:
                yield f": keepalive\n\n"
                await asyncio.sleep(0.01)
                event_data = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result_payload})
                yield f"event: message\ndata: {event_data}\n\n"

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={"mcp-session-id": state["session_id"]},
            )

        # 6. Default standard JSON-RPC 2.0 response
        resp_data = {"jsonrpc": "2.0", "id": req_id, "result": result_payload}
        return Response(
            content=json.dumps(resp_data),
            status_code=200,
            media_type="application/json",
            headers={"mcp-session-id": state["session_id"]},
        )

    # -----------------------------------------------------------------------
    # Legacy SSE Endpoints (/sse and /messages)
    # -----------------------------------------------------------------------

    @app.get("/sse")
    async def handle_legacy_sse(request: Request) -> StreamingResponse:
        """Initiate legacy SSE stream, announce POST endpoint, and push messages."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        state["legacy_sse_queues"].append(queue)

        async def legacy_stream() -> AsyncGenerator[str, None]:
            try:
                # 1. Announce POST endpoint
                yield f"event: endpoint\ndata: /messages?sessionId={state['session_id']}\n\n"
                # 2. Yield messages pushed to queue
                while True:
                    msg = await queue.get()
                    yield f"event: message\ndata: {msg}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                if queue in state["legacy_sse_queues"]:
                    state["legacy_sse_queues"].remove(queue)

        return StreamingResponse(
            legacy_stream(),
            media_type="text/event-stream",
            headers={"mcp-session-id": state["session_id"]},
        )

    @app.post("/messages")
    async def handle_legacy_messages(request: Request) -> Response:
        """Receive legacy POST request, process, and forward response over /sse stream."""
        body_bytes = await request.body()
        rpc_req = json.loads(body_bytes.decode("utf-8"))
        req_id = rpc_req.get("id")
        method = rpc_req.get("method")
        params = rpc_req.get("params") or {}

        if req_id is not None:
            result_payload = await _dispatch_mcp_method(method, params, state)
            resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": result_payload}
            resp_str = json.dumps(resp_payload)
            for q in list(state["legacy_sse_queues"]):
                await q.put(resp_str)

        return Response(status_code=202)

    return app, state


async def _dispatch_mcp_method(method: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Simulate MCP methods for mock server."""
    if method == "initialize":
        requested_ver = params.get("protocolVersion", state["protocol_version"])
        return {
            "protocolVersion": requested_ver,
            "serverInfo": {
                "name": "mock-http-server",
                "version": "1.0.0",
                "title": "Mock Remote MCP Server",
                "description": "Deterministic local test fixture",
            },
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True},
                "prompts": {"listChanged": True},
            },
            "instructions": "Use mock tools safely",
        }

    elif method == "ping":
        return {}

    elif method == "tools/list":
        cursor = params.get("cursor")
        if cursor == "page2":
            return {
                "tools": [
                    {
                        "name": "tool_page_2",
                        "description": "Second page tool",
                        "inputSchema": {"type": "object"},
                    }
                ],
                "nextCursor": None,
            }
        elif cursor is None and params.get("paginate") is True:
            return {
                "tools": [
                    {
                        "name": "tool_page_1",
                        "description": "First page tool",
                        "inputSchema": {"type": "object"},
                    }
                ],
                "nextCursor": "page2",
            }
        else:
            return {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo input message",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"msg": {"type": "string"}},
                            "required": ["msg"],
                        },
                    },
                    {
                        "name": "mutate_data",
                        "description": "Mutating operation requiring permissions",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"action": {"type": "string"}},
                        },
                    },
                    {
                        "name": "slow_op",
                        "description": "Sleep for specified seconds",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"delay": {"type": "number"}},
                        },
                    },
                    {
                        "name": "oversized_output",
                        "description": "Return payload exceeding max response size",
                        "inputSchema": {"type": "object"},
                    },
                ]
            }

    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        if tool_name == "echo":
            msg = args.get("msg", "empty")
            return {
                "content": [{"type": "text", "text": f"Echo: {msg}"}],
                "isError": False,
            }
        elif tool_name == "mutate_data":
            action = args.get("action", "default")
            return {
                "content": [{"type": "text", "text": f"Mutated: {action}"}],
                "isError": False,
            }
        elif tool_name == "slow_op":
            delay = float(args.get("delay", 0.5))
            await asyncio.sleep(delay)
            return {
                "content": [{"type": "text", "text": f"Completed after {delay}s"}],
                "isError": False,
            }
        elif tool_name == "oversized_output":
            # Generate 2MB payload
            huge_text = "X" * (2 * 1024 * 1024)
            return {
                "content": [{"type": "text", "text": huge_text}],
                "isError": False,
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
                    "uri": "file://mock/readme.txt",
                    "name": "Mock Readme",
                    "mimeType": "text/plain",
                    "description": "A sample mock resource",
                }
            ]
        }

    elif method == "resources/read":
        uri = params.get("uri")
        return {
            "contents": [
                {
                    "uri": uri or "file://mock/readme.txt",
                    "mimeType": "text/plain",
                    "text": "Hello from mock MCP resource!",
                }
            ]
        }

    elif method == "prompts/list":
        return {
            "prompts": [
                {
                    "name": "mock_code_review",
                    "description": "Perform review of code",
                    "arguments": [{"name": "code", "description": "Code content", "required": True}],
                }
            ]
        }

    elif method == "prompts/get":
        prompt_name = params.get("name")
        return {
            "description": f"Prompt {prompt_name}",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": "Review this code carefully."},
                }
            ],
        }

    return {"status": "ok"}


class MockMCPServer:
    """Manages an in-process Uvicorn loopback server instance for testing."""

    def __init__(self) -> None:
        self.app, self.state = create_mock_mcp_app()
        self.sock: Optional[socket.socket] = None
        self.server: Optional[uvicorn.Server] = None
        self.server_task: Optional[asyncio.Task[None]] = None
        self.port: int = 0
        self.url: str = ""
        self.sse_url: str = ""

    async def start(self) -> str:
        """Start local TCP loopback server on an ephemeral free port."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.sse_url = f"http://127.0.0.1:{self.port}/sse"

        config = uvicorn.Config(
            self.app,
            log_level="critical",
            timeout_keep_alive=5,
        )
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.sock]))

        # Wait for server to start
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.02)

        if not self.server.started:
            raise RuntimeError("Mock MCP HTTP server failed to start within timeout")

        return self.url

    async def stop(self) -> None:
        """Stop local server and close socket."""
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            try:
                await asyncio.wait_for(self.server_task, timeout=3.0)
            except (Exception, asyncio.CancelledError):
                self.server_task.cancel()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
