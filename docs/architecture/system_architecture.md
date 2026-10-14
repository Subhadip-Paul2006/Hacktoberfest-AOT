# AgentBridge Protocol and Architecture Design Notes

> **File:** `docs/architecture/system_architecture.md`
> **Purpose:** Preserve earlier protocol and architecture proposals for review without presenting them as implemented behavior.
> **Audience:** Maintainers evaluating future design work.
> **Status:** Historical proposal; not a current implementation specification.

> **Current-state reference:** Use [AI and Runtime Architecture](ai-architecture.md) for behavior verified against the repository. The gateway, frame schema, timing values, security guarantees, and adapter contract below are not confirmed by the current implementation. Treat code blocks as illustrative proposals until separately implemented and tested.

## Contents

- [Gateway and tunnel proposal](#1-gateway--tunnel-protocol-specification)
- [Security and sandboxing proposals](#2-security--sandboxing-invariants)
- [Agent adapter proposal](#3-agent-adapter-contract)

## 1. Gateway & Tunnel Protocol Specification

### 1.1 Outbound Tunnel Architecture
AgentBridge decouples remote chat clients (Telegram, Discord, Slack) from the developer's workstation using an **Outbound Secure WebSocket Tunnel**. The local workstation establishes and maintains a persistent, bi-directional, TLS 1.3 WebSocket connection to the AgentBridge Cloud Gateway.

```
+-------------------+              +-------------------------+              +-----------------------+
| Chat Client       |              | Cloud Gateway           |              | Local Workstation     |
| (e.g., Telegram)  |              | (wss://gateway.domain)  |              | (AgentBridge Runtime) |
+---------+---------+              +------------+------------+              +-----------+-----------+
          |                                     |                                       |
          |                                     |<====== TLS 1.3 WebSocket =============| (Outbound Connect)
          |                                     |        (Mutual Token Handshake)       |
          |                                     |                                       |
          |-------- User Action / Command ----->|                                       |
          |         (e.g., "Allow Once")        |                                       |
          |                                     |------- Dispatch Frame (JSON-RPC) ---->|
          |                                     |        {"method": "permission.grant"} |
          |                                     |                                       |--- Execute Locally
          |                                     |                                       |    (Unblock Process)
          |                                     |<------ Event Frame (Telemetry) -------|
          |                                     |        {"event": "AGENT_PROGRESS"}    |
          |<------- Live Status Update ---------|                                       |
          |                                     |                                       |
```

### 1.2 WebSocket Frame Schema
All messages exchanged over the tunnel adhere to the following unified frame format:

```json
{
  "frame_id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
  "version": "1.0",
  "timestamp": 1727200000000,
  "type": "REQUEST | RESPONSE | EVENT | HEARTBEAT",
  "channel": "control | agent | terminal | telemetry",
  "payload": {
    "method": "permission.decision",
    "params": {
      "permission_id": "perm_98234",
      "decision": "ALLOW_ONCE",
      "authorized_by": "telegram:67891234"
    }
  },
  "signature": "ed25519_signature_hex"
}
```

### 1.3 Heartbeat & Liveness State Machine
- **Interval**: Workstation sends `PING` frame every 20 seconds.
- **Gateway Response**: Gateway responds with `PONG` frame within 5 seconds.
- **Dead Peer Detection**: If 3 consecutive heartbeats are missed (60s), connection is severed.
- **Backoff & Reconnect**: Workstation applies exponential backoff with jitter (`1s`, `2s`, `4s`, `8s`, up to `30s` max).
- **Presence Notification**: Cloud Gateway notifies active Telegram sessions immediately when workstation transitions between `ONLINE` and `OFFLINE`.

---

## 2. Security & Sandboxing Invariants

### 2.1 Workspace Boundary Enforcement
AgentBridge strictly prohibits arbitrary filesystem browsing. Every file access, terminal command, or agent execution MUST be validated against the registered workspace root.

```python
import os
from pathlib import Path

def validate_path(workspace_root: Path, requested_path: Path) -> Path:
    resolved_root = workspace_root.resolve()
    resolved_target = (resolved_root / requested_path).resolve()

    # Path traversal check
    if not resolved_target.is_relative_to(resolved_root):
        raise PermissionError(f"Access denied: {requested_path} escapes workspace boundary.")

    # Windows junction / symlink resolution check
    if resolved_target.is_symlink():
        real_target = Path(os.path.realpath(resolved_target))
        if not real_target.is_relative_to(resolved_root):
            raise PermissionError(f"Access denied: Symlink points outside workspace boundary.")

    return resolved_target
```

### 2.2 Subprocess Command Execution Guard
Commands initiated via AgentBridge are never executed through unvalidated shell strings (`os.system` or `subprocess.Popen(cmd, shell=True)`). Instead, all commands are parsed into token arrays and inspected by the Deterministic Policy Engine.

---

## 3. Agent Adapter Contract

Every agent integration implements the following contract:

```python
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, Any, List
from dataclasses import dataclass

@dataclass
class AgentStatus:
    state: str  # IDLE, RUNNING, WAITING_PERMISSION, BLOCKED, COMPLETED, ERROR
    current_phase: str
    active_tool: str | None
    pending_permission: Dict[str, Any] | None
    token_usage: Dict[str, int]

class AgentAdapter(ABC):
    @abstractmethod
    async def initialize(self, workspace_path: str, config: Dict[str, Any]) -> bool:
        """Initialize adapter and verify binary/API availability."""
        pass

    @abstractmethod
    async def dispatch_task(self, session_id: str, prompt: str) -> str:
        """Dispatch a prompt or coding objective to the agent."""
        pass

    @abstractmethod
    async def poll_status(self, session_id: str) -> AgentStatus:
        """Query real-time health, state, and active tool execution."""
        pass

    @abstractmethod
    async def handle_permission(self, permission_id: str, decision: str) -> bool:
        """Relay remote approval or rejection to the agent process."""
        pass

    @abstractmethod
    async def stream_events(self, session_id: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream structured telemetry, logs, and tool calls."""
        pass

    @abstractmethod
    async def terminate(self, session_id: str) -> bool:
        """Safely halt agent execution and release workspace locks."""
        pass
```
