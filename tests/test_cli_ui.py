"""Unit tests for SŪTRA CLI UI presentation and animated startup experience."""
from __future__ import annotations

import io
import os
import unittest
from unittest.mock import MagicMock, patch

from rich.console import Console

from sutra.cli import main
from sutra.ui import StartupState, SutraUI, is_interactive, supports_unicode


class SutraUITests(unittest.TestCase):
    def test_banner_rendering_plain_fallback(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True)
        ui = SutraUI(console=console)
        ui.interactive = False
        ui.print_banner()
        output = buf.getvalue()
        self.assertIn("SAMANVAYA", output)
        self.assertIn("Remote Agent Runtime", output)
        self.assertIn("By The Loozers", output)

    def test_banner_rendering_interactive(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, no_color=True)
        ui = SutraUI(console=console)
        ui.interactive = True
        ui.print_banner()
        output = buf.getvalue()
        self.assertIn("SAMANVAYA", output)
        self.assertIn("By The Loozers", output)

    def test_status_panel_plain_fallback(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True)
        ui = SutraUI(console=console)
        ui.interactive = False

        state = StartupState(
            config_loaded=True,
            workspaces_count=2,
            projects_count=4,
            control_plane_status="CONNECTED",
            telegram_status="CONNECTED",
            agents_count=5,
            is_ready=True,
        )
        ui.render_status_panel(state)
        output = buf.getvalue()
        self.assertIn("Runtime:       ONLINE", output)
        self.assertIn("Control Plane: CONNECTED", output)
        self.assertIn("Workspace:     4 projects (2 roots)", output)
        self.assertIn("Telegram:      CONNECTED", output)
        self.assertIn("Agents:        5 available", output)

    def test_run_animated_startup_execution(self):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True)
        ui = SutraUI(console=console)
        ui.interactive = False

        stages_visited = []

        def dummy_callback(stage: str, state: StartupState):
            stages_visited.append(stage)
            if stage == "Agents":
                state.agents_count = 5

        state = ui.run_animated_startup(dummy_callback)
        self.assertTrue(state.is_ready)
        self.assertEqual(len(stages_visited), 6)
        self.assertIn("Configuration", stages_visited)
        self.assertIn("State", stages_visited)
        self.assertIn("Workspace", stages_visited)


class SutraCLIRegressionTests(unittest.TestCase):
    @patch("sys.argv", ["sutra", "status"])
    def test_cli_status_command(self):
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            main()
            output = fake_out.getvalue()
            self.assertIn("Runtime", output)
            self.assertIn("ONLINE", output)

    @patch("sys.argv", ["sutra"])
    def test_cli_default_startup_command(self):
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            main()
            output = fake_out.getvalue()
            self.assertIn("SAMANVAYA", output)
            self.assertIn("By The Loozers", output)

    @patch("sys.argv", ["sutra", "agents"])
    def test_cli_agents_command_preserved(self):
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            main()
            output = fake_out.getvalue()
            self.assertIn("Antigravity", output)
            self.assertIn("Cursor", output)
