# AgentBridge — Security Architecture & Threat Model

This document outlines the security architecture, threat model, boundaries, and countermeasures implemented in AgentBridge.

---

## 1. Trust Boundaries

```
[ Telegram Operator ]
         |  (Untrusted Internet / TLS Encrypted)
[ TelegramBot & Control Plane (Cloud) ]
         |  (WSS / Mutually Authenticated Channel)
[ Local Runtime & Workspace Files (Workstation) ]
         |  (Subprocess Isolation & Bounds)
[ AI Agent Execution Process ]
```

---

## 2. Threat Analysis & Countermeasures

### T1: Unauthorized Remote Access
- **Threat**: An attacker discovers the control plane port or attempts brute-force commands.
- **Mitigation**:
  - Control plane requires operator bearer token with strict minimum length requirements.
  - WebSocket frames require `token`, `session_id`, and `device_id` validation.
  - Telegram bot enforces explicit `user_id` allowlisting. Unlisted users are rejected immediately.

### T2: Replay Attacks on WebSocket Transport
- **Threat**: An attacker intercepts a valid WSS command frame and replays it later.
- **Mitigation**:
  - Each frame includes a UTC timestamp. Frames outside the 60-second window are rejected.
  - Each session maintains an incrementing generation counter (`session_generation`). Frames with outdated generations are discarded.

### T3: Accidental Destructive Commands by Autonomous Agents
- **Threat**: An AI agent issues `rm -rf /` or attempts database destruction.
- **Mitigation**:
  - Hard-coded **CRITICAL Safety Invariant**: CRITICAL risk actions cannot be auto-approved by persistent rules or session grants.
  - Mandatory one-off human approval required via Telegram inline keyboard callback.

### T4: Secret & Credential Leakage via ChatOps
- **Threat**: Command stdout or logs containing API keys, AWS credentials, or passwords are broadcast to Telegram.
- **Mitigation**:
  - Outgoing message pipeline passes through regex redactor [`redact_secrets()`](../../sutra/redaction.py) stripping:
    - OpenAI keys (`sk-...`)
    - GitHub tokens (`ghp_...`, `gho_...`)
    - AWS access keys (`AKIA...`)
    - Bearer and Authorization tokens
    - Private file path leaks
