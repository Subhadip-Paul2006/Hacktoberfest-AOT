"""Canonical Connector abstract interface defining the extension contract.

All future connector implementations (MCP servers, REST integrations, and CLI tools)
implement this interface to expose capabilities, health diagnostics, resource discovery,
and request execution.
"""
from __future__ import annotations

import abc
from collections.abc import Sequence
from typing import Any, Optional, Union

from .errors import ConnectorInvalidRequestError
from .models import (
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorHealth,
    ConnectorRequest,
    ConnectorResource,
    ConnectorResult,
    ConnectorStatus,
    ConnectorToolDefinition,
    ConnectorType,
)


class Connector(abc.ABC):
    """Canonical interface and domain boundary for SŪTRA connectors.
    
    A connector represents an integration point (e.g. MCP Server, REST API, CLI tool)
    with a declared identity, capabilities, and lifecycle.
    
    Invariants:
    1. Connectors do NOT own Telegram messaging logic.
    2. Connectors do NOT bypass or directly evaluate PermissionEngine decisions.
    3. Connectors do NOT implement duplicate retry, rate-limiting, or caching logic.
    4. Connectors do NOT store plaintext secrets or credentials in metadata.
    5. All executions are asynchronous and correlate via request_id.
    """

    @abc.abstractmethod
    def identity(self) -> ConnectorDescriptor:
        """Return the immutable descriptor detailing identity, type, capabilities, and status."""
        ...

    @property
    def connector_id(self) -> str:
        """Convenience accessor for the unique connector identifier."""
        return self.identity().connector_id

    @property
    def name(self) -> str:
        """Human-readable display name of the connector."""
        return self.identity().name

    @property
    def connector_type(self) -> ConnectorType:
        """Transport or architectural type (MCP, REST, CLI)."""
        return self.identity().type

    @property
    def status(self) -> ConnectorStatus:
        """Current lifecycle status of the connector."""
        return self.identity().status

    def capabilities(self) -> frozenset[ConnectorCapability]:
        """Return the set of capabilities declared by this connector."""
        return self.identity().capabilities

    def has_capability(self, capability: ConnectorCapability) -> bool:
        """Check whether this connector declares the specified capability."""
        return capability in self.capabilities()

    def require_capability(self, capability: ConnectorCapability) -> None:
        """Ensure the connector supports the capability; raise ConnectorInvalidRequestError if not."""
        if not self.has_capability(capability):
            raise ConnectorInvalidRequestError(
                connector_id=self.connector_id,
                message=f"Connector '{self.connector_id}' does not support required capability '{capability.value}'",
                details={"required_capability": capability.value, "declared_capabilities": [c.value for c in self.capabilities()]},
            )

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the underlying connection, session, or transport handshake.
        
        Subclasses should update their status to READY upon success, or raise
        ConnectorConnectionError upon failure.
        """
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Gracefully terminate active connections, stop child processes, and clean up resources.
        
        Subclasses should transition their status to DISCONNECTED.
        """
        ...

    @abc.abstractmethod
    async def health(self) -> ConnectorHealth:
        """Perform an active health check or ping against the integration endpoint.
        
        Returns a ConnectorHealth diagnostic snapshot with latency and status.
        """
        ...

    async def discover(self) -> Sequence[Union[ConnectorToolDefinition, ConnectorResource]]:
        """Discover tools, prompts, or resources exposed by this connector.
        
        Enforces ConnectorCapability.DISCOVERY before invocation.
        """
        self.require_capability(ConnectorCapability.DISCOVERY)
        return ()

    @abc.abstractmethod
    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        """Execute an operation within the connector domain boundary.
        
        The request is verified against capabilities, executed through the provider,
        and mapped into a normalized ConnectorResult.
        """
        ...
