"""MCP protocol session and lifecycle manager for SAMANVAYA · SŪTRA.

Coordinates protocol initialization, version negotiation, server capability discovery,
and session state transitions over an abstract MCPTransport.

Invariants:
1. Supports modern (2026-07-28) and backwards-compatible (2025-03-26, 2024-11-05) flows.
2. Strictly rejects unsupported protocol versions without guessing or crashing.
3. Server identity is treated as informational only, never an authorization boundary.
4. Thread-safe and asyncio lock-protected state transitions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..errors import (
    ConnectorConnectionError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorProviderError,
    ConnectorTimeoutError,
)
from .models import (
    MCPClientCapabilities,
    MCPClientInfo,
    MCPProtocolVersion,
    MCPServerCapabilities,
    MCPServerInfo,
    MCPSessionState,
)
from .transport import MCPTransport

log = logging.getLogger(__name__)


class MCPSession:
    """Manages an active Model Context Protocol session over a transport."""

    def __init__(
        self,
        transport: MCPTransport,
        *,
        client_info: Optional[MCPClientInfo] = None,
        client_capabilities: Optional[MCPClientCapabilities] = None,
        preferred_protocol_version: Optional[MCPProtocolVersion] = None,
    ) -> None:
        self._transport = transport
        self._client_info = client_info or MCPClientInfo()
        self._client_capabilities = client_capabilities or MCPClientCapabilities()
        self._preferred_version = preferred_protocol_version or MCPProtocolVersion.latest()

        self._state: MCPSessionState = MCPSessionState.DISCONNECTED
        self._negotiated_version: Optional[MCPProtocolVersion] = None
        self._server_info: Optional[MCPServerInfo] = None
        self._server_capabilities: MCPServerCapabilities = MCPServerCapabilities()
        self._instructions: Optional[str] = None
        self._last_latency_ms: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def transport(self) -> MCPTransport:
        return self._transport

    @property
    def state(self) -> MCPSessionState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state == MCPSessionState.READY

    @property
    def protocol_version(self) -> Optional[MCPProtocolVersion]:
        return self._negotiated_version

    @property
    def client_info(self) -> MCPClientInfo:
        return self._client_info

    @property
    def server_info(self) -> Optional[MCPServerInfo]:
        return self._server_info

    @property
    def server_capabilities(self) -> MCPServerCapabilities:
        return self._server_capabilities

    @property
    def client_capabilities(self) -> MCPClientCapabilities:
        return self._client_capabilities

    @property
    def instructions(self) -> Optional[str]:
        return self._instructions

    @property
    def last_latency_ms(self) -> Optional[float]:
        return self._last_latency_ms

    async def initialize(self, *, timeout: float = 10.0) -> None:
        """Establish transport connection and execute MCP protocol handshake."""
        async with self._lock:
            if self._state == MCPSessionState.READY:
                return

            self._state = MCPSessionState.CONNECTING

            # 1. Open transport if not already open
            if not self._transport.is_connected():
                try:
                    await self._transport.open()
                except Exception as exc:
                    self._state = MCPSessionState.ERROR
                    raise ConnectorConnectionError(
                        connector_id="mcp.session",
                        message=f"Failed to open MCP transport: {exc}",
                        cause=exc,
                    ) from exc

            # 2. Build 'initialize' request payload
            init_params: dict[str, Any] = {
                "protocolVersion": self._preferred_version.value,
                "clientInfo": self._client_info.to_dict(),
                "capabilities": self._client_capabilities.to_dict(),
            }

            # 3. Send initialize request
            try:
                result = await self._transport.send_request("initialize", init_params, timeout=timeout)
            except ConnectorTimeoutError:
                self._state = MCPSessionState.ERROR
                raise
            except Exception as exc:
                self._state = MCPSessionState.ERROR
                raise ConnectorProviderError(
                    connector_id="mcp.session",
                    message=f"MCP initialization request failed: {exc}",
                    cause=exc,
                ) from exc

            # 4. Negotiate protocol version
            raw_version = str(result.get("protocolVersion", ""))
            if not raw_version:
                self._state = MCPSessionState.ERROR
                raise ConnectorProviderError(
                    connector_id="mcp.session",
                    message="Server initialize response missing 'protocolVersion'",
                )

            if not MCPProtocolVersion.is_supported(raw_version):
                self._state = MCPSessionState.ERROR
                raise ConnectorInvalidRequestError(
                    connector_id="mcp.session",
                    message=(
                        f"Unsupported MCP protocol version '{raw_version}'. "
                        f"Supported: {MCPProtocolVersion.supported_versions()}"
                    ),
                    details={
                        "server_version": raw_version,
                        "client_supported": MCPProtocolVersion.supported_versions(),
                    },
                )

            self._negotiated_version = MCPProtocolVersion(raw_version)

            # 5. Extract Server Info & Capabilities
            raw_server = result.get("serverInfo") or {}
            self._server_info = MCPServerInfo(
                name=str(raw_server.get("name", "unknown-mcp-server")),
                version=str(raw_server.get("version", "unknown")),
                title=raw_server.get("title"),
                description=raw_server.get("description"),
            )

            raw_caps = result.get("capabilities") or {}
            self._server_capabilities = MCPServerCapabilities.from_dict(raw_caps)
            self._instructions = result.get("instructions")

            # 6. Send initialized notification
            try:
                await self._transport.send_notification("notifications/initialized", {})
            except Exception as exc:
                log.warning("Server did not accept 'notifications/initialized': %s", exc)

            self._state = MCPSessionState.READY
            log.info(
                "MCP session initialized: version=%s, server='%s' (%s), capabilities(tools=%s, resources=%s, prompts=%s)",
                self._negotiated_version.value,
                self._server_info.name,
                self._server_info.version,
                self._server_capabilities.tools,
                self._server_capabilities.resources,
                self._server_capabilities.prompts,
            )

    async def close(self) -> None:
        """Gracefully terminate the MCP session and close underlying transport."""
        async with self._lock:
            self._state = MCPSessionState.CLOSED
            try:
                await self._transport.close()
            except Exception as exc:
                log.warning("Error closing transport during session termination: %s", exc)

    async def ping(self, timeout: float = 5.0) -> float:
        """Measure roundtrip latency to the server."""
        if not self.is_ready:
            raise ConnectorNotConnectedError(
                connector_id="mcp.session",
                message="Cannot ping: session is not initialized or ready",
            )
        try:
            latency = await self._transport.ping(timeout=timeout)
            self._last_latency_ms = latency
            return latency
        except Exception as exc:
            self._state = MCPSessionState.DEGRADED
            raise ConnectorConnectionError(
                connector_id="mcp.session",
                message=f"MCP ping failed: {exc}",
                cause=exc,
            ) from exc

    async def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send an authenticated/initialized request through the active session."""
        if not self.is_ready:
            raise ConnectorNotConnectedError(
                connector_id="mcp.session",
                message=f"Cannot execute '{method}': MCP session is in state '{self._state.value}'",
            )
        return await self._transport.send_request(method, params, timeout=timeout)
