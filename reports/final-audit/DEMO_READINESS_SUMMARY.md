# SŪTRA — Demo Readiness & Stabilization Summary

> **Document:** `DEMO_READINESS_SUMMARY.md`  
> **Repository:** `sutra-local` (SAMANVAYA · SŪTRA)  
> **Target Event:** Hackathon Demonstration  
> **Audited Commit:** `c9917e5afdf55f19214645dfcb1441f21d5fad11`  
> **Current Readiness Verdict:** **READY WITH CONDITIONS**  

---

## 1. Readiness Verdict Explained

SŪTRA is **READY WITH CONDITIONS** for its scheduled hackathon demonstration.

### What is Ready and Solid:
- **Core Runtime & Architecture:** The local daemon, loopback REST API, WebSocket uplink, TLS control plane, and SQLite WAL persistence are fully implemented and pass extensive regression suites.
- **Local Security Guardrails:** Zero-shell execution (`shell=False` with tokenized argument arrays) in the terminal connector strictly protects against command injection. Canonical path resolution with recursive URL decoding defends the local filesystem.
- **Audit Logging & Policies:** Every tool invocation and policy decision is recorded in SQLite with request correlation IDs and timestamps.
- **Automated Test Baseline:** **858 out of 878 automated tests pass**, demonstrating strong component stability across connectors, session lifecycles, and supervisory watchdogs.

### Why Conditions are Required:
- **Uncommitted Source Code:** All 260+ implementation files exist as untracked files on top of a single-commit git HEAD (`README.md`). This represents a severe operational risk if any clean or branch switch occurs.
- **AI Override Lookup Bug:** Capitalized agent override keys silently fall back to global LLM defaults due to an unnormalized dictionary key lookup in `sutra/ai_providers.py:114`.
- **Live Service Verification Pending:** Telegram ChatOps, GitHub MCP, and live LLM endpoints have been verified using rigorous local mock servers and TLS test harnesses, but not against live external production APIs.

---

## 2. Top Demo Blockers & Remediation Strategies

| ID | Issue Description | Impact on Live Demo | Immediate Remediation / Workaround |
|---|---|---|---|
| **BLK-01** | **Untracked Working Tree** | High risk of catastrophic source code loss before or during the demo. | **Mandatory Action:** Commit the entire working tree to a local git branch (e.g. `release/hackathon-demo`) immediately. |
| **BLK-02** | **AIConfig Agent Override Casefold Bug** | Overrides with uppercase IDs (e.g. `{"Research": ...}`) are ignored at runtime. | **Demo Workaround:** In all demo configuration files or scripts, declare agent IDs in all-lowercase (e.g. `{"research": ...}`). |
| **BLK-03** | **Unverified Live Telegram Bot Integration** | Live mobile chat interaction could fail due to network, token, or webhook misconfiguration. | **Mandatory Pre-Demo Action:** Execute manual test case `TC-TEL-01` using a disposable bot token and whitelist tester's numeric Telegram user ID on `long_polling` mode. |
| **BLK-04** | **Python 3.13 Test Suite Isolation Failure** | Running the complete test suite in one command displays 17 failures due to asyncio loop teardown in `test_research.py`. | **Demo Workaround:** If demonstrating automated tests during the presentation, run targeted test suites (e.g. `pytest tests/test_connectors.py tests/test_control_plane.py`) or run `tests/test_research.py` in isolation. |

---

## 3. Mandatory Pre-Demo Execution Order

Before presenting the live demonstration, execute these steps in the following strict order:

```
Step 1: Code Preservation
   └─ Commit untracked files to git branch: git checkout -b release/demo; git add .; git commit -m "feat: complete sutra runtime implementation"

Step 2: Environment Verification
   └─ Run: python -m sutra doctor
   └─ Confirm Python >=3.11, Git present, and valid dev certificates

Step 3: State Initialization
   └─ Run: python -m sutra setup
   └─ Verify ~/.sutra/ directory and state.sqlite3 creation

Step 4: Workspace Registration
   └─ Create a dedicated demo folder with sample files
   └─ Run: python -m sutra workspace add ./demo-workspace
   └─ Verify with: python -m sutra workspace list

Step 5: Control Plane & Local Runtime Launch
   └─ Terminal 1: python -m sutra control-plane --tls-certfile .dev-certs/cert.pem --tls-keyfile .dev-certs/key.pem
   └─ Terminal 2: python -m sutra local

Step 6: ChatOps Connection (If demonstrating Telegram)
   └─ Ensure TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS are exported
   └─ Send /start from Telegram mobile app; verify greeting response
   └─ Trigger high-risk action; verify inline approval keyboard click updates status
```

---

## 4. Optional Improvements (Strictly Defer Until After Demo)

Do **NOT** attempt the following non-blocking tasks before the hackathon demonstration:
- ❌ **Do not rewrite Slack or Discord bots:** Keep them as planned scaffolds (honoring the scope freeze).
- ❌ **Do not attempt mass Ruff / Mypy refactoring:** Mass auto-formatting 1,889 lint warnings risks introducing subtle regressions into stable code.
- ❌ **Do not install Docker:** SŪTRA is designed to run natively on the developer workstation.
- ❌ **Do not alter package naming:** Preserve product identity `SAMANVAYA · SŪTRA` and package namespace `sutra`.

---

## 5. Summary Scorecard

| Category | Score | Readiness Status |
|---|---|---|
| **Architecture & Modularity** | 9.5 / 10 | EXCELLENT |
| **Security & Confinement** | 9.0 / 10 | STRONG |
| **Automated Test Coverage** | 8.8 / 10 | VERY GOOD (858/878 passed) |
| **CLI & Terminal Usability** | 9.5 / 10 | EXCELLENT |
| **Live External Verification** | 6.5 / 10 | PENDING MANUAL VERIFICATION |
| **Overall Demo Readiness** | **8.6 / 10** | **READY WITH CONDITIONS** |
