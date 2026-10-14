"""SŪTRA local CLI with animated modern terminal experience."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from .adapters import AgentRegistry
from .apps.telegram import TelegramBot
from .dev_certificate import create_certificate
from .discovery import discover_projects
from .runtime import Runtime
from .security import validate_access_root
from .state import StateStore
from .ui import StartupState, SutraUI, supports_unicode


def _home() -> Path:
    val = os.environ.get("SUTRA_HOME") or os.environ.get("AGENTBRIDGE_HOME")
    if val:
        return Path(val).expanduser()
    return (Path.home() / ".sutra").expanduser()


def _perform_startup_stage(stage: str, state: StartupState) -> None:
    if stage == "Configuration":
        from dotenv import load_dotenv
        load_dotenv(override=False)
        state.config_loaded = True
        state.home_dir = str(_home())
    elif stage == "State":
        home = _home()
        home.mkdir(parents=True, exist_ok=True)
        store = StateStore(home / "state.sqlite3")
        try:
            workspaces = store.workspaces()
            state.workspaces_count = len(workspaces)
        finally:
            store.close()
    elif stage == "Workspace":
        store = StateStore(_home() / "state.sqlite3")
        try:
            workspaces = store.workspaces()
            projects = discover_projects(workspaces)
            state.projects_count = len(projects)
        finally:
            store.close()
    elif stage == "Control Plane":
        cp_url = os.environ.get("SUTRA_CONTROL_PLANE_URL", os.environ.get("AGENTBRIDGE_CONTROL_PLANE_URL", ""))
        state.control_plane_url = cp_url
        if cp_url:
            state.control_plane_status = "CONFIGURED"
        else:
            state.control_plane_status = "STANDBY"
    elif stage == "Bot Integrations":
        from .apps.common.registry import AppRegistry
        reg = AppRegistry()
        tg = reg.get_app("telegram")
        state.telegram_status = tg.status.value.upper() if tg else "DISABLED"
    elif stage == "Agent Registry":
        registry = AgentRegistry()
        state.agents_count = len(registry.list())


def main() -> None:
    import sys
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(errors="replace")
        except Exception:
            pass

    display_name = "SAMANVAYA · SŪTRA" if supports_unicode() else "SAMANVAYA - SUTRA"
    parser = argparse.ArgumentParser(
        prog="sutra",
        description=f"{display_name} — Remote Agent Runtime",
    )
    subs = parser.add_subparsers(dest="command", required=False)

    local = subs.add_parser("local", help="start the local runtime")
    local.add_argument("--host", default="127.0.0.1")
    local.add_argument("--port", type=int, default=8765)

    cp = subs.add_parser("control-plane", help="run the TLS control plane locally")
    cp.add_argument("--host", default="127.0.0.1")
    cp.add_argument("--port", type=int, default=8766)
    cp.add_argument("--tls-certfile", default="")
    cp.add_argument("--tls-keyfile", default="")

    cert = subs.add_parser("dev-certificate", help="create a localhost TLS certificate for development")
    cert.add_argument("--output-dir", default=".dev-certs")

    setup_cmd = subs.add_parser("setup", help="initialize development certificates and local state")
    setup_cmd.add_argument("--output-dir", default=".dev-certs")

    status_cmd = subs.add_parser("status", help="display runtime and subsystem status")

    ws = subs.add_parser("workspace", help="manage explicitly approved workspaces")
    ws_sub = ws.add_subparsers(dest="workspace_command", required=True)
    add = ws_sub.add_parser("add")
    add.add_argument("path")
    remove = ws_sub.add_parser("remove")
    remove.add_argument("path")
    ws_sub.add_parser("list")

    subs.add_parser("projects", help="discover projects in registered workspaces")
    subs.add_parser("agents", help="show adapter placeholders")
    subs.add_parser("doctor", help="run environment diagnostics")

    # Connectors CLI (Phase 7.8A)
    conn = subs.add_parser("connectors", help="manage remote MCP and local connectors")
    conn_sub = conn.add_subparsers(dest="connectors_action", required=False)

    c_add = conn_sub.add_parser("add", help="register a connector")
    c_add.add_argument("connector_id", nargs="?", default="github-mcp", help="connector identifier (default: github-mcp)")
    c_add.add_argument("--endpoint", default="", help="custom MCP endpoint URL")
    c_add.add_argument("--provider", default="github", help="connector provider (default: github)")
    c_add.add_argument("--workspace", default="", help="authorized workspace root directory for terminal connector")

    c_auth = conn_sub.add_parser("auth", help="authenticate a connector using personal access token")
    c_auth.add_argument("connector_id", nargs="?", default="github-mcp", help="connector identifier (default: github-mcp)")
    c_auth.add_argument("--token", default="", help="token value (optional; prompts securely if omitted)")
    c_auth.add_argument("--clear", action="store_true", help="clear stored credentials for connector")

    c_test = conn_sub.add_parser("test", help="test connectivity, protocol handshake, and tool discovery")
    c_test.add_argument("connector_id", nargs="?", default="github-mcp", help="connector identifier (default: github-mcp)")

    c_status = conn_sub.add_parser("status", help="show configuration and credential status")
    c_status.add_argument("connector_id", nargs="?", default="github-mcp", help="connector identifier (default: github-mcp)")

    c_tools = conn_sub.add_parser("tools", help="list discovered tools on the remote MCP server")
    c_tools.add_argument("connector_id", nargs="?", default="github-mcp", help="connector identifier (default: github-mcp)")

    conn_sub.add_parser("list", help="list registered connectors")

    c_remove = conn_sub.add_parser("remove", help="remove a registered connector")
    c_remove.add_argument("connector_id", nargs="?", default="github-mcp", help="connector identifier (default: github-mcp)")

    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(override=False)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="[%(levelname)s] %(message)s")

    if args.command is None:
        ui = SutraUI()
        state = ui.run_animated_startup(_perform_startup_stage)
        ui.render_status_panel(state)
        ui.console.print(
            "[dim]Commands: [bold white]sutra local[/bold white] · "
            "[bold white]sutra control-plane[/bold white] · "
            "[bold white]sutra status[/bold white] · "
            "[bold white]sutra projects[/bold white] · "
            "[bold white]sutra --help[/bold white][/dim]\n"
        )
        return

    if args.command == "status":
        ui = SutraUI()
        state = StartupState()
        for stage in ("Configuration", "State", "Workspace", "Control Plane", "Bot Integrations", "Agent Registry"):
            _perform_startup_stage(stage, state)
        ui.render_status_panel(state)
        return

    if args.command == "setup":
        ui = SutraUI()
        ui.print_banner()
        home = _home()
        home.mkdir(parents=True, exist_ok=True)
        ui.console.print(f"  [green]✓[/green] SŪTRA Home: {home}")
        cert_path, key_path = create_certificate(args.output_dir)
        ui.console.print(f"  [green]✓[/green] TLS Certificate: {cert_path}")
        ui.console.print(f"  [green]✓[/green] TLS Private Key: {key_path}")
        ui.console.print("\n[bold green]SŪTRA setup completed successfully.[/bold green]\n")
        return

    if args.command == "dev-certificate":
        cert_path, key_path = create_certificate(args.output_dir)
        print(f"Created certificate: {cert_path}\nCreated private key: {key_path}")
        return

    if args.command == "doctor":
        from .doctor import run_doctor
        run_doctor()
        return

    if args.command == "connectors":
        from .connectors.cli import handle_connectors_command
        ui = SutraUI()
        import sys
        sys.exit(handle_connectors_command(args, ui))

    if args.command == "control-plane":
        import uvicorn
        from .control_plane import ControlPlane
        operator_token = os.environ.get("SUTRA_CONTROL_PLANE_TOKEN", os.environ.get("AGENTBRIDGE_CONTROL_PLANE_TOKEN", ""))
        if len(operator_token) < 32:
            raise SystemExit("Set SUTRA_CONTROL_PLANE_TOKEN to a random value of at least 32 characters.")
        certfile = args.tls_certfile or os.environ.get("SUTRA_TLS_CERTFILE", os.environ.get("AGENTBRIDGE_TLS_CERTFILE", ""))
        keyfile = args.tls_keyfile or os.environ.get("SUTRA_TLS_KEYFILE", os.environ.get("AGENTBRIDGE_TLS_KEYFILE", ""))
        if not certfile or not keyfile or not Path(certfile).is_file() or not Path(keyfile).is_file():
            raise SystemExit("TLS certificate and key are required; set SUTRA_TLS_CERTFILE and SUTRA_TLS_KEYFILE.")
        database = os.environ.get("SUTRA_CONTROL_PLANE_DB", os.environ.get("AGENTBRIDGE_CONTROL_PLANE_DB", str(_home()) / "control-plane.sqlite3"))
        control_plane = ControlPlane(database, operator_token)
        try:
            try:
                allowed_ids_raw = os.environ.get("SUTRA_TELEGRAM_ALLOWED_USER_IDS", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
                allowed_ids = {int(part.strip()) for part in allowed_ids_raw.split(",") if part.strip()}
            except ValueError as exc:
                raise SystemExit("TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric Telegram user IDs.") from exc
            bot_token = os.environ.get("SUTRA_TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
            telegram_mode = os.environ.get("SUTRA_TELEGRAM_POLLING_MODE", os.environ.get("TELEGRAM_POLLING_MODE", "long_polling")).strip().lower()
            if telegram_mode not in {"long_polling", "webhook"}:
                raise SystemExit("TELEGRAM_POLLING_MODE must be long_polling or webhook.")
            webhook_url = os.environ.get("SUTRA_TELEGRAM_WEBHOOK_URL", os.environ.get("TELEGRAM_WEBHOOK_URL", "")) if telegram_mode == "webhook" else ""
            webhook_secret = os.environ.get("SUTRA_TELEGRAM_WEBHOOK_SECRET", os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")) if telegram_mode == "webhook" else ""
            if telegram_mode == "webhook" and (not webhook_url or not webhook_secret):
                raise SystemExit("Webhook mode requires TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET.")
            bot = TelegramBot(control_plane, bot_token, allowed_ids, webhook_url,
                              webhook_secret) if bot_token and allowed_ids else None
            uvicorn.run(control_plane.create_app(bot), host=args.host, port=args.port,
                        ssl_certfile=certfile, ssl_keyfile=keyfile, ws_max_size=32768,
                        log_level=os.environ.get("LOG_LEVEL", "INFO").lower())
        finally:
            control_plane.store.close()
        return

    if args.command == "local":
        ui = SutraUI()
        state = ui.run_animated_startup(_perform_startup_stage)
        ui.render_status_panel(state)
        gateway = os.environ.get("SUTRA_GATEWAY_URL", os.environ.get("AGENTBRIDGE_GATEWAY_URL", ""))
        cp_url = os.environ.get("SUTRA_CONTROL_PLANE_URL", os.environ.get("AGENTBRIDGE_CONTROL_PLANE_URL", ""))
        ca_file = os.environ.get("SUTRA_TLS_CA_FILE", os.environ.get("AGENTBRIDGE_TLS_CA_FILE")) or None
        try:
            asyncio.run(Runtime(str(_home()), gateway, args.host, args.port, int(os.environ.get("SUTRA_RECONNECT_MAX_BACKOFF", os.environ.get("AGENTBRIDGE_RECONNECT_MAX_BACKOFF", "30"))), cp_url, ca_file).run())
        except KeyboardInterrupt:
            pass
        return

    store = StateStore(_home() / "state.sqlite3")
    try:
        if args.command == "workspace":
            if args.workspace_command == "add":
                try:
                    path = validate_access_root(args.path)
                except (OSError, ValueError) as exc:
                    raise SystemExit(str(exc)) from exc
                added = store.add_workspace(str(path))
                logging.info("Workspace %s: %s", "registered" if added else "already registered", path)
            elif args.workspace_command == "remove":
                store.remove_workspace(str(Path(args.path).expanduser().resolve(strict=False)))
            print("\n".join(store.workspaces()))
        elif args.command == "projects":
            print(json.dumps(discover_projects(store.workspaces()), indent=2))
        elif args.command == "agents":
            print(json.dumps([item.__dict__ for item in AgentRegistry().list()], indent=2))
    finally:
        store.close()
