"""Canonical Connector Registry and Lifecycle Management subsystem.

The ConnectorRegistry serves as the single in-process source of truth for:
- Registered connector instances
- Connector identity and type
- Connector lifecycle state machine (REGISTERED, CONNECTING, READY, DEGRADED, DISCONNECTED, ERROR)
- Availability state (enabled / disabled) separate from connectivity
- Instance health diagnostics
- Normalized inspection descriptors for future Analyzer pipelines (Phase 7.4)
- Safe persistence of connector metadata (never secrets)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, List, Optional, Sequence, Union

from sutra.metrics import Metrics, get_metrics
from sutra.redaction import redact_secrets

from .errors import (
    ConnectorAlreadyExistsError,
    ConnectorConnectionError,
    ConnectorDisabledError,
    ConnectorError,
    ConnectorErrorCode,
    ConnectorInvalidRequestError,
    ConnectorLifecycleError,
    ConnectorNotConnectedError,
    ConnectorNotFoundError,
)
from .interface import Connector
from .models import (
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorDescriptor,
    ConnectorHealth,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
    ConnectorType,
    sanitize_metadata,
)

log = logging.getLogger(__name__)

# Valid state machine transitions
_VALID_TRANSITIONS: dict[ConnectorStatus, frozenset[ConnectorStatus]] = {
    ConnectorStatus.REGISTERED: frozenset({
        ConnectorStatus.CONNECTING,
        ConnectorStatus.DISCONNECTED,
    }),
    ConnectorStatus.CONNECTING: frozenset({
        ConnectorStatus.READY,
        ConnectorStatus.DEGRADED,
        ConnectorStatus.ERROR,
        ConnectorStatus.DISCONNECTED,
    }),
    ConnectorStatus.READY: frozenset({
        ConnectorStatus.DEGRADED,
        ConnectorStatus.DISCONNECTED,
        ConnectorStatus.ERROR,
    }),
    ConnectorStatus.DEGRADED: frozenset({
        ConnectorStatus.READY,
        ConnectorStatus.DISCONNECTED,
        ConnectorStatus.ERROR,
    }),
    ConnectorStatus.DISCONNECTED: frozenset({
        ConnectorStatus.CONNECTING,
    }),
    ConnectorStatus.ERROR: frozenset({
        ConnectorStatus.CONNECTING,
        ConnectorStatus.DISCONNECTED,
    }),
}


@dataclass
class _ConnectorEntry:
    """Internal registry container tracking a live connector instance and its lifecycle state."""
    connector: Connector
    enabled: bool = True
    status: ConnectorStatus = ConnectorStatus.REGISTERED
    registered_at: float = field(default_factory=time.time)
    last_health: Optional[ConnectorHealth] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConnectorRegistry:
    """Central registry and lifecycle manager for all SŪTRA connectors.
    
    Provides thread-safe and async-safe registration, lookup, state management,
    availability toggles, and unified diagnostic inspections.
    """

    def __init__(
        self,
        *,
        activity_store: Optional[Any] = None,
        metrics: Optional[Metrics] = None,
        db: Optional[sqlite3.Connection] = None,
        db_lock: Optional[RLock] = None,
    ) -> None:
        self._entries: dict[str, _ConnectorEntry] = {}
        self._registry_lock = asyncio.Lock()
        self._activity_store = activity_store
        self._metrics = metrics or get_metrics()
        self._db = db
        self._db_lock = db_lock or RLock()
        if self._db is not None:
            self._init_persistence_schema()

    # -----------------------------------------------------------------------
    # Persistence helpers (Stores metadata only, NEVER secrets)
    # -----------------------------------------------------------------------

    def _init_persistence_schema(self) -> None:
        """Initialize the persistent metadata table in SQLite."""
        with self._db_lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS connector_configs (
                    connector_id   TEXT PRIMARY KEY,
                    name           TEXT NOT NULL,
                    connector_type TEXT NOT NULL,
                    provider       TEXT NOT NULL,
                    enabled        INTEGER NOT NULL DEFAULT 1,
                    version        TEXT NOT NULL DEFAULT '1.0.0',
                    description    TEXT NOT NULL DEFAULT '',
                    metadata       TEXT NOT NULL DEFAULT '{}',
                    created_at     REAL NOT NULL,
                    updated_at     REAL NOT NULL
                )
            """)
            self._db.commit()

    def _persist_config(self, desc: ConnectorDescriptor) -> None:
        """Persist safe connector metadata reference to SQLite."""
        if self._db is None:
            return
        clean_meta = sanitize_metadata(desc.metadata)
        try:
            now = time.time()
            meta_json = json.dumps(clean_meta, sort_keys=True)
            with self._db_lock:
                self._db.execute(
                    """INSERT OR REPLACE INTO connector_configs
                    (connector_id, name, connector_type, provider, enabled, version, description, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        desc.connector_id,
                        desc.name,
                        desc.type.value if hasattr(desc.type, "value") else str(desc.type),
                        desc.provider,
                        1 if desc.enabled else 0,
                        desc.version,
                        desc.description,
                        meta_json,
                        now,
                        now,
                    ),
                )
                self._db.commit()
        except Exception as exc:
            log.warning("Failed to persist connector config for '%s': %s", desc.connector_id, exc)

    def _delete_persisted_config(self, connector_id: str) -> None:
        """Remove connector metadata reference from SQLite."""
        if self._db is None:
            return
        try:
            with self._db_lock:
                self._db.execute(
                    "DELETE FROM connector_configs WHERE connector_id = ?",
                    (connector_id,),
                )
                self._db.commit()
        except Exception as exc:
            log.warning("Failed to delete persisted config for '%s': %s", connector_id, exc)

    def load_persisted_configs(self) -> list[dict[str, Any]]:
        """Retrieve stored connector configuration references."""
        if self._db is None:
            return []
        try:
            with self._db_lock:
                cur = self._db.execute(
                    "SELECT connector_id, name, connector_type, provider, enabled, version, description, metadata, created_at, updated_at FROM connector_configs ORDER BY connector_id"
                )
                rows = cur.fetchall()
            configs = []
            for r in rows:
                meta = {}
                try:
                    meta = json.loads(r[7])
                except Exception:
                    pass
                configs.append({
                    "connector_id": r[0],
                    "name": r[1],
                    "connector_type": r[2],
                    "provider": r[3],
                    "enabled": bool(r[4]),
                    "version": r[5],
                    "description": r[6],
                    "metadata": meta,
                    "created_at": r[8],
                    "updated_at": r[9],
                })
            return configs
        except Exception as exc:
            log.warning("Failed to load persisted connector configs: %s", exc)
            return []

    # -----------------------------------------------------------------------
    # Activity Store & Metrics Recording
    # -----------------------------------------------------------------------

    def _record_event(
        self,
        connector_id: str,
        event_type: str,
        status: ConnectorStatus,
        request_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record lifecycle event in ActivityStore if available."""
        if self._activity_store is not None and hasattr(self._activity_store, "record_connector_event"):
            try:
                self._activity_store.record_connector_event(
                    connector_id=connector_id,
                    event_type=event_type,
                    status=status.value,
                    request_id=request_id,
                    metadata=sanitize_metadata(metadata),
                )
            except Exception as exc:
                log.warning("Failed to record connector event for '%s': %s", connector_id, exc)

    # -----------------------------------------------------------------------
    # Registry Management API
    # -----------------------------------------------------------------------

    async def register(
        self,
        connector: Connector,
        *,
        enabled: bool = True,
        request_id: Optional[str] = None,
    ) -> ConnectorDescriptor:
        """Register a new connector instance in the registry.
        
        Rejects duplicate IDs deterministically.
        """
        if not isinstance(connector, Connector):
            raise TypeError(f"Expected Connector instance, got {type(connector).__name__}")

        connector_id = connector.connector_id
        if not connector_id or not connector_id.strip():
            raise ConnectorInvalidRequestError(
                connector_id="",
                message="Cannot register connector with empty connector_id",
            )

        async with self._registry_lock:
            if connector_id in self._entries:
                raise ConnectorAlreadyExistsError(
                    connector_id=connector_id,
                    message=f"Connector '{connector_id}' is already registered in registry",
                )

            # Determine initial status from identity, defaulting to REGISTERED
            init_status = connector.status or ConnectorStatus.REGISTERED
            entry = _ConnectorEntry(
                connector=connector,
                enabled=enabled,
                status=init_status,
                registered_at=time.time(),
            )
            self._entries[connector_id] = entry

        # Build current descriptor
        desc = self._build_descriptor(entry)
        self._persist_config(desc)
        self._metrics.record("connector_registered", 1)
        self._record_event(
            connector_id=connector_id,
            event_type="registered",
            status=entry.status,
            request_id=request_id,
            metadata={"name": desc.name, "type": desc.type.value, "enabled": enabled},
        )
        log.info("Registered connector '%s' (%s, enabled=%s)", connector_id, desc.type.value, enabled)
        return desc

    async def unregister(
        self,
        connector_id: str,
        *,
        request_id: Optional[str] = None,
    ) -> ConnectorDescriptor:
        """Remove a connector from the registry.
        
        If the connector is currently connected, it is gracefully disconnected first.
        """
        async with self._registry_lock:
            entry = self._entries.get(connector_id)
            if entry is None:
                raise ConnectorNotFoundError(
                    connector_id=connector_id,
                    message=f"Cannot unregister: connector '{connector_id}' not found",
                )
            del self._entries[connector_id]

        # Acquire per-connector lock to clean up
        async with entry.lock:
            if entry.status in (ConnectorStatus.READY, ConnectorStatus.CONNECTING, ConnectorStatus.DEGRADED):
                try:
                    await entry.connector.disconnect()
                except Exception as exc:
                    log.warning("Error during graceful disconnect while unregistering '%s': %s", connector_id, exc)
                entry.status = ConnectorStatus.DISCONNECTED

        desc = self._build_descriptor(entry)
        self._delete_persisted_config(connector_id)
        self._metrics.record("connector_unregistered", 1)
        self._record_event(
            connector_id=connector_id,
            event_type="unregistered",
            status=ConnectorStatus.DISCONNECTED,
            request_id=request_id,
        )
        log.info("Unregistered connector '%s'", connector_id)
        return desc

    def exists(self, connector_id: str) -> bool:
        """Check whether a connector is registered."""
        return connector_id in self._entries

    def get(self, connector_id: str) -> Optional[Connector]:
        """Get the live Connector instance by ID, or None if not found."""
        entry = self._entries.get(connector_id)
        return entry.connector if entry is not None else None

    def require(self, connector_id: str) -> Connector:
        """Get the live Connector instance or raise ConnectorNotFoundError."""
        conn = self.get(connector_id)
        if conn is None:
            raise ConnectorNotFoundError(
                connector_id=connector_id,
                message=f"Connector '{connector_id}' not found in registry",
            )
        return conn

    def list(
        self,
        *,
        status: Optional[ConnectorStatus] = None,
        enabled_only: bool = False,
    ) -> list[ConnectorDescriptor]:
        """List descriptors for registered connectors with optional filtering."""
        results: list[ConnectorDescriptor] = []
        for entry in self._entries.values():
            if enabled_only and not entry.enabled:
                continue
            if status is not None and entry.status != status:
                continue
            results.append(self._build_descriptor(entry))
        return results

    def is_enabled(self, connector_id: str) -> bool:
        """Check whether the given connector is enabled."""
        entry = self._get_entry(connector_id)
        return entry.enabled

    async def enable(
        self,
        connector_id: str,
        *,
        request_id: Optional[str] = None,
    ) -> ConnectorDescriptor:
        """Enable an intentionally disabled connector."""
        entry = self._get_entry(connector_id)
        async with entry.lock:
            entry.enabled = True
            desc = self._build_descriptor(entry)

        self._persist_config(desc)
        self._metrics.record("connector_enable", 1)
        self._record_event(
            connector_id=connector_id,
            event_type="enabled",
            status=entry.status,
            request_id=request_id,
        )
        log.info("Enabled connector '%s'", connector_id)
        return desc

    async def disable(
        self,
        connector_id: str,
        *,
        request_id: Optional[str] = None,
    ) -> ConnectorDescriptor:
        """Disable a connector. If currently active, gracefully disconnects it."""
        entry = self._get_entry(connector_id)
        async with entry.lock:
            entry.enabled = False
            # If active, gracefully disconnect
            if entry.status in (ConnectorStatus.READY, ConnectorStatus.CONNECTING, ConnectorStatus.DEGRADED):
                try:
                    await entry.connector.disconnect()
                except Exception as exc:
                    log.warning("Error disconnecting connector '%s' during disable: %s", connector_id, exc)
                entry.status = ConnectorStatus.DISCONNECTED

            desc = self._build_descriptor(entry)

        self._persist_config(desc)
        self._metrics.record("connector_disable", 1)
        self._record_event(
            connector_id=connector_id,
            event_type="disabled",
            status=entry.status,
            request_id=request_id,
        )
        log.info("Disabled connector '%s'", connector_id)
        return desc

    def status(self, connector_id: str) -> ConnectorStatus:
        """Get the current lifecycle status of a connector."""
        entry = self._get_entry(connector_id)
        return entry.status

    def descriptor(self, connector_id: str) -> ConnectorDescriptor:
        """Get the full up-to-date descriptor for a connector."""
        entry = self._get_entry(connector_id)
        return self._build_descriptor(entry)

    # -----------------------------------------------------------------------
    # Lifecycle Management (State Machine)
    # -----------------------------------------------------------------------

    def _validate_transition(
        self,
        connector_id: str,
        current: ConnectorStatus,
        target: ConnectorStatus,
    ) -> None:
        """Enforce strict lifecycle state machine transitions."""
        if current == target:
            return
        allowed = _VALID_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ConnectorLifecycleError(
                connector_id=connector_id,
                message=f"Illegal lifecycle transition for '{connector_id}': cannot move from {current.value} to {target.value}",
                details={"current_status": current.value, "target_status": target.value, "allowed_transitions": [s.value for s in allowed]},
            )

    async def connect(
        self,
        connector_id: str,
        *,
        request_id: Optional[str] = None,
    ) -> ConnectorHealth:
        """Transition connector to CONNECTING and execute connection handshake."""
        entry = self._get_entry(connector_id)

        async with entry.lock:
            if not entry.enabled:
                raise ConnectorDisabledError(
                    connector_id=connector_id,
                    message=f"Cannot connect: connector '{connector_id}' is disabled",
                )

            # If already READY, perform a health check and return
            if entry.status == ConnectorStatus.READY:
                return await self._check_health_internal(entry)

            self._validate_transition(connector_id, entry.status, ConnectorStatus.CONNECTING)
            entry.status = ConnectorStatus.CONNECTING
            self._record_event(
                connector_id=connector_id,
                event_type="connect_started",
                status=ConnectorStatus.CONNECTING,
                request_id=request_id,
            )

            try:
                await entry.connector.connect()
                entry.status = ConnectorStatus.READY
                self._metrics.record("connector_connect_success", 1)
                self._record_event(
                    connector_id=connector_id,
                    event_type="connected",
                    status=ConnectorStatus.READY,
                    request_id=request_id,
                )
                log.info("Successfully connected connector '%s'", connector_id)
            except Exception as exc:
                entry.status = ConnectorStatus.ERROR
                self._metrics.record("connector_connect_error", 1)
                self._record_event(
                    connector_id=connector_id,
                    event_type="error",
                    status=ConnectorStatus.ERROR,
                    request_id=request_id,
                    metadata={"error": str(exc)},
                )
                log.error("Failed to connect connector '%s': %s", connector_id, exc)
                if isinstance(exc, ConnectorError):
                    raise
                raise ConnectorConnectionError(
                    connector_id=connector_id,
                    message=f"Connection failed for '{connector_id}': {exc}",
                    cause=exc,
                ) from exc

            return await self._check_health_internal(entry)

    async def disconnect(
        self,
        connector_id: str,
        *,
        request_id: Optional[str] = None,
    ) -> None:
        """Gracefully disconnect a connector and transition to DISCONNECTED."""
        entry = self._get_entry(connector_id)

        async with entry.lock:
            if entry.status == ConnectorStatus.DISCONNECTED:
                return

            self._validate_transition(connector_id, entry.status, ConnectorStatus.DISCONNECTED)
            try:
                await entry.connector.disconnect()
            except Exception as exc:
                log.warning("Exception during disconnect of '%s': %s", connector_id, exc)
            finally:
                entry.status = ConnectorStatus.DISCONNECTED
                self._metrics.record("connector_disconnect", 1)
                self._record_event(
                    connector_id=connector_id,
                    event_type="disconnected",
                    status=ConnectorStatus.DISCONNECTED,
                    request_id=request_id,
                )
                log.info("Disconnected connector '%s'", connector_id)

    # -----------------------------------------------------------------------
    # Health Diagnostics
    # -----------------------------------------------------------------------

    async def _check_health_internal(self, entry: _ConnectorEntry) -> ConnectorHealth:
        """Invoke health check on connector with complete error isolation."""
        self._metrics.record("connector_health_check", 1)
        try:
            health = await entry.connector.health()
            entry.last_health = health
            # Synchronize status if connector self-reports degraded or error
            if not health.healthy:
                if health.status in (ConnectorStatus.DEGRADED, ConnectorStatus.ERROR):
                    entry.status = health.status
                elif entry.status == ConnectorStatus.READY:
                    entry.status = ConnectorStatus.DEGRADED
            return health
        except Exception as exc:
            log.warning("Health check failed for connector '%s': %s", entry.connector.connector_id, exc)
            entry.status = ConnectorStatus.ERROR
            error_health = ConnectorHealth(
                status=ConnectorStatus.ERROR,
                healthy=False,
                message=redact_secrets(f"Health check failed: {exc}"),
            )
            entry.last_health = error_health
            return error_health

    async def health(self, connector_id: str) -> ConnectorHealth:
        """Perform health check on a specific connector with error isolation."""
        entry = self._get_entry(connector_id)
        async with entry.lock:
            return await self._check_health_internal(entry)

    async def health_all(self) -> dict[str, ConnectorHealth]:
        """Check health of all registered connectors concurrently with fault isolation."""
        connector_ids = list(self._entries.keys())
        tasks = [self.health(cid) for cid in connector_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        health_map: dict[str, ConnectorHealth] = {}
        for cid, res in zip(connector_ids, results):
            if isinstance(res, Exception):
                health_map[cid] = ConnectorHealth(
                    status=ConnectorStatus.ERROR,
                    healthy=False,
                    message=f"Health diagnostic error: {res}",
                )
            else:
                health_map[cid] = res
        return health_map

    # -----------------------------------------------------------------------
    # Execution Dispatch Boundary
    # -----------------------------------------------------------------------

    async def execute(self, request: ConnectorRequest) -> ConnectorResult:
        """Dispatch execution to the registered connector instance.
        
        Enforces enable/disable and connection prerequisites.
        """
        entry = self._get_entry(request.connector_id)

        if not entry.enabled:
            raise ConnectorDisabledError(
                connector_id=request.connector_id,
                message=f"Cannot execute operation on disabled connector '{request.connector_id}'",
            )

        if entry.status not in (ConnectorStatus.READY, ConnectorStatus.DEGRADED):
            raise ConnectorNotConnectedError(
                connector_id=request.connector_id,
                message=f"Cannot execute operation: connector '{request.connector_id}' is in {entry.status.value} state (must be READY)",
            )

        return await entry.connector.execute(request)

    # -----------------------------------------------------------------------
    # Future Analyzer Contract (Phase 7.4 Extension Point)
    # -----------------------------------------------------------------------

    def get_analysis_descriptor(self, connector_id: str) -> ConnectorAnalysisDescriptor:
        """Return standardized descriptor for the Phase 7.4 Generic Environment & Connector Analyzer.
        
        Provides configuration, capability, client, and transport declarations
        in a provider-neutral structure, enabling a single verification pipeline.
        """
        entry = self._get_entry(connector_id)
        desc = self._build_descriptor(entry)

        # Extract transport hint from metadata if declared
        transport = desc.metadata.get("transport", "custom")
        declared_env = tuple(desc.metadata.get("declared_env_vars", ()))
        declared_bin = tuple(desc.metadata.get("declared_binary_names", ()))

        return ConnectorAnalysisDescriptor(
            connector_id=desc.connector_id,
            name=desc.name,
            type=desc.type,
            provider=desc.provider,
            transport=transport,
            enabled=entry.enabled,
            status=entry.status,
            capabilities=desc.capabilities,
            version=desc.version,
            description=desc.description,
            declared_env_vars=declared_env,
            declared_binary_names=declared_bin,
            metadata=dict(desc.metadata),
            health=entry.last_health,
        )

    # -----------------------------------------------------------------------
    # Internal Helpers
    # -----------------------------------------------------------------------

    def _get_entry(self, connector_id: str) -> _ConnectorEntry:
        """Retrieve entry or raise ConnectorNotFoundError."""
        entry = self._entries.get(connector_id)
        if entry is None:
            raise ConnectorNotFoundError(
                connector_id=connector_id,
                message=f"Connector '{connector_id}' not found in registry",
            )
        return entry

    def _build_descriptor(self, entry: _ConnectorEntry) -> ConnectorDescriptor:
        """Construct up-to-date descriptor from live entry."""
        base = entry.connector.identity()
        # Merge live status, enabled state, and provider
        provider = base.provider or base.metadata.get("provider", "")
        return ConnectorDescriptor(
            connector_id=base.connector_id,
            name=base.name,
            type=base.type,
            version=base.version,
            description=base.description,
            provider=provider,
            enabled=entry.enabled,
            capabilities=base.capabilities,
            status=entry.status,
            metadata=dict(base.metadata),
        )




def create_permission_request(
    operation: str,
    connector_id: str,
    *,
    request_id: str,
    agent_id: str = "agent-system",
    session_id: str = "session-system",
    risk_level: str = "HIGH",
    description: str = "",
) -> Any:
    """Helper to build a PermissionRequest for registry operations without tight coupling."""
    from sutra.permissions import PermissionRequest, PermissionRiskLevel

    risk_enum = PermissionRiskLevel(risk_level) if hasattr(PermissionRiskLevel, risk_level) else PermissionRiskLevel.HIGH
    return PermissionRequest(
        request_id=request_id,
        action_type="connector_admin",
        resource_path=connector_id,
        command=f"connector.{operation}",
        agent_id=agent_id,
        session_id=session_id,
        risk_level=risk_enum,
        description=description or f"Connector registry administrative operation: {operation} on '{connector_id}'",
    )
