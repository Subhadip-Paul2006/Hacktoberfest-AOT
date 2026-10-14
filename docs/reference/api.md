# AgentBridge — API & Command Reference

This document provides a reference for the Control Plane endpoints, WebSocket protocol frames, and Local Runtime dispatch commands.

---

## 1. Control Plane Endpoints

### Health & Monitoring
- `GET /health/live`: Returns `{"status": "ok"}` if the control plane process is alive.
- `GET /health/ready`: Returns `{"status": "ready", "database": "connected"}` if database and stores are ready.
- `GET /v1/metrics`: Returns Prometheus/JSON metrics from `MetricsRegistry`.

### Device Management
- `POST /v1/devices/pair/request`: Generates a single-use pairing code.
- `POST /v1/devices/pair/complete`: Validates pairing code and returns device bearer token.
- `POST /v1/devices/revoke`: Revokes authorization for a device.
- `GET /v1/devices`: Lists registered devices and connection status.

### WebSocket Bridge
- `WSS /v1/devices/connect`: Authenticated WebSocket channel between local runtime and control plane.

---

## 2. WebSocket Protocol Frames

### Command Frame (Control Plane → Local Runtime)
```json
{
  "type": "command",
  "request_id": "req-uuid-1234",
  "device_id": "AB-PC-123456",
  "session_id": "sess-uuid-7890",
  "generation": 1,
  "action": "agent.prompt",
  "payload": {
    "prompt": "Run unit tests"
  },
  "timestamp": 1728200000.0
}
```

### Response Frame (Local Runtime → Control Plane)
```json
{
  "type": "response",
  "request_id": "req-uuid-1234",
  "device_id": "AB-PC-123456",
  "session_id": "sess-uuid-7890",
  "generation": 1,
  "status": "success",
  "payload": {
    "exit_code": 0,
    "stdout": "All tests passed"
  },
  "runtime_processing_ms": 14.5
}
```

---

## 3. Local Runtime Commands

| Action | Description | Parameters |
| :--- | :--- | :--- |
| `runtime.status` | Inspect device status, resource usage, and host info | None |
| `project.list` | List discovered projects within approved access roots | None |
| `project.refresh` | Trigger background scan of workspaces | None |
| `file.read` | Read file with bounded window (max 5,000 lines) | `{"path": str, "offset": int, "limit": int}` |
| `file.list` | Paginated directory listing (max 100 entries) | `{"path": str, "cursor": str}` |
| `agent.start` | Initialize and launch an agent session | `{"agent": str, "project_path": str}` |
| `agent.prompt` | Send prompt to running agent session | `{"session_id": str, "prompt": str}` |
| `agent.stop` | Terminate running agent session and process tree | `{"session_id": str}` |
