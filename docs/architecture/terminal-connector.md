# AgentBridge — Secure Local Terminal Connector (Phase 7.8B)

## 1. Architectural Overview & Objective

The **Terminal Connector** (`TerminalConnector`) provides secure, bounded, local command-execution capabilities conforming to the canonical AgentBridge `Connector` contract.

Identity & Configuration:
- `connector_id`: `terminal-local`
- `provider`: `terminal`
- `type`: `ConnectorType.CLI`
- `transport_hint`: `cli`

### Fundamental Distinction: Terminal Connector vs MCP Connectors

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             AgentBridge CONNECTOR REGISTRY                        │
├──────────────────────────────────────┬──────────────────────────────────────┤
│        MCP CONNECTORS                │         TERMINAL CONNECTOR           │
│   (e.g. GitHub MCP Connector)        │       (Local Terminal Connector)     │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ • Transport: JSON-RPC over stdio /   │ • Transport: Native OS Process via   │
│   Streamable HTTP                    │   asyncio.create_subprocess_exec     │
│ • Protocol: Model Context Protocol   │ • Protocol: Direct Argument Array    │
│ • Target: External/remote servers or │ • Target: Local host operating       │
│   sandboxed tools                    │   system executables                 │
│ • Execution: Delegated to server     │ • Execution: Locally supervised      │
│ • Boundary: Server-defined tools     │ • Boundary: PolicyEngine + Workspaces│
│ • NOT a shell or terminal            │ • Strictly shell=False, bounded      │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

The Terminal Connector is **NOT** an MCP server and is **NOT** a fake MCP provider. It executes local CLI binaries directly under native asynchronous process supervision while enforcing strict safety invariants.

---

## 2. Safe Execution Model: Strict `shell=False`

Arbitrary shell execution is a high-risk security hazard on developer workstations. The Terminal Connector enforces an uncompromising execution invariant:

1. **Native Asynchronous API Only:**
   ```python
   asyncio.create_subprocess_exec(
       executable,
       *args,
       cwd=str(resolved_cwd),
       env=controlled_env,
       stdin=asyncio.subprocess.DEVNULL,
       stdout=asyncio.subprocess.PIPE,
       stderr=asyncio.subprocess.PIPE,
   )
   ```
2. **NEVER `shell=True`:** No shell string parsing, no shell interpreters invoked implicitly.
3. **No Shell Token Injection:** Any executable name or argument containing `;`, `&&`, `||`, `|`, `>`, `>>`, `<`, `&`, `` ` ``, `$()`, `${}`, `\n`, or `\r` is rejected before process creation.
4. **Shell Wrapper Prohibition:** Passing inline execution strings through shell interpreters (`bash -c`, `sh -c`, `cmd.exe /c`, `powershell.exe -Command`, `pwsh -c`) is explicitly forbidden and rejected with `ConnectorPermissionError`.
5. **Preserved Argument Boundaries:** Arguments are passed as typed elements in an immutable array; word-splitting, parameter expansion, and globbing never occur at the shell level.

---

## 3. Command Authorization & Risk Classification

All commands pass through the authoritative [`PolicyEngine`](../../sutra/policy_engine.py) permission boundary prior to acquiring workspace locks or spawning processes.

### 3.1 Precedence & Classification Rules

1. **CRITICAL Invariant Guard:**
   Commands matching dangerous patterns ([`CRITICAL_COMMAND_PATTERNS`](../../sutra/permissions.py))—such as `git push --force`, `rm -rf`, disk format, or recursive permission overwrites—are classified as `PermissionRiskLevel.CRITICAL`.
   - **Immutable Rule:** Persistent policies can **never** bypass CRITICAL operations.
   - **Requirement:** Every execution requires an explicit single-use `ALLOW_ONCE` decision tied to the unique `request_id`.
2. **Safe Read-Only Operations (`LOW` Risk):**
   Known non-destructive queries (e.g. `git status`, `git diff`, `git log`, `python --version`, `node --version`, directory listings) and commands assigned to the `safe_read_only` profile are classified as `LOW` risk.
   - Auto-approved by default when no blocking policy is configured.
3. **Mutating & Unknown Commands (`HIGH` Risk):**
   Project builds, test runs, file modifications, git commits, and unclassified binaries are classified as `HIGH` risk.
   - Requires explicit grant (`ALLOW_ONCE` or `ALLOW_SESSION`) from the operator.
4. **No Spawn on Denial:**
   A denied or unauthorized request raises `ConnectorPermissionError` immediately; child processes are **never** spawned.

---

## 4. Workspace Confinement & Boundary Protection

Commands execute strictly within an authorized set of workspace roots configured at connector registration.

### 4.1 Boundary Enforcements

- **Canonical Resolution:** Working directories are canonicalized via [`_canonical`](../../sutra/security.py) (resolving symlinks and case normalization).
- **Confinement Check:** The target directory must exist, be a directory, and reside within one of the declared `workspace_roots`.
- **Filesystem Root Rejection:** Targeting root directories (`/` on Unix, drive roots like `C:\` on Windows) is rejected.
- **Path Traversal Escape Protection:** Arguments containing `..` are validated relative to `cwd`; if the resolved path escapes the workspace root, execution is rejected.
- **Sensitive Credential Protection:** Arguments targeting sensitive credential files or directories (such as `.ssh`, `id_rsa`, `id_ed25519`, `.aws`, `.gnupg`, `credentials`) are rejected with `ConnectorPermissionError`.

### 4.2 Workspace Lock Integration

When `lock_workspace=True` is specified:
- The connector acquires a directory-level exclusive write lock via [`WorkspaceLockManager`](../../sutra/workspace_lock.py).
- If another session holds an overlapping lock, `WorkspaceConflictError` is normalized into a `ConnectorError` with `ConnectorErrorCode.PROVIDER_ERROR`.
- Locks are released safely in a `finally` block upon completion, error, timeout, or cancellation.

---

## 5. Controlled Environment Policy & Secret Sanitization

Subprocesses never inherit unrestricted parent process environment variables.

### 5.1 Environment Whitelist

The connector builds a minimal execution environment via `build_controlled_environment`:
- **Windows Baseline (`SAFE_ENV_WHITELIST_NT`):** `PATH`, `SYSTEMROOT`, `SYSTEMDRIVE`, `COMSPEC`, `TEMP`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMFILES`.
- **POSIX Baseline (`SAFE_ENV_WHITELIST_POSIX`):** `PATH`, `HOME`, `LANG`, `TMPDIR`, `USER`, `SHELL`, `TERM`.
- **Secret Scrubbing:** Any variable whose name matches sensitive patterns (`token`, `secret`, `password`, `key`, `cred`, `auth`, `bearer`, `sutra_`, `github_`) is stripped from the inherited environment.
- **Explicit References:** Callers can supply specific non-sensitive custom environment variables; any supplied sensitive keys are filtered out.

### 5.2 Secret Redaction in Arguments & Outputs

All stdout, stderr, exception messages, and recorded tool call arguments pass through centralized [`redact_secrets`](../../sutra/redaction.py):
- Private key blocks replaced with `[REDACTED PRIVATE KEY]`.
- GitHub tokens, API keys, JWTs replaced with `[REDACTED TOKEN]`.
- Authorization headers replaced with `Authorization: [REDACTED AUTH TOKEN]`.
- Raw secret-bearing arguments and output are **never** persisted to disk or stored in SQLite.

---

## 6. Resource Limits & Process Tree Cleanup

### 6.1 Configurable Bounds

- `default_timeout`: Bounded execution timeout (default: 30.0s).
- `concurrency_limit`: Maximum simultaneous running commands governed by `asyncio.Semaphore` (default: 5).
- `max_stdout_bytes` / `max_stderr_bytes`: Maximum captured stream sizes (default: 1MB); outputs exceeding bounds are cleanly sliced and flagged with `truncated=True`.

### 6.2 Deep Process Tree Termination

When a command times out or is cancelled, child processes and any spawned descendants are terminated safely via `kill_process_tree`:
1. **Graceful Termination:** Issues `proc.terminate()` and awaits exit.
2. **Forceful Tree Kill:**
   - **Windows:** Executes `taskkill /F /T /PID <pid>` to kill child process trees cleanly.
   - **POSIX:** Issues `proc.kill()` and awaits cleanup.
3. **No Zombie Processes:** Prevents leaked background jobs or orphaned children.

---

## 7. Command Profiles

Command profiles define structured execution templates:

```python
@dataclass(frozen=True)
class CommandProfile:
    name: str
    description: str
    allowed_executables: frozenset[str]
    allowed_argument_patterns: tuple[re.Pattern, ...]
    denied_argument_patterns: tuple[re.Pattern, ...]
    default_timeout: float
    allowed_env_vars: frozenset[str]
    read_only: bool
```

### Built-in Profiles:
1. `safe_read_only`: Diagnostic and read-only inspection commands (`git status`, `git diff`, `ls`, `dir`, `echo`, `pwd`). Rejects mutating git subcommands (`commit`, `push`, `merge`, `rebase`).
2. `project_test`: Test runners (`pytest`, `npm test`, `cargo test`) with extended timeouts (120s).

---

## 8. CLI Usage Examples

### Adding the Terminal Connector
```bash
# Add terminal connector bound to the current project workspace
sutra connectors add terminal-local --type cli --workspace /path/to/project

# Add with custom executable allowlist
sutra connectors add terminal-local --type cli --workspace . --options allowed_executables=git,python,pytest
```

### Inspecting Status and Health
```bash
# Verify connector status
sutra connectors status terminal-local

# Check exposed tools
sutra connectors tools terminal-local
```

### Testing the Connector
```bash
# Run non-destructive verification
sutra connectors test terminal-local
```

### Removing / Disconnecting
```bash
sutra connectors remove terminal-local
```

---

## 9. Limitations & Explicit Non-Goals

1. **No Interactive TTY / Indefinite Services:** The connector only runs bounded, non-interactive commands that produce an exit status or reach a configured timeout. Interactive programs (e.g. `vim`, `nano`, `python` REPL) are unsupported.
2. **No Arbitrary Shell Script Execution:** Does not accept shell scripts or raw pipeline strings.
3. **No Remote Execution:** Executes strictly on the local host machine within authorized workspace directories.
4. **Not a Hardware Sandbox:** Does not claim an isolated virtual machine or container boundary; safety relies on working directory confinement, path validation, controlled environments, and PolicyEngine gates.
