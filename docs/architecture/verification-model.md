# AgentBridge — Verification Model & Evidence Principles

**Document Version:** 1.0.0
**Phase:** 7.4 — Generic Environment & Connector Analyzer
**Status:** COMPLETE & VERIFIED
**Date:** 2026-10-08

---

## 1. The Five Axioms of Evidence-Based Verification

In AgentBridge, system integrity depends on absolute honesty regarding external state. The verification model is built upon five foundational axioms:

### Axiom 1: Configuration Does Not Imply Reachability
Declaring an API token, base URL, or port in configuration only proves syntactic configuration. It does **not** prove that the network route is open, DNS resolves, or the remote server is alive.

### Axiom 2: Reachability Does Not Imply Authentication
A TCP connection or HTTP 200/404 response proves transport reachability. It does **not** prove that credentials, tokens, or client certificates are valid and accepted.

### Axiom 3: Authentication Does Not Imply Capability Availability
Valid credentials prove identity. They do **not** guarantee that the downstream account has permission to execute specific tools, read resources, or invoke schemas.

### Axiom 4: Verification Does Not Imply Usage
A connector that passes all diagnostic health checks and capability discovery is `VERIFIED`. It is **not** `OBSERVED_IN_USE` unless verifiable execution history exists in the local audit log.

### Axiom 5: Unknown Is Never Failure
When a test cannot be performed—such as during an offline dry run or when an upstream endpoint lacks an echo probe—the result is strictly `UNKNOWN`. Treating `UNKNOWN` as `FAIL` corrupts observability and causes false-positive alerts.

---

## 2. Verification States (7 Distinct States)

| State | Description | Required Evidence |
|-------|-------------|-------------------|
| `UNCONFIGURED` | Missing essential configuration parameters or declared environment variables. | Declared environment variables missing or empty. |
| `CONFIGURED` | Configuration parameters and environment variables are present and syntactically valid. | Declared environment variables present; config keys valid. Reachability unverified. |
| `CONNECTED` | Transport connection established to remote endpoint or local process. | Connection probe succeeded. Authentication unverified. |
| `AUTHENTICATED` | Credentials or tokens validated and accepted by upstream service. | Upstream auth handshake succeeded. Capabilities unverified. |
| `CAPABILITY_DISCOVERED` | Tools, resources, and schemas enumerated. | Declared tools and capabilities discovered. Health unverified. |
| `VERIFIED` | Full end-to-end operational diagnostic passed. | Config valid, connected, authenticated, capabilities discovered, and health check passed. |
| `FAILED` | Definitive failure encountered during verification. | Connection refused, invalid credentials (401/403), or health check reporting unhealthy. |

---

## 3. Usage Evidence States (Orthogonal Dimension)

Usage is measured strictly from the local SQLite `ActivityStore` by inspecting `tool_call_start` event records:

| Usage State | Criterion | Meaning |
|-------------|-----------|---------|
| `OBSERVED_IN_USE` | `count(tool_call_start) >= 1` | The connector has been actively invoked by agents or operators. |
| `NOT_OBSERVED` | `count(tool_call_start) == 0` | The connector is registered and verified, but has never been invoked. |
| `UNKNOWN` | `ActivityStore is None` | Audit store unavailable; usage cannot be determined without guessing. |

---

## 4. Check Status Lifecycle

Every individual check inside an analysis yields an `AnalysisCheckResult` with one of four statuses:

- `PASS`: Evidence confirms the condition is met.
- `FAIL`: Evidence confirms the condition failed (e.g. invalid auth, connection timeout).
- `UNKNOWN`: Insufficient evidence to evaluate the condition (e.g. offline dry run, no test mechanism).
- `SKIPPED`: Check was bypassed intentionally due to prior stage outcome.

---

## 5. Standard Test Cases (Section 28 Verification Matrix)

The following canonical cases validate the separation model:

### Case A: Configured, Connected, Authenticated, Verified, Usage Unknown
- **Input**: Valid credentials present, server reachable, auth valid, health PASS, `ActivityStore=None`.
- **State**: `VERIFIED`
- **Checks**:
  - `CONFIGURATION`: `PASS`
  - `CONNECTION`: `PASS`
  - `AUTHENTICATION`: `PASS`
  - `CAPABILITIES`: `PASS`
  - `HEALTH`: `PASS`
  - `USAGE_EVIDENCE`: `UNKNOWN` (Usage: `UNKNOWN`)

### Case B: Configured, Unreachable, Auth Unknown, Not Verified
- **Input**: Valid configuration present, network host unreachable.
- **State**: `FAILED`
- **Checks**:
  - `CONFIGURATION`: `PASS`
  - `CONNECTION`: `FAIL`
  - `AUTHENTICATION`: `UNKNOWN` (never attempted because connection failed)
  - `VERIFIED`: `NO`

### Case C: Configured, Connected, Authenticated, Verified, Observed in Use
- **Input**: Healthy connector with 1+ tool execution recorded in `ActivityStore`.
- **State**: `VERIFIED`
- **Checks**:
  - `CONFIGURATION`: `PASS`
  - `CONNECTION`: `PASS`
  - `AUTHENTICATION`: `PASS`
  - `HEALTH`: `PASS`
  - `USAGE_EVIDENCE`: `PASS` (Usage: `OBSERVED_IN_USE`, `call_count >= 1`)

### Case D: Unconfigured, Connected Unknown, Auth Unknown
- **Input**: Missing required environment variable (`TEST_API_TOKEN` not set).
- **State**: `UNCONFIGURED`
- **Checks**:
  - `CONFIGURATION`: `FAIL` (Missing required env vars)
  - `CONNECTION`: `SKIPPED`
  - `AUTHENTICATION`: `UNKNOWN`
  - `VERIFIED`: `NO`

### Case E: Configured, Dry Run (Connection Untested), Auth Unknown
- **Input**: Configuration present, `test_connection=False` (offline / dry configuration mode).
- **State**: `CONFIGURED`
- **Checks**:
  - `CONFIGURATION`: `PASS`
  - `CONNECTION`: `SKIPPED`
  - `AUTHENTICATION`: `UNKNOWN`
  - `VERIFIED`: `NO`
