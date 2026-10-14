"""Transport abstraction for the Model Context Protocol (MCP) in SAMANVAYA · SŪTRA.

Defines the transport interface decoupling the MCP client core from physical
communication mechanisms (future stdio, Streamable HTTP, SSE, or test doubles).

Invariants:
1. Pure abstract interface: Contains ZERO stdio process spawning or HTTP network calls.
2. Supports asynchronous request/response and fire-and-forget notifications.
3. Clean error reporting mapped to SŪTRA ConnectorError hierarchy.
4. Cancellation and timeout compatible.
"""
from __future__ import annotations

import abc
import time
from typing import Any, Optional

from ..errors import (
    ConnectorConnectionError,
    ConnectorNotConnectedError,
    ConnectorTimeoutError,
)


class MCPTransport(abc.ABC):
    """Abstract transport boundary for MCP communication.
    
    Subclasses in later phases will implement concrete transports:
    - Phase 7.6: MCPStdioTransport (local subprocess stdin/stdout)
    - Phase 7.7: MCPRemoteTransport (Streamable HTTP / SSE)
    - Testing: FakeMCPTransport (deterministic in-memory double)
    """

    @abc.abstractmethod
    async def open(self) -> None:
        """Establish the underlying transport stream or channel.
        
        Raises:
            ConnectorConnectionError: If transport cannot be opened.
        """
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Gracefully terminate the transport stream and release underlying resources."""
        ...

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Return True if the transport is open and ready to exchange messages."""
        ...

    @abc.abstractmethod
    async def send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the correlated response.
        
        Args:
            method: The MCP protocol method (e.g. 'initialize', 'tools/list').
            params: Optional dictionary of method parameters.
            timeout: Optional per-request timeout in seconds.
            
        Returns:
            The 'result' field of the JSON-RPC response.
            
        Raises:
            ConnectorNotConnectedError: If transport is not open.
            ConnectorTimeoutError: If the request times out.
            ConnectorProviderError: If the server returns a JSON-RPC error.
        """
        ...

    @abc.abstractmethod
    async def send_notification(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        """Send a one-way notification that does not expect a response.
        
        Args:
            method: The notification method (e.g. 'notifications/initialized').
            params: Optional notification parameters.
            
        Raises:
            ConnectorNotConnectedError: If transport is not open.
        """
        ...

    async def ping(self, timeout: float = 5.0) -> float:
        """Measure roundtrip latency to the MCP server.
        
        Default implementation calls 'ping' method; returns RTT in milliseconds.
        """
        if not self.is_connected():
            raise ConnectorNotConnectedError(
                connector_id="mcp.transport",
                message="Cannot ping: transport is not connected",
            )
        t0 = time.perf_counter()
        await self.send_request("ping", {}, timeout=timeout)
        return (time.perf_counter() - t0) * 1000.0

    def transport_info(self) -> dict[str, Any]:
        """Informational metadata regarding the transport type and status."""
        return {
            "type": self.__class__.__name__,
            "connected": self.is_connected(),
        }
