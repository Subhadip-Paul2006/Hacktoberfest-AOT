"""Connector domain errors and normalized error categories.

Defines provider-neutral error models ensuring that MCP, REST, and CLI
connectors share consistent exception handling without leaking transport
or SDK-specific abstractions.
"""
from __future__ import annotations

import enum
from typing import Any, Optional


class ConnectorErrorCode(str, enum.Enum):
    """Canonical error codes for connector domain operations."""
    NOT_CONNECTED = "NOT_CONNECTED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    TIMEOUT = "TIMEOUT"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    LIFECYCLE_ERROR = "LIFECYCLE_ERROR"
    DISABLED = "DISABLED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ConnectorError(Exception):
    """Base exception for all connector domain errors.
    
    Provides standardized error codes, connector context, and conversion
    to normalized ConnectorResult payloads.
    """

    def __init__(
        self,
        connector_id: str,
        message: str,
        code: ConnectorErrorCode = ConnectorErrorCode.UNKNOWN_ERROR,
        details: Optional[dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(f"[{connector_id}] {code.value}: {message}")
        self.connector_id = connector_id
        self.message = message
        self.code = code
        self.details = details or {}
        self.cause = cause

    def to_result(self) -> Any:
        """Convert exception into a normalized ConnectorResult failure representation."""
        from .models import ConnectorResult
        return ConnectorResult.fail(
            error=self.message,
            error_code=self.code.value,
            metadata={"connector_id": self.connector_id, **self.details},
        )


class ConnectorNotConnectedError(ConnectorError):
    """Raised when an operation is attempted on a disconnected or uninitialized connector."""

    def __init__(
        self,
        connector_id: str,
        message: str = "Connector is not connected",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.NOT_CONNECTED,
            details=details,
        )


class ConnectorConnectionError(ConnectorError):
    """Raised when connection establishment to the target system fails."""

    def __init__(
        self,
        connector_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.CONNECTION_FAILED,
            details=details,
            cause=cause,
        )


class ConnectorTimeoutError(ConnectorError):
    """Raised when an operation times out at the connector boundary."""

    def __init__(
        self,
        connector_id: str,
        message: str = "Connector operation timed out",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.TIMEOUT,
            details=details,
        )


class ConnectorAuthError(ConnectorError):
    """Raised when authentication credentials for the connector provider fail."""

    def __init__(
        self,
        connector_id: str,
        message: str = "Connector authentication failed",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.AUTHENTICATION_ERROR,
            details=details,
        )


class ConnectorPermissionError(ConnectorError):
    """Raised when authorization or permission check fails for a requested operation."""

    def __init__(
        self,
        connector_id: str,
        message: str = "Operation rejected by permission policy",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.AUTHORIZATION_ERROR,
            details=details,
        )


class ConnectorInvalidRequestError(ConnectorError):
    """Raised when a request is malformed, missing parameters, or requests an unsupported capability."""

    def __init__(
        self,
        connector_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.INVALID_REQUEST,
            details=details,
        )


class ConnectorNotFoundError(ConnectorError):
    """Raised when the requested tool, resource, or prompt is not found."""

    def __init__(
        self,
        connector_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.NOT_FOUND,
            details=details,
        )


class ConnectorProviderError(ConnectorError):
    """Raised when the underlying provider returns an error."""

    def __init__(
        self,
        connector_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.PROVIDER_ERROR,
            details=details,
            cause=cause,
        )


class ConnectorUnavailableError(ConnectorError):
    """Raised when the connector or downstream provider is unavailable (circuit open, host down)."""

    def __init__(
        self,
        connector_id: str,
        message: str = "Connector provider is currently unavailable",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.UNAVAILABLE,
            details=details,
        )


class ConnectorToolError(ConnectorError):
    """Raised when execution of a tool on the connector fails."""

    def __init__(
        self,
        connector_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.TOOL_ERROR,
            details=details,
            cause=cause,
        )


class ConnectorAlreadyExistsError(ConnectorError):
    """Raised when registering a connector with an ID that already exists."""

    def __init__(
        self,
        connector_id: str,
        message: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message or f"Connector with ID '{connector_id}' is already registered",
            code=ConnectorErrorCode.ALREADY_EXISTS,
            details=details,
        )


class ConnectorLifecycleError(ConnectorError):
    """Raised when an invalid lifecycle state transition is attempted."""

    def __init__(
        self,
        connector_id: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message,
            code=ConnectorErrorCode.LIFECYCLE_ERROR,
            details=details,
        )


class ConnectorDisabledError(ConnectorError):
    """Raised when attempting an operation on an intentionally disabled connector."""

    def __init__(
        self,
        connector_id: str,
        message: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            connector_id=connector_id,
            message=message or f"Connector '{connector_id}' is disabled",
            code=ConnectorErrorCode.DISABLED,
            details=details,
        )

