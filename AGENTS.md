# AGENTS.md: Operational Protocols & Mandatory Graphify Rules

> **File:** `AGENTS.md`  
> **Purpose:** Authoritative instructions, coding standards, and token-optimization protocols for AI coding agents operating on SŪTRA.  
> **Audience:** All AI coding assistants (Google Antigravity, OpenAI Codex, Cursor, Claude Code, Aider).  
> **Status:** Active & Enforced.  

---

## Table of Contents

- [1. Operational Principles for AI Agents](#1-operational-principles-for-ai-agents)
- [2. Mandatory Graphify Token Reduction Protocol](#2-mandatory-graphify-token-reduction-protocol)
- [3. Security & Architecture Invariants](#3-security--architecture-invariants)
- [4. Coding & Refactoring Constraints](#4-coding--refactoring-constraints)
- [5. Verification & Testing Standards](#5-verification--testing-standards)
- [6. Document & Diagram Preservation Rules](#6-document--diagram-preservation-rules)

---

## 1. Operational Principles for AI Agents

1. **Identity & Naming:** The product identity is **SAMANVAYA · SŪTRA** (legacy: `AgentBridge`). Python package and module identity is `sutra`. Do not introduce arbitrary renames or package structural rewrites.
2. **Read Before Changing:** Always inspect relevant documentation (`PRD.md`, `TRD.md`, `SECURITY.md`, `AI_INSTRUCTIONS.md`), current Git state (`git status`), and callers before modifying code.
3. **Preserve User Work:** Never overwrite, revert, reset, or discard uncommitted developer work in the working tree.
4. **Minimal, Tested Changes:** Implement surgical, minimal fixes. Add regression tests for every bug fix.
5. **No Hallucinations:** Never claim something works because a class or method exists. Verify through test execution and code-path tracing.

---

## 2. Mandatory Graphify Token Reduction Protocol

This repository maintains an active Graphify knowledge graph in [`graphify-out/`](file:///d:/practice-file/graphify-out/) (5,100+ nodes, 13,800+ edges, 217 communities, 155 wiki documents).

To eliminate context window bloat and achieve **60–85% token reduction** on every prompt:

### Rule A: Graph-First Discovery (Zero Raw File Sweeps)
- **NEVER** run broad workspace-wide grep searches or dump multi-hundred line directory listings into the context window.
- **ALWAYS** query Graphify first when investigating symbols, components, call flows, or concepts:
  ```bash
  # Broad context BFS traversal (exact definitions: src=<path> loc=L<line>)
  python -m graphify query "<concept or question>"

  # Focused symbol inspection
  python -m graphify explain "<symbol>"

  # Relationship tracing between two components
  python -m graphify path "<componentA>" "<componentB>"
  ```

### Rule B: Surgical Code Slicing (No Full-File Dumps)
- Once Graphify identifies the target file and line (`loc=Lxx`), use `view_file` with strict `StartLine` and `EndLine` (typically 30–60 lines around the target).
- Never load entire 500+ line files unless modifying the entire file.

### Rule C: Pre-Compiled Wiki for Architecture
- For system-level or module-level questions, navigate [`graphify-out/wiki/index.md`](file:///d:/practice-file/graphify-out/wiki/index.md) before reading source directories.

### Rule D: Zero-Token AST Synchronization
- After modifying Python code files in this session, keep the knowledge graph synchronized:
  ```bash
  python -m graphify update .
  ```
  *(AST extraction only, 0 LLM API token cost).*

---

## 3. Security & Architecture Invariants

1. **Local Data Sovereignty:** Private source code and files MUST stay on the developer's physical machine. Never upload raw code to cloud relays.
2. **Zero Shell Execution:** Prohibit `os.system()` and `subprocess.Popen(shell=True)`. All local terminal commands must be tokenized as discrete argument arrays (`["git", "status"]`) with `shell=False`.
3. **Strict Path Confinement:** Enforce canonical normalization (`Path.resolve(strict=False)`) and recursive URL decoding (4 passes) on all file operations. Symlinks pointing outside registered workspace boundaries MUST be rejected.
4. **Zero Secret Leakage:** Never print, log, or commit `.env`, private keys (`*.pem`, `*.key`), certificates (`*.crt`), or API tokens (`sk-`, `ghp_`).
5. **Deterministic Policy Model:** Operations are classified into `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`. `CRITICAL` operations strictly require human double-confirmation and can **NEVER** be auto-approved or persisted to `ALLOW_ALWAYS`.

---

## 4. Coding & Refactoring Constraints

- **Scope Freeze:** Do NOT introduce new product features, new connectors, or unrelated services. Focus on stabilizing, verifying, and testing the existing MVP.
- **Scaffold Integrity:** Preserve Slack and Discord scaffolds as planned future modules; do not attempt to turn them into operational connectors during maintenance tasks.
- **No Mandatory Docker:** SŪTRA is designed to run natively on the developer's workstation with access to local tools and approved processes. Do not make Docker mandatory.
- **Human-Readable Code:** Use clear, descriptive names for functions, methods, and classes. Maintain documentation and type annotations.

---

## 5. Verification & Testing Standards

- **Truthful Reporting:** Only report tests as passed if you actually executed them and captured output.
- **Run Baseline Commands:**
  ```bash
  # Run the full automated test suite
  python -m pytest -q

  # Run focused connector tests
  python -m pytest tests/test_connectors.py tests/test_mcp_client.py tests/test_terminal_connector.py -v
  ```
- **Failure-Mode Coverage:** Add tests covering timeouts, cancellation, duplicate requests, network dropped connections, and permission rejections.

---

## 6. Document & Diagram Preservation Rules

- **Preserve Existing Architecture Diagrams:** The 41 JSON diagram sources located in `docs/architecture/diagrams/` (`.architecture.json`, `.workflow.json`, `.sequence.json`, `.lifecycle.json`) are authoritative project assets. Do not casually overwrite or mass-delete them.
- **Documentation Headers:** All major documents must begin with a standardized header indicating file path, purpose, audience, and status.
- **Cross-Reference:** Complement [AI_INSTRUCTIONS.md](AI_INSTRUCTIONS.md), [SECURITY.md](SECURITY.md), and [TRD.md](TRD.md).
