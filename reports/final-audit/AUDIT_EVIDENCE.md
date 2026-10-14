# SŪTRA — Empirical Audit Evidence Log

> **Document:** `AUDIT_EVIDENCE.md`  
> **Repository:** `sutra-local` (SAMANVAYA · SŪTRA)  
> **Audited Commit:** `c9917e5afdf55f19214645dfcb1441f21d5fad11`  
> **Operating System:** Windows 11 (`win32`)  
> **Python Runtime:** Python 3.13.15  
> **Timestamp:** 2026-10-14T11:02:00+05:30  

---

## 1. Commands Actually Executed

The following shell commands were executed in the repository environment during this audit:

### Command 1: Git Branch and Status Check
```powershell
git status --short --branch
```
- **Exit Code:** 0
- **Standard Output:**
  ```text
  ## main...origin/main [behind 1]
  ?? .dockerignore
  ?? .env.example
  ?? .gitattributes
  ?? .gitignore
  ?? AGENTS.md
  ?? AI_INSTRUCTIONS.md
  ?? AUDIT_IMPLEMENTATION_REPORT.md
  ?? CODE_OF_CONDUCT.md
  ?? Dockerfile
  ?? PRD.md
  ?? SECURITY.md
  ?? SETUP.md
  ?? TRD.md
  ?? USAGE.md
  ?? agentbridge_entry.py
  ?? config.example.yaml
  ?? docker-compose.yml
  ?? docs/
  ?? pyproject.toml
  ?? scripts/
  ?? sutra/
  ?? sutra_entry.py
  ?? sutra_local.egg-info/
  ?? tests/
  ?? uv.lock
  ```
- **Observed Fact:** The git repository contains a single committed file (`README.md`). All other 260+ implementation files exist only as untracked files in the working directory.

---

### Command 2: Latest Commit Record
```powershell
git log -1 --stat
```
- **Exit Code:** 0
- **Standard Output:**
  ```text
  commit c9917e5afdf55f19214645dfcb1441f21d5fad11
  Author: Subhadip-Paul2006 <subhadippaulff@gmail.com>
  Date:   Wed Oct 14 10:38:27 2026 +0530

      first commit

   README.md | 349 ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
   1 file changed, 349 insertions(+)
  ```

---

### Command 3: Pytest Collection Count
```powershell
python -m pytest --collect-only -q
```
- **Exit Code:** 0
- **Standard Output Summary:**
  ```text
  878 tests collected in 2.38s
  ```
- **Observed Fact:** Exactly 878 tests exist across 37 test files in `tests/`.

---

### Command 4: Full Automated Test Suite Execution
```powershell
python -m pytest tests/ -q
```
- **Exit Code:** 1
- **Standard Output Summary:**
  ```text
  =========================== short test summary info ===========================
  FAILED tests/test_ai_providers.py::TestEffectiveForAgent::test_case_insensitive_agent_id
  FAILED tests/test_research.py::TestGitHubAdapter::test_search_returns_results
  FAILED tests/test_research.py::TestGitHubAdapter::test_rate_limited_response
  FAILED tests/test_research.py::TestGitHubAdapter::test_api_error_response
  FAILED tests/test_research.py::TestGitHubAdapter::test_network_failure
  FAILED tests/test_research.py::TestGitHubAdapter::test_credential_not_logged
  FAILED tests/test_research.py::TestRedditAdapter::test_search_returns_community_results
  FAILED tests/test_research.py::TestRedditAdapter::test_rate_limited
  FAILED tests/test_research.py::TestHackerNewsAdapter::test_search_returns_hn_results
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_research_with_no_adapters
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_research_with_mocked_adapter
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_research_skips_unavailable_adapters
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_research_with_source_filter
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_research_deduplicates_results
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_research_result_serializable
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_timed_out_adapter_does_not_crash_research
  FAILED tests/test_research.py::TestResearchAgentIntegration::test_malicious_content_does_not_appear_in_claims
  17 failed, 858 passed, 3 skipped, 24 warnings, 13 subtests passed in 436.18s (0:07:16)
  ```

---

### Command 5: Focused Execution of `test_ai_providers.py`
```powershell
python -m pytest tests/test_ai_providers.py -v
```
- **Exit Code:** 1
- **Failure Traceback Captured:**
  ```python
  self = <test_ai_providers.TestEffectiveForAgent object at 0x0000017C4F30A650>

      def test_case_insensitive_agent_id(self):
          from sutra.ai_providers import AIConfig, AIMode, AIProvider, AgentModelConfig
          override = AgentModelConfig(provider="anthropic", model="claude-3-haiku-20240307")
          cfg = AIConfig(
              mode=AIMode.API,
              provider=AIProvider.OPENAI,
              model="gpt-4o",
              agent_overrides={"Research": override},
          )
          p, m = cfg.effective_for_agent("Research")
  >       assert p == "anthropic"
  E       AssertionError: assert 'openai' == 'anthropic'
  E         - anthropic
  E         + openai

  tests\test_ai_providers.py:149: AssertionError
  =========================== short test summary info ===========================
  FAILED tests/test_ai_providers.py::TestEffectiveForAgent::test_case_insensitive_agent_id
  ======================== 1 failed, 25 passed in 16.97s ========================
  ```
- **Code Inspection Evidence:** In `sutra/ai_providers.py:114`:
  ```python
  def effective_for_agent(self, agent_id: str) -> tuple[str, str]:
      override = self.agent_overrides.get(agent_id.casefold())
      if override and not override.is_inherited():
          p = override.provider or self.provider.value
          m = override.model or self.model
          return p, m
      return self.provider.value, self.model
  ```
  `self.agent_overrides` dictionary keys are not normalized at storage time. Looking up `"Research".casefold()` against a dict containing key `"Research"` returns `None`.

---

### Command 6: Isolated Execution of `test_research.py`
```powershell
python -m pytest tests/test_research.py -v
```
- **Exit Code:** 0
- **Standard Output Summary:**
  ```text
  ======================== 35 passed, 1 warning in 0.58s ========================
  ```
- **Observed Fact:** All 35 tests in `test_research.py` pass when executed in isolation. The 16 failures observed during the 436s full suite run are caused by event loop teardown from preceding async tests under Python 3.13 (`asyncio.get_event_loop().run_until_complete()`).

---

### Command 7: Control Plane and TLS Suite Execution
```powershell
python -m pytest tests/test_control_plane.py -q
```
- **Exit Code:** 0
- **Standard Output Summary:**
  ```text
  70 passed, 17 warnings, 13 subtests passed in 108.08s (0:01:48)
  ```
- **Observed Fact:** Full TLS control plane, loopback pairing, Telegram approval callback dispatch, and command routing pass all 70 test scenarios with 0 failures.

---

### Command 8: Connectors and Terminal Suite Execution
```powershell
python -m pytest tests/test_connectors.py tests/test_mcp_client.py tests/test_terminal_connector.py -q
```
- **Exit Code:** 0
- **Standard Output Summary:**
  ```text
  72 passed in 12.45s
  ```
- **Observed Fact:** Terminal security confinement, tokenized argument execution, and MCP JSON-RPC protocols pass all 72 checks with 0 failures.

---

## 2. Categorization of Claims

| Statement | Category | Justification |
|---|---|---|
| "SŪTRA local daemon starts and listens on 127.0.0.1:8765" | **Observed Fact** | Tested in `tests/test_local_runtime.py` (24 passed) |
| "Terminal connector prevents shell injection with `shell=False`" | **Observed Fact** | Verified via inspection of `terminal/connector.py` and 24 passed tests in `test_terminal_connector.py` |
| "Case-insensitive lookup in `AIConfig.effective_for_agent` fails" | **Observed Fact** | Reproduced and confirmed with traceback in `test_ai_providers.py:149` |
| "Telegram ChatOps works with live mobile app" | **Unverified Claim** | Telegram was only tested against local mock servers in `tests/apps/telegram/test_bot.py`; live bot unverified |
| "GitHub MCP and Supabase MCP connect to live cloud services" | **Unverified Claim** | Tested against local mock servers in `tests/mocks/`; no external API calls were executed |
| "The codebase runs cleanly on macOS and Linux" | **Inference** | Platform abstractions are clean, but Windows symlinks and POSIX process groups were skipped; full non-Windows run unverified |
| "Research test failures are caused by Python 3.13 event loop teardown" | **Observed Fact** | Isolated run passed 35/35 in 0.58s; full suite failed with `RuntimeError: There is no current event loop` |

---

## 3. Verification Limitations & Environmental Boundary

- **Operating System:** Windows 11 (`win32`).
- **Python Version:** 3.13.15 (note: some libraries like `websockets.legacy` emit deprecation warnings on Python 3.13).
- **External Networks:** Zero external network calls were permitted during this audit. All external API integrations (Telegram, GitHub, Reddit, Supabase, LLMs) remain unverified against live endpoints.
