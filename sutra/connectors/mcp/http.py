"""Remote MCP HTTP transport implementation for SAMANVAYA · SŪTRA.

Enables secure, transport-level communication with remote MCP servers over:
1. Streamable HTTP (Primary, modern MCP standard: POST JSON-RPC with direct JSON
   or streaming text/event-stream responses).
2. Legacy SSE (Compatibility fallback: GET /sse connection establishing an event
   stream, with POST to an announced endpoint).

Invariants:
1. Transport only: Does NOT duplicate MCPClient, MCPSession, or Connector logic.
2. Security: Enforces HTTPS for remote endpoints, validates schemes, bounds redirects.
3. Secret-safe: Authorization headers, tokens, and credentials are never logged,
   leaked to metrics, or persisted in ActivityStore.
4. Bounded resources: Strict limits on response size, SSE event size, and concurrent requests.
5. Error normalization: HTTP and network failures map cleanly to SŪTRA ConnectorError types.
6. Cancellation safe: Request cancellation propagates cleanly without task or connection leaks.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

import httpx

from sutra.metrics import get_metrics
from sutra.redaction import redact_secrets
from ..errors import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorInvalidRequestError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorProviderError,
    ConnectorTimeoutError,
    ConnectorUnavailableError,
)
from ..models import sanitize_metadata
from .transport import MCPTransport

log = logging.getLogger(__name__)

# Sensitive header substrings to redact
_SENSITIVE_HEADER_KEYS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key",
    "token", "secret", "cookie", "set-cookie", "x-auth-token",
})


class MCPHttpState(str, enum.Enum):
    """Lifecycle states of the remote MCP HTTP transport."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    CLOSING = "CLOSING"
    ERROR = "ERROR"


class HttpTransportMode(str, enum.Enum):
    """Transport protocol mode for HTTP MCP servers."""
    STREAMABLE_HTTP = "streamable_http"  # Modern standard (POST with JSON or SSE response)
    LEGACY_SSE = "legacy_sse"            # Legacy 2024-11 standard (GET /sse + POST /endpoint)


def _sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of headers with secrets and tokens redacted."""
    safe: dict[str, str] = {}
    for k, v in headers.items():
        k_lower = k.lower()
        if any(sens in k_lower for sens in _SENSITIVE_HEADER_KEYS):
            safe[k] = "[REDACTED]"
        else:
            safe[k] = v
    return safe


def _mask_url_credentials(raw_url: str) -> str:
    """Mask any embedded username/password in a URL."""
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            if parsed.username:
                netloc = f"***@{netloc}"
            return urllib.parse.urlunsplit((
                parsed.scheme,
                netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            ))
        return raw_url
    except Exception:
        return "[REDACTED_URL]"


@dataclass(frozen=True)
class MCPHttpConfig:
    """Strongly-typed configuration for remote MCP HTTP transport.
    
    Attributes:
        url: Target MCP endpoint URL (must be https:// unless allow_http=True).
        headers: Optional static HTTP headers to include with requests.
        auth_provider: Optional sync or async callable providing dynamic auth headers.
        connect_timeout: Seconds to wait for TCP connection establishment.
        request_timeout: Default per-request write/read timeout in seconds.
        read_timeout: Maximum seconds to wait for streaming response chunks.
        shutdown_timeout: Grace period in seconds for connection teardown.
        max_response_bytes: Maximum allowed size in bytes for a JSON response body.
        max_event_bytes: Maximum allowed size in bytes for a single SSE event.
        allow_http: If True, permits plain HTTP (always allowed for localhost/127.0.0.1).
        legacy_sse: If True, uses legacy MCP SSE mode (GET /sse + POST).
        max_redirects: Maximum number of HTTP redirects to follow.
        session_id_header: Header name used by server to identify MCP session.
        max_concurrent_requests: Concurrency limit for simultaneous requests.
        verify_ssl: Whether to verify SSL/TLS certificates.
        http_client: Optional pre-configured httpx.AsyncClient (for testing/mocking).
    """
    url: str
    headers: Optional[Mapping[str, str]] = None
    auth_provider: Optional[Callable[[], Union[Mapping[str, str], Awaitable[Mapping[str, str]]]]] = None
    connect_timeout: float = 10.0
    request_timeout: float = 30.0
    read_timeout: float = 30.0
    shutdown_timeout: float = 5.0
    max_response_bytes: int = 10 * 1024 * 1024  # 10MB
    max_event_bytes: int = 1024 * 1024          # 1MB
    allow_http: bool = False
    legacy_sse: bool = False
    max_redirects: int = 5
    session_id_header: str = "mcp-session-id"
    max_concurrent_requests: int = 100
    verify_ssl: bool = True
    http_client: Optional[httpx.AsyncClient] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate URL scheme and security policies."""
        if not self.url or not isinstance(self.url, str):
            raise ConnectorInvalidRequestError(
                connector_id="mcp.http",
                message="MCP HTTP transport requires a non-empty string URL",
            )
        try:
            parsed = urllib.parse.urlsplit(self.url)
        except Exception as exc:
            raise ConnectorInvalidRequestError(
                connector_id="mcp.http",
                message=f"Invalid URL format: {exc}",
                cause=exc,
            ) from exc

        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ConnectorInvalidRequestError(
                connector_id="mcp.http",
                message=f"Invalid URL scheme '{scheme}'. Only 'https' and 'http' are supported.",
                details={"scheme": scheme},
            )

        # Enforce HTTPS unless allow_http=True or localhost
        if scheme == "http" and not self.allow_http:
            host = (parsed.hostname or "").lower()
            is_local = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local")
            if not is_local:
                raise ConnectorInvalidRequestError(
                    connector_id="mcp.http",
                    message=(
                        f"Insecure scheme 'http://' is not permitted for remote host '{host}'. "
                        "HTTPS is required for remote MCP servers unless allow_http=True."
                    ),
                    details={"host": host},
                )


class HttpMCPTransport(MCPTransport):
    """Remote MCP transport supporting Streamable HTTP and legacy SSE."""

    def __init__(self, config: MCPHttpConfig) -> None:
        config.validate()
        self._config = config
        self._state = MCPHttpState.DISCONNECTED
        self._session_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._owns_client: bool = True
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._lock = asyncio.Lock()
        self._request_counter: int = 0

        # Legacy SSE mode specific state
        self._legacy_post_endpoint: Optional[str] = None
        self._legacy_sse_task: Optional[asyncio.Task[None]] = None
        self._legacy_pending_requests: dict[Union[int, str], asyncio.Future[dict[str, Any]]] = {}
        self._legacy_stream_response: Optional[httpx.Response] = None

    # -----------------------------------------------------------------------
    # Properties & State
    # -----------------------------------------------------------------------

    @property
    def config(self) -> MCPHttpConfig:
        return self._config

    @property
    def state(self) -> MCPHttpState:
        return self._state

    @property
    def mode(self) -> HttpTransportMode:
        return HttpTransportMode.LEGACY_SSE if self._config.legacy_sse else HttpTransportMode.STREAMABLE_HTTP

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def is_connected(self) -> bool:
        return self._state == MCPHttpState.CONNECTED and self._client is not None

    def transport_info(self) -> dict[str, Any]:
        """Return safe, non-sensitive diagnostic info for the analyzer and logs."""
        return {
            "type": self.__class__.__name__,
            "mode": self.mode.value,
            "connected": self.is_connected(),
            "state": self._state.value,
            "url": _mask_url_credentials(self._config.url),
            "allow_http": self._config.allow_http,
            "has_auth": (
                self._config.headers is not None
                and any(k.lower() in _SENSITIVE_HEADER_KEYS for k in self._config.headers.keys())
            ) or (self._config.auth_provider is not None),
            "has_session_id": self._session_id is not None,
        }

    # -----------------------------------------------------------------------
    # Header Construction & Authentication Injection
    # -----------------------------------------------------------------------

    async def _build_headers(self, custom: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """Assemble all request headers with authentication injected."""
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Sutra-MCP-HTTP/1.0",
        }

        # 1. Static configured headers
        if self._config.headers:
            headers.update(self._config.headers)

        # 2. Dynamic auth provider hook
        if self._config.auth_provider:
            try:
                auth_res = self._config.auth_provider()
                if asyncio.iscoroutine(auth_res):
                    auth_headers = await auth_res
                else:
                    auth_headers = auth_res  # type: ignore[assignment]
                if isinstance(auth_headers, Mapping):
                    headers.update(auth_headers)
            except Exception as exc:
                log.error("Auth provider failed during header construction: %s", exc)
                raise ConnectorAuthError(
                    connector_id="mcp.http",
                    message=f"Failed to generate authentication headers: {exc}",
                    cause=exc,
                ) from exc

        # 3. Session tracking header
        if self._session_id:
            headers[self._config.session_id_header] = self._session_id

        # 4. Caller overrides
        if custom:
            headers.update(custom)

        return headers

    # -----------------------------------------------------------------------
    # Lifecycle: open() and close()
    # -----------------------------------------------------------------------

    async def open(self) -> None:
        """Establish HTTP connection and verify reachability."""
        async with self._lock:
            if self._state == MCPHttpState.CONNECTED:
                return

            self._state = MCPHttpState.CONNECTING
            metrics = get_metrics()
            metrics.record("mcp_http_connect_total", 1.0)

            try:
                # 1. Initialize httpx client
                if self._config.http_client is not None:
                    self._client = self._config.http_client
                    self._owns_client = False
                else:
                    timeout = httpx.Timeout(
                        connect=self._config.connect_timeout,
                        read=self._config.read_timeout,
                        write=self._config.request_timeout,
                        pool=self._config.connect_timeout,
                    )
                    self._client = httpx.AsyncClient(
                        timeout=timeout,
                        follow_redirects=True,
                        max_redirects=self._config.max_redirects,
                        verify=self._config.verify_ssl,
                    )
                    self._owns_client = True

                # 2. If legacy SSE, establish GET /sse stream connection
                if self._config.legacy_sse:
                    await self._open_legacy_sse()

                self._state = MCPHttpState.CONNECTED
                metrics.record("mcp_http_connect_success_total", 1.0)
                log.info(
                    "MCP HTTP transport opened: mode=%s, url=%s",
                    self.mode.value,
                    _mask_url_credentials(self._config.url),
                )
            except Exception as exc:
                self._state = MCPHttpState.ERROR
                metrics.record("mcp_http_connect_error_total", 1.0)
                await self._cleanup()
                mapped = self._map_network_error(exc, action="connect")
                raise mapped from exc

    async def close(self) -> None:
        """Gracefully terminate HTTP client, cancel background tasks, and clean up."""
        async with self._lock:
            if self._state == MCPHttpState.DISCONNECTED:
                return
            self._state = MCPHttpState.CLOSING
            metrics = get_metrics()
            metrics.record("mcp_http_disconnect_total", 1.0)
            await self._cleanup()
            self._state = MCPHttpState.DISCONNECTED
            log.info("MCP HTTP transport cleanly closed")

    async def _cleanup(self) -> None:
        """Internal cleanup for tasks, streams, and httpx client."""
        # Cancel any pending legacy SSE requests
        for fut in list(self._legacy_pending_requests.values()):
            if not fut.done():
                fut.set_exception(
                    ConnectorNotConnectedError(
                        connector_id="mcp.http",
                        message="Transport closed while request was pending",
                    )
                )
        self._legacy_pending_requests.clear()

        # Cancel legacy SSE listener task
        if self._legacy_sse_task and not self._legacy_sse_task.done():
            self._legacy_sse_task.cancel()
            try:
                await asyncio.wait_for(self._legacy_sse_task, timeout=self._config.shutdown_timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._legacy_sse_task = None

        # Close legacy response stream
        if self._legacy_stream_response is not None:
            try:
                await self._legacy_stream_response.aclose()
            except Exception:
                pass
            self._legacy_stream_response = None

        # Close HTTP client if owned
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    # -----------------------------------------------------------------------
    # Legacy SSE Transport Lifecycle
    # -----------------------------------------------------------------------

    async def _open_legacy_sse(self) -> None:
        """Connect to legacy SSE endpoint, read endpoint event, and spawn background listener."""
        assert self._client is not None
        headers = await self._build_headers({"Accept": "text/event-stream"})
        endpoint_event_received = asyncio.Event()
        endpoint_holder: list[str] = []
        error_holder: list[Exception] = []

        async def _legacy_listener() -> None:
            try:
                req = self._client.build_request("GET", self._config.url, headers=headers)
                response = await self._client.send(req, stream=True)
                self._legacy_stream_response = response

                if response.status_code != 200:
                    err = self._map_http_status_error(response.status_code, "legacy SSE connect")
                    error_holder.append(err)
                    endpoint_event_received.set()
                    return

                # Record session id if present
                sess_hdr = response.headers.get(self._config.session_id_header)
                if sess_hdr:
                    self._session_id = sess_hdr

                current_event = "message"
                data_lines: list[str] = []

                async for line in response.aiter_lines():
                    if line.startswith(":"):
                        continue  # comment / keepalive
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
                    elif line == "":
                        # Event boundary
                        full_data = "\n".join(data_lines)
                        data_lines.clear()

                        if current_event == "endpoint":
                            endpoint_holder.append(full_data)
                            endpoint_event_received.set()
                        elif current_event == "message" and full_data:
                            self._dispatch_legacy_message(full_data)
                        current_event = "message"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not endpoint_event_received.is_set():
                    error_holder.append(exc)
                    endpoint_event_received.set()
                log.warning("Legacy SSE listener stream terminated: %s", exc)
                self._state = MCPHttpState.ERROR

        self._legacy_sse_task = asyncio.create_task(_legacy_listener())

        # Wait for endpoint event
        try:
            await asyncio.wait_for(endpoint_event_received.wait(), timeout=self._config.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectorTimeoutError(
                connector_id="mcp.http",
                message=f"Timeout waiting for legacy SSE 'endpoint' event from {self._config.url}",
            ) from exc

        if error_holder:
            raise error_holder[0]

        if not endpoint_holder:
            raise ConnectorConnectionError(
                connector_id="mcp.http",
                message="Legacy SSE connection established but server sent no 'endpoint' event",
            )

        raw_endpoint = endpoint_holder[0].strip()
        # Resolve against base URL
        self._legacy_post_endpoint = urllib.parse.urljoin(self._config.url, raw_endpoint)
        log.info("Legacy SSE announced POST endpoint: %s", self._legacy_post_endpoint)

    def _dispatch_legacy_message(self, raw_json: str) -> None:
        """Dispatch an incoming JSON-RPC response from legacy SSE stream to awaiting future."""
        try:
            payload = json.loads(raw_json)
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        req_id = payload.get("id")
        if req_id is not None and req_id in self._legacy_pending_requests:
            fut = self._legacy_pending_requests.pop(req_id)
            if not fut.done():
                fut.set_result(payload)

    # -----------------------------------------------------------------------
    # Request Dispatch: send_request()
    # -----------------------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the correlated result."""
        if not self.is_connected():
            raise ConnectorNotConnectedError(
                connector_id="mcp.http",
                message=f"Cannot execute '{method}': HTTP transport is not connected (state: {self._state.value})",
            )

        req_timeout = timeout or self._config.request_timeout
        self._request_counter += 1
        req_id = self._request_counter

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        metrics = get_metrics()
        metrics.record("mcp_http_request_total", 1.0)

        async with self._semaphore:
            if self._config.legacy_sse:
                return await self._send_request_legacy(req_id, payload, req_timeout)
            else:
                return await self._send_request_streamable(req_id, payload, req_timeout)

    async def _send_request_streamable(
        self,
        req_id: Union[int, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Dispatch via Streamable HTTP (modern POST with JSON or SSE streaming response)."""
        assert self._client is not None
        headers = await self._build_headers({"Content-Type": "application/json"})
        body_bytes = json.dumps(payload).encode("utf-8")

        try:
            req = self._client.build_request(
                "POST",
                self._config.url,
                headers=headers,
                content=body_bytes,
                timeout=timeout,
            )
            response = await self._client.send(req, stream=True)
        except Exception as exc:
            self._record_error_metric(exc)
            mapped = self._map_network_error(exc, action="send_request")
            raise mapped from exc

        try:
            # Check HTTP status
            if response.status_code >= 400:
                await response.aread()
                err = self._map_http_status_error(response.status_code, f"request '{payload['method']}'")
                raise err

            # Track session id if returned in response headers
            sess_hdr = response.headers.get(self._config.session_id_header)
            if sess_hdr:
                self._session_id = sess_hdr

            content_type = response.headers.get("content-type", "").lower()

            # Case A: Streaming SSE response
            if "text/event-stream" in content_type:
                return await self._read_sse_stream_response(response, req_id, timeout)

            # Case B: Standard application/json response
            return await self._read_json_response(response, req_id)
        finally:
            await response.aclose()

    async def _read_json_response(self, response: httpx.Response, req_id: Union[int, str]) -> dict[str, Any]:
        """Read and parse direct JSON-RPC response."""
        content = await response.aread()
        if len(content) > self._config.max_response_bytes:
            raise ConnectorProviderError(
                connector_id="mcp.http",
                message=(
                    f"Response size ({len(content)} bytes) exceeded configured limit "
                    f"of {self._config.max_response_bytes} bytes"
                ),
            )

        try:
            text = content.decode("utf-8")
            data = json.loads(text)
        except Exception as exc:
            raise ConnectorProviderError(
                connector_id="mcp.http",
                message=f"Malformed JSON-RPC response from server: {exc}",
                cause=exc,
            ) from exc

        return self._extract_result(data, req_id)

    async def _read_sse_stream_response(
        self,
        response: httpx.Response,
        req_id: Union[int, str],
        timeout: float,
    ) -> dict[str, Any]:
        """Read lines from SSE response stream until event with matching id arrives."""
        current_event = "message"
        data_lines: list[str] = []
        total_bytes = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async for line in response.aiter_lines():
            if loop.time() > deadline:
                raise ConnectorTimeoutError(
                    connector_id="mcp.http",
                    message="Timed out reading streaming SSE response",
                )

            total_bytes += len(line.encode("utf-8"))
            if total_bytes > self._config.max_response_bytes:
                raise ConnectorProviderError(
                    connector_id="mcp.http",
                    message="Streaming response exceeded maximum allowed bytes",
                )

            if line.startswith(":"):
                continue  # keepalive / comment
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                chunk = line[len("data:"):].strip()
                if len(chunk.encode("utf-8")) > self._config.max_event_bytes:
                    raise ConnectorProviderError(
                        connector_id="mcp.http",
                        message="SSE event data exceeded max_event_bytes limit",
                    )
                data_lines.append(chunk)
            elif line == "":
                # Event boundary
                full_data = "\n".join(data_lines)
                data_lines.clear()
                if full_data:
                    try:
                        event_json = json.loads(full_data)
                    except Exception:
                        continue  # Skip unparseable chunks

                    if isinstance(event_json, dict) and event_json.get("id") == req_id:
                        return self._extract_result(event_json, req_id)
                current_event = "message"

        raise ConnectorProviderError(
            connector_id="mcp.http",
            message=f"SSE stream closed without delivering response for request id={req_id}",
        )

    async def _send_request_legacy(
        self,
        req_id: Union[int, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Dispatch via legacy SSE (POST to announced endpoint, await response on SSE stream)."""
        assert self._client is not None
        if not self._legacy_post_endpoint:
            raise ConnectorConnectionError(
                connector_id="mcp.http",
                message="Cannot send legacy request: POST endpoint was not announced by server",
            )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._legacy_pending_requests[req_id] = fut

        headers = await self._build_headers({"Content-Type": "application/json"})
        body_bytes = json.dumps(payload).encode("utf-8")

        try:
            post_res = await self._client.post(
                self._legacy_post_endpoint,
                headers=headers,
                content=body_bytes,
                timeout=timeout,
            )
            if post_res.status_code >= 400:
                self._legacy_pending_requests.pop(req_id, None)
                raise self._map_http_status_error(post_res.status_code, f"legacy POST '{payload['method']}'")

            # Await response from GET SSE listener
            response_payload = await asyncio.wait_for(fut, timeout=timeout)
            return self._extract_result(response_payload, req_id)
        except asyncio.TimeoutError as exc:
            self._legacy_pending_requests.pop(req_id, None)
            raise ConnectorTimeoutError(
                connector_id="mcp.http",
                message=f"Timeout waiting for legacy SSE response for request id={req_id}",
            ) from exc
        except Exception as exc:
            self._legacy_pending_requests.pop(req_id, None)
            self._record_error_metric(exc)
            mapped = self._map_network_error(exc, action="legacy_send_request")
            raise mapped from exc

    def _extract_result(self, data: dict[str, Any], req_id: Union[int, str]) -> dict[str, Any]:
        """Extract 'result' or raise mapped error from JSON-RPC response."""
        if not isinstance(data, dict):
            raise ConnectorProviderError(
                connector_id="mcp.http",
                message=f"Server returned non-object JSON-RPC payload: {type(data)}",
            )

        if "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                code = err.get("code")
                msg = redact_secrets(str(err.get("message", "Unknown MCP error")))
                details = err.get("data")
                if code in (-32600, -32602):
                    raise ConnectorInvalidRequestError(
                        connector_id="mcp.http",
                        message=f"JSON-RPC invalid request: {msg}",
                        details={"code": code, "error": details},
                    )
                elif code == -32601:
                    raise ConnectorNotFoundError(
                        connector_id="mcp.http",
                        message=f"JSON-RPC method not found: {msg}",
                        details={"code": code, "error": details},
                    )
                else:
                    raise ConnectorProviderError(
                        connector_id="mcp.http",
                        message=f"MCP server error ({code}): {msg}",
                        details={"code": code, "error": details},
                    )
            raise ConnectorProviderError(
                connector_id="mcp.http",
                message=f"MCP server returned error: {redact_secrets(str(err))}",
            )

        if "result" in data:
            res = data["result"]
            if isinstance(res, dict):
                return res
            return {"value": res}

        # Some responses might be empty dict or standard object
        return data

    # -----------------------------------------------------------------------
    # Notifications: send_notification()
    # -----------------------------------------------------------------------

    async def send_notification(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        """Send a one-way notification that does not expect a JSON-RPC response."""
        if not self.is_connected():
            raise ConnectorNotConnectedError(
                connector_id="mcp.http",
                message=f"Cannot send notification '{method}': transport is not connected",
            )

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }

        assert self._client is not None
        target_url = self._legacy_post_endpoint if self._config.legacy_sse else self._config.url
        if not target_url:
            return

        headers = await self._build_headers({"Content-Type": "application/json"})
        body_bytes = json.dumps(payload).encode("utf-8")

        async with self._semaphore:
            try:
                res = await self._client.post(
                    target_url,
                    headers=headers,
                    content=body_bytes,
                    timeout=self._config.request_timeout,
                )
                if res.status_code >= 400:
                    log.warning(
                        "MCP notification '%s' returned HTTP %d: %s",
                        method,
                        res.status_code,
                        res.text[:200],
                    )
            except Exception as exc:
                log.warning("Failed to send MCP notification '%s': %s", method, exc)

    # -----------------------------------------------------------------------
    # Error Mapping & Metric Helpers
    # -----------------------------------------------------------------------

    def _record_error_metric(self, exc: Exception) -> None:
        """Record corresponding error metric."""
        metrics = get_metrics()
        metrics.record("mcp_http_error_total", 1.0)
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            metrics.record("mcp_http_timeout_total", 1.0)

    def _map_http_status_error(self, status: int, action: str) -> ConnectorError:
        """Map HTTP error status codes to canonical SŪTRA ConnectorError types."""
        safe_url = _mask_url_credentials(self._config.url)
        if status in (401,):
            return ConnectorAuthError(
                connector_id="mcp.http",
                message=f"HTTP 401 Unauthorized during {action} at {safe_url}",
                details={"status_code": status},
            )
        elif status in (403,):
            return ConnectorPermissionError(
                connector_id="mcp.http",
                message=f"HTTP 403 Forbidden during {action} at {safe_url}",
                details={"status_code": status},
            )
        elif status == 404:
            return ConnectorNotFoundError(
                connector_id="mcp.http",
                message=f"HTTP 404 Not Found during {action} at {safe_url}",
                details={"status_code": status},
            )
        elif status == 429:
            return ConnectorUnavailableError(
                connector_id="mcp.http",
                message=f"HTTP 429 Too Many Requests (rate limited) during {action}",
                details={"status_code": status, "retryable": True},
            )
        elif status >= 500:
            return ConnectorProviderError(
                connector_id="mcp.http",
                message=f"HTTP {status} Server Error during {action} at {safe_url}",
                details={"status_code": status, "retryable": (status in (502, 503, 504))},
            )
        else:
            return ConnectorInvalidRequestError(
                connector_id="mcp.http",
                message=f"HTTP {status} Client Error during {action} at {safe_url}",
                details={"status_code": status},
            )

    def _map_network_error(self, exc: Exception, action: str) -> ConnectorError:
        """Map low-level network or httpx exceptions to canonical SŪTRA errors."""
        safe_url = _mask_url_credentials(self._config.url)
        if isinstance(exc, ConnectorError):
            return exc
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            return ConnectorTimeoutError(
                connector_id="mcp.http",
                message=f"Timeout during {action} connecting to {safe_url}: {exc}",
            )
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return ConnectorConnectionError(
                connector_id="mcp.http",
                message=f"Connection failed during {action} to {safe_url}: {exc}",
                cause=exc,
            )
        if isinstance(exc, httpx.TooManyRedirects):
            return ConnectorConnectionError(
                connector_id="mcp.http",
                message=f"Too many redirects during {action} to {safe_url}",
                cause=exc,
            )
        if isinstance(exc, (httpx.DecodingError, json.JSONDecodeError)):
            return ConnectorProviderError(
                connector_id="mcp.http",
                message=f"Failed to decode response from {safe_url}: {exc}",
                cause=exc,
            )
        return ConnectorProviderError(
            connector_id="mcp.http",
            message=f"HTTP transport error during {action}: {redact_secrets(str(exc))}",
            cause=exc,
        )


# Backward-compatible and canonical aliases
MCPHttpTransport = HttpMCPTransport
