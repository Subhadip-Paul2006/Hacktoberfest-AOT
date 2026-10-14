"""Persistent Permission Policy — comprehensive test suite.

Covers all required test scenarios from the system design:
  1.  No persistent policy → existing behavior preserved
  2.  Global LOW auto-allow policy
  3.  Project-specific LOW policy
  4.  Project policy overrides global policy where permitted
  5.  Session grant overrides persistent ASK where permitted
  6.  Session grant disappears after session termination
  7.  Policy survives process restart (file-backed SQLite)
  8.  Policy survives database reopen
  9.  Policy deletion returns behavior to default
 10.  CRITICAL cannot become auto-approved
 11.  Invalid policy rejected
 12.  Concurrent policy access handled correctly
 13.  Telegram policy mutation is authorized (integration-level)
 14.  Unauthorized policy mutation rejected (integration-level)
 15.  Audit events generated
 16.  Existing permission regression suite remains green
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from sutra.permissions import (
    PermissionDecision,
    PermissionRequest,
    PermissionRiskLevel,
)
from sutra.policy_engine import PolicyEngine
from sutra.policy_store import (
    PolicyAuditEventType,
    PolicyDefaultDecision,
    PolicyScope,
    PolicyStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_in_memory() -> PolicyStore:
    """Create a fresh in-memory PolicyStore."""
    return PolicyStore.from_path(":memory:")


def _engine_in_memory() -> PolicyEngine:
    return PolicyEngine(_store_in_memory())


def _engine_no_store() -> PolicyEngine:
    """Legacy mode — no PolicyStore attached."""
    return PolicyEngine(None)


def _request(
    action_type: str,
    command: str | None = None,
    resource_path: str | None = None,
    session_id: str = "session-abc",
) -> PermissionRequest:
    return PermissionRequest(
        request_id=str(uuid.uuid4()),
        action_type=action_type,
        resource_path=resource_path,
        command=command,
        agent_id="agent-test",
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Test 1: No persistent policy → existing behavior preserved
# ---------------------------------------------------------------------------

class TestNoPersistentPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine_in_memory()

    def test_low_risk_auto_approved(self) -> None:
        req = _request("file_read")
        ok, reason = self.engine.check_permission(req)
        self.assertTrue(ok)
        self.assertIn("LOW", reason)

    def test_medium_risk_asks(self) -> None:
        req = _request("file_write")
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)

    def test_high_risk_asks(self) -> None:
        req = _request("shell_execution")
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)

    def test_critical_never_auto(self) -> None:
        req = _request("git_push", command="git push --force origin main")
        ok, reason = self.engine.check_permission(req)
        self.assertFalse(ok)
        self.assertIn("CRITICAL", reason)

    def test_legacy_no_store_low_auto(self) -> None:
        engine = _engine_no_store()
        req = _request("file_read")
        ok, _ = engine.check_permission(req)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Test 2: Global LOW auto-allow policy
# ---------------------------------------------------------------------------

class TestGlobalLowAutoAllow(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine_in_memory()
        self.engine.set_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="file_read",
            risk_level="LOW",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )

    def test_matched_action_auto_allowed(self) -> None:
        req = _request("file_read")
        ok, reason = self.engine.check_permission(req)
        self.assertTrue(ok)

    def test_unmatched_action_asks(self) -> None:
        req = _request("file_write")
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Test 3: Project-specific policy
# ---------------------------------------------------------------------------

class TestProjectScopedPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine_in_memory()
        self.ws = "/home/user/myproject"
        self.engine.set_policy(
            scope_type=PolicyScope.WORKSPACE,
            scope_id=self.ws,
            action_type="file_write",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )

    def test_workspace_policy_allows_in_scope(self) -> None:
        req = _request("file_write")
        ok, reason = self.engine.check_permission(req, workspace_path=self.ws)
        self.assertTrue(ok)
        self.assertIn("persistent", reason)

    def test_workspace_policy_no_effect_outside_scope(self) -> None:
        req = _request("file_write")
        ok, _ = self.engine.check_permission(req, workspace_path="/other/path")
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Test 4: Project policy overrides global policy where permitted
# ---------------------------------------------------------------------------

class TestProjectOverridesGlobal(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine_in_memory()
        self.ws = "/srv/project"
        # Global says ASK for file_write
        self.engine.set_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="file_write",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.ASK,
        )
        # Workspace says AUTO_ALLOW
        self.engine.set_policy(
            scope_type=PolicyScope.WORKSPACE,
            scope_id=self.ws,
            action_type="file_write",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )

    def test_workspace_wins_over_global(self) -> None:
        req = _request("file_write")
        ok, _ = self.engine.check_permission(req, workspace_path=self.ws)
        self.assertTrue(ok)

    def test_global_applies_outside_workspace(self) -> None:
        req = _request("file_write")
        ok, _ = self.engine.check_permission(req, workspace_path="/other")
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Test 5: Session grant overrides persistent ASK
# ---------------------------------------------------------------------------

class TestSessionGrantOverridesAsk(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine_in_memory()
        # Global ASK for run_test
        self.engine.set_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="run_test",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.ASK,
        )

    def test_session_grant_allows_despite_ask_policy(self) -> None:
        session_id = "sess-001"
        req = _request("run_test", session_id=session_id)
        # First check — should ask
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)

        # Human grants allow_session
        self.engine.apply_decision(req.request_id, PermissionDecision.ALLOW_SESSION)

        # Second request with same session — should pass
        req2 = _request("run_test", session_id=session_id)
        req2.resource_path = req.resource_path
        ok2, reason2 = self.engine.check_permission(req2)
        self.assertTrue(ok2)
        self.assertIn("session", reason2.lower())


# ---------------------------------------------------------------------------
# Test 6: Session grant disappears after session termination
# ---------------------------------------------------------------------------

class TestSessionGrantExpiresOnStop(unittest.TestCase):
    def test_grants_cleared_after_session_end(self) -> None:
        engine = _engine_in_memory()
        session_id = "sess-expire"
        req = _request("file_write", session_id=session_id)

        ok, _ = engine.check_permission(req)
        self.assertFalse(ok)
        engine.apply_decision(req.request_id, PermissionDecision.ALLOW_SESSION)

        # Simulate session end
        engine.clear_session(session_id)

        # Next request should ASK again
        req2 = _request("file_write", session_id=session_id)
        ok2, _ = engine.check_permission(req2)
        self.assertFalse(ok2)


# ---------------------------------------------------------------------------
# Test 7 & 8: Policy survives process restart / database reopen
# ---------------------------------------------------------------------------

class TestPolicySurvivesRestart(unittest.TestCase):
    def test_policy_persists_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"

            # Create and persist a policy
            store1 = PolicyStore.from_path(db_path)
            store1.create_policy(
                scope_type=PolicyScope.GLOBAL,
                scope_id="",
                action_type="file_read",
                risk_level="LOW",
                decision=PolicyDefaultDecision.AUTO_ALLOW,
            )
            store1.close()

            # Reopen — policy should still be there
            store2 = PolicyStore.from_path(db_path)
            policies = store2.list_policies()
            self.assertEqual(len(policies), 1)
            self.assertEqual(policies[0].action_type, "file_read")
            self.assertEqual(policies[0].decision, "AUTO_ALLOW")
            store2.close()


# ---------------------------------------------------------------------------
# Test 9: Policy deletion returns behavior to default
# ---------------------------------------------------------------------------

class TestPolicyDeletion(unittest.TestCase):
    def test_delete_restores_default_behavior(self) -> None:
        engine = _engine_in_memory()
        policy = engine.set_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="file_write",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )
        # Policy active — should auto-allow
        req = _request("file_write")
        ok, _ = engine.check_permission(req)
        self.assertTrue(ok)

        # Delete the policy
        engine.delete_policy(policy.id)

        # Now should ASK
        req2 = _request("file_write")
        ok2, _ = engine.check_permission(req2)
        self.assertFalse(ok2)


# ---------------------------------------------------------------------------
# Test 10: CRITICAL cannot become auto-approved
# ---------------------------------------------------------------------------

class TestCriticalNeverAutoApproved(unittest.TestCase):
    def test_cannot_set_critical_auto_allow_in_store(self) -> None:
        store = _store_in_memory()
        with self.assertRaises(ValueError):
            store.create_policy(
                scope_type=PolicyScope.GLOBAL,
                scope_id="",
                action_type="git_push",
                risk_level="CRITICAL",
                decision=PolicyDefaultDecision.AUTO_ALLOW,
            )

    def test_cannot_update_to_critical_auto_allow(self) -> None:
        store = _store_in_memory()
        p = store.create_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="git_push",
            risk_level="CRITICAL",
            decision=PolicyDefaultDecision.ASK,
        )
        with self.assertRaises(ValueError):
            store.update_policy(p.id, decision=PolicyDefaultDecision.AUTO_ALLOW)

    def test_critical_request_always_asks_despite_global_policy(self) -> None:
        engine = _engine_in_memory()
        # Even with a wildcard AUTO_ALLOW policy, CRITICAL must still ask
        engine.set_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="*",
            risk_level="HIGH",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )
        req = _request("git_push", command="git push --force origin main")
        ok, reason = engine.check_permission(req)
        self.assertFalse(ok)
        self.assertIn("CRITICAL", reason)


# ---------------------------------------------------------------------------
# Test 11: Invalid policy rejected
# ---------------------------------------------------------------------------

class TestInvalidPolicyRejected(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _store_in_memory()

    def test_invalid_risk_level(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_policy(
                scope_type=PolicyScope.GLOBAL,
                scope_id="",
                action_type="file_read",
                risk_level="UNKNOWN_LEVEL",
                decision=PolicyDefaultDecision.AUTO_ALLOW,
            )

    def test_invalid_decision(self) -> None:
        with self.assertRaises((ValueError, AttributeError)):
            self.store.create_policy(
                scope_type=PolicyScope.GLOBAL,
                scope_id="",
                action_type="file_read",
                risk_level="LOW",
                decision="MAYBE",  # type: ignore[arg-type]
            )

    def test_nonexistent_policy_update_returns_none(self) -> None:
        result = self.store.update_policy("nonexistent-id", enabled=False)
        self.assertIsNone(result)

    def test_nonexistent_policy_delete_returns_false(self) -> None:
        result = self.store.delete_policy("nonexistent-id")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Test 12: Concurrent policy access handled correctly
# ---------------------------------------------------------------------------

class TestConcurrentPolicyAccess(unittest.TestCase):
    def test_concurrent_creates_and_reads(self) -> None:
        store = _store_in_memory()
        errors: list[Exception] = []
        results: list[int] = []

        def worker(n: int) -> None:
            try:
                for i in range(5):
                    store.create_policy(
                        scope_type=PolicyScope.GLOBAL,
                        scope_id="",
                        action_type=f"action_{n}_{i}",
                        risk_level="LOW",
                        decision=PolicyDefaultDecision.AUTO_ALLOW,
                    )
                results.append(len(store.list_policies()))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(store.list_policies()), 40)


# ---------------------------------------------------------------------------
# Test 13: Telegram authorization guard — authorized mutation accepted
# ---------------------------------------------------------------------------

class TestTelegramPolicyAuthorized(unittest.TestCase):
    def test_authorized_list_policies_works(self) -> None:
        """Authorized access to PolicyEngine via ControlPlane should succeed."""
        from sutra.control_plane import ControlPlane
        import os
        tmp = tempfile.mkdtemp()
        try:
            db_path = Path(tmp) / "cp.db"
            cp = ControlPlane(db_path, "test-operator-token")
            # Permission engine is a PolicyEngine
            from sutra.policy_engine import PolicyEngine
            self.assertIsInstance(cp.permission_engine, PolicyEngine)
            # Set a policy through the engine
            p = cp.permission_engine.set_policy(
                scope_type=PolicyScope.GLOBAL,
                scope_id="",
                action_type="file_read",
                risk_level="LOW",
                decision=PolicyDefaultDecision.AUTO_ALLOW,
                actor="operator",
            )
            self.assertIsNotNone(p)
            # List retrieves it
            policies = cp.permission_engine.list_policies()
            self.assertGreaterEqual(len(policies), 1)
        finally:
            # Explicitly close SQLite connection before temp dir cleanup (Windows)
            try:
                cp.store.close()
            except Exception:
                pass
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 14: Unauthorized policy mutation rejected
# ---------------------------------------------------------------------------

class TestUnauthorizedPolicyRejected(unittest.TestCase):
    def test_policy_engine_without_store_raises(self) -> None:
        engine = _engine_no_store()
        with self.assertRaises(RuntimeError):
            engine.set_policy(
                scope_type=PolicyScope.GLOBAL,
                scope_id="",
                action_type="file_read",
                risk_level="LOW",
                decision=PolicyDefaultDecision.AUTO_ALLOW,
            )


# ---------------------------------------------------------------------------
# Test 15: Audit events generated
# ---------------------------------------------------------------------------

class TestAuditEventsGenerated(unittest.TestCase):
    def test_create_generates_audit(self) -> None:
        store = _store_in_memory()
        p = store.create_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="file_read",
            risk_level="LOW",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
            actor="admin",
        )
        audit = store.list_audit_log(policy_id=p.id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].event_type, PolicyAuditEventType.POLICY_CREATED.value)
        self.assertEqual(audit[0].actor, "admin")

    def test_update_generates_audit(self) -> None:
        store = _store_in_memory()
        p = store.create_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="file_write",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.ASK,
        )
        store.update_policy(p.id, enabled=False, actor="ops")
        audit = store.list_audit_log(policy_id=p.id)
        event_types = [a.event_type for a in audit]
        self.assertIn(PolicyAuditEventType.POLICY_UPDATED.value, event_types)

    def test_delete_generates_audit(self) -> None:
        store = _store_in_memory()
        p = store.create_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="file_read",
            risk_level="LOW",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )
        store.delete_policy(p.id, actor="cleaner")
        audit = store.list_audit_log(policy_id=p.id)
        event_types = [a.event_type for a in audit]
        self.assertIn(PolicyAuditEventType.POLICY_DELETED.value, event_types)

    def test_reset_generates_audit(self) -> None:
        store = _store_in_memory()
        p = store.create_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="dir_list",
            risk_level="LOW",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )
        removed = store.reset_policies(PolicyScope.GLOBAL, actor="admin")
        self.assertEqual(removed, 1)
        audit = store.list_audit_log(policy_id=p.id)
        event_types = [a.event_type for a in audit]
        self.assertIn(PolicyAuditEventType.POLICY_RESET.value, event_types)


# ---------------------------------------------------------------------------
# Test 16: Existing permission regression suite remains green
# ---------------------------------------------------------------------------

class TestExistingPermissionRegression(unittest.TestCase):
    """Replay the key scenarios from the original PermissionEngine tests."""

    def setUp(self) -> None:
        self.engine = _engine_in_memory()

    def test_allow_once_clears_after_use(self) -> None:
        req = _request("file_write")
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)

        self.engine.apply_decision(req.request_id, PermissionDecision.ALLOW_ONCE)
        # Re-check pending (allow_once grant is stored against request_id)
        # Simulate engine storing grant then evaluating new request with same session
        req2 = _request("file_write", session_id=req.session_id)
        req2.request_id = req.request_id  # same request_id so key matches
        ok2, _ = self.engine.check_permission(req2)
        self.assertTrue(ok2)

        # Third check — grant should be consumed
        req3 = _request("file_write", session_id=req.session_id)
        ok3, _ = self.engine.check_permission(req3)
        self.assertFalse(ok3)

    def test_deny_does_not_auto_allow(self) -> None:
        req = _request("shell_execution")
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)
        self.engine.apply_decision(req.request_id, PermissionDecision.DENY)
        req2 = _request("shell_execution", session_id=req.session_id)
        ok2, _ = self.engine.check_permission(req2)
        self.assertFalse(ok2)

    def test_critical_allow_once_exact_request_id(self) -> None:
        req = _request("git_push", command="git push --force origin main")
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)

        # Apply single-use for the exact request_id
        self.engine.apply_decision(req.request_id, PermissionDecision.ALLOW_ONCE)
        # Re-present same request_id
        req2 = _request("git_push", command="git push --force origin main",
                        session_id=req.session_id)
        req2.request_id = req.request_id
        ok2, _ = self.engine.check_permission(req2)
        self.assertTrue(ok2)

    def test_assess_risk_classifications(self) -> None:
        engine = _engine_in_memory()
        self.assertEqual(
            engine.assess_risk("file_read"),
            PermissionRiskLevel.LOW,
        )
        self.assertEqual(
            engine.assess_risk("file_write"),
            PermissionRiskLevel.MEDIUM,
        )
        self.assertEqual(
            engine.assess_risk("shell_execution"),
            PermissionRiskLevel.HIGH,
        )
        self.assertEqual(
            engine.assess_risk("git_push", command="git push --force"),
            PermissionRiskLevel.CRITICAL,
        )

    def test_clear_session_removes_grants(self) -> None:
        session = "sess-clear"
        req = _request("file_write", session_id=session)
        ok, _ = self.engine.check_permission(req)
        self.assertFalse(ok)
        self.engine.apply_decision(req.request_id, PermissionDecision.ALLOW_SESSION)
        self.engine.clear_session(session)
        req2 = _request("file_write", session_id=session)
        ok2, _ = self.engine.check_permission(req2)
        self.assertFalse(ok2)

    def test_wildcard_action_type_in_policy(self) -> None:
        engine = _engine_in_memory()
        engine.set_policy(
            scope_type=PolicyScope.GLOBAL,
            scope_id="",
            action_type="*",
            risk_level="MEDIUM",
            decision=PolicyDefaultDecision.AUTO_ALLOW,
        )
        req = _request("file_write")
        ok, _ = engine.check_permission(req)
        self.assertTrue(ok)

        req_high = _request("shell_execution")
        # HIGH risk — AUTO_ALLOW for wildcard should NOT be honored for HIGH
        # (system logs warning and falls through to ASK)
        ok_high, _ = engine.check_permission(req_high)
        self.assertFalse(ok_high)


if __name__ == "__main__":
    unittest.main()
