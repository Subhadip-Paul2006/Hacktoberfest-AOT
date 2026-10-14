"""High-level, transport-agnostic Model Context Protocol (MCP) Client Core.

Exposes normalized tools, resources, and prompts APIs over an abstract MCPSession.
Enforces secret redaction, bounded pagination, strict capability discovery,
and error normalization to the SŪTRA ConnectorError hierarchy.

Invariants:
1. Pure client core: Contains NO stdio or HTTP transport implementation.
2. Does NOT grant permissions or execute autonomous decision making.
3. Completely sanitizes arguments and results before returning or logging.
4. Bounded pagination ensures zero infinite loops.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sutra.redaction import redact_secrets
from ..errors import (
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorProviderError,
    ConnectorTimeoutError,
    ConnectorToolError,
)
from ..models import sanitize_metadata
from .models import (
    MCPClientCapabilities,
    MCPClientInfo,
    MCPPage,
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
from .session import MCPSession
from .transport import MCPTransport

log = logging.getLogger(__name__)


class MCPClient:
    """Canonical transport-agnostic client for interacting with an MCP server."""

    def __init__(
        self,
        transport: MCPTransport,
        *,
        client_info: Optional[MCPClientInfo] = None,
        client_capabilities: Optional[MCPClientCapabilities] = None,
        preferred_protocol_version: Optional[MCPProtocolVersion] = None,
        connector_id: str = "mcp.client",
    ) -> None:
        self.connector_id = connector_id
        self._session = MCPSession(
            transport,
            client_info=client_info,
            client_capabilities=client_capabilities,
            preferred_protocol_version=preferred_protocol_version,
        )

    @property
    def session(self) -> MCPSession:
        """Underlying active MCPSession."""
        return self._session

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected and ready."""
        return self._session.is_ready

    @property
    def state(self) -> MCPSessionState:
        """Current protocol session state."""
        return self._session.state

    @property
    def protocol_version(self) -> Optional[MCPProtocolVersion]:
        """Negotiated protocol specification version."""
        return self._session.protocol_version

    @property
    def server_info(self) -> Optional[MCPServerInfo]:
        """Informational server identity metadata."""
        return self._session.server_info

    @property
    def server_capabilities(self) -> MCPServerCapabilities:
        """Server capabilities advertised during handshake."""
        return self._session.server_capabilities

    # -----------------------------------------------------------------------
    # Lifecycle Management
    # -----------------------------------------------------------------------

    async def connect(self, *, timeout: float = 10.0) -> None:
        """Connect to the server and complete the protocol handshake."""
        await self._session.initialize(timeout=timeout)

    async def disconnect(self) -> None:
        """Close the active session and disconnect transport."""
        await self._session.close()

    async def ping(self, timeout: float = 5.0) -> float:
        """Measure roundtrip latency to the server."""
        return await self._session.ping(timeout=timeout)

    # -----------------------------------------------------------------------
    # Tools API
    # -----------------------------------------------------------------------

    async def list_tools(
        self,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> MCPPage[MCPTool]:
        """List a single page of tools exposed by the MCP server."""
        if not self._session.is_ready:
            raise ConnectorNotConnectedError(
                connector_id=self.connector_id,
                message="Cannot list tools: MCP client is not connected",
            )
        if not self._session.server_capabilities.tools:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'tools' capability",
                details={"declared_capabilities": self._session.server_capabilities.__dict__},
            )

        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit

        result = await self._execute_request("tools/list", params, timeout=timeout)
        raw_tools = result.get("tools") or []
        next_cursor = result.get("nextCursor")

        tools: list[MCPTool] = []
        for t in raw_tools:
            if isinstance(t, dict) and t.get("name"):
                tools.append(
                    MCPTool(
                        name=str(t["name"]),
                        description=str(t.get("description", "")),
                        input_schema=t.get("inputSchema") or {},
                        output_schema=t.get("outputSchema"),
                        metadata=sanitize_metadata(t.get("metadata") or {}),
                    )
                )

        return MCPPage(items=tools, next_cursor=next_cursor)

    async def list_all_tools(
        self,
        *,
        max_pages: int = 10,
        max_items: int = 500,
        timeout_per_page: Optional[float] = None,
    ) -> list[MCPTool]:
        """Fetch all tools across bounded paginated responses."""
        all_tools: list[MCPTool] = []
        cursor: Optional[str] = None
        pages_fetched = 0

        while pages_fetched < max_pages:
            page = await self.list_tools(cursor=cursor, timeout=timeout_per_page)
            pages_fetched += 1
            for tool in page.items:
                all_tools.append(tool)
                if len(all_tools) >= max_items:
                    log.warning("Tool listing reached maximum items bound (%d)", max_items)
                    return all_tools

            if not page.has_more:
                break
            cursor = page.next_cursor

        return all_tools

    async def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> MCPToolCallResult:
        """Invoke an MCP tool with sanitized parameters and normalize the result."""
        if not self._session.is_ready:
            raise ConnectorNotConnectedError(
                connector_id=self.connector_id,
                message="Cannot call tool: MCP client is not connected",
            )
        if not self._session.server_capabilities.tools:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'tools' capability",
            )
        if not name or not name.strip():
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="Tool name cannot be empty",
            )

        sanitized_args = sanitize_metadata(arguments or {})
        call_params: dict[str, Any] = {
            "name": name,
            "arguments": sanitized_args,
        }

        try:
            result = await self._execute_request("tools/call", call_params, timeout=timeout)
        except (ConnectorError, ConnectionResetError, ConnectionRefusedError, TimeoutError, OSError):
            raise
        except Exception as exc:
            raise ConnectorToolError(
                connector_id=self.connector_id,
                message=f"Error executing MCP tool '{name}': {exc}",
                cause=exc,
            ) from exc

        raw_content = result.get("content") or []
        is_error = bool(result.get("isError", False))
        structured = result.get("structuredContent") or result.get("structured_data") or result.get("structuredData")

        # Sanitize text blocks within content
        clean_content: list[dict[str, Any]] = []
        for block in raw_content:
            if isinstance(block, dict):
                clean_block = dict(block)
                if "text" in clean_block and isinstance(clean_block["text"], str):
                    clean_block["text"] = redact_secrets(clean_block["text"])
                clean_content.append(clean_block)
            elif isinstance(block, str):
                clean_content.append({"type": "text", "text": redact_secrets(block)})

        return MCPToolCallResult(
            content=clean_content,
            is_error=is_error,
            structured_data=sanitize_metadata(structured) if isinstance(structured, dict) else structured,
            metadata={"request_id": request_id, "tool_name": name},
        )

    # -----------------------------------------------------------------------
    # Resources API
    # -----------------------------------------------------------------------

    async def list_resources(
        self,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> MCPPage[MCPResource]:
        """List a single page of resources exposed by the MCP server."""
        if not self._session.is_ready:
            raise ConnectorNotConnectedError(
                connector_id=self.connector_id,
                message="Cannot list resources: MCP client is not connected",
            )
        if not self._session.server_capabilities.resources:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'resources' capability",
                details={"declared_capabilities": self._session.server_capabilities.__dict__},
            )

        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit

        result = await self._execute_request("resources/list", params, timeout=timeout)
        raw_resources = result.get("resources") or []
        next_cursor = result.get("nextCursor")

        resources: list[MCPResource] = []
        for r in raw_resources:
            if isinstance(r, dict) and r.get("uri") and r.get("name"):
                resources.append(
                    MCPResource(
                        uri=str(r["uri"]),
                        name=str(r["name"]),
                        description=str(r.get("description", "")),
                        mime_type=r.get("mimeType"),
                        size=r.get("size"),
                        metadata=sanitize_metadata(r.get("metadata") or {}),
                    )
                )

        return MCPPage(items=resources, next_cursor=next_cursor)

    async def list_all_resources(
        self,
        *,
        max_pages: int = 10,
        max_items: int = 500,
        timeout_per_page: Optional[float] = None,
    ) -> list[MCPResource]:
        """Fetch all resources across bounded paginated responses."""
        all_resources: list[MCPResource] = []
        cursor: Optional[str] = None
        pages_fetched = 0

        while pages_fetched < max_pages:
            page = await self.list_resources(cursor=cursor, timeout=timeout_per_page)
            pages_fetched += 1
            for res in page.items:
                all_resources.append(res)
                if len(all_resources) >= max_items:
                    log.warning("Resource listing reached maximum items bound (%d)", max_items)
                    return all_resources

            if not page.has_more:
                break
            cursor = page.next_cursor

        return all_resources

    async def read_resource(
        self,
        uri: str,
        *,
        timeout: Optional[float] = None,
    ) -> list[MCPResourceContent]:
        """Read resource content by URI."""
        if not self._session.is_ready:
            raise ConnectorNotConnectedError(
                connector_id=self.connector_id,
                message="Cannot read resource: MCP client is not connected",
            )
        if not self._session.server_capabilities.resources:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'resources' capability",
            )
        if not uri or not uri.strip():
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="Resource URI cannot be empty",
            )

        result = await self._execute_request("resources/read", {"uri": uri}, timeout=timeout)
        raw_contents = result.get("contents") or []

        contents: list[MCPResourceContent] = []
        for c in raw_contents:
            if isinstance(c, dict) and c.get("uri"):
                text_content = c.get("text")
                if text_content and isinstance(text_content, str):
                    text_content = redact_secrets(text_content)
                contents.append(
                    MCPResourceContent(
                        uri=str(c["uri"]),
                        mime_type=c.get("mimeType"),
                        text=text_content,
                        blob=c.get("blob"),
                    )
                )

        return contents

    async def list_resource_templates(
        self,
        *,
        cursor: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> list[MCPResourceTemplate]:
        """List parameterized dynamic resource templates."""
        if not self._session.server_capabilities.resources:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'resources' capability",
            )

        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor

        result = await self._execute_request("resources/templates/list", params, timeout=timeout)
        raw_templates = result.get("resourceTemplates") or []

        templates: list[MCPResourceTemplate] = []
        for t in raw_templates:
            if isinstance(t, dict) and t.get("uriTemplate") and t.get("name"):
                templates.append(
                    MCPResourceTemplate(
                        uri_template=str(t["uriTemplate"]),
                        name=str(t["name"]),
                        description=str(t.get("description", "")),
                        mime_type=t.get("mimeType"),
                    )
                )

        return templates

    # -----------------------------------------------------------------------
    # Prompts API
    # -----------------------------------------------------------------------

    async def list_prompts(
        self,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> MCPPage[MCPPrompt]:
        """List a single page of prompts exposed by the MCP server."""
        if not self._session.is_ready:
            raise ConnectorNotConnectedError(
                connector_id=self.connector_id,
                message="Cannot list prompts: MCP client is not connected",
            )
        if not self._session.server_capabilities.prompts:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'prompts' capability",
                details={"declared_capabilities": self._session.server_capabilities.__dict__},
            )

        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit

        result = await self._execute_request("prompts/list", params, timeout=timeout)
        raw_prompts = result.get("prompts") or []
        next_cursor = result.get("nextCursor")

        prompts: list[MCPPrompt] = []
        for p in raw_prompts:
            if isinstance(p, dict) and p.get("name"):
                args_list: list[MCPPromptArgument] = []
                for a in p.get("arguments") or []:
                    if isinstance(a, dict) and a.get("name"):
                        args_list.append(
                            MCPPromptArgument(
                                name=str(a["name"]),
                                description=str(a.get("description", "")),
                                required=bool(a.get("required", False)),
                            )
                        )
                prompts.append(
                    MCPPrompt(
                        name=str(p["name"]),
                        description=str(p.get("description", "")),
                        arguments=args_list,
                    )
                )

        return MCPPage(items=prompts, next_cursor=next_cursor)

    async def list_all_prompts(
        self,
        *,
        max_pages: int = 10,
        max_items: int = 500,
        timeout_per_page: Optional[float] = None,
    ) -> list[MCPPrompt]:
        """Fetch all prompts across bounded paginated responses."""
        all_prompts: list[MCPPrompt] = []
        cursor: Optional[str] = None
        pages_fetched = 0

        while pages_fetched < max_pages:
            page = await self.list_prompts(cursor=cursor, timeout=timeout_per_page)
            pages_fetched += 1
            for p in page.items:
                all_prompts.append(p)
                if len(all_prompts) >= max_items:
                    log.warning("Prompt listing reached maximum items bound (%d)", max_items)
                    return all_prompts

            if not page.has_more:
                break
            cursor = page.next_cursor

        return all_prompts

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> MCPPromptResult:
        """Render prompt template by name."""
        if not self._session.is_ready:
            raise ConnectorNotConnectedError(
                connector_id=self.connector_id,
                message="Cannot get prompt: MCP client is not connected",
            )
        if not self._session.server_capabilities.prompts:
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="MCP server does not support 'prompts' capability",
            )
        if not name or not name.strip():
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message="Prompt name cannot be empty",
            )

        sanitized_args = sanitize_metadata(arguments or {})
        params = {"name": name, "arguments": sanitized_args}
        result = await self._execute_request("prompts/get", params, timeout=timeout)

        raw_messages = result.get("messages") or []
        messages: list[MCPPromptMessage] = []
        for m in raw_messages:
            if isinstance(m, dict) and m.get("role") and m.get("content"):
                role = str(m["role"])
                content = m["content"]
                if isinstance(content, dict) and "text" in content and isinstance(content["text"], str):
                    content = {**content, "text": redact_secrets(content["text"])}
                messages.append(MCPPromptMessage(role=role, content=content))

        return MCPPromptResult(
            description=redact_secrets(str(result.get("description", ""))),
            messages=messages,
        )

    # -----------------------------------------------------------------------
    # Internal Request Helper with Error Normalization
    # -----------------------------------------------------------------------

    async def _execute_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Dispatch request through session and normalize any exceptions."""
        try:
            return await self._session.request(method, params, timeout=timeout)
        except (
            ConnectorNotConnectedError,
            ConnectorInvalidRequestError,
            ConnectorTimeoutError,
            ConnectorConnectionError,
            ConnectionResetError,
            ConnectionRefusedError,
            OSError,
        ):
            raise
        except asyncio.CancelledError:
            log.info("MCP request '%s' was cancelled", method)
            raise
        except Exception as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise ConnectorNotFoundError(
                    connector_id=self.connector_id,
                    message=f"Requested resource or tool not found in '{method}': {exc}",
                ) from exc
            raise ConnectorProviderError(
                connector_id=self.connector_id,
                message=f"MCP request '{method}' failed: {exc}",
                cause=exc,
            ) from exc
