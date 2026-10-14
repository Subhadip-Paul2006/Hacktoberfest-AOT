"""SŪTRA Connector implementation bridging the MCP Client Core to the system domain.

Implements the canonical sutra.connectors.Connector contract, exposing MCP tools
and resources as normalized ConnectorToolDefinitions, integrating with ActivityStore,
respecting the PermissionEngine boundary, and exposing ConnectorAnalysisDescriptor
for the Phase 7.4 Generic Analyzer.

Invariants:
1. Reuses shared SŪTRA resilience and redaction infrastructure.
2. Does NOT grant permissions or evaluate policies directly.
3. Completely abstracts transport specifics away from the rest of SŪTRA.
4. Normalizes all MCP results into ConnectorResult.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any, Optional, Union

from sutra.activity_store import ActivityStore
from sutra.permissions import PermissionRequest, PermissionRiskLevel
from sutra.redaction import redact_secrets
from ..errors import (
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorPermissionError,
    ConnectorToolError,
)
from ..interface import Connector
from ..models import (
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorHealth,
    ConnectorRequest,
    ConnectorResource,
    ConnectorResult,
    ConnectorStatus,
    ConnectorToolDefinition,
    ConnectorType,
    sanitize_metadata,
)
from .client import MCPClient
from .models import (
    MCPClientCapabilities,
    MCPClientInfo,
    MCPProtocolVersion,
    MCPSessionState,
)
from .transport import MCPTransport

log = logging.getLogger(__name__)


def _session_state_to_connector_status(state: MCPSessionState) -> ConnectorStatus:
    """Map MCPSessionState to generic SŪTRA ConnectorStatus."""
    mapping = {
        MCPSessionState.DISCONNECTED: ConnectorStatus.DISCONNECTED,
        MCPSessionState.CONNECTING: ConnectorStatus.CONNECTING,
        MCPSessionState.READY: ConnectorStatus.READY,
        MCPSessionState.DEGRADED: ConnectorStatus.DEGRADED,
        MCPSessionState.ERROR: ConnectorStatus.ERROR,
        MCPSessionState.CLOSED: ConnectorStatus.DISCONNECTED,
    }
    return mapping.get(state, ConnectorStatus.REGISTERED)


class MCPConnector(Connector):
    """SŪTRA Connector adapter encapsulating a Model Context Protocol client session."""

    def __init__(
        self,
        connector_id: str,
        transport: MCPTransport,
        *,
        name: Optional[str] = None,
        description: str = "MCP server integration",
        client_info: Optional[MCPClientInfo] = None,
        client_capabilities: Optional[MCPClientCapabilities] = None,
        preferred_protocol_version: Optional[MCPProtocolVersion] = None,
        declared_capabilities: Optional[frozenset[ConnectorCapability]] = None,
        activity_store: Optional[ActivityStore] = None,
        policy_engine: Optional[Any] = None,
        provider: str = "mcp",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._connector_id = connector_id
        self._name = name or f"MCP Server ({connector_id})"
        self._description = description
        self._activity_store = activity_store
        self._policy_engine = policy_engine
        self._provider = provider
        self._custom_metadata = sanitize_metadata(metadata or {})
        self._declared_capabilities = declared_capabilities

        self._client = MCPClient(
            transport,
            client_info=client_info,
            client_capabilities=client_capabilities,
            preferred_protocol_version=preferred_protocol_version,
            connector_id=connector_id,
        )

    @property
    def client(self) -> MCPClient:
        """Direct access to the underlying MCPClient core."""
        return self._client

    # -----------------------------------------------------------------------
    # Canonical Connector Interface Implementation
    # -----------------------------------------------------------------------

    def identity(self) -> ConnectorDescriptor:
        """Return immutable descriptor detailing identity, capabilities, and status."""
        state = self._client.state
        status = _session_state_to_connector_status(state)

        # Build dynamic capabilities from server declaration or static fallback
        if self._client.is_connected:
            caps = self._client.server_capabilities.to_connector_capabilities()
        elif self._declared_capabilities is not None:
            caps = self._declared_capabilities
        else:
            caps = frozenset({
                ConnectorCapability.DISCOVERY,
                ConnectorCapability.TOOL_EXECUTION,
                ConnectorCapability.HEALTH_CHECK,
                ConnectorCapability.READ,
            })

        meta: dict[str, Any] = {
            **self._custom_metadata,
            "transport": self._client.session.transport.__class__.__name__,
            "mcp_session_state": state.value,
        }
        if self._client.protocol_version:
            meta["protocol_version"] = self._client.protocol_version.value
        if self._client.server_info:
            meta["server_name"] = self._client.server_info.name
            meta["server_version"] = self._client.server_info.version

        return ConnectorDescriptor(
            connector_id=self._connector_id,
            name=self._name,
            type=ConnectorType.MCP,
            provider=self._provider,
            version="1.0.0",
            description=self._description,
            capabilities=caps,
            status=status,
            metadata=meta,
            enabled=True,
        )

    def analysis_descriptor(self) -> ConnectorAnalysisDescriptor:
        """Return standardized descriptor for Phase 7.4 Generic Analyzer."""
        desc = self.identity()
        transport_hint = self._custom_metadata.get(
            "transport_hint",
            "streamable_http" if "http" in self._client.session.transport.__class__.__name__.lower() else "mcp_custom"
        )
        return ConnectorAnalysisDescriptor(
            connector_id=desc.connector_id,
            name=desc.name,
            type=desc.type,
            provider=self._provider,
            transport=transport_hint,
            enabled=desc.enabled,
            status=desc.status,
            capabilities=desc.capabilities,
            version=desc.version,
            description=desc.description,
            declared_env_vars=tuple(self._custom_metadata.get("declared_env_vars", ())),
            declared_binary_names=tuple(self._custom_metadata.get("declared_binary_names", ())),
            metadata=dict(desc.metadata),
        )

    async def connect(self) -> None:
        """Establish MCP session handshake."""
        try:
            await self._client.connect()
        except Exception as exc:
            log.error("Failed to connect MCP connector '%s': %s", self._connector_id, exc)
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorConnectionError(
                connector_id=self._connector_id,
                message=f"MCP connection failed: {exc}",
                cause=exc,
            ) from exc

    async def disconnect(self) -> None:
        """Terminate MCP session and transport."""
        await self._client.disconnect()

    async def health(self) -> ConnectorHealth:
        """Perform active ping and measure roundtrip latency."""
        if not self._client.is_connected:
            return ConnectorHealth(
                status=self.status,
                healthy=False,
                message=f"MCP session is not connected ({self._client.state.value})",
            )
        try:
            latency = await self._client.ping()
            return ConnectorHealth(
                status=ConnectorStatus.READY,
                healthy=True,
                latency_ms=latency,
                message="MCP server ping successful",
                details={
                    "protocol_version": self._client.protocol_version.value if self._client.protocol_version else "unknown",
                    "server_info": self._client.server_info.__dict__ if self._client.server_info else {},
                },
            )
        except Exception as exc:
            return ConnectorHealth(
                status=ConnectorStatus.DEGRADED,
                healthy=False,
                message=f"MCP ping failed: {exc}",
            )

    async def discover(self) -> Sequence[Union[ConnectorToolDefinition, ConnectorResource]]:
        """Discover tools and resources exposed by the MCP server."""
        self.require_capability(ConnectorCapability.DISCOVERY)
        if not self._client.is_connected:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message="Cannot discover: MCP connector is not connected",
            )

        items: list[Union[ConnectorToolDefinition, ConnectorResource]] = []

        # Discover tools if supported
        if self._client.server_capabilities.tools:
            try:
                tools = await self._client.list_all_tools()
                for t in tools:
                    is_ro = t.name.startswith(
                        ("get_", "list_", "read_", "search_", "view_", "describe_")
                    ) or bool(t.metadata.get("read_only") or t.metadata.get("readOnly"))
                    items.append(
                        t.to_connector_tool_definition(
                            read_only=is_ro,
                            risk_level="LOW" if is_ro else "HIGH",
                        )
                    )
            except Exception as exc:
                log.warning("Tool discovery error on '%s': %s", self._connector_id, exc)

        # Discover resources if supported
        if self._client.server_capabilities.resources:
            try:
                resources = await self._client.list_all_resources()
                for r in resources:
                    items.append(r.to_connector_resource())
            except Exception as exc:
                log.warning("Resource discovery error on '%s': %s", self._connector_id, exc)

        return tuple(items)

    async def discover_tools(self) -> Sequence[ConnectorToolDefinition]:
        """Convenience method to discover only tools exposed by the MCP server."""
        items = await self.discover()
        return tuple(i for i in items if isinstance(i, ConnectorToolDefinition))


    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        """Execute an operation within the connector boundary with ActivityStore auditing."""
        t0 = time.time()
        op = request.operation

        # Resolve operation routing
        if op.startswith("tool:") or op == "tool_call" or (not op.startswith("resource:") and not op.startswith("prompt:")):
            # Tool call operation
            tool_name = op[5:] if op.startswith("tool:") else request.parameters.get("name") or op
            return await self._execute_tool_call(request, tool_name)
        elif op.startswith("resource:") or op == "read_resource":
            uri = op[9:] if op.startswith("resource:") else request.parameters.get("uri", "")
            return await self._execute_read_resource(request, uri)
        elif op.startswith("prompt:") or op == "get_prompt":
            prompt_name = op[7:] if op.startswith("prompt:") else request.parameters.get("name", "")
            return await self._execute_get_prompt(request, prompt_name)
        else:
            raise ConnectorInvalidRequestError(
                connector_id=self._connector_id,
                message=f"Unknown or unsupported operation '{op}'",
            )

    # -----------------------------------------------------------------------
    # Internal Execution Handlers
    # -----------------------------------------------------------------------

    async def _execute_tool_call(self, request: ConnectorRequest, tool_name: str) -> ConnectorResult:
        """Execute tool call with capability check, secret sanitization, and ActivityStore auditing."""
        self.require_capability(ConnectorCapability.TOOL_EXECUTION)
        if not self._client.is_connected:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message=f"Cannot execute tool '{tool_name}': connector is not connected",
            )

        args = request.parameters.get("arguments", request.parameters)
        if isinstance(args, dict) and "name" in args and args["name"] == tool_name:
            args = {k: v for k, v in args.items() if k != "name"}

        clean_args = sanitize_metadata(args) if isinstance(args, dict) else {}

        tc_id: Optional[str] = None
        # Audit start in ActivityStore
        if self._activity_store is not None:
            try:
                tc_id = self._activity_store.record_tool_call(
                    request_id=request.request_id,
                    connector_id=self._connector_id,
                    tool_name=f"{self._connector_id}.{tool_name}",
                    arguments=clean_args,
                    status="running",
                )
            except Exception as exc:
                log.warning("Failed to record tool call start in ActivityStore: %s", exc)

        t0 = time.time()
        try:
            # PolicyEngine authorization boundary
            if self._policy_engine is not None:
                is_read_only = tool_name.startswith(
                    ("get_", "list_", "read_", "search_", "view_", "describe_")
                )

                if is_read_only:
                    risk_level = PermissionRiskLevel.LOW
                else:
                    risk_level = self._policy_engine.assess_risk(
                        action_type="tool_call",
                        command=f"{self._connector_id}.{tool_name}",
                        resource_path=f"{self._connector_id}:{tool_name}",
                    )
                    if risk_level == PermissionRiskLevel.LOW:
                        risk_level = PermissionRiskLevel.HIGH

                perm_req = PermissionRequest(
                    request_id=request.request_id,
                    action_type="tool_read" if is_read_only else "tool_call",
                    resource_path=f"{self._connector_id}:{tool_name}",
                    command=f"{self._connector_id}.{tool_name}",
                    agent_id=request.actor or "agent-system",
                    session_id=request.session_id or "session-system",
                    risk_level=risk_level,
                    description=f"Invocation of tool '{tool_name}' on connector '{self._connector_id}'",
                )
                allowed, reason = self._policy_engine.check_permission(perm_req)
                if not allowed:
                    raise ConnectorPermissionError(
                        connector_id=self._connector_id,
                        message=f"Operation '{tool_name}' rejected by permission policy: {reason}",
                        details={"tool_name": tool_name, "risk_level": risk_level.value},
                    )

            mcp_res = await self._client.call_tool(
                name=tool_name,
                arguments=clean_args,
                request_id=request.request_id,
            )
            duration_ms = (time.time() - t0) * 1000.0
            clean_content = redact_secrets(mcp_res.get_text_content())

            result = ConnectorResult(
                success=not mcp_res.is_error,
                content=clean_content,
                structured_data=sanitize_metadata(mcp_res.structured_data) if isinstance(mcp_res.structured_data, dict) else mcp_res.structured_data,
                metadata={
                    "request_id": request.request_id,
                    "connector_id": self._connector_id,
                    "tool_name": tool_name,
                    "duration_ms": duration_ms,
                    **sanitize_metadata(mcp_res.metadata),
                },
                error=clean_content if mcp_res.is_error else None,
            )

            # Audit completion in ActivityStore
            if self._activity_store is not None and tc_id:
                try:
                    self._activity_store.update_tool_call(
                        tool_call_id=tc_id,
                        status="success" if result.success else "error",
                        result=clean_content,
                        duration_ms=duration_ms,
                    )
                except Exception as exc:
                    log.warning("Failed to record tool call end in ActivityStore: %s", exc)

            return result

        except Exception as exc:
            duration_ms = (time.time() - t0) * 1000.0
            err_msg = str(exc)
            if self._activity_store is not None and tc_id:
                try:
                    self._activity_store.update_tool_call(
                        tool_call_id=tc_id,
                        status="error",
                        result=err_msg,
                        duration_ms=duration_ms,
                    )
                except Exception as audit_exc:
                    log.warning("Failed to record tool call error in ActivityStore: %s", audit_exc)

            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorToolError(
                connector_id=self._connector_id,
                message=f"Tool '{tool_name}' execution failed: {err_msg}",
                cause=exc,
            ) from exc

    async def _execute_read_resource(self, request: ConnectorRequest, uri: str) -> ConnectorResult:
        """Execute resource read operation."""
        self.require_capability(ConnectorCapability.RESOURCES)
        if not self._client.is_connected:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message="Cannot read resource: connector is not connected",
            )
        contents = await self._client.read_resource(uri)
        raw_items = [
            {"uri": c.uri, "mimeType": c.mime_type, "text": c.text, "blob": c.blob}
            for c in contents
        ]
        text_summary = "\n".join(c.text for c in contents if c.text)
        return ConnectorResult(
            success=True,
            content=text_summary,
            structured_data=raw_items,
            metadata={"uri": uri, "request_id": request.request_id},
        )

    async def _execute_get_prompt(self, request: ConnectorRequest, prompt_name: str) -> ConnectorResult:
        """Execute prompt rendering operation."""
        self.require_capability(ConnectorCapability.PROMPTS)
        if not self._client.is_connected:
            raise ConnectorNotConnectedError(
                connector_id=self._connector_id,
                message="Cannot get prompt: connector is not connected",
            )
        args = request.parameters.get("arguments", request.parameters)
        prompt_res = await self._client.get_prompt(prompt_name, arguments=args if isinstance(args, dict) else {})
        messages_data = [
            {"role": m.role, "content": m.content}
            for m in prompt_res.messages
        ]
        return ConnectorResult(
            success=True,
            content=prompt_res.description,
            structured_data=messages_data,
            metadata={"prompt_name": prompt_name, "request_id": request.request_id},
        )
