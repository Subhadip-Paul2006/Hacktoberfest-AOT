# AgentBridge Sequence & Protocol Flows

## 1. Remote Command & Permission Approval Flow

This diagram illustrates how a blocked coding agent on a home PC communicates a permission request through AgentBridge to a developer on mobile Telegram and receives confirmation without requiring inbound ports.

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer (Telegram on Phone)
    participant TG as Telegram Bot API
    participant Cloud as AgentBridge Cloud Control Plane
    participant Daemon as AgentBridge Local Runtime (PC)
    participant Adapter as Agent Adapter Layer
    participant Agent as Coding Agent (Antigravity / Cursor)
    participant FS as Local Filesystem & Git

    Note over Daemon,Cloud: Persistent Outbound TLS WebSocket Active
    Agent->>Adapter: Requests Tool Call (git add backend/parser.py)
    Adapter->>Daemon: Intercept Permission Hook (Risk: MEDIUM)
    Daemon->>Cloud: WSS Frame: PERMISSION_REQUIRED (perm_101, cmd, risk)
    Cloud->>TG: sendMessage with Inline Keyboard
    TG->>Dev: ⚠️ Permission Required: "git add backend/parser.py" [Allow Once] [Deny]

    Dev->>TG: Taps [ Allow Once ]
    TG->>Cloud: callback_query (perm_101, ALLOW_ONCE)
    Cloud->>Daemon: WSS Frame: PERMISSION_DECISION (perm_101, ALLOW_ONCE)
    Daemon->>Adapter: Unblock Hook (Decision: ALLOW_ONCE)
    Adapter->>Agent: Tool Authorized
    Agent->>FS: Executes: git add backend/parser.py
    FS-->>Agent: Exit Code 0
    Agent-->>Adapter: Tool Completed Successfully
    Adapter-->>Daemon: Event: TOOL_SUCCESS
    Daemon-->>Cloud: WSS Frame: AGENT_PROGRESS
    Cloud-->>TG: editMessageText: ✅ Tool Approved & Executed
```

---

## 2. Jules Remote Asynchronous Workflow

This diagram highlights the fundamental operational difference between local agents (which execute against the local filesystem) and **Jules** (which operates asynchronously against remote GitHub repositories).

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer (Telegram)
    participant Cloud as AgentBridge Cloud
    participant Daemon as AgentBridge Local Runtime
    participant JulesAd as Jules Adapter (@google/jules)
    participant JulesCloud as Google Jules Service
    participant GitHub as GitHub Repository

    Dev->>Cloud: /jules new "Refactor auth middleware to async"
    Cloud->>Daemon: Dispatch Jules Task Frame
    Daemon->>JulesAd: jules remote new --repo owner/repo "..."
    JulesAd->>JulesCloud: Cloud Task Dispatched via Jules CLI / API
    JulesCloud->>GitHub: Clones Repo into Google Cloud Sandbox

    loop Polling & Telemetry
        JulesCloud-->>JulesAd: Session Logs & State (PLANNING -> CODING -> TESTING)
        JulesAd-->>Daemon: Structured Event (JULES_PROGRESS)
        Daemon-->>Cloud: Relay Event
        Cloud-->>Dev: Telegram Notification: "Jules running tests on branch jules/auth-refactor..."
    end

    JulesCloud->>GitHub: Pushes Commits & Opens Pull Request #42
    JulesCloud-->>JulesAd: Task Completed (PR #42 Ready)
    JulesAd-->>Daemon: JULES_COMPLETED (pr_url: github.com/.../pull/42)
    Daemon-->>Cloud: Notify Completion
    Cloud-->>Dev: 🚀 Jules Task Complete! PR #42 opened. [ Review Diff ] [ Pull Locally ]

    opt Developer triggers local pull
        Dev->>Cloud: Taps [ Pull Locally ]
        Cloud->>Daemon: Execute git pull origin jules/auth-refactor
        Daemon->>GitHub: Pull commits into local workspace
        Daemon-->>Dev: ✅ Local workspace updated with Jules branch.
    end
```

---

## 3. Safe Selective Git Staging & PR Workflow

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer (Telegram)
    participant Cloud as AgentBridge Cloud
    participant Daemon as Local Runtime
    participant Git as Local Git Subsystem
    participant GH as GitHub REST API

    Dev->>Cloud: /git changes
    Cloud->>Daemon: Query Git Status
    Daemon->>Git: git status --porcelain=v1
    Git-->>Daemon: Returns modified files
    Note over Daemon: Sanitizer filters out .env, *.pem, credentials
    Daemon-->>Cloud: Filtered change list (3 safe files)
    Cloud-->>Dev: Shows changed files with selection checkboxes

    Dev->>Cloud: Taps [ Select All ] -> [ Commit & Push ]
    Cloud->>Dev: Prompts: "Enter commit message:"
    Dev->>Cloud: "feat(core): implement phase 3 parser"
    Cloud->>Daemon: Execute Git Workflow
    Daemon->>Git: git checkout -b feature/phase-03-parser
    Daemon->>Git: git add backend/parser.py tests/test_parser.py
    Daemon->>Git: git commit -m "feat(core): implement phase 3 parser"
    Daemon->>Git: git push origin feature/phase-03-parser
    Daemon->>GH: POST /repos/:owner/:repo/pulls
    GH-->>Daemon: PR #18 Created
    Daemon-->>Cloud: PR metadata
    Cloud-->>Dev: 🎉 Branch pushed & PR #18 created successfully!
```
