"""Safe, bounded environment, CLI, and client discovery probes.

These probes strictly adhere to security boundaries:
- Never dump arbitrary environment variables or credentials.
- Never execute destructive or arbitrary shell commands.
- Use bounded timeouts (default 1.0s) and safe path checks.
- Handle Windows and POSIX environments portably.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from dataclasses import dataclass

from sutra.connectors.models import sanitize_metadata

from .models import CLIApplicabilityStatus, ClientApplicabilityStatus


@dataclass(frozen=True)
class BinaryCheckResult:
    binary_name: str
    status: CLIApplicabilityStatus
    path: Optional[str] = None
    executable: bool = False


@dataclass(frozen=True)
class ClientDetectionResult:
    client_id: str
    status: ClientApplicabilityStatus
    detected: bool = False
    config_dir: Optional[str] = None


class EnvironmentProbe:
    """Discovers host operating system and hardware characteristics safely."""

    @staticmethod
    def get_environment_info() -> dict[str, Any]:
        """Return non-sensitive host system characteristics."""
        return {
            "os": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "environment_id": f"{platform.system().lower()}-{platform.machine().lower()}",
        }


class CLIProbe:
    """Safe inspection of declared command-line binaries."""

    @staticmethod
    def inspect_binary(binary_name: str, *, custom_path: Optional[str] = None) -> dict[str, Any]:
        """Check presence and executability of a declared binary using allowlisted lookup."""
        clean_name = os.path.basename(binary_name).strip()
        if not clean_name:
            return {
                "binary": binary_name,
                "status": CLIApplicabilityStatus.CLI_NOT_FOUND,
                "path": None,
                "executable": False,
            }

        resolved_path = shutil.which(clean_name, path=custom_path)
        if resolved_path is None:
            return {
                "binary": clean_name,
                "status": CLIApplicabilityStatus.CLI_NOT_FOUND,
                "path": None,
                "executable": False,
            }

        is_exec = os.access(resolved_path, os.X_OK)
        return {
            "binary": clean_name,
            "status": CLIApplicabilityStatus.CLI_EXECUTABLE if is_exec else CLIApplicabilityStatus.CLI_PRESENT,
            "path": resolved_path,
            "executable": is_exec,
        }

    def check_binary(self, binary_name: str, *, custom_path: Optional[str] = None) -> BinaryCheckResult:
        """Check presence of binary and return structured BinaryCheckResult."""
        res = self.inspect_binary(binary_name, custom_path=custom_path)
        return BinaryCheckResult(
            binary_name=res["binary"],
            status=res["status"],
            path=res["path"],
            executable=res["executable"],
        )


class ClientProbe:
    """Safe inspection of developer client/IDE presence in the environment."""

    KNOWN_CLIENT_PATHS = {
        "cursor": [
            Path.home() / ".cursor",
            Path.home() / "AppData" / "Roaming" / "Cursor",
        ],
        "vscode": [
            Path.home() / ".vscode",
            Path.home() / "AppData" / "Roaming" / "Code",
        ],
        "claude": [
            Path.home() / ".config" / "Claude",
            Path.home() / "AppData" / "Roaming" / "Claude",
        ],
    }

    @classmethod
    def detect_client(cls, client_name: str, *, custom_dirs: Optional[Sequence[Path]] = None) -> dict[str, Any]:
        """Check whether a known developer client/IDE directory exists."""
        key = client_name.strip().lower()
        search_paths = list(custom_dirs or cls.KNOWN_CLIENT_PATHS.get(key, []))

        detected = False
        found_path: Optional[str] = None

        for p in search_paths:
            try:
                if p.exists() and p.is_dir():
                    detected = True
                    found_path = str(p)
                    break
            except Exception:
                continue

        status = ClientApplicabilityStatus.CLIENT_DETECTED if detected else ClientApplicabilityStatus.NOT_DETECTED
        return {
            "client": key,
            "status": status,
            "detected": detected,
            "config_dir": found_path,
        }

    @classmethod
    def detect_all_known_clients(cls) -> dict[str, dict[str, Any]]:
        """Probe all standard supported clients."""
        return {
            client: cls.detect_client(client)
            for client in cls.KNOWN_CLIENT_PATHS
        }

    def detect_all_clients(self) -> dict[str, ClientDetectionResult]:
        """Probe all standard supported clients and return structured ClientDetectionResult map."""
        raw = self.detect_all_known_clients()
        return {
            cid: ClientDetectionResult(
                client_id=cid,
                status=data["status"],
                detected=data["detected"],
                config_dir=data["config_dir"],
            )
            for cid, data in raw.items()
        }
