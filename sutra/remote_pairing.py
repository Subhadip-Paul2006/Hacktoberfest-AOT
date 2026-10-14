"""TLS-verified client flow for pairing a local runtime with the control plane."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import ssl
import sys
import time
import uuid
from urllib.parse import urlsplit

import httpx
from . import __version__

log = logging.getLogger(__name__)


class PairingSession:
    def __init__(self, session_id: str, device_id: str | None = None):
        self.session_id = session_id
        self.device_id = device_id
        self.request_id: str | None = None
        self.pairing_code: str | None = None
        self.poll_secret: str | None = None
        self.poll_secret_hash: str | None = None
        self.expires_at: float = 0.0
        self.state: str = "BOOT"
        self.token: str | None = None
        self.generation: int = 0

    @property
    def is_valid(self) -> bool:
        return self.state not in ("INVALIDATED", "EXPIRED") and time.monotonic() < self.expires_at


class RemotePairingClient:
    def __init__(self, control_plane_url: str, gateway_url: str, ca_file: str | None = None):
        if not control_plane_url:
            parts = urlsplit(gateway_url)
            if parts.scheme != "wss" or not parts.netloc:
                raise ValueError("Configure an https control-plane URL or a wss gateway URL")
            control_plane_url = f"https://{parts.netloc}"
        if not control_plane_url.startswith("https://"):
            raise ValueError("Remote pairing requires an https:// control-plane URL")
        self.base_url = control_plane_url.rstrip("/")
        self.verify: ssl.SSLContext = ssl.create_default_context(cafile=ca_file)
        self.active_session: PairingSession | None = None

    def invalidate_session(self) -> None:
        if self.active_session:
            self.active_session.state = "INVALIDATED"
            self.active_session = None
            log.info("SESSION_INVALIDATED")
            log.info("PAIRING_REQUIRED")

    async def pair(self, store: object, device_name: str = "SŪTRA PC",
                   session_id: str | None = None) -> tuple[str, str]:
        # Reuse existing valid active session if available
        session = self.active_session
        limits = httpx.Limits(max_connections=5, max_keepalive_connections=5, keepalive_expiry=30.0)
        async with httpx.AsyncClient(verify=self.verify, timeout=10, limits=limits) as client:
            if session is not None and session.is_valid and (session_id is None or session.session_id == session_id):
                log.info("Reusing active pairing session")
            else:
                sid = session_id or str(uuid.uuid4())
                existing_device_id = store.get_device("device_id")
                session = PairingSession(sid, existing_device_id)
                poll_secret = secrets.token_urlsafe(32)
                session.poll_secret = poll_secret
                session.poll_secret_hash = hashlib.sha256(poll_secret.encode()).hexdigest()
                session.state = "WAITING_FOR_PAIRING"
                payload = {
                    "device_id": existing_device_id,
                    "device_name": device_name,
                    "runtime_version": __version__,
                    "platform": sys.platform,
                    "poll_secret_hash": session.poll_secret_hash,
                    "session_id": sid,
                }
                try:
                    response = await client.post(self.base_url + "/v1/pairing/requests", json=payload)
                    response.raise_for_status()
                    request = response.json()
                    session.request_id = request["request_id"]
                    session.device_id = request["device_id"]
                    session.pairing_code = str(request["pairing_code"])
                    session.expires_at = time.monotonic() + int(request["expires_in"])
                    self.active_session = session
                    log.info("SESSION_CREATED")
                    log.info("PAIRING_CODE_ISSUED")
                    print("\nRemote pairing code: " + session.pairing_code)
                    print("Enter this code with the authorized Telegram bot. Expires in 300 seconds.")
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    log.error("Remote pairing request failed (%s)", type(exc).__name__)
                    raise RuntimeError("Remote pairing failed; check control-plane availability and TLS configuration") from None

            try:
                while time.monotonic() < session.expires_at:
                    log.info("CREDENTIAL_WAIT")
                    try:
                        response = await client.get(
                            f"{self.base_url}/v1/pairing/requests/{session.request_id}/complete",
                            headers={"Authorization": f"Bearer {session.poll_secret}"},
                        )
                    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                        log.debug("Polling transient error (%s); retrying", type(exc).__name__)
                        await asyncio.sleep(1)
                        continue

                    if response.status_code == 200:
                        result = response.json()
                        session.state = "PAIRING_APPROVED"
                        session.token = result["device_token"]
                        session.device_id = result["device_id"]
                        session.generation = int(result["generation"])
                        store.set_device("device_id", result["device_id"])
                        store.set_device("gateway_token", result["device_token"])
                        store.set_device("session_id", result["session_id"])
                        store.set_device("session_generation", str(result["generation"]))
                        store.set_device("gateway_token_received_at", str(int(time.time())))
                        log.info("PAIRING_APPROVED")
                        log.info("CREDENTIAL_RECEIVED")
                        session.state = "CREDENTIAL_READY"
                        return result["device_id"], result["device_token"]

                    if response.status_code == 202:
                        await asyncio.sleep(1)
                        continue

                    session.state = "INVALIDATED"
                    self.active_session = None
                    log.info("SESSION_INVALIDATED")
                    log.info("PAIRING_REQUIRED")
                    raise RuntimeError("Remote pairing request was rejected or expired")

            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.error("Remote pairing polling failed (%s)", type(exc).__name__)
                raise RuntimeError("Remote pairing failed; check control-plane availability and TLS configuration") from None

        session.state = "EXPIRED"
        self.active_session = None
        log.info("SESSION_INVALIDATED")
        log.info("PAIRING_REQUIRED")
        raise RuntimeError("Remote pairing code expired before approval")
