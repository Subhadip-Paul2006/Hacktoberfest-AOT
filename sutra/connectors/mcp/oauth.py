"""Reusable Model Context Protocol (MCP) OAuth 2.0 & PKCE authorization module.

Provides a provider-neutral OAuth 2.0 client supporting:
- Proof Key for Code Exchange (PKCE, RFC 7636) with SHA-256 code challenge
- Cryptographic state parameter generation and validation to prevent CSRF
- Local loopback HTTP server callback for interactive CLI authentication
- Token exchange (authorization_code -> OAuthToken)
- Automatic token refresh (refresh_token -> OAuthToken) with expiry calculation
- Safe serialization and zero secret leakage in string representations and logs.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, Optional, Tuple

import httpx

from sutra.redaction import redact_secrets

log = logging.getLogger(__name__)


# ===========================================================================
# Exceptions
# ===========================================================================

class OAuthError(Exception):
    """Base exception for OAuth authorization operations."""


class OAuthCancelledError(OAuthError):
    """Raised when user cancels or denies authorization."""


class OAuthStateMismatchError(OAuthError):
    """Raised when callback state does not match expected cryptographic state."""


class OAuthTimeoutError(OAuthError):
    """Raised when waiting for browser callback exceeds timeout limit."""


class OAuthTokenExchangeError(OAuthError):
    """Raised when exchanging authorization code for access token fails."""


class OAuthTokenRefreshError(OAuthError):
    """Raised when refreshing an expired OAuth token fails."""


# ===========================================================================
# PKCE Utilities (RFC 7636)
# ===========================================================================

def generate_code_verifier(length: int = 64) -> str:
    """Generate a cryptographically secure random PKCE code verifier (RFC 7636)."""
    # Allowed characters: unreserved characters [A-Z], [a-z], [0-9], "-", ".", "_", "~"
    return secrets.token_urlsafe(length)[:length]


def generate_code_challenge(code_verifier: str) -> str:
    """Compute S256 code challenge from code verifier (RFC 7636)."""
    sha256_hash = hashlib.sha256(code_verifier.encode("ascii")).digest()
    # Base64url encode without padding
    return base64.urlsafe_b64encode(sha256_hash).decode("ascii").rstrip("=")


def generate_pkce_pair(length: int = 64) -> Tuple[str, str]:
    """Generate (code_verifier, code_challenge) tuple using S256 method."""
    verifier = generate_code_verifier(length)
    challenge = generate_code_challenge(verifier)
    return verifier, challenge


def generate_oauth_state(length: int = 32) -> str:
    """Generate a cryptographically secure random state parameter."""
    return secrets.token_urlsafe(length)


# ===========================================================================
# Token Model
# ===========================================================================

@dataclass
class OAuthToken:
    """Represents an OAuth 2.0 token payload with expiry and refresh metadata."""
    access_token: str
    token_type: str = "Bearer"
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    scope: Optional[str] = None
    raw_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def is_expired(self, buffer_seconds: float = 60.0) -> bool:
        """Check if access token is expired or within buffer_seconds of expiring."""
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - buffer_seconds)

    def to_json(self) -> str:
        """Serialize token securely to JSON string for encrypted/keyring storage."""
        data = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, json_str: str) -> OAuthToken:
        """Deserialize token from JSON string."""
        data = json.loads(json_str)
        return cls(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            scope=data.get("scope"),
        )

    @classmethod
    def from_token_response(cls, payload: dict[str, Any]) -> OAuthToken:
        """Create OAuthToken from OAuth token endpoint response."""
        access_token = payload.get("access_token")
        if not access_token:
            raise OAuthTokenExchangeError("Missing 'access_token' in token response")

        token_type = payload.get("token_type", "Bearer")
        refresh_token = payload.get("refresh_token")
        scope = payload.get("scope")

        expires_in = payload.get("expires_in")
        expires_at: Optional[float] = None
        if expires_in is not None:
            try:
                expires_at = time.time() + float(expires_in)
            except (ValueError, TypeError):
                expires_at = None

        return cls(
            access_token=str(access_token),
            token_type=str(token_type),
            refresh_token=str(refresh_token) if refresh_token else None,
            expires_at=expires_at,
            scope=str(scope) if scope else None,
            raw_payload={k: v for k, v in payload.items() if k not in ("access_token", "refresh_token")},
        )

    def __repr__(self) -> str:
        tok_preview = f"{self.access_token[:4]}...{self.access_token[-3:]}" if len(self.access_token) >= 8 else "***"
        has_refresh = bool(self.refresh_token)
        return f"<OAuthToken access_token='{tok_preview}' token_type='{self.token_type}' has_refresh={has_refresh} expires_at={self.expires_at}>"

    def __str__(self) -> str:
        return self.__repr__()


# ===========================================================================
# OAuth Flow Configuration
# ===========================================================================

@dataclass(frozen=True)
class OAuthFlowConfig:
    """Configuration for an interactive OAuth 2.0 PKCE flow."""
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    client_secret: Optional[str] = None
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    scopes: tuple[str, ...] = ()
    loopback_host: str = "127.0.0.1"
    loopback_port: int = 8765
    timeout_seconds: float = 120.0


# ===========================================================================
# Local Loopback Callback Server
# ===========================================================================

class _LoopbackRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler capturing the OAuth authorization code and state from redirect."""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress standard logging to prevent leaking query params
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        # Store parameters in server instance
        server: _OAuthHTTPServer = self.server  # type: ignore[assignment]
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        error = query.get("error", [None])[0]
        error_desc = query.get("error_description", [None])[0]

        server.received_code = code
        server.received_state = state
        server.received_error = error
        server.received_error_description = error_desc

        if error or not code:
            err_msg = error_desc or error or "Authorization was denied or failed."
            body = (
                "<!DOCTYPE html><html><body style='font-family: sans-serif; text-align: center; padding-top: 50px;'>"
                "<h1 style='color: #e53e3e;'>Authentication Failed</h1>"
                f"<p>{redact_secrets(err_msg)}</p>"
                "<p>You can close this tab and return to SŪTRA.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(400)
        else:
            body = (
                "<!DOCTYPE html><html><body style='font-family: sans-serif; text-align: center; padding-top: 50px;'>"
                "<h1 style='color: #38a169;'>Authentication Successful</h1>"
                "<p>SŪTRA has successfully received your authorization.</p>"
                "<p>You may now close this tab and return to your terminal.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

        # Signal completion
        if server.callback_event:
            server.callback_event.set()


class _OAuthHTTPServer(HTTPServer):
    """HTTPServer subclass carrying OAuth state attributes."""
    received_code: Optional[str] = None
    received_state: Optional[str] = None
    received_error: Optional[str] = None
    received_error_description: Optional[str] = None
    callback_event: Optional[asyncio.Event] = None


class OAuthLoopbackServer:
    """Manages the lifecycle of the local loopback HTTP server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._server: Optional[_OAuthHTTPServer] = None
        self._thread: Optional[Thread] = None

    def start(self, callback_event: asyncio.Event) -> None:
        """Start the loopback server in a background thread."""
        try:
            self._server = _OAuthHTTPServer((self.host, self.port), _LoopbackRequestHandler)
        except OSError as exc:
            # If default port is in use, try binding to port 0 (dynamic port)
            log.warning("Port %d in use (%s), falling back to dynamic port", self.port, exc)
            self._server = _OAuthHTTPServer((self.host, 0), _LoopbackRequestHandler)
            self.port = self._server.server_address[1]

        self._server.callback_event = callback_event
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.debug("OAuth loopback server listening on http://%s:%d", self.host, self.port)

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/callback"

    def shutdown(self) -> None:
        """Shutdown and cleanup the server."""
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                log.debug("Error shutting down loopback server: %s", exc)
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    @property
    def received_code(self) -> Optional[str]:
        return self._server.received_code if self._server else None

    @property
    def received_state(self) -> Optional[str]:
        return self._server.received_state if self._server else None

    @property
    def received_error(self) -> Optional[str]:
        return self._server.received_error if self._server else None

    @property
    def received_error_description(self) -> Optional[str]:
        return self._server.received_error_description if self._server else None


# ===========================================================================
# MCP OAuth Client Core
# ===========================================================================

class MCPOAuthClient:
    """Generic, reusable OAuth 2.0 PKCE client for remote MCP servers."""

    def __init__(self, config: OAuthFlowConfig) -> None:
        self.config = config

    def build_authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: Optional[str] = None,
        extra_params: Optional[dict[str, str]] = None,
    ) -> str:
        """Construct the full authorization URL with PKCE and state query parameters."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri or self.config.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if self.config.scopes:
            params["scope"] = " ".join(self.config.scopes)
        if extra_params:
            params.update(extra_params)

        parsed = urllib.parse.urlsplit(self.config.authorization_endpoint)
        existing_query = urllib.parse.parse_qs(parsed.query)
        # Merge params
        combined = {**{k: v[0] for k, v in existing_query.items()}, **params}
        new_query = urllib.parse.urlencode(combined)

        return urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            new_query,
            parsed.fragment,
        ))

    async def authenticate_interactive(
        self,
        *,
        open_browser: bool = True,
        extra_auth_params: Optional[dict[str, str]] = None,
        browser_opener: Optional[Any] = None,
    ) -> OAuthToken:
        """Execute the interactive browser authorization flow.
        
        1. Spawn loopback server on 127.0.0.1.
        2. Generate PKCE pair and cryptographic state.
        3. Open system browser to authorization endpoint (or invoke browser_opener).
        4. Wait for loopback callback with timeout.
        5. Verify state matches to prevent CSRF.
        6. Exchange code for OAuthToken.
        """
        loopback = OAuthLoopbackServer(
            host=self.config.loopback_host,
            port=self.config.loopback_port,
        )
        callback_event = asyncio.Event()
        loopback.start(callback_event)

        code_verifier, code_challenge = generate_pkce_pair()
        state = generate_oauth_state()

        auth_url = self.build_authorization_url(
            state=state,
            code_challenge=code_challenge,
            redirect_uri=loopback.redirect_uri,
            extra_params=extra_auth_params,
        )

        try:
            if browser_opener is not None:
                res = browser_opener(auth_url)
                if asyncio.iscoroutine(res):
                    await res
            elif open_browser:
                import webbrowser
                log.info("Opening browser for OAuth authorization: %s", auth_url)
                opened = webbrowser.open(auth_url)
                if not opened:
                    log.warning("Could not automatically launch browser; manual navigation required.")

            # Wait for callback event with timeout
            try:
                await asyncio.wait_for(callback_event.wait(), timeout=self.config.timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise OAuthTimeoutError(
                    f"OAuth authorization timed out after {self.config.timeout_seconds:.0f} seconds."
                ) from exc

            # Check for error or denial
            if loopback.received_error:
                err_desc = loopback.received_error_description or loopback.received_error
                if loopback.received_error in ("access_denied", "user_denied", "cancelled"):
                    raise OAuthCancelledError(f"OAuth authorization was cancelled or denied: {err_desc}")
                raise OAuthError(f"OAuth authorization error: {err_desc}")

            # Verify cryptographic state
            if loopback.received_state != state:
                raise OAuthStateMismatchError("OAuth state mismatch: possible CSRF attempt or invalid state.")

            code = loopback.received_code
            if not code:
                raise OAuthTokenExchangeError("No authorization code received in callback.")

            # Exchange code for token
            return await self.exchange_code(
                code=code,
                code_verifier=code_verifier,
                redirect_uri=loopback.redirect_uri,
            )
        finally:
            loopback.shutdown()

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: Optional[str] = None,
    ) -> OAuthToken:
        """Exchange an authorization code and PKCE code_verifier for an OAuthToken."""
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "code": code,
            "redirect_uri": redirect_uri or self.config.redirect_uri,
            "code_verifier": code_verifier,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    self.config.token_endpoint,
                    data=data,
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:
                raise OAuthTokenExchangeError(f"Network error during OAuth token exchange: {exc}") from exc

            if resp.status_code != 200:
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("error_description") or err_json.get("error") or resp.text
                except Exception:
                    err_msg = resp.text
                raise OAuthTokenExchangeError(
                    f"OAuth token exchange failed (HTTP {resp.status_code}): {redact_secrets(str(err_msg))}"
                )

            try:
                payload = resp.json()
            except Exception as exc:
                raise OAuthTokenExchangeError(f"Invalid JSON in token response: {exc}") from exc

            return OAuthToken.from_token_response(payload)

    async def refresh_token(self, refresh_token: str) -> OAuthToken:
        """Exchange a refresh token for a fresh OAuthToken."""
        clean_refresh = refresh_token.strip()
        if not clean_refresh:
            raise OAuthTokenRefreshError("Refresh token cannot be empty.")

        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "client_id": self.config.client_id,
            "refresh_token": clean_refresh,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    self.config.token_endpoint,
                    data=data,
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:
                raise OAuthTokenRefreshError(f"Network error during OAuth token refresh: {exc}") from exc

            if resp.status_code != 200:
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("error_description") or err_json.get("error") or resp.text
                except Exception:
                    err_msg = resp.text
                raise OAuthTokenRefreshError(
                    f"OAuth token refresh failed (HTTP {resp.status_code}): {redact_secrets(str(err_msg))}"
                )

            try:
                payload = resp.json()
            except Exception as exc:
                raise OAuthTokenRefreshError(f"Invalid JSON in refresh response: {exc}") from exc

            # If the response does not include a new refresh_token, reuse existing
            if not payload.get("refresh_token"):
                payload["refresh_token"] = clean_refresh

            return OAuthToken.from_token_response(payload)
