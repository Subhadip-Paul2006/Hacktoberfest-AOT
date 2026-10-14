"""Centralized permission and authorization engine enforcing execution guardrails."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)


class PermissionRiskLevel(str, Enum):
    LOW = "LOW"            # Read-only queries, status, file viewing
    MEDIUM = "MEDIUM"      # Non-destructive file edits within project scope, running tests
    HIGH = "HIGH"          # Arbitrary shell execution, package installations, git commits
    CRITICAL = "CRITICAL"  # Force push, recursive deletions, system changes (NEVER auto-approved)


# Backward compatibility alias
PermissionLevel = PermissionRiskLevel


class PermissionDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    MODIFY = "modify"


# Dangerous command patterns
CRITICAL_COMMAND_PATTERNS = [
    re.compile(r"\bgit\s+push\b.*(?:--force|-f\b)", re.IGNORECASE),
    re.compile(r"\brm\s+(-[rfRF]{1,4}|--recursive)\b", re.IGNORECASE),
    re.compile(r"\bformat\b|\bfdisk\b|\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\b", re.IGNORECASE),
    re.compile(r"\b(del|rmdir|rd)\s+[/][sS][/][qQ]\s+[cC]:\\", re.IGNORECASE),
]

HIGH_COMMAND_PATTERNS = [
    re.compile(r"\b(curl|wget)\b.*\|\s*(bash|sh|powershell|cmd)\b", re.IGNORECASE),
    re.compile(r"\b(npm|pip|pip3|cargo|yarn|pnpm)\s+install\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(commit|merge|rebase|reset)\b", re.IGNORECASE),
]


@dataclass
class PermissionRequest:
    request_id: str
    action_type: str        # "shell_command", "file_write", "file_delete", "git_operation", "tool_call"
    resource_path: str | None
    command: str | None
    agent_id: str
    session_id: str
    user_id: int | None = None
    risk_level: PermissionRiskLevel = PermissionRiskLevel.HIGH
    description: str = ""


class PermissionEngine:
    """Evaluates and enforces permission checks across all execution routes."""

    def __init__(self) -> None:
        # (session_id, action_type, normalized_resource) -> PermissionDecision
        self._session_grants: dict[tuple[str, str, str], PermissionDecision] = {}
        self._pending_requests: dict[str, PermissionRequest] = {}

    def assess_risk(
        self,
        action_type: str,
        command: str | None = None,
        resource_path: str | None = None,
    ) -> PermissionRiskLevel:
        """Classify the risk level of an operation."""
        # 1. Check for CRITICAL commands
        if command:
            for pat in CRITICAL_COMMAND_PATTERNS:
                if pat.search(command):
                    return PermissionRiskLevel.CRITICAL

        # 2. Check for HIGH commands
        if command:
            for pat in HIGH_COMMAND_PATTERNS:
                if pat.search(command):
                    return PermissionRiskLevel.HIGH

        if action_type in ("git_force_push",):
            return PermissionRiskLevel.CRITICAL

        if action_type in (
            "file_delete", "git_push", "git_commit", "git_pr_create", "shell_execution",
            "git_pr_comment", "github_pr_comment", "git_pr_merge", "github_pr_merge",
            "git_ci_rerun", "github_ci_rerun", "git_ci_cancel", "github_ci_cancel",
            "github_pr_review_request", "github_pr_update",
        ):
            return PermissionRiskLevel.HIGH

        if action_type in (
            "file_write", "run_test", "git_stage", "git_unstage",
            "github_pr_label", "github_pr_assign", "github_pr_unlabel",
        ):
            return PermissionRiskLevel.MEDIUM

        if action_type in (
            "file_read", "dir_list", "project_status", "agent_status",
            "git_status", "git_diff", "git_branch", "git_log", "git_preview",
            "git_pr_list", "github_pr_list", "git_pr_view", "github_pr_view",
            "git_pr_files", "github_pr_files", "git_pr_comments", "github_pr_comments",
            "git_pr_reviews", "github_pr_reviews", "git_ci_runs", "github_ci_runs",
            "git_ci_run_get", "github_ci_run_get", "git_ci_logs", "github_ci_logs",
            "git_releases_list", "github_releases_list",
            "tool_read",
        ):
            return PermissionRiskLevel.LOW

        return PermissionRiskLevel.HIGH

    def check_permission(
        self,
        request: PermissionRequest,
        *,
        workspace_path: str | None = None,
    ) -> tuple[bool, str]:
        """Check whether an action can execute immediately or requires human authorization.
        
        Returns:
            (allowed: bool, reason: str)
        """
        # Determine actual risk
        request.risk_level = self.assess_risk(request.action_type, request.command, request.resource_path)

        # Critical actions NEVER auto-approved or allowed by session
        if request.risk_level == PermissionRiskLevel.CRITICAL:
            # Check if specifically granted ALLOW_ONCE for this exact request_id
            grant_key = (request.session_id, request.action_type, request.request_id)
            if self._session_grants.get(grant_key) == PermissionDecision.ALLOW_ONCE:
                self._session_grants.pop(grant_key, None)
                return True, "Explicit single-use approval for CRITICAL operation."
            self._pending_requests[request.request_id] = request
            return False, f"CRITICAL operation ({request.command or request.action_type}) requires explicit Telegram authorization."

        # LOW risk operations are auto-approved
        if request.risk_level == PermissionRiskLevel.LOW:
            return True, "Auto-approved LOW risk operation."

        # Check existing session grants for MEDIUM/HIGH
        resource_key = request.resource_path or request.command or "*"
        session_key = (request.session_id, request.action_type, resource_key)
        wildcard_key = (request.session_id, request.action_type, "*")

        if self._session_grants.get(session_key) == PermissionDecision.ALLOW_SESSION:
            return True, "Approved by session grant."
        if self._session_grants.get(wildcard_key) == PermissionDecision.ALLOW_SESSION:
            return True, "Approved by session wildcard grant."

        # Check single-use grant
        once_key = (request.session_id, request.action_type, request.request_id)
        if self._session_grants.get(once_key) == PermissionDecision.ALLOW_ONCE:
            self._session_grants.pop(once_key, None)
            return True, "Approved once."

        # Not yet approved
        self._pending_requests[request.request_id] = request
        return False, f"{request.risk_level.value} risk action requires authorization."

    def apply_decision(
        self,
        request_id: str,
        decision: PermissionDecision,
        modified_command: str | None = None,
    ) -> bool:
        """Apply human decision (from Telegram callback)."""
        req = self._pending_requests.pop(request_id, None)
        if not req:
            return False

        if decision == PermissionDecision.DENY:
            log.info("Permission denied for request %s (session %s)", request_id, req.session_id)
            return True

        if decision == PermissionDecision.ALLOW_ONCE:
            self._session_grants[(req.session_id, req.action_type, req.request_id)] = PermissionDecision.ALLOW_ONCE
            return True

        if decision == PermissionDecision.ALLOW_SESSION:
            # Cannot give session-wide grant to CRITICAL operations or PR merge operations
            if req.risk_level == PermissionRiskLevel.CRITICAL or req.action_type in ("git_pr_merge", "github_pr_merge"):
                self._session_grants[(req.session_id, req.action_type, req.request_id)] = PermissionDecision.ALLOW_ONCE
            else:
                resource_key = req.resource_path or req.command or "*"
                self._session_grants[(req.session_id, req.action_type, resource_key)] = PermissionDecision.ALLOW_SESSION
            return True

        if decision == PermissionDecision.MODIFY:
            if modified_command:
                req.command = modified_command
                req.risk_level = self.assess_risk(req.action_type, modified_command, req.resource_path)
            self._session_grants[(req.session_id, req.action_type, req.request_id)] = PermissionDecision.ALLOW_ONCE
            return True

        return False

    def clear_session(self, session_id: str) -> None:
        """Clear all session grants on session termination."""
        keys_to_remove = [k for k in self._session_grants if k[0] == session_id]
        for k in keys_to_remove:
            self._session_grants.pop(k, None)
