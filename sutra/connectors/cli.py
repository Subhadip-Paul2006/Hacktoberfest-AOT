"""CLI handler for SŪTRA connector management, authentication, testing, and inspection."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from sutra.credentials import (
    CredentialInfo,
    CredentialProvider,
    CredentialSource,
    GitHubCredentialProvider,
    KeyringBackend,
    KeyringUnavailableError,
    SupabaseCredentialProvider,
)
from sutra.redaction import redact_secrets
from sutra.ui import SutraUI

from .analyzer import ConnectorAnalyzer, VerificationState
from .errors import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorTimeoutError,
)
from .mcp.github import (
    DEFAULT_CONNECTOR_ID,
    DEFAULT_GITHUB_MCP_URL,
    GitHubMCPConnector,
)
from .mcp.oauth import (
    MCPOAuthClient,
    OAuthCancelledError,
    OAuthError,
    OAuthFlowConfig,
    OAuthTimeoutError,
)
from .mcp.supabase import (
    DEFAULT_CONNECTOR_ID as DEFAULT_SUPABASE_CONNECTOR_ID,
    DEFAULT_FEATURES as DEFAULT_SUPABASE_FEATURES,
    DEFAULT_SUPABASE_MCP_URL,
    SupabaseMCPConnector,
    validate_project_ref,
)
from .models import ConnectorDescriptor, ConnectorStatus, ConnectorType
from .registry import ConnectorRegistry
from .terminal import TerminalConnector

log = logging.getLogger(__name__)


def _get_credential_provider(connector_id: str, provider: Optional[str] = None) -> CredentialProvider:
    """Resolve the appropriate credential provider for a given connector."""
    if provider == "supabase" or connector_id.startswith("supabase"):
        return SupabaseCredentialProvider(connector_id)
    return GitHubCredentialProvider(connector_id)


def _get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Connect to SQLite state database with WAL mode."""
    if db_path is None:
        home_str = os.environ.get("SUTRA_HOME") or os.environ.get("AGENTBRIDGE_HOME")
        home = Path(home_str).expanduser() if home_str else (Path.home() / ".sutra").expanduser()
        home.mkdir(parents=True, exist_ok=True)
        db_path = home / "state.sqlite3"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connector_configs (
            connector_id   TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            connector_type TEXT NOT NULL,
            provider       TEXT NOT NULL,
            enabled        INTEGER NOT NULL DEFAULT 1,
            version        TEXT NOT NULL DEFAULT '1.0.0',
            description    TEXT NOT NULL DEFAULT '',
            metadata       TEXT NOT NULL DEFAULT '{}',
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _get_registry(db_path: Optional[Path] = None) -> ConnectorRegistry:
    """Create a persistent ConnectorRegistry instance backed by SQLite."""
    conn = _get_db(db_path)
    return ConnectorRegistry(db=conn, db_lock=RLock())


def handle_connectors_command(args: argparse.Namespace, ui: Optional[SutraUI] = None) -> int:
    """Entrypoint for `sutra connectors` subcommands."""
    ui = ui or SutraUI()
    action = getattr(args, "connectors_action", None)

    if not action or action == "help":
        ui.console.print("\n[bold cyan]SŪTRA Connector Management[/bold cyan]")
        ui.console.print("Commands:")
        ui.console.print("  [white]sutra connectors add [id][/white]     Register a connector")
        ui.console.print("  [white]sutra connectors auth [id][/white]    Authenticate using Personal Access Token")
        ui.console.print("  [white]sutra connectors test [id][/white]    Test connection and tool discovery")
        ui.console.print("  [white]sutra connectors status [id][/white]  Display connector and credential status")
        ui.console.print("  [white]sutra connectors tools [id][/white]   List discovered remote MCP tools")
        ui.console.print("  [white]sutra connectors list[/white]        List all configured connectors")
        ui.console.print("  [white]sutra connectors remove [id][/white]  Remove a connector and its credentials\n")
        return 0

    if action == "add":
        return _cmd_add(args, ui)
    elif action == "auth":
        return _cmd_auth(args, ui)
    elif action == "test":
        return _cmd_test(args, ui)
    elif action == "status":
        return _cmd_status(args, ui)
    elif action == "tools":
        return _cmd_tools(args, ui)
    elif action == "list":
        return _cmd_list(args, ui)
    elif action == "remove":
        return _cmd_remove(args, ui)
    else:
        ui.console.print(f"[red]Unknown connectors action: {action}[/red]")
        return 1


def _cmd_add(args: argparse.Namespace, ui: SutraUI) -> int:
    """Register a connector configuration."""
    connector_id = getattr(args, "connector_id", None) or DEFAULT_CONNECTOR_ID
    endpoint = getattr(args, "endpoint", None)
    provider = getattr(args, "provider", None) or ("supabase" if "supabase" in connector_id else "github")
    workspace = getattr(args, "workspace", None) or ""

    registry = _get_registry()
    existing = registry.get(connector_id)
    if existing:
        ui.console.print(f"[yellow]Connector '{connector_id}' is already registered.[/yellow]")
        return 0

    if provider == "terminal" or connector_id == "terminal-local":
        ws_root = workspace or str(Path.cwd())
        desc = ConnectorDescriptor(
            connector_id=connector_id,
            name="Local Terminal",
            type=ConnectorType.CLI,
            provider="terminal",
            description="Local shell-less terminal command execution connector",
            metadata={
                "workspace_roots": [ws_root],
                "transport_hint": "cli",
            },
        )
        registry._persist_config(desc)
        ui.console.print(f"\n[bold green]✓ Registered connector '{connector_id}'[/bold green]")
        ui.console.print(f"  [dim]Provider:[/dim]  terminal")
        ui.console.print(f"  [dim]Type:[/dim]      CLI")
        ui.console.print(f"  [dim]Workspace:[/dim] {ws_root}")
        ui.console.print("\n[cyan]Next Steps:[/cyan]")
        ui.console.print(f"  1. Run [bold white]sutra connectors test {connector_id}[/bold white] to verify workspace access\n")
        return 0

    if provider == "supabase":
        ep = endpoint or DEFAULT_SUPABASE_MCP_URL
        project_ref = getattr(args, "project_ref", None)
        read_only = getattr(args, "read_only", True)
        features_arg = getattr(args, "features", None)
        if isinstance(features_arg, str):
            features = [f.strip() for f in features_arg.split(",") if f.strip()]
        elif isinstance(features_arg, (list, tuple)):
            features = list(features_arg)
        else:
            features = list(DEFAULT_SUPABASE_FEATURES)

        if project_ref:
            try:
                project_ref = validate_project_ref(project_ref)
            except ValueError as ve:
                ui.console.print(f"[red]Error:[/red] {ve}")
                return 1

        name = f"Supabase Remote MCP Server ({project_ref})" if project_ref else "Supabase Remote MCP Server"
        desc = ConnectorDescriptor(
            connector_id=connector_id,
            name=name,
            type=ConnectorType.MCP,
            provider="supabase",
            description="Official Supabase Remote MCP Server integration",
            metadata={
                "endpoint": ep,
                "project_ref": project_ref,
                "read_only": read_only,
                "features": features,
                "transport_hint": "streamable_http",
            },
        )
        registry._persist_config(desc)

        ui.console.print(f"\n[bold green]✓ Registered connector '{connector_id}'[/bold green]")
        ui.console.print(f"  [dim]Provider:[/dim]    supabase")
        ui.console.print(f"  [dim]Endpoint:[/dim]    {ep}")
        ui.console.print(f"  [dim]Project Ref:[/dim] {project_ref or '(unscoped)'}")
        ui.console.print(f"  [dim]Read-Only:[/dim]   {read_only}")
        ui.console.print(f"  [dim]Features:[/dim]    {', '.join(features)}")
        ui.console.print(f"  [dim]Transport:[/dim]   streamable_http")
        ui.console.print("\n[cyan]Next Steps:[/cyan]")
        ui.console.print(f"  1. Run [bold white]sutra connectors auth {connector_id}[/bold white] to configure your access token or browser login")
        ui.console.print(f"  2. Or export [bold white]SUTRA_SUPABASE_ACCESS_TOKEN=<token>[/bold white] in your environment")
        ui.console.print(f"  3. Run [bold white]sutra connectors test {connector_id}[/bold white] to verify connection\n")
        return 0

    # GitHub or generic MCP
    ep = endpoint or DEFAULT_GITHUB_MCP_URL
    desc = ConnectorDescriptor(
        connector_id=connector_id,
        name="GitHub Remote MCP Server" if provider == "github" else f"{provider.capitalize()} MCP Server",
        type=ConnectorType.MCP,
        provider=provider,
        description="Official GitHub Remote MCP Server integration" if provider == "github" else "MCP integration",
        metadata={
            "endpoint": ep,
            "transport_hint": "streamable_http",
        },
    )
    registry._persist_config(desc)

    ui.console.print(f"\n[bold green]✓ Registered connector '{connector_id}'[/bold green]")
    ui.console.print(f"  [dim]Provider:[/dim]  {provider}")
    ui.console.print(f"  [dim]Endpoint:[/dim]  {ep}")
    ui.console.print(f"  [dim]Transport:[/dim] streamable_http")

    if provider == "github":
        ui.console.print("\n[cyan]Next Steps:[/cyan]")
        ui.console.print(f"  1. Run [bold white]sutra connectors auth {connector_id}[/bold white] to configure your GitHub token")
        ui.console.print(f"  2. Or export [bold white]SUTRA_GITHUB_TOKEN=<token>[/bold white] in your environment")
        ui.console.print(f"  3. Run [bold white]sutra connectors test {connector_id}[/bold white] to verify connection\n")

    return 0


def _cmd_auth(args: argparse.Namespace, ui: SutraUI) -> int:
    """Configure or clear authentication token for a connector."""
    connector_id = getattr(args, "connector_id", None) or DEFAULT_CONNECTOR_ID
    clear = getattr(args, "clear", False)

    conn = _get_db()
    row = conn.execute("SELECT provider FROM connector_configs WHERE connector_id = ?", (connector_id,)).fetchone()
    provider_name = (row[0] if row else None) or ("supabase" if "supabase" in connector_id else "github")

    provider = _get_credential_provider(connector_id, provider_name)

    if clear:
        removed = provider.remove_token()
        if removed:
            ui.console.print(f"[green]✓ Removed credentials for '{connector_id}'.[/green]")
        else:
            ui.console.print(f"[yellow]No stored credentials found for '{connector_id}'.[/yellow]")
        return 0

    token = getattr(args, "token", None) or ""
    interactive_oauth = getattr(args, "oauth", False) or getattr(args, "interactive", False)

    if provider_name == "supabase":
        if interactive_oauth:
            ui.console.print("\n[bold cyan]Starting Supabase Browser OAuth Login...[/bold cyan]")
            client_id = getattr(args, "client_id", None) or os.environ.get("SUTRA_SUPABASE_CLIENT_ID") or "sutra-cli-mcp"
            oauth_cfg = OAuthFlowConfig(
                authorization_endpoint="https://api.supabase.com/v1/oauth/authorize",
                token_endpoint="https://api.supabase.com/v1/oauth/token",
                client_id=client_id,
            )
            oauth_client = MCPOAuthClient(oauth_cfg)
            try:
                oauth_tok = asyncio.run(oauth_client.authenticate_interactive(open_browser=True))
                provider.store_oauth_token(oauth_tok)
                ui.console.print(f"\n[bold green]✓ Supabase OAuth credentials saved securely in OS Keyring for '{connector_id}'.[/bold green]")
                ui.console.print(f"Run [bold white]sutra connectors test {connector_id}[/bold white] to verify connectivity.\n")
                return 0
            except OAuthCancelledError:
                ui.console.print("\n[yellow]OAuth authorization was cancelled or denied.[/yellow]\n")
                return 1
            except Exception as exc:
                ui.console.print(f"\n[red]OAuth authorization failed: {redact_secrets(str(exc))}[/red]\n")
                return 1

        if not token:
            ui.console.print("\n[bold cyan]Supabase MCP Authentication Setup[/bold cyan]")
            ui.console.print("To authenticate with the official Supabase Remote MCP Server, provide a Personal Access Token (PAT):")
            ui.console.print("  1. Visit: [underline]https://supabase.com/dashboard/account/tokens[/underline]")
            ui.console.print("  2. Generate a new Personal Access Token.")
            ui.console.print("  3. Paste the token below (input is masked for security).\n")
            try:
                token = getpass.getpass("Enter Supabase Personal Access Token: ").strip()
            except (KeyboardInterrupt, EOFError):
                ui.console.print("\n[dim]Authentication cancelled.[/dim]")
                return 1

        if not token:
            ui.console.print("[red]Token cannot be empty.[/red]")
            return 1

        try:
            provider.store_token(token)
            ui.console.print(f"\n[bold green]✓ Credential saved securely in OS Keyring for '{connector_id}'.[/bold green]")
            ui.console.print(f"Run [bold white]sutra connectors test {connector_id}[/bold white] to verify connectivity.\n")
            return 0
        except KeyringUnavailableError:
            ui.console.print(
                "\n[yellow]Notice: OS Keyring is unavailable in this environment.[/yellow]\n"
                "SŪTRA refuses to store plaintext secrets on disk.\n"
                "Please configure your token via environment variable:\n\n"
                f"  export SUTRA_SUPABASE_ACCESS_TOKEN=\"<your-token>\"\n\n"
                "Or add it to your .env file (ensure .env is in .gitignore).\n"
            )
            return 0
        except Exception as exc:
            ui.console.print(f"[red]Failed to store credential: {exc}[/red]")
            return 1

    # Default to GitHub PAT flow
    if not token:
        ui.console.print("\n[bold cyan]GitHub MCP Authentication Setup[/bold cyan]")
        ui.console.print("To use the official GitHub Remote MCP Server, provide a fine-grained Personal Access Token:")
        ui.console.print("  1. Visit: [underline]https://github.com/settings/tokens?type=beta[/underline]")
        ui.console.print("  2. Select your repository/repositories.")
        ui.console.print("  3. Grant minimal required permissions (e.g. Read metadata, issues, pull requests).")
        ui.console.print("  4. Paste the token below (input is masked for security).\n")
        try:
            token = getpass.getpass("Enter GitHub Personal Access Token: ").strip()
        except (KeyboardInterrupt, EOFError):
            ui.console.print("\n[dim]Authentication cancelled.[/dim]")
            return 1

    if not token:
        ui.console.print("[red]Token cannot be empty.[/red]")
        return 1

    try:
        provider.store_token(token)
        ui.console.print(f"\n[bold green]✓ Credential saved securely in OS Keyring for '{connector_id}'.[/bold green]")
        ui.console.print(f"Run [bold white]sutra connectors test {connector_id}[/bold white] to verify connectivity.\n")
        return 0
    except KeyringUnavailableError:
        ui.console.print(
            "\n[yellow]Notice: OS Keyring is unavailable in this environment.[/yellow]\n"
            "SŪTRA refuses to store plaintext secrets on disk.\n"
            "Please configure your token via environment variable:\n\n"
            f"  export SUTRA_GITHUB_TOKEN=\"<your-token>\"\n\n"
            "Or add it to your .env file (ensure .env is in .gitignore).\n"
        )
        return 0
    except Exception as exc:
        ui.console.print(f"[red]Failed to store credential: {exc}[/red]")
        return 1


def _cmd_test(args: argparse.Namespace, ui: SutraUI) -> int:
    """Test connection, protocol handshake, and tool discovery."""
    connector_id = getattr(args, "connector_id", None) or DEFAULT_CONNECTOR_ID
    conn = _get_db()
    row = conn.execute(
        "SELECT connector_type, provider, metadata FROM connector_configs WHERE connector_id = ?",
        (connector_id,),
    ).fetchone()

    if (row and (row[1] == "terminal" or row[0] == "CLI")) or connector_id == "terminal-local":
        meta = json.loads(row[2]) if (row and row[2]) else {}
        workspaces = meta.get("workspace_roots", [str(Path.cwd())])
        return asyncio.run(_async_test_terminal(connector_id, workspaces, ui))

    if (row and row[1] == "supabase") or connector_id.startswith("supabase"):
        meta = json.loads(row[2]) if (row and row[2]) else {}
        return asyncio.run(_async_test_supabase(connector_id, meta, ui))

    return asyncio.run(_async_test(connector_id, ui))


async def _async_test_supabase(connector_id: str, metadata: dict[str, Any], ui: SutraUI) -> int:
    provider = SupabaseCredentialProvider(connector_id)
    info = provider.get_info()

    if not info.has_credential:
        ui.console.print(f"\n[red]✗ No credentials configured for '{connector_id}'.[/red]")
        ui.console.print(f"Run [bold white]sutra connectors auth {connector_id}[/bold white] or set SUTRA_SUPABASE_ACCESS_TOKEN.\n")
        return 1

    project_ref = metadata.get("project_ref")
    read_only = metadata.get("read_only", True)
    features = metadata.get("features", DEFAULT_SUPABASE_FEATURES)
    endpoint = metadata.get("endpoint", DEFAULT_SUPABASE_MCP_URL)

    ui.console.print(f"\n[cyan]Testing Supabase connection for '{connector_id}'...[/cyan]")
    ui.console.print(f"  [dim]Credential Source:[/dim] {info.source.value}")
    if info.masked_preview:
        ui.console.print(f"  [dim]Token Preview:[/dim]     {info.masked_preview}")
    if project_ref:
        ui.console.print(f"  [dim]Project Scope:[/dim]     {project_ref}")
    ui.console.print(f"  [dim]Read-Only Mode:[/dim]    {read_only}")

    connector = SupabaseMCPConnector(
        connector_id=connector_id,
        endpoint=endpoint,
        project_ref=project_ref,
        read_only=read_only,
        features=features,
        credential_provider=provider,
    )

    t0 = time.time()
    try:
        await connector.connect()
        duration_ms = (time.time() - t0) * 1000.0

        tools_page = await connector.client.list_tools()
        tools_count = len(tools_page.items)

        ui.console.print(f"  [green]✓[/green] Connected to {connector.endpoint} in {duration_ms:.1f}ms")
        ui.console.print(f"  [green]✓[/green] Protocol Version: {connector.client.protocol_version.value if connector.client.protocol_version else 'unknown'}")
        ui.console.print(f"  [green]✓[/green] Tools Discovered: {tools_count}")
        if project_ref and connector.project_access_verified:
            ui.console.print(f"  [green]✓[/green] Project Access: Verified ({project_ref})")
        ui.console.print("\n[bold green]Supabase connector is healthy and ready for use.[/bold green]\n")
        return 0

    except ConnectorAuthError as exc:
        ui.console.print(f"\n[red]✗ Authentication failed (HTTP 401):[/red] {exc}")
        ui.console.print("Please verify your Supabase token is valid and has not expired.\n")
        return 1
    except ConnectorPermissionError as exc:
        ui.console.print(f"\n[red]✗ Authorization denied (HTTP 403):[/red] {exc}")
        ui.console.print("Please check project permissions for your token.\n")
        return 1
    except ConnectorNotFoundError as exc:
        ui.console.print(f"\n[red]✗ Project not found (HTTP 404):[/red] {exc}\n")
        return 1
    except ConnectorTimeoutError as exc:
        ui.console.print(f"\n[red]✗ Connection timed out:[/red] {exc}\n")
        return 1
    except Exception as exc:
        clean_err = redact_secrets(str(exc))
        ui.console.print(f"\n[red]✗ Connection test failed:[/red] {clean_err}\n")
        return 1
    finally:
        try:
            await connector.disconnect()
        except Exception:
            pass



async def _async_test_terminal(connector_id: str, workspaces: list[str], ui: SutraUI) -> int:
    ui.console.print(f"\n[cyan]Testing terminal connector '{connector_id}'...[/cyan]")
    ui.console.print(f"  [dim]Type:[/dim]            CLI")
    ui.console.print(f"  [dim]Workspaces:[/dim]      {workspaces}")

    connector = TerminalConnector(
        connector_id=connector_id,
        workspace_roots=workspaces,
    )
    t0 = time.time()
    try:
        await connector.connect()
        duration_ms = (time.time() - t0) * 1000.0
        health = await connector.health()
        tools = await connector.discover()

        ui.console.print(f"  [green]✓[/green] Workspace verified in {duration_ms:.1f}ms")
        ui.console.print(f"  [green]✓[/green] Health Status: {health.status.value} (healthy={health.healthy})")
        ui.console.print(f"  [green]✓[/green] Tools Discovered: {len(tools)} ({', '.join(t.name for t in tools)})")
        ui.console.print("\n[bold green]Terminal connector is healthy and ready for use.[/bold green]\n")
        return 0
    except Exception as exc:
        clean_err = redact_secrets(str(exc))
        ui.console.print(f"\n[red]✗ Terminal connection test failed:[/red] {clean_err}\n")
        return 1
    finally:
        await connector.disconnect()


async def _async_test(connector_id: str, ui: SutraUI) -> int:
    provider = GitHubCredentialProvider(connector_id)
    info = provider.get_info()

    if not info.has_credential:
        ui.console.print(f"\n[red]✗ No credentials configured for '{connector_id}'.[/red]")
        ui.console.print(f"Run [bold white]sutra connectors auth {connector_id}[/bold white] or set SUTRA_GITHUB_TOKEN.\n")
        return 1

    ui.console.print(f"\n[cyan]Testing connection for '{connector_id}'...[/cyan]")
    ui.console.print(f"  [dim]Credential Source:[/dim] {info.source.value}")
    if info.masked_preview:
        ui.console.print(f"  [dim]Token Preview:[/dim]     {info.masked_preview}")

    connector = GitHubMCPConnector(
        connector_id=connector_id,
        credential_provider=provider,
    )

    t0 = time.time()
    try:
        await connector.connect()
        duration_ms = (time.time() - t0) * 1000.0

        tools_page = await connector.client.list_tools()
        tools_count = len(tools_page.items)

        ui.console.print(f"  [green]✓[/green] Connected to {connector.endpoint} in {duration_ms:.1f}ms")
        ui.console.print(f"  [green]✓[/green] Protocol Version: {connector.client.protocol_version.value if connector.client.protocol_version else 'unknown'}")
        ui.console.print(f"  [green]✓[/green] Tools Discovered: {tools_count}")
        ui.console.print("\n[bold green]Connector is healthy and ready for use.[/bold green]\n")
        return 0

    except ConnectorAuthError as exc:
        ui.console.print(f"\n[red]✗ Authentication failed (HTTP 401):[/red] {exc}")
        ui.console.print("Please verify your GitHub token is valid and has not expired.\n")
        return 1
    except Exception as exc:
        clean_err = redact_secrets(str(exc))
        ui.console.print(f"\n[red]✗ Connection test failed:[/red] {clean_err}\n")
        return 1
    finally:
        try:
            await connector.disconnect()
        except Exception:
            pass


def _cmd_status(args: argparse.Namespace, ui: SutraUI) -> int:
    """Display configuration and credential status."""
    connector_id = getattr(args, "connector_id", None) or DEFAULT_CONNECTOR_ID
    conn = _get_db()
    row = conn.execute(
        "SELECT connector_id, name, provider, enabled, metadata, connector_type FROM connector_configs WHERE connector_id=?",
        (connector_id,),
    ).fetchone()

    if (row and (row[2] == "terminal" or row[5] == "CLI")) or connector_id == "terminal-local":
        meta = json.loads(row[4]) if (row and row[4]) else {}
        workspaces = meta.get("workspace_roots", [str(Path.cwd())])
        ui.console.print(f"\n[bold cyan]Connector Status: {connector_id}[/bold cyan]")
        ui.console.print(f"  [dim]Name:[/dim]              {row[1] if row else 'Local Terminal'}")
        ui.console.print(f"  [dim]Provider:[/dim]          terminal")
        ui.console.print(f"  [dim]Type:[/dim]              CLI")
        ui.console.print(f"  [dim]Workspace Roots:[/dim]   {workspaces}")
        ui.console.print(f"  [dim]Security Mode:[/dim]     Native asyncio.subprocess (shell=False)\n")
        return 0

    prov = (row[2] if row else None) or ("supabase" if "supabase" in connector_id else "github")
    provider = _get_credential_provider(connector_id, prov)
    info = provider.get_info()

    ui.console.print(f"\n[bold cyan]Connector Status: {connector_id}[/bold cyan]")
    if row:
        meta = json.loads(row[4]) if row[4] else {}
        ui.console.print(f"  [dim]Name:[/dim]              {row[1]}")
        ui.console.print(f"  [dim]Provider:[/dim]          {row[2]}")
        ui.console.print(f"  [dim]Enabled:[/dim]           {'Yes' if row[3] else 'No'}")
        ui.console.print(f"  [dim]Endpoint:[/dim]          {meta.get('endpoint', DEFAULT_SUPABASE_MCP_URL if prov == 'supabase' else DEFAULT_GITHUB_MCP_URL)}")
        if prov == "supabase":
            ui.console.print(f"  [dim]Project Scope:[/dim]     {meta.get('project_ref') or '(unscoped)'}")
            ui.console.print(f"  [dim]Read-Only Mode:[/dim]    {meta.get('read_only', True)}")
            ui.console.print(f"  [dim]Features:[/dim]          {', '.join(meta.get('features', DEFAULT_SUPABASE_FEATURES))}")
    else:
        ui.console.print(f"  [dim]Registered:[/dim]        No (Default instance)")
        ui.console.print(f"  [dim]Endpoint:[/dim]          {DEFAULT_SUPABASE_MCP_URL if prov == 'supabase' else DEFAULT_GITHUB_MCP_URL}")

    ui.console.print(f"  [dim]Credential Status:[/dim] {'CONFIGURED' if info.has_credential else 'NOT CONFIGURED'}")
    ui.console.print(f"  [dim]Credential Source:[/dim] {info.source.value}")
    if info.masked_preview:
        ui.console.print(f"  [dim]Token Preview:[/dim]     {info.masked_preview}")
    ui.console.print(f"  [dim]Secret Storage:[/dim]    {info.service_name} (Memory / Keyring / Env)\n")

    return 0


def _cmd_tools(args: argparse.Namespace, ui: SutraUI) -> int:
    """List discovered tools from the MCP server or terminal connector."""
    connector_id = getattr(args, "connector_id", None) or DEFAULT_CONNECTOR_ID
    return asyncio.run(_async_tools(connector_id, ui))


async def _async_tools(connector_id: str, ui: SutraUI) -> int:
    conn = _get_db()
    row = conn.execute(
        "SELECT connector_type, provider, metadata FROM connector_configs WHERE connector_id = ?",
        (connector_id,),
    ).fetchone()

    if (row and (row[1] == "terminal" or row[0] == "CLI")) or connector_id == "terminal-local":
        meta = json.loads(row[2]) if (row and row[2]) else {}
        workspaces = meta.get("workspace_roots", [str(Path.cwd())])
        connector = TerminalConnector(connector_id=connector_id, workspace_roots=workspaces)
        await connector.connect()
        try:
            tools = await connector.discover()
            ui.console.print(f"\n[bold cyan]Available Tools on '{connector_id}':[/bold cyan]")
            for t in tools:
                mode = "[green]READ-ONLY[/green]" if t.read_only else "[yellow]MUTATING[/yellow]"
                ui.console.print(f"  • [bold white]{t.name}[/bold white] ({mode}, Risk: {t.risk_level})")
                if t.description:
                    ui.console.print(f"    [dim]{t.description}[/dim]")
            ui.console.print("")
            return 0
        finally:
            await connector.disconnect()

    prov = (row[1] if row else None) or ("supabase" if "supabase" in connector_id else "github")

    if prov == "supabase":
        provider = SupabaseCredentialProvider(connector_id)
        if not provider.has_credential():
            ui.console.print(f"[red]✗ No credentials configured for '{connector_id}'.[/red]")
            return 1
        meta = json.loads(row[2]) if (row and row[2]) else {}
        connector = SupabaseMCPConnector(
            connector_id=connector_id,
            endpoint=meta.get("endpoint", DEFAULT_SUPABASE_MCP_URL),
            project_ref=meta.get("project_ref"),
            read_only=meta.get("read_only", True),
            features=meta.get("features", DEFAULT_SUPABASE_FEATURES),
            credential_provider=provider,
        )
        try:
            await connector.connect()
            page = await connector.client.list_tools()
            tools = page.items

            ui.console.print(f"\n[bold cyan]Discovered Tools on '{connector_id}' ({len(tools)} total)[/bold cyan]\n")
            for t in tools:
                is_ro = connector.is_tool_read_only(t.name, t.metadata)
                mode = "[dim green]READ-ONLY[/dim green]" if is_ro else "[yellow]MUTATING[/yellow]"
                desc = t.description.strip() if t.description else "No description"
                ui.console.print(f"  • [bold white]{t.name}[/bold white] {mode}")
                ui.console.print(f"    [dim]{desc}[/dim]")
            ui.console.print("")
            return 0
        except Exception as exc:
            ui.console.print(f"[red]Failed to discover tools: {redact_secrets(str(exc))}[/red]")
            return 1
        finally:
            try:
                await connector.disconnect()
            except Exception:
                pass

    provider = GitHubCredentialProvider(connector_id)
    if not provider.has_credential():
        ui.console.print(f"[red]✗ No credentials configured for '{connector_id}'.[/red]")
        return 1

    connector = GitHubMCPConnector(
        connector_id=connector_id,
        credential_provider=provider,
    )

    try:
        await connector.connect()
        page = await connector.client.list_tools()
        tools = page.items

        ui.console.print(f"\n[bold cyan]Discovered Tools on '{connector_id}' ({len(tools)} total)[/bold cyan]\n")
        for t in tools:
            mode = "[dim green]READ-ONLY[/dim green]" if t.read_only else "[yellow]MUTATING[/yellow]"
            desc = t.description.strip() if t.description else "No description"
            ui.console.print(f"  • [bold white]{t.name}[/bold white] {mode}")
            ui.console.print(f"    [dim]{desc}[/dim]")
        ui.console.print("")
        return 0
    except Exception as exc:
        ui.console.print(f"[red]Failed to discover tools: {redact_secrets(str(exc))}[/red]")
        return 1
    finally:
        try:
            await connector.disconnect()
        except Exception:
            pass


def _cmd_list(args: argparse.Namespace, ui: SutraUI) -> int:
    """List all configured connectors in SQLite."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT connector_id, name, provider, enabled, metadata FROM connector_configs ORDER BY connector_id"
    ).fetchall()

    ui.console.print(f"\n[bold cyan]Configured Connectors ({len(rows)})[/bold cyan]\n")
    if not rows:
        ui.console.print("  [dim]No connectors explicitly added. Default instance: 'github-mcp'[/dim]\n")
        return 0

    for r in rows:
        cid, name, prov, en, meta_str = r
        provider = _get_credential_provider(cid, prov)
        has_cred = provider.has_credential()
        cred_str = "[green]AUTH CONFIGURED[/green]" if has_cred else "[yellow]AUTH PENDING[/yellow]"
        ui.console.print(f"  • [bold white]{cid}[/bold white] ({prov}) — {cred_str}")
        ui.console.print(f"    [dim]{name}[/dim]")
    ui.console.print("")
    return 0


def _cmd_remove(args: argparse.Namespace, ui: SutraUI) -> int:
    """Remove a connector and its credentials."""
    connector_id = getattr(args, "connector_id", None) or DEFAULT_CONNECTOR_ID
    conn = _get_db()
    row = conn.execute("SELECT provider FROM connector_configs WHERE connector_id=?", (connector_id,)).fetchone()
    prov = (row[0] if row else None) or ("supabase" if "supabase" in connector_id else "github")

    cur = conn.execute("DELETE FROM connector_configs WHERE connector_id=?", (connector_id,))
    conn.commit()

    provider = _get_credential_provider(connector_id, prov)
    provider.remove_token()

    if cur.rowcount > 0:
        ui.console.print(f"[green]✓ Removed connector '{connector_id}' and associated credentials.[/green]")
    else:
        ui.console.print(f"[yellow]Cleared credentials for '{connector_id}'.[/yellow]")
    return 0
