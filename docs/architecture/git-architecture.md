# AgentBridge Git & GitHub Architecture

## 1. Architectural Philosophy

AgentBridge Phase 6 implements **Safe Git & GitHub Integration**, enabling mobile engineers to inspect, review, stage, commit, push, and open pull requests remotely via Telegram ChatOps while enforcing strict permission and workspace security guardrails.

The foundation of this architecture is the **strict separation** of:

1. **Local Git Subsystem**:
   - Source of truth is the local `.git` repository and working tree on the host workstation.
   - Handles: repository detection, porcelain-v1 status parsing, bounded diff retrieval, branch topology, interactive file staging, safe commit execution, and remote push.
   - Operations execute via local bounded subprocesses (`git`) without passing through external networks or APIs.

2. **GitHub Remote Subsystem**:
   - Operates against the GitHub REST API (`api.github.com`).
   - Handles: remote URL detection, repository metadata, and Pull Request creation and inspection.
   - Completely decoupled from local Git operations: if a repository is not hosted on GitHub (e.g., GitLab, Bitbucket, self-hosted), all local Git features continue functioning normally.

```
+-------------------------------------------------------------------------+
|                              Telegram ChatOps                           |
|      [Status]  [Diff]  [Branches]  [Stage]  [Commit]  [Push]  [PR]      |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
|                    Control Plane & Permission Engine                    |
|   - Policy Precedence Chain (CRITICAL > SESSION > PROJECT > GLOBAL)      |
|   - High-Risk Human Approval Gating (Commit, Push, PR)                  |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
|                             Local Runtime                               |
|   - Workspace Lock Manager (Concurrent write race condition protection) |
|   - Project boundary containment checks                                 |
+-------------------------------------------------------------------------+
                                    │
                  ┌─────────────────┴─────────────────┐
                  ▼                                   ▼
+-----------------------------------+   +---------------------------------+
|       Local Git Adapter           |   |       GitHub API Adapter        |
|  (sutra/git/local.py)             |   |  (sutra/git/github.py)          |
|  - .git / Working tree            |   |  - Remote detection             |
|  - Porcelain v1 parser            |   |  - REST Pull Request creation   |
|  - Bounded secret-redacted diff   |   |  - Token discovery              |
|  - Staging & commit               |   |                                 |
|  - Protected push                 |   |                                 |
+-----------------------------------+   +---------------------------------+
```

---

## 2. GitHub MCP Boundary

> [!IMPORTANT]
> **GitHub MCP is NOT required for core Phase 6 Git operations.**
> Local Git commands (`status`, `diff`, `branch`, `stage`, `commit`, `push`) communicate directly with the local `.git` repository on your machine.
> The GitHub adapter connects directly via lightweight standard HTTPS REST APIs for Pull Request creation. If MCP connectors are configured in the future, GitHub MCP remains an optional external connector and will never be a prerequisite for local Git functionality.

---

## 3. Security Guardrails

1. **No Raw Shell Commands**: Users cannot run arbitrary shell syntax or raw `/git <command>`. All interactions flow through strictly validated structured requests (`git.status`, `git.diff`, `git.stage`, `git.commit`, `git.push`, `git.pr_create`).
2. **Force-Push Invariant**: Destructive force-pushes (`git push --force`, `--force-with-lease`, `-f`) are strictly blocked by safety invariants at both the adapter and permission engine layers.
3. **Secret Redaction**: Diffs and commit logs are automatically sanitized via AgentBridge's centralized secret redaction filter before transmission to mobile clients.
4. **Workspace Concurrency Locking**: Mutating Git operations (`stage`, `commit`, `push`) verify that no active autonomous agent session holds an exclusive write lock on the project, preventing corrupted working trees.
5. **No IDE Required**: AgentBridge runs entirely on the host workstation background runtime. No IDE (VS Code, Cursor, JetBrains) is required to inspect diffs, commit, or push.
