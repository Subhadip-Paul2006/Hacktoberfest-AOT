# Implementation and Feature Matrix

This table distinguishes repository implementation from tested release support. “Implemented” does not mean production-ready or live-provider verified.

| Area | Repository evidence | Current status | Release note |
|---|---|---|---|
| Local Python package and `sutra` CLI | `pyproject.toml`, `sutra/cli.py` | Implemented; CLI help and import paths work on Windows/Python 3.13 | Build/install and supported-version matrix still need release CI. |
| Local control plane and device pairing | `sutra/control_plane.py`, local TLS/session tests | Automated local integration coverage | No production deployment or cross-platform validation. |
| Workspace/project discovery and file preview | `sutra/workspace.py`, codebase explorer modules and tests | Implemented with automated coverage | Manual large-file and access-root validation remains. |
| Permission policies and audit store | `sutra/permissions.py`, `sutra/policy_engine.py`, `sutra/policy_store.py`, `sutra/activity_store.py` | Implemented and tested in local suite | MCP resources/prompts currently bypass the policy and audit boundary. |
| Generic CLI agent adapter | `sutra/agents/generic_cli.py` | Implemented; forced process cleanup hardened in this pass | POSIX process-group behavior needs Linux/macOS execution. |
| Terminal connector | `sutra/connectors/terminal/` | Implemented with bounded capture, policy checks, workspace CWD validation | Argument path confinement is incomplete for absolute paths; do not claim full filesystem sandboxing. |
| MCP protocol core and HTTP/stdio transports | `sutra/connectors/mcp/` | Implemented with local/mock transport tests | Resource and prompt calls need centralized authorization/audit. |
| GitHub REST integration | `sutra/git/`, GitHub collaboration modules | Implemented with mocked tests | No live GitHub API validation in this audit. |
| GitHub MCP connector | `sutra/connectors/mcp/github.py` | Implemented with local/mock MCP tests | No live GitHub MCP validation. |
| Supabase MCP and credential/OAuth helpers | `sutra/connectors/mcp/supabase.py`, `sutra/credentials.py` | Implemented with local/mock server and credential tests | No live Supabase project validation; inspect provider policy before release. |
| Telegram app and callback handling | `sutra/apps/telegram/` | Implemented; local tests; callback false-success fixed | No live Telegram bot validation. |
| Slack and Discord | Docs describe future integrations | Planned / not implemented as supported integrations | Exclude from MVP capability claims. |
| Windows | Current audit host | Pre-change baseline suite passed; post-change full run was interrupted at approximately 51%, with two visible failures and no tracebacks | Post-change suite outcome is unknown; Python 3.13 only in this environment. |
| Linux and macOS | No runner available in this audit | Unverified | POSIX-only cleanup test skipped on Windows. |
| CI, lint, type checking, release packaging | No `.github` workflow or project tool config found | Missing release gates | Add CI before declaring a release candidate. |

## Naming and configuration observations

The package metadata and console command identify the product as `sutra-local` / `sutra`. Existing `AGENTBRIDGE_HOME` handling and historical AgentBridge references are compatibility/documentation strings, not a reason to rename the product. `.env.example` currently sets `SUTRA_HOME=~/.agentbridge`, while code/docs use `~/.sutra`; align this example before release.
