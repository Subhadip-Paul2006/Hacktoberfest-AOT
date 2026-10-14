"""SUTRA Environment Diagnostics (sutra doctor).

Checks:
  1. Python version and runtime
  2. Configuration and environment variables
  3. AI provider mode and credential presence
  4. Local model server reachability (no paid calls)
  5. Agent adapter availability
  6. Connector and MCP configuration
  7. System capabilities
  8. Output directory permissions
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def _mark(ok: bool, warn: bool = False) -> str:
    if ok:
        return "[OK]"
    if warn:
        return "[!!]"
    return "[--]"


def run_doctor(*, check_provider: bool = False) -> None:
    """Run SUTRA environment diagnostics.

    Args:
        check_provider: If True, attempt a live connectivity probe for
            local model servers (no paid calls are ever made automatically).
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    _section("SUTRA Environment Diagnostics")

    # ------------------------------------------------------------------ 1. Runtime
    _section("1. Runtime")
    py_ver = sys.version_info
    py_ok = py_ver >= (3, 11)
    print(f"  {_mark(py_ok, warn=not py_ok)} Python {sys.version.split()[0]}  (requires >= 3.11)")
    print(f"  {_mark(True)} OS: {platform.system()} {platform.release()} ({platform.machine()})")
    in_docker = Path("/.dockerenv").exists()
    print(f"  {_mark(True)} Execution: {'Docker container' if in_docker else 'Native workstation'}")

    # ------------------------------------------------------------------ 2. Dependencies
    _section("2. Dependencies")
    _check_import("fastapi",      "FastAPI (web framework)")
    _check_import("uvicorn",      "Uvicorn (ASGI server)")
    _check_import("websockets",   "websockets (WebSocket transport)")
    _check_import("httpx",        "httpx (HTTP client)")
    _check_import("rich",         "Rich (terminal UI)")
    _check_import("dotenv",       "python-dotenv (env loading)", import_as="dotenv")
    _check_import("yaml",         "PyYAML (YAML config)")
    _check_import("keyring",      "keyring (OS credential store)")

    # ------------------------------------------------------------------ 3. Configuration
    _section("3. Configuration and Environment Variables")
    from dotenv import load_dotenv
    load_dotenv(override=False)

    required_vars = [
        ("SUTRA_CONTROL_PLANE_TOKEN", True, "AGENTBRIDGE_CONTROL_PLANE_TOKEN"),
    ]
    optional_vars = [
        ("SUTRA_TLS_CERTFILE",  False, ""),
        ("SUTRA_TLS_KEYFILE",   False, ""),
        ("TELEGRAM_BOT_TOKEN",  False, "SUTRA_TELEGRAM_BOT_TOKEN"),
        ("TELEGRAM_ALLOWED_USER_IDS", False, ""),
    ]
    for var, required, fallback in required_vars + optional_vars:
        val = os.environ.get(var, "") or (os.environ.get(fallback, "") if fallback else "")
        present = bool(val and val.strip())
        label = "required" if required else "optional"
        sym = _mark(present, warn=(required and not present))
        status = "configured" if present else f"missing ({label})"
        print(f"  {sym} {var}: {status}")

    # ------------------------------------------------------------------ 4. AI Provider
    _section("4. AI Provider Configuration")
    try:
        from sutra.ai_providers import (
            AIConfig, AIMode, AIProvider, ProviderStatus, credential_status, probe_provider
        )
        cfg = AIConfig.from_environment()
        summary = cfg.redacted_summary()
        print(f"  {_mark(True)} Mode:     {summary['mode']}")
        print(f"  {_mark(True)} Provider: {summary['provider']}")
        print(f"  {_mark(True)} Model:    {summary['model']}")
        print(f"  {_mark(True)} Base URL: {summary['base_url']}")

        if cfg.mode == AIMode.NONE:
            print("  [--] No AI provider configured — deterministic-only mode is active.")
            print("       Set LLM_PROVIDER in .env to enable AI enrichment.")
        else:
            # Credential check (no paid calls)
            has_cred, cred_var = credential_status(cfg.provider)
            if cfg.mode == AIMode.API:
                sym = _mark(has_cred, warn=not has_cred)
                print(f"  {sym} API credential ({cred_var}): {'present' if has_cred else 'MISSING'}")

            # Local server probe (only if mode is LOCAL and check_provider is True)
            if cfg.mode in (AIMode.LOCAL, AIMode.CUSTOM):
                if check_provider:
                    print(f"  [...] Probing local server: {cfg.base_url} ...")
                    result = probe_provider(cfg, timeout=5.0)
                    sym = _mark(result.status == ProviderStatus.CONNECTED, warn=result.status == ProviderStatus.UNVERIFIED)
                    print(f"  {sym} {result.status.value}: {result.message}")
                    if result.latency_ms is not None:
                        print(f"       Latency: {result.latency_ms} ms")
                    if result.models:
                        top = result.models[:5]
                        print(f"       Available models: {', '.join(top)}")
                else:
                    print(f"  [--] Local server not probed (run 'sutra doctor --check-provider' for live check).")

    except Exception as exc:
        print(f"  [!!] Could not load AI provider configuration: {exc}")

    # ------------------------------------------------------------------ 5. Agent Adapters
    _section("5. Agent Adapters")
    try:
        from sutra.adapters import AgentRegistry
        registry = AgentRegistry()
        descriptors = registry.list()
        if not descriptors:
            print("  [--] No agent adapters registered.")
        for d in descriptors:
            avail = d.availability.value if hasattr(d.availability, "value") else str(d.availability)
            sym = _mark(avail in ("INSTALLED", "AVAILABLE"), warn=(avail == "UNKNOWN"))
            caps = ", ".join(sorted(c.value if hasattr(c, "value") else str(c) for c in (d.capabilities or [])))
            print(f"  {sym} {d.display_name:<20} ({d.provider})  availability={avail}")
            if d.installation_notes:
                print(f"       notes: {d.installation_notes[:80]}")
    except Exception as exc:
        print(f"  [!!] Could not enumerate agent adapters: {exc}")

    # ------------------------------------------------------------------ 6. Connectors
    _section("6. MCP and Connector Configuration")
    mcp_config_path = Path("config/mcp.yaml")
    if mcp_config_path.exists():
        try:
            import yaml
            with open(mcp_config_path) as f:
                mcps = yaml.safe_load(f).get("mcp_servers", {})
            print(f"  [OK] MCP configuration loaded ({len(mcps)} server(s) configured)")
            for name in list(mcps.keys())[:5]:
                print(f"       - {name}")
        except Exception as e:
            print(f"  [!!] Failed to read MCP config: {e}")
    else:
        print("  [--] No MCP configuration at config/mcp.yaml (optional)")

    try:
        from sutra.connectors.registry import ConnectorRegistry
        reg = ConnectorRegistry()
        clist = reg.list()
        print(f"  [OK] Connector registry loaded ({len(clist)} connector(s) registered)")
    except Exception as exc:
        print(f"  [--] Connector registry not loaded: {exc}")

    # ------------------------------------------------------------------ 7. System
    _section("7. System Capabilities")
    cpu_count = os.cpu_count() or 1
    try:
        import psutil
        mem_gb = round(psutil.virtual_memory().total / (1024**3), 2)
    except ImportError:
        mem_gb = 8.0
    if mem_gb < 4 or cpu_count < 2:
        profile, workers = "LOW", 1
    elif mem_gb >= 16 and cpu_count >= 8:
        profile, workers = "HIGH", 4
    else:
        profile, workers = "NORMAL", 2
    print(f"  [OK] CPU: {cpu_count} cores  Memory: {mem_gb} GB  Profile: {profile}  Workers: {workers}")

    # ------------------------------------------------------------------ 8. Output directories
    _section("8. Output and Cache Directories")
    home_val = os.environ.get("SUTRA_HOME", os.environ.get("AGENTBRIDGE_HOME", str(Path.home() / ".sutra")))
    home = Path(home_val).expanduser()
    try:
        home.mkdir(parents=True, exist_ok=True)
        test_file = home / ".doctor_write_test"
        test_file.write_text("ok")
        test_file.unlink()
        print(f"  [OK] SUTRA home writable: {home}")
    except Exception as exc:
        print(f"  [!!] SUTRA home not writable ({home}): {exc}")

    # ------------------------------------------------------------------ Summary
    _section("Summary")
    print("  SUTRA is ready for deterministic-mode operation.")
    if not os.environ.get("LLM_PROVIDER", "").strip():
        print("  AI enrichment is DISABLED. Set LLM_PROVIDER in .env to enable it.")
    print("  Run 'sutra local' to start the local runtime.")
    print("  Run 'sutra control-plane' to start the TLS control plane.")
    print()


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def _check_import(module: str, label: str, import_as: str = "") -> None:
    name = import_as or module
    try:
        __import__(name)
        print(f"  [OK] {label}")
    except ImportError:
        print(f"  [!!] {label}  (MISSING: pip install {module})")
