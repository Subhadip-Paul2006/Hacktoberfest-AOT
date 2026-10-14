# SŪTRA Production Readiness — Phase 0 Baseline Report

> **File:** `reports/production-readiness/PHASE_0_BASELINE.md`
> **Purpose:** Preservation checkpoint and reproducible baseline for the SŪTRA production-readiness effort.
> **Audience:** Release engineering, senior developers, security reviewers.
> **Status:** Active — Phase 0 Complete.
> **Generated:** 2026-10-14T11:36-11:55+05:30
> **Author:** Automated audit — no application code modified, nothing pushed.

---

## 1. Repository and Environment Snapshot

| Parameter | Observed Value |
|---|---|
| **OS / Platform** | Windows (win32 / Windows 11) |
| **Python (system)** | 3.13.15 — `C:\Users\SUBHADIP PAUL\AppData\Local\Programs\Python\Python313\python.exe` |
| **Python (venv)** | 3.14.0 — `d:\practice-file\.venv\Scripts\python.exe` — **pytest NOT installed in venv** |
| **pytest Version** | 9.1.1 (pluggy 1.6.0) — runs only under system Python 3.13.15 |
| **Project Name** | `sutra-local` v0.1.0 — installed (editable) as `agentbridge-local` |
| **Git Branch** | `main` |
| **HEAD Commit (local)** | `c9917e5afdf55f19214645dfcb1441f21d5fad11` — "first commit" (2026-10-14 10:38:27 +0530) |
| **Remote** | `origin` -> `https://github.com/Subhadip-Paul2006/Hacktoberfest-AOT.git` |
| **Remote Ahead** | `origin/main` is 2 commits ahead (2c5909c, 246e10e — README-only changes) |
| **Working Tree** | 260+ files untracked; zero uncommitted modifications to tracked files |
| **Installed Package** | `agentbridge-local` (editable) at `D:\practice-file` |
| **Pytest Plugins** | `anyio-4.12.1`, `langsmith-0.12.2`, `asyncio-1.4.0`, `cov-7.1.0` |

### Entry Points

| Script | Path | Role |
|---|---|---|
| `sutra` CLI | `sutra/cli.py::main` | Primary user entry point |
| `sutra_entry.py` | root | Alternate bare launcher |
| `agentbridge_entry.py` | root | Legacy entry (legacy identity) |
| Loopback daemon | `sutra/runtime.py` | 127.0.0.1:8765 |
| Control plane | `sutra/control_plane.py` | TLS FastAPI + WSS |

---

## 2. Git Preservation Status

**CRITICAL / P0 — DEF-001 (Untracked Working Tree):**
Only `README.md` is committed in git HEAD. Every other source file —
sutra/, tests/, docs/, scripts/, and all configuration files — is UNTRACKED.
Any destructive git operation (git clean -fd, git checkout ., git reset --hard)
would permanently erase 260+ files with zero recovery path.

### Git History

| Commit | Hash | Message |
|---|---|---|
| Local HEAD | c9917e5 | "first commit" |
| origin/main+1 | 246e10e | "Fix corrupted README content" |
| origin/main+2 | 2c5909c | "Update README.md" |

Local is BEHIND remote by 2 commits. Remote contains only README updates.
The SŪTRA implementation exists exclusively in the untracked local working tree.

### Preservation Actions Taken This Session

- NO git reset, git checkout, git clean, or git stash executed.
- NO branch switches performed.
- NO git pull or git fetch executed.
- NO git push executed.
- Working tree verified identical to state at session start.

---

## 3. Tracked / Untracked / Ignored File Summary

### Tracked Files (in git HEAD)

| File | Notes |
|---|---|
| README.md | Only committed file (349 lines, added in c9917e5) |

### Untracked Files (at immediate risk of loss on git clean -fd)

```
.dockerignore       .env.example        .gitattributes
.gitignore          .kilo/              .mypy_cache/
.ruff_cache/        AGENTS.md           AI_INSTRUCTIONS.md
AUDIT_IMPLEMENTATION_REPORT.md          CODE_OF_CONDUCT.md
Dockerfile          PRD.md              SECURITY.md
SETUP.md            TRD.md              USAGE.md
agentbridge_entry.py config.example.yaml docker-compose.yml
docs/               pyproject.toml      reports/
scripts/            sutra/              sutra_entry.py
sutra_local.egg-info/ tests/            uv.lock
```

Total: ~28 top-level items encompassing 260+ files.

### Gitignore Coverage (verified via `git check-ignore -v`)

| Pattern | Matched Paths | Verified |
|---|---|---|
| `.env` | .env | YES |
| `.env.*` + `!.env.example` | .env.local, .env.production | YES |
| `__pycache__/` | All bytecode cache dirs | YES |
| `.pytest_cache/` | .pytest_cache/ | YES |
| `.venv/`, `venv/` | .venv/ | YES |
| `build/`, `dist/` | build/, dist/ | YES |
| `agentbridge_local.egg-info/` | agentbridge_local.egg-info/ | YES |
| `*.sqlite3*` | Database files | YES |
| `.sutra/` | State directory | YES |
| `.dev-certs/`, `certs/` | Certificate directories | YES |
| `graphify-out/` | Knowledge graph output | YES |
| `.agents/`, `.codex/`, `.gemini/` | AI agent tooling dirs | YES |
| `GEMINI.md`, `PHASES.md` | AI agent instruction files | YES |

### GITIGNORE GAP IDENTIFIED

`sutra_local.egg-info/` is NOT covered by .gitignore.
Only `agentbridge_local.egg-info/` (old package name) is ignored on line 11.
The new package name egg-info directory would be accidentally staged by `git add .`.
Fix: Add `sutra_local.egg-info/` to .gitignore before any commit.

---

## 4. Secret-Scanning Result

VALUES ARE NEVER PRINTED. Only key names and locations reported.

### .env File

.env exists on disk and is correctly excluded by .gitignore rule L1
(verified: `git check-ignore -v .env` -> `.gitignore:1:.env .env`).

Keys present in .env (values REDACTED):

| Line | Key | Classification |
|---|---|---|
| L1 | APP_ENV | Low |
| L2 | LOG_LEVEL | Low |
| L3 | AGENTBRIDGE_HOME | Low |
| L6 | AGENTBRIDGE_DEVICE_ID | Medium |
| L7 | AGENTBRIDGE_DEVICE_TOKEN | HIGH — bearer credential |
| L8 | AGENTBRIDGE_CONTROL_PLANE_URL | Low |
| L9 | AGENTBRIDGE_GATEWAY_URL | Low |
| L10 | AGENTBRIDGE_RECONNECT_MAX_BACKOFF | Low |
| L11 | AGENTBRIDGE_CONTROL_PLANE_TOKEN | HIGH — bearer credential |
| L12 | AGENTBRIDGE_TLS_CERTFILE | Medium |
| L13 | AGENTBRIDGE_TLS_KEYFILE | HIGH — private key path |
| L14 | AGENTBRIDGE_TLS_CA_FILE | Medium |
| L18 | TELEGRAM_BOT_TOKEN | HIGH — live API token |
| L19 | TELEGRAM_ALLOWED_USER_IDS | Medium |
| L20 | TELEGRAM_POLLING_MODE | Low |
| L23 | LLM_PROVIDER | Low |
| L24 | OLLAMA_BASE_URL | Low |
| L25 | OLLAMA_MODEL | Low |
| L28 | OPENAI_API_KEY | HIGH — cloud API key |
| L29 | ANTHROPIC_API_KEY | HIGH — cloud API key |
| L30 | GEMINI_API_KEY | HIGH — cloud API key |
| L33 | GITHUB_TOKEN | HIGH — GitHub PAT |
| L34 | GITHUB_DEFAULT_OWNER | Low |
| L37 | JULES_CLI_ENABLED | Low |
| L38 | JULES_AUTO_PULL_ON_MERGE | Low |

### Verdict

| Target | Result |
|---|---|
| .env in git index | CLEAN — excluded, not staged |
| .env.example content | CLEAN — placeholder values only |
| Private key files at root (*.pem, *.key) | CLEAN — none found |
| Secret values in this report | NONE — zero values disclosed |
| sutra/ source embedded secrets | Deferred to Phase 1 static analysis |

WARNING: .env contains live API tokens (Telegram, OpenAI, Anthropic, Gemini, GitHub PAT).
Correctly excluded from git. Risk: local exposure if directory is in cloud sync or screenshared.

---

## 5. Baseline Test Results

### Execution Record

| Parameter | Value |
|---|---|
| Command | `python -m pytest d:\practice-file\tests -q --tb=no --no-header` |
| Python | 3.13.15 (system) |
| pytest | 9.1.1 |
| Collected | 878 tests across 37 test modules |
| Passed | 858 |
| Failed | 17 |
| Skipped | 3 |
| Subtests Passed | 13 |
| Warnings | 23 |
| Duration | 326.04s (5 min 26 sec) |
| Exit Code | 1 |

### Failure 1: DEF-002 AIConfig Case-Insensitive Lookup (1 test)

```
FAILED tests/test_ai_providers.py::TestEffectiveForAgent::test_case_insensitive_agent_id
  AssertionError: assert 'openai' == 'anthropic'
```

Root cause: AIConfig.effective_for_agent() in sutra/ai_providers.py:114
calls .casefold() on the lookup key, but agent_overrides dictionary keys
are stored un-normalized at construction time. Capitalized keys like
"Research" or "Antigravity" fail lookup and silently fall back to global LLM default.
Independently reproduced: Yes (1 failed, 25 passed in 2.63s in isolated run).

### Failure 2: DEF-003 Asyncio Event-Loop Contamination (16 tests)

All in tests/test_research.py::
  TestGitHubAdapter::{test_search_returns_results, test_rate_limited_response,
    test_api_error_response, test_network_failure, test_credential_not_logged}
  TestRedditAdapter::{test_search_returns_community_results, test_rate_limited}
  TestHackerNewsAdapter::test_search_returns_hn_results
  TestResearchAgentIntegration::{8 tests}
Error: RuntimeError: There is no current event loop in thread 'MainThread'

Root cause: tests/test_research.py:36 uses deprecated
asyncio.get_event_loop().run_until_complete(coro). Under Python 3.13,
after prior async tests close the thread-local loop, this raises RuntimeError
instead of creating a new loop.

Isolation proof (confirmed this session):
  python -m pytest tests/test_research.py -q --tb=no
  -> 35 passed, 1 warning in 0.71s
This is a test-order contamination failure, NOT an application defect.

### Skipped Tests — All Legitimate

| Test | Reason |
|---|---|
| test_hardening_regressions.py:74 | POSIX os.setpgid/os.killpg — not applicable on Windows |
| test_local_runtime.py:60 | Symlink creation requires Windows Developer Mode |
| test_codebase_explorer.py:191 | Same Windows symlink privilege constraint |

### Warnings

| Source | Count | Classification |
|---|---|---|
| websockets.legacy deprecation (Python 3.13) | 17 | Expected; will escalate in future websockets release |
| PytestUnraisableExceptionWarning (unclosed transport) | 2 | Test cleanup artifact |
| RuntimeWarning: coroutine never awaited | 4 | Side-effect of test_research.py loop teardown |

---

## 6. Differences from Previous Audit

Previous audit: reports/final-audit/FINAL_AUDIT_REPORT.md
Timestamp: 2026-10-14T11:00+05:30, same HEAD c9917e5.

| Metric | Previous Audit | Phase 0 Run | Delta |
|---|---|---|---|
| Collected | 878 | 878 | Identical |
| Passed | 858 | 858 | Identical |
| Failed | 17 | 17 | Identical |
| Skipped | 3 | 3 | Identical |
| Duration | 436.18s | 326.04s | ~25% faster (resource variance) |
| Python | 3.13.15 | 3.13.15 | Same |
| pytest | 9.1.1 | 9.1.1 | Same |

Baseline is FULLY REPRODUCIBLE. Failure names and root causes match exactly.
No regression. No improvement.

### New Findings vs. Prior Audit

| Finding | Status |
|---|---|
| `sutra_local.egg-info/` not in .gitignore | NEW — not noted in prior audit |
| System Python 3.13.15 vs venv Python 3.14.0 mismatch; pytest missing from venv | NEW |
| origin/main 2 commits ahead (README only) | CONFIRMED — consistent with prior audit |

---

## 7. Risks and Blockers

### P0 — Critical

| ID | Risk | Impact | State |
|---|---|---|---|
| RISK-001 | 260+ files untracked — implementation not in git | Irrecoverable loss on clean checkout | UNMITIGATED |
| RISK-002 | Local HEAD 2 commits behind origin/main | git pull could create merge conflict touching working tree | UNMITIGATED |

### P1 — High (Fix Before Phase 1)

| ID | Risk | Impact | State |
|---|---|---|---|
| RISK-003 | AIConfig casefold bug | Agent LLM overrides silently fail; wrong provider selected | Open (DEF-002) |
| RISK-004 | test_research.py asyncio loop contamination | CI reports 17 failures; CI gate blocked | Open (DEF-003) |
| RISK-005 | sutra_local.egg-info/ not in .gitignore | Build artifact accidentally committed | Gitignore gap |
| RISK-006 | pytest not in .venv; venv is Python 3.14 pre-release | Tests non-reproducible in venv | Environment gap |

### P2 — Medium (Fix Before Production)

| ID | Risk | Impact | State |
|---|---|---|---|
| RISK-007 | MCP read_resource/get_prompt bypass policy engine | Untrusted content without audit/approval | Architecture gap (DEF-005) |
| RISK-008 | Terminal connector lacks absolute path screening | Agent could read system files outside workspace | Security gap (DEF-006) |
| RISK-009 | 1,889 Ruff violations + 72 Mypy errors (prior audit) | Code quality debt | Unquantified in this phase |

### P3 — Low

| ID | Risk |
|---|---|
| RISK-010 | No CI configuration (GitHub Actions) |
| RISK-011 | 17 websockets.legacy deprecation warnings |
| RISK-012 | Remote README history mismatch |

---

## 8. Exact Commands Executed

```powershell
git -C "d:\practice-file" log --oneline -5
git -C "d:\practice-file" log --oneline --all
git -C "d:\practice-file" status --short
git -C "d:\practice-file" remote -v
git -C "d:\practice-file" branch -a
git -C "d:\practice-file" show --stat HEAD
git -C "d:\practice-file" ls-files --cached
git -C "d:\practice-file" ls-files --others --exclude-standard --directory
git -C "d:\practice-file" check-ignore -v .env .env.example config.yaml sutra_local.egg-info sutra_exports graphify-out .venv build dist
git -C "d:\practice-file" check-ignore -v sutra_local.egg-info
git -C "d:\practice-file" check-ignore -v agentbridge_local.egg-info
d:\practice-file\.venv\Scripts\python.exe --version   # -> Python 3.14.0
python --version                                       # -> Python 3.13.15
python -m pytest --version                             # -> pytest 9.1.1
python -m pytest d:\practice-file\tests -q --tb=no --no-header
# -> 17 failed, 858 passed, 3 skipped, 23 warnings, 13 subtests in 326.04s
python -m pytest d:\practice-file\tests\test_research.py -q --tb=no --no-header
# -> 35 passed, 1 warning in 0.71s
python -m pytest d:\practice-file\tests\test_ai_providers.py -q --tb=short --no-header
# -> 1 failed (test_case_insensitive_agent_id), 25 passed in 2.63s
```

No application code was modified during any of the above commands.

---

## 9. Safe Local Checkpoint Recommendation

HUMAN APPROVAL REQUIRED before executing any git staging or commit.

### Step A — Pre-Commit Safety Checks

1. Add `sutra_local.egg-info/` to .gitignore (after line 11).
2. Verify .env excluded: `git check-ignore -v .env`
   Expected: `.gitignore:1:.env   .env`
3. Verify no *.pem, *.key, or *secret* files exist outside ignored paths.

### Step B — Explicit File-by-File Staging (NO WILDCARDS)

```powershell
git add AGENTS.md AI_INSTRUCTIONS.md AUDIT_IMPLEMENTATION_REPORT.md
git add CODE_OF_CONDUCT.md Dockerfile SECURITY.md SETUP.md TRD.md PRD.md USAGE.md
git add GEMINI.md PHASES.md
git add .dockerignore .env.example .gitattributes .gitignore
git add agentbridge_entry.py sutra_entry.py config.example.yaml docker-compose.yml
git add pyproject.toml uv.lock
git add sutra/ tests/ docs/ scripts/ reports/
```

### Step C — Verify Before Commit

```powershell
git status
git diff --cached --name-only | Sort-Object
```
Manually review every file name before proceeding.

### Step D — Commit

```powershell
git commit -m "chore: preserve complete SUTRA implementation baseline (Phase 0)

Adds sutra/, tests/, docs/, scripts/, config templates, audit reports.
Baseline: 878 collected, 858 passed, 17 failed (2 known defects), 3 skipped.
No production code modified. Resolves DEF-001 (data-loss risk)."
```

### Step E — STOP. Do NOT push.

Human developer retains sole authority over push decisions.

### Files to Strictly Exclude

.env, .venv/, build/, dist/, graphify-out/, agentbridge_local.egg-info/,
sutra_local.egg-info/ (add to .gitignore first), __pycache__/, .pytest_cache/,
.agents/, .codex/, .gemini/, .mypy_cache/, .ruff_cache/, .kilo/

---

## 10. No-Change and No-Push Confirmation

1. NO application code was changed. Zero modifications to sutra/, tests/, docs/, scripts/.
2. NO destructive git action performed. No git reset, git checkout, git clean, git stash.
3. NOTHING pushed. No git push, no remote branch creation, no PR opened.
4. NO packages installed or upgraded. Existing system Python 3.13.15 environment used.
5. NO secrets printed. .env key names only extracted; zero values disclosed anywhere.
6. Working tree state at end of Phase 0 is identical to session start.
   Only new file: reports/production-readiness/PHASE_0_BASELINE.md (this file).

---

## Summary

| Category | Finding |
|---|---|
| Git State | 260+ files untracked; only README.md committed; origin 2 ahead (README only) |
| Baseline | 878 collected, 858 passed, 17 failed, 3 skipped — FULLY REPRODUCIBLE |
| Failures | (1) ai_providers.py casefold bug; (2) test_research.py asyncio loop contamination |
| Secrets | .env present with live creds; git-excluded; zero values disclosed |
| Gitignore Gap | sutra_local.egg-info/ not ignored — fix before any commit |
| Environment | pytest missing from .venv (Python 3.14.0 pre-release); tests require system Python 3.13.15 |
| Security | MCP resource/prompt bypasses audit engine (P2); terminal lacks absolute path screening (P2) |
| Audit Delta | Identical to FINAL_AUDIT_REPORT.md; 2 new environment findings |

---

## Next Steps for Phase 1

Preconditions before Phase 1 begins:
1. [Human-Approved] Developer authorizes checkpoint commit plan.
2. [Pre-commit] Add sutra_local.egg-info/ to .gitignore.
3. [Pre-commit] Stage and commit all untracked source (Steps B-D above).
4. [Phase 1] Fix DEF-002: normalize agent_overrides keys in AIConfig.__post_init__ (sutra/ai_providers.py:114).
5. [Phase 1] Fix DEF-003: replace asyncio.get_event_loop().run_until_complete() with asyncio.run() in tests/test_research.py:36.
6. [Phase 1] Resolve venv/Python mismatch — install pytest in .venv or repin to Python 3.13.
7. [Phase 1] Run Ruff + Mypy; produce lint baseline.
8. [Phase 1] Begin P2 security gap remediation (DEF-005, DEF-006).

Phase 1 must use the same evidence-first, preservation-first rules:
check -> fix -> test -> regression loop.
No feature work, no dependency upgrades, no architectural changes.
