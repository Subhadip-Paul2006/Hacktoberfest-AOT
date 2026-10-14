# Permission Policy Reference

This document provides a specification of policy structures, risk levels, evaluation semantics, and configuration models used by the AgentBridge Policy Engine.

---

## 1. Data Models

### Policy Definition
```python
from dataclasses import dataclass

@dataclass
class PermissionPolicy:
    policy_id: str             # Unique UUID
    scope_type: str            # "global" or "project"
    scope_id: str              # Workspace canonical path if scope_type is "project", else ""
    action_type: str           # Target action e.g. "file_write", "bash_exec", or "*"
    decision: str              # "allow", "ask", or "deny"
    risk_level: str            # "low", "medium", "high", "critical"
    description: str           # Human-readable rationale
    created_at: float          # Epoch timestamp
    updated_at: float          # Epoch timestamp
    enabled: bool              # True if active
```

---

## 2. Policy Decisions

| Decision | Semantics |
| :--- | :--- |
| `allow` | Permits action execution automatically without human confirmation (unless risk is `CRITICAL`). |
| `ask` | Always prompts the operator via Telegram inline keyboard before executing. |
| `deny` | Rejects the action immediately, returning an authorization error to the agent. |

---

## 3. Risk Levels

| Risk Level | Classification Criteria | Default Policy Action | Override Capability |
| :--- | :--- | :--- | :--- |
| `LOW` | Read-only actions (e.g. `file_read`, `dir_list`, `git_status`) | `allow` | Can be overridden by policy |
| `MEDIUM` | Non-destructive modifications (e.g. `file_write`, `test_run`) | `ask` | Can be overridden by policy |
| `HIGH` | Broader system actions (e.g. `bash_exec`, `git_push`) | `ask` | Can be overridden by policy |
| `CRITICAL` | Irreversible destructive actions (e.g. `rm -rf`, `drop_db`, format) | `ask` | **CANNOT** be overridden by `allow` |

---

## 4. Wildcard Matching

- If `action_type="*"` is specified on a policy, it applies to all actions in that scope not matched by an exact-name policy.
- Exact action names (e.g., `bash_exec`) always take precedence over wildcard `*` rules within the same scope.
- Project-scoped policies always take precedence over global policies regardless of wildcarding.
