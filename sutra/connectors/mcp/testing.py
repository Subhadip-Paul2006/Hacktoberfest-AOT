"""Deterministic test double for Model Context Protocol (MCP) transport in SAMANVAYA · SŪTRA.

Allows comprehensive in-memory unit and integration testing of the MCP client,
session, connector adapter, error handling, pagination, and capability routing
without spawning subprocesses or opening network sockets.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ..errors import (
    ConnectorConnectionError,
    ConnectorNotConnectedError,
    ConnectorProviderError,
    ConnectorTimeoutError,
)
from .models import MCPProtocolVersion
from .transport import MCPTransport


class FakeMCPTransport(MCPTransport):
    """Deterministic in-memory MCP transport for test verification."""

    def __init__(
        self,
        *,
        server_protocol_version: str = MCPProtocolVersion.V2026_07_28.value,
        server_name: str = "fake-mcp-server",
        server_version: str = "1.0.0",
        capabilities: Optional[dict[str, Any]] = None,
        fail_open: bool = False,
        fail_init: bool = False,
        unsupported_version: Optional[str] = None,
        timeout_methods: Optional[set[str]] = None,
        disconnect_on_request: bool = False,
        simulated_latency_ms: float = 2.5,
    ) -> None:
        self._connected = False
        self._server_version = unsupported_version or server_protocol_version
        self._server_name = server_name
        self._server_app_version = server_version
        self._capabilities = capabilities or {
            "tools": {"listChanged": True},
            "resources": {"subscribe": True, "listChanged": True},
            "prompts": {"listChanged": True},
        }

        self.fail_open = fail_open
        self.fail_init = fail_init
        self.timeout_methods = timeout_methods or set()
        self.disconnect_on_request = disconnect_on_request
        self.simulated_latency_ms = simulated_latency_ms

        # In-memory catalogs
        self._tools: list[dict[str, Any]] = []
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._resources: list[dict[str, Any]] = []
        self._resource_contents: dict[str, list[dict[str, Any]]] = {}
        self._resource_templates: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []
        self._prompt_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

        # Inspection audit trails
        self.requests_sent: list[tuple[str, Optional[dict[str, Any]]]] = []
        self.notifications_sent: list[tuple[str, Optional[dict[str, Any]]]] = []

    # -----------------------------------------------------------------------
    # Setup / Configuration Helpers for Tests
    # -----------------------------------------------------------------------

    def add_tool(
        self,
        name: str,
        description: str = "",
        input_schema: Optional[dict[str, Any]] = None,
        handler: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> None:
        """Register a mock tool and optional execution handler."""
        self._tools.append({
            "name": name,
            "description": description,
            "inputSchema": input_schema or {"type": "object", "properties": {}},
        })
        if handler:
            self._tool_handlers[name] = handler

    def add_resource(
        self,
        uri: str,
        name: str,
        description: str = "",
        mime_type: Optional[str] = "text/plain",
        text_content: Optional[str] = None,
    ) -> None:
        """Register a mock resource and optional contents."""
        self._resources.append({
            "uri": uri,
            "name": name,
            "description": description,
            "mimeType": mime_type,
        })
        if text_content is not None:
            self._resource_contents[uri] = [{"uri": uri, "mimeType": mime_type, "text": text_content}]

    def add_prompt(
        self,
        name: str,
        description: str = "",
        arguments: Optional[list[dict[str, Any]]] = None,
        handler: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> None:
        """Register a mock prompt template."""
        self._prompts.append({
            "name": name,
            "description": description,
            "arguments": arguments or [],
        })
        if handler:
            self._prompt_handlers[name] = handler

    # -----------------------------------------------------------------------
    # MCPTransport Interface Implementation
    # -----------------------------------------------------------------------

    async def open(self) -> None:
        if self.fail_open:
            raise ConnectorConnectionError(
                connector_id="fake.transport",
                message="Simulated connection failure opening fake transport",
            )
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        if not self._connected:
            raise ConnectorNotConnectedError(
                connector_id="fake.transport",
                message=f"Cannot send '{method}': fake transport is disconnected",
            )
        if self.disconnect_on_request:
            self._connected = False
            raise ConnectorConnectionError(
                connector_id="fake.transport",
                message="Transport disconnected during request exchange",
            )
        if method in self.timeout_methods:
            raise ConnectorTimeoutError(
                connector_id="fake.transport",
                message=f"Simulated timeout on MCP method '{method}'",
            )

        self.requests_sent.append((method, params))
        p = params or {}

        # Handle MCP Protocol Methods
        if method == "initialize":
            if self.fail_init:
                raise ConnectorProviderError(
                    connector_id="fake.transport",
                    message="Simulated server refusal during initialize handshake",
                )
            return {
                "protocolVersion": self._server_version,
                "serverInfo": {
                    "name": self._server_name,
                    "version": self._server_app_version,
                },
                "capabilities": self._capabilities,
            }

        elif method == "ping":
            return {}

        elif method == "tools/list":
            cursor = p.get("cursor")
            limit = p.get("limit", 100)
            # Simulate pagination if cursor provided
            start_idx = int(cursor) if cursor and cursor.isdigit() else 0
            end_idx = min(start_idx + limit, len(self._tools))
            page_tools = self._tools[start_idx:end_idx]
            next_cursor = str(end_idx) if end_idx < len(self._tools) else None
            return {"tools": page_tools, "nextCursor": next_cursor}

        elif method == "tools/call":
            tool_name = p.get("name")
            tool_args = p.get("arguments") or {}
            handler = self._tool_handlers.get(tool_name)
            if handler:
                res = handler(tool_args)
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, dict) and ("content" in res or "isError" in res):
                    return res
                return {"content": [{"type": "text", "text": str(res)}], "isError": False}

            # Default echo execution
            return {
                "content": [{"type": "text", "text": f"Executed tool '{tool_name}' successfully"}],
                "isError": False,
                "structuredContent": {"executed": tool_name, "args": tool_args},
            }

        elif method == "resources/list":
            cursor = p.get("cursor")
            limit = p.get("limit", 100)
            start_idx = int(cursor) if cursor and cursor.isdigit() else 0
            end_idx = min(start_idx + limit, len(self._resources))
            page_res = self._resources[start_idx:end_idx]
            next_cursor = str(end_idx) if end_idx < len(self._resources) else None
            return {"resources": page_res, "nextCursor": next_cursor}

        elif method == "resources/read":
            uri = p.get("uri")
            contents = self._resource_contents.get(uri)
            if contents is not None:
                return {"contents": contents}
            return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": f"Sample resource data for {uri}"}]}

        elif method == "resources/templates/list":
            return {"resourceTemplates": self._resource_templates}

        elif method == "prompts/list":
            cursor = p.get("cursor")
            limit = p.get("limit", 100)
            start_idx = int(cursor) if cursor and cursor.isdigit() else 0
            end_idx = min(start_idx + limit, len(self._prompts))
            page_prompts = self._prompts[start_idx:end_idx]
            next_cursor = str(end_idx) if end_idx < len(self._prompts) else None
            return {"prompts": page_prompts, "nextCursor": next_cursor}

        elif method == "prompts/get":
            prompt_name = p.get("name")
            prompt_args = p.get("arguments") or {}
            handler = self._prompt_handlers.get(prompt_name)
            if handler:
                res = handler(prompt_args)
                if asyncio.iscoroutine(res):
                    res = await res
                return res
            return {
                "description": f"Rendered prompt {prompt_name}",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": f"Prompt user input for {prompt_name}"}}
                ],
            }

        else:
            raise ConnectorProviderError(
                connector_id="fake.transport",
                message=f"Unknown or unhandled JSON-RPC method '{method}'",
            )

    async def send_notification(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self._connected:
            raise ConnectorNotConnectedError(
                connector_id="fake.transport",
                message="Cannot send notification: fake transport is disconnected",
            )
        self.notifications_sent.append((method, params))

    async def ping(self, timeout: float = 5.0) -> float:
        if not self._connected:
            raise ConnectorNotConnectedError(
                connector_id="fake.transport",
                message="Cannot ping: fake transport is disconnected",
            )
        if "ping" in self.timeout_methods:
            raise ConnectorTimeoutError(
                connector_id="fake.transport",
                message="Simulated timeout on ping",
            )
        return self.simulated_latency_ms
