# Git from Telegram Guide

Manage your local Git repositories and open GitHub pull requests directly from your phone via AgentBridge Telegram ChatOps.

---

## 1. Prerequisites & Environment

1. **Host Workstation**: AgentBridge runtime (`sutra`) must be running on your development machine with access to your project directories.
2. **Git Available**: Git CLI installed and available in the workstation's PATH.
3. **No IDE Required**: Neither VS Code, Cursor, nor any other editor needs to be open. AgentBridge interacts directly with the local `.git` repository.
4. **GitHub Authentication (Optional for PRs)**:
   - To create Pull Requests, set `GITHUB_TOKEN` or `GH_TOKEN` in your environment, or authenticate your workstation using the official `gh` CLI (`gh auth login`).
   - AgentBridge never saves, logs, or transmits your tokens.

---

## 2. Navigating to Git

1. Open Telegram and tap **📁 Projects**.
2. Select your desired project.
3. Tap the **🔀 Git** button.

If the selected directory is a Git repository, AgentBridge displays the current branch, upstream tracking status, and working tree summary.

---

## 3. Core Operations

### Checking Status
Tap **[📊 Status]** to see a categorized breakdown of changes:
- Staged changes (`✅`)
- Unstaged modifications (`📝`)
- Untracked files (`❓`)
- Merge conflicts (`💥`)

### Inspecting Diffs
Tap **[📝 Diff]** to inspect changes:
- By default, displays working-tree unstaged diffs.
- Tap **[🔄 View Staged Diff]** to inspect what will be in the next commit.
- Diffs are line-bounded and sensitive secrets are automatically redacted before transmission.

### Staging Files (Mobile Staging)
1. Tap **[☑️ Stage]**.
2. Tap individual files to toggle selection (`☑️` / `☐`).
3. Tap **[Stage Selected]** to stage your choices.
4. Tap **[Stage All]** to stage all changes, or **[Unstage All]** to reset the staging area.

### Committing Changes
1. Ensure at least one file is staged.
2. In the Telegram chat, send:
   ```text
   /commit Added authentication middleware
   ```
   *or tap **[💾 Quick Commit]** on the Commit screen.*
3. If permission authorization is required, approve the prompt.
4. AgentBridge executes the commit and displays the new commit hash.

### Pushing to Remote
1. Tap **[🚀 Push]**.
2. Inspect the preview: remote name, target branch, and commits ahead.
3. Tap **[🚀 Confirm Push]**.
4. AgentBridge executes `git push` safely without force flags.

### Opening a Pull Request
1. Tap **[🔀 GitHub PR]**.
2. If your remote is hosted on GitHub, AgentBridge displays the head branch and target base branch.
3. Send:
   ```text
   /pr Add OAuth2 token refresh support
   ```
   *or tap **[🔀 Create PR]**.*
4. Upon approval, AgentBridge contacts the GitHub REST API and sends you the PR number and link.
