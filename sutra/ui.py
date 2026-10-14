"""SŪTRA CLI Visual Presentation & Animated Startup Experience.

Provides a modern developer-tool aesthetic using Rich with graceful fallback
to plain text for non-interactive or non-Unicode terminals.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live


def is_interactive() -> bool:
    """Return True if running in an interactive terminal."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("CI") or os.environ.get("DEBIAN_FRONTEND") == "noninteractive":
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def supports_unicode() -> bool:
    """Return True if the current terminal supports UTF-8 characters."""
    encoding = (sys.stdout.encoding or "").lower()
    if "utf" in encoding or "utf-8" in encoding:
        return True
    if os.name != "nt":
        return True
    return bool(os.environ.get("WT_SESSION"))  # Windows Terminal


@dataclass
class StartupState:
    config_loaded: bool = False
    config_path: str = ""
    home_dir: str = ""
    workspaces_count: int = 0
    projects_count: int = 0
    control_plane_status: str = "STANDBY"
    control_plane_url: str = ""
    telegram_status: str = "DISABLED"
    agents_count: int = 0
    is_ready: bool = False


class SutraUI:
    """Terminal UI manager for SAMANVAYA · SŪTRA."""

    def __init__(self, console: Optional[Console] = None) -> None:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:
                pass
        self.unicode = supports_unicode()
        self.interactive = is_interactive()
        self.brand_name = "SŪTRA" if self.unicode else "SUTRA"
        self.console = console or Console(
            highlight=False,
            force_terminal=self.interactive,
            no_color=bool(os.environ.get("NO_COLOR")),
        )

    def print_banner(self) -> None:
        """Render the SAMANVAYA · SŪTRA startup hierarchy banner."""
        if not self.interactive:
            self.console.print(f"=== SAMANVAYA · {self.brand_name} ===")
            self.console.print("Remote Agent Runtime")
            self.console.print("By The Loozers\n")
            return

        # Modern developer-tool aesthetic: clean, minimal, dark-terminal friendly
        content = Text()
        content.append("\n", style="")
        content.append("    SAMANVAYA\n", style="bold cyan")
        content.append(f"    {self.brand_name}\n\n", style="bold white")
        content.append("    Remote Agent Runtime\n\n", style="italic bright_black")
        content.append("                                            By The Loozers\n", style="dim white")

        panel = Panel(
            content,
            border_style="bright_blue",
            padding=(0, 2),
            subtitle="",
        )
        self.console.print(panel)

    def run_animated_startup(
        self,
        perform_stage_callback: Callable[[str, StartupState], None],
    ) -> StartupState:
        """Execute deterministic startup stages with animated indicator."""
        state = StartupState()
        stages = [
            ("Configuration", "Loading configuration and environment"),
            ("State", "Initializing local runtime state"),
            ("Workspace", "Validating registered workspaces"),
            ("Control Plane", "Checking Control Plane connection"),
            ("Bot Integrations", "Discovering ChatOps integrations"),
            ("Agent Registry", "Loading agent adapters"),
        ]

        self.print_banner()

        spinner_frames = ["◌", "◍", "◉"] if self.unicode else [".", "o", "O"]
        check_symbol = "✓" if self.unicode else "[OK]"

        if not self.interactive:
            # Plain fallback mode for non-interactive / CI terminals
            for stage_name, desc in stages:
                perform_stage_callback(stage_name, state)
                self.console.print(f"  {check_symbol} {stage_name}")
            state.is_ready = True
            return state

        # Interactive animated execution
        completed_stages: List[str] = []

        for stage_name, desc in stages:
            # Brief real execution with animation
            frame_idx = 0
            with Live(console=self.console, refresh_per_second=15, transient=True) as live:
                # Run the actual startup work
                perform_stage_callback(stage_name, state)
                # Short indicator frame to give a clean feedback feel without artificial delay
                for _ in range(3):
                    indicator = spinner_frames[frame_idx % len(spinner_frames)]
                    live.update(Text(f"  {indicator} {desc}...", style="cyan"))
                    time.sleep(0.04)
                    frame_idx += 1

            completed_stages.append(stage_name)
            self.console.print(f"  [green]{check_symbol}[/green] [bold white]{stage_name}[/bold white]")

        state.is_ready = True
        self.console.print()
        return state

    def render_status_panel(self, state: StartupState) -> None:
        """Render the compact post-startup status panel."""
        dot = "●" if self.unicode else "*"

        if not self.interactive:
            self.console.print(f"\n{self.brand_name} Runtime Status:")
            self.console.print(f"  Runtime:       ONLINE")
            self.console.print(f"  Control Plane: {state.control_plane_status}")
            self.console.print(f"  Workspace:     {state.projects_count} projects ({state.workspaces_count} roots)")
            self.console.print(f"  Telegram:      {state.telegram_status}")
            self.console.print(f"  Agents:        {state.agents_count} available")
            self.console.print("\nReady.\n")
            return

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", width=16)
        table.add_column(style="bold white")

        table.add_row(f"[green]{dot}[/green] Runtime", "[green]ONLINE[/green]")

        cp_color = "green" if state.control_plane_status in ("CONNECTED", "ONLINE") else ("yellow" if state.control_plane_status == "CONFIGURED" else "bright_black")
        table.add_row(f"[green]{dot}[/green] Control Plane", f"[{cp_color}]{state.control_plane_status}[/{cp_color}]")

        table.add_row(
            f"[green]{dot}[/green] Workspace",
            f"{state.projects_count} project(s) in {state.workspaces_count} root(s)",
        )

        tg_color = "green" if state.telegram_status in ("CONNECTED", "ENABLED") else "bright_black"
        table.add_row(f"[green]{dot}[/green] Telegram", f"[{tg_color}]{state.telegram_status}[/{tg_color}]")

        table.add_row(f"[green]{dot}[/green] Agents", f"{state.agents_count} available")

        panel = Panel(
            table,
            title=f"[bold white]{self.brand_name} Runtime[/bold white]",
            border_style="cyan",
            padding=(1, 2),
            subtitle="[bold green]Ready.[/bold green]",
            subtitle_align="left",
        )
        self.console.print(panel)
        self.console.print()
