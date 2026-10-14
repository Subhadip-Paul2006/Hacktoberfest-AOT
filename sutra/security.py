"""Reusable filesystem boundary checks. No file operations are exposed remotely."""
from __future__ import annotations
import ctypes
import os
import ntpath
from pathlib import Path
from pathlib import PureWindowsPath
from urllib.parse import unquote

def _canonical(path: str | Path) -> Path:
    value = str(path)
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value: break
        value = decoded
    else:
        # Reject input that is still changing after the documented decode limit.
        raise ValueError("Path has excessive URL encoding")
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        windows_path = PureWindowsPath(value)
        parts = [part.rstrip(" .") if part not in (windows_path.anchor, ".", "..") else part
                 for part in windows_path.parts]
        value = str(Path(windows_path.anchor, *parts[1:])) if parts else value
    resolved = Path(value).expanduser().resolve(strict=False)
    if os.name == "nt":
        # Expand existing 8.3 aliases so comparisons use the physical long path.
        buffer = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.kernel32.GetLongPathNameW(str(resolved), buffer, len(buffer))
        if result and result < len(buffer):
            resolved = Path(buffer.value).resolve(strict=False)
    return resolved

def validate_workspace_root(path: str | Path) -> Path:
    """Resolve an existing workspace and reject filesystem roots."""
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Workspace must be an existing directory")
    if os.name == "nt":
        if ntpath.normcase(str(resolved).rstrip("\\/")) == ntpath.normcase(str(resolved.anchor).rstrip("\\/")):
            raise ValueError("Drive and share roots cannot be registered as workspaces")
    elif resolved == Path(resolved.anchor):
        raise ValueError("Filesystem root cannot be registered as a workspace")
    return resolved

def validate_access_root(path: str | Path) -> Path:
    """Resolve an explicitly approved discovery root, including a drive/share root."""
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Access root must be an existing directory")
    return resolved

def is_path_allowed(path: str | Path, registered_workspaces: list[str | Path]) -> bool:
    """Return whether a path resolves within an explicitly registered root."""
    try:
        target = _canonical(path)
        for root in registered_workspaces:
            canonical_root = _canonical(root)
            if os.name == "nt":
                root_text = str(canonical_root).rstrip("\\/").casefold()
                target_text = str(target).casefold()
                if target_text == root_text or target_text.startswith(root_text + "\\"):
                    return True
            elif target == canonical_root or target.is_relative_to(canonical_root):
                return True
    except (OSError, RuntimeError, ValueError):
        return False
    return False

def require_allowed(path: str | Path, registered_workspaces: list[str | Path]) -> Path:
    try:
        target = _canonical(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PermissionError(f"Path could not be safely normalized: {path}") from exc
    if not is_path_allowed(target, registered_workspaces):
        raise PermissionError(f"Path is outside registered workspaces: {path}")
    return target
