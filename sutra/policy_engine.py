"""Layered permission policy evaluation engine.

Implements the deterministic precedence chain:

    CRITICAL SAFETY RULE          (immutable, cannot be overridden)
    ↓
    SESSION GRANT                 (temporary, expires with session)
    ↓
    PROJECT / WORKSPACE POLICY    (persistent, scoped to a workspace path)
    ↓
    GLOBAL POLICY                 (persistent, applies to all workspaces)
    ↓
    DEFAULT POLICY                (built-in risk-level defaults)

Key guarantees
--------------
* CRITICAL operations can NEVER be set to AUTO_ALLOW by any persistent policy.
* ALLOW_SESSION grants are temporary: they expire when ``clear_session()`` is called.
* ALLOW_SESSION grants do NOT automatically create persistent policies.
* All policy mutations emit audit records via PolicyStore.
* When no PolicyStore is attached, behavior is identical to the previous in-memory
  PermissionEngine (full backward compatibility).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
    PermissionRiskLevel,
)
from .policy_store import PolicyDefaultDecision, PolicyScope, PolicyStore

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class PolicyEngine:
    """Wraps PermissionEngine with persistent policy lookup.

    Usage::

        store  = PolicyStore.from_path(db_path)
        engine = PolicyEngine(store)

        allowed, reason = engine.check_permission(request)
    """

    def __init__(self, policy_store: PolicyStore | None = None) -> None:
        self._base = PermissionEngine()            # in-memory session grants
        self._store = policy_store                 # may be None (legacy mode)

    # ------------------------------------------------------------------
    # Public API mirroring PermissionEngine
    # ------------------------------------------------------------------

    def assess_risk(
        self,
        action_type: str,
        command: str | None = None,
        resource_path: str | None = None,
    ) -> PermissionRiskLevel:
        return self._base.assess_risk(action_type, command, resource_path)

    def check_permission(
        self,
        request: PermissionRequest,
        *,
        workspace_path: str | None = None,
    ) -> tuple[bool, str]:
        """Evaluate the request through the full precedence chain.

        Parameters
        ----------
        request:
            The permission request to evaluate.
        workspace_path:
            Canonical workspace/project path for policy scoping.  When
            provided, workspace policies are consulted before global ones.

        Returns
        -------
        (allowed, reason)
        """
        # 1. Let base engine classify risk
        request.risk_level = self._base.assess_risk(
            request.action_type, request.command, request.resource_path
        )

        # 2. CRITICAL SAFETY RULE — immutable
        if request.risk_level == PermissionRiskLevel.CRITICAL:
            # Only a single-use ALLOW_ONCE grant recorded against this exact
            # request_id can unlock a CRITICAL op (set by apply_decision).
            grant_key = (request.session_id, request.action_type, request.request_id)
            if self._base._session_grants.get(grant_key) == PermissionDecision.ALLOW_ONCE:
                self._base._session_grants.pop(grant_key, None)
                return True, "Explicit single-use approval for CRITICAL operation."
            self._base._pending_requests[request.request_id] = request
            return (
                False,
                f"CRITICAL operation ({request.command or request.action_type}) "
                "requires explicit authorization — persistent policies cannot override this.",
            )

        # 3. SESSION GRANT check (temporary, in-memory)
        resource_key = request.resource_path or request.command or "*"
        session_key = (request.session_id, request.action_type, resource_key)
        wildcard_session_key = (request.session_id, request.action_type, "*")
        once_key = (request.session_id, request.action_type, request.request_id)

        if self._base._session_grants.get(session_key) == PermissionDecision.ALLOW_SESSION:
            return True, "Approved by session grant."
        if self._base._session_grants.get(wildcard_session_key) == PermissionDecision.ALLOW_SESSION:
            return True, "Approved by session wildcard grant."
        if self._base._session_grants.get(once_key) == PermissionDecision.ALLOW_ONCE:
            self._base._session_grants.pop(once_key, None)
            return True, "Approved once."

        # 4. PERSISTENT POLICY lookup (project → global)
        if self._store is not None:
            persistent = self._store.resolve_policy(
                action_type=request.action_type,
                risk_level=request.risk_level.value,
                workspace_path=workspace_path,
            )
            if persistent == PolicyDefaultDecision.AUTO_ALLOW:
                # Double-check: AUTO_ALLOW is only permissible for LOW/MEDIUM
                if request.risk_level in (
                    PermissionRiskLevel.LOW, PermissionRiskLevel.MEDIUM
                ):
                    return True, f"Auto-approved by persistent {request.risk_level.value} policy."
                # Policy data is corrupt or was somehow set incorrectly for HIGH
                log.warning(
                    "Ignoring AUTO_ALLOW policy for HIGH risk action '%s' — "
                    "defaulting to ASK",
                    request.action_type,
                )
            elif persistent == PolicyDefaultDecision.DENY:
                self._base._pending_requests.pop(request.request_id, None)
                return False, f"Denied by persistent policy for {request.action_type}."
            elif persistent == PolicyDefaultDecision.ASK:
                # Fall through to pending/ASK
                pass

        # 5. DEFAULT POLICY (built-in fallback)
        if request.risk_level == PermissionRiskLevel.LOW:
            return True, "Auto-approved LOW risk operation."

        # MEDIUM / HIGH — requires authorization
        self._base._pending_requests[request.request_id] = request
        return (
            False,
            f"{request.risk_level.value} risk action requires authorization.",
        )

    def apply_decision(
        self,
        request_id: str,
        decision: PermissionDecision,
        modified_command: str | None = None,
    ) -> bool:
        """Apply human decision (from Telegram callback).
        Delegates entirely to the base PermissionEngine.
        Session grants do NOT become persistent policies automatically.
        """
        return self._base.apply_decision(request_id, decision, modified_command)

    def clear_session(self, session_id: str) -> None:
        """Expire all temporary session grants for the given session."""
        self._base.clear_session(session_id)

    # ------------------------------------------------------------------
    # Policy management API (thin wrappers exposing PolicyStore ops)
    # ------------------------------------------------------------------

    @property
    def store(self) -> PolicyStore | None:
        return self._store

    def set_policy(
        self,
        scope_type: PolicyScope,
        scope_id: str,
        action_type: str,
        risk_level: str,
        decision: PolicyDefaultDecision,
        *,
        description: str = "",
        actor: str = "system",
    ):
        """Create a new persistent policy.  Raises ValueError for invalid inputs."""
        if self._store is None:
            raise RuntimeError("No PolicyStore configured — cannot create persistent policies")
        return self._store.create_policy(
            scope_type=scope_type,
            scope_id=scope_id,
            action_type=action_type,
            risk_level=risk_level,
            decision=decision,
            description=description,
            actor=actor,
        )

    def get_policy(self, policy_id: str):
        if self._store is None:
            return None
        return self._store.get_policy(policy_id)

    def list_policies(self, **kwargs):
        if self._store is None:
            return []
        return self._store.list_policies(**kwargs)

    def update_policy(self, policy_id: str, **kwargs):
        if self._store is None:
            raise RuntimeError("No PolicyStore configured")
        return self._store.update_policy(policy_id, **kwargs)

    def delete_policy(self, policy_id: str, actor: str = "system") -> bool:
        if self._store is None:
            return False
        return self._store.delete_policy(policy_id, actor=actor)

    def reset_policies(
        self,
        scope_type: PolicyScope,
        scope_id: str = "",
        actor: str = "system",
    ) -> int:
        if self._store is None:
            return 0
        return self._store.reset_policies(scope_type, scope_id, actor=actor)

    def evaluate_policy(
        self,
        action_type: str,
        risk_level: str,
        workspace_path: str | None = None,
    ) -> PolicyDefaultDecision | None:
        """Dry-run policy resolution without creating a PermissionRequest."""
        if self._store is None:
            return None
        return self._store.resolve_policy(action_type, risk_level, workspace_path)
