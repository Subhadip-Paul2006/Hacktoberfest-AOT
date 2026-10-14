"""One-time, expiring local development pairing handshake."""
from __future__ import annotations
import hashlib
import secrets
import time
import uuid
from .state import StateStore

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

class PairingManager:
    def __init__(self, store: StateStore, ttl_seconds: int = 300):
        self.store = store; self.ttl_seconds = ttl_seconds
        self._code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        self._digest = hashlib.sha256(self._code.encode()).digest()
        self._expires_at = time.monotonic() + ttl_seconds
        self._used = False
    @property
    def display_code(self) -> str:
        return f"{self._code[:4]}-{self._code[4:]}"
    def pair(self, provided_code: str, device_name: str = "") -> str:
        candidate = provided_code.replace("-", "").strip().upper()
        digest = hashlib.sha256(candidate.encode()).digest()
        if self._used or time.monotonic() >= self._expires_at or not secrets.compare_digest(digest, self._digest):
            raise PermissionError("Pairing code is invalid, expired, or already used")
        self._used = True
        self._code = ""; self._digest = b""
        device_id = f"AB-PC-{secrets.token_hex(3).upper()}"
        token = secrets.token_urlsafe(32)
        session_id = str(uuid.uuid4())
        generation = int(self.store.get_device("session_generation") or 0) + 1
        self.store.set_device("device_id", device_id)
        self.store.set_device("device_name", device_name[:80])
        self.store.set_device("device_token", token)
        self.store.set_device("session_id", session_id)
        self.store.set_device("session_generation", str(generation))
        self.store.set_device("created_at", str(int(time.time())))
        return device_id

def new_frame_id() -> str:
    return str(uuid.uuid4())
