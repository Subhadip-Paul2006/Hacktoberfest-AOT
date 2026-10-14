"""Workspace and file concurrency management: shared read and exclusive write locks."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .security import _canonical

log = logging.getLogger(__name__)


class LockMode(str, Enum):
    READ = "READ"      # Shared lock: multiple readers allowed
    WRITE = "WRITE"    # Exclusive lock: only one writer allowed, no readers


class WorkspaceConflictError(Exception):
    """Raised when acquiring a workspace or file lock fails due to an existing conflict."""
    def __init__(self, message: str, path: str, mode: LockMode, held_by_session: str):
        super().__init__(message)
        self.path = path
        self.mode = mode
        self.held_by_session = held_by_session


@dataclass
class LockEntry:
    path: Path
    mode: LockMode
    session_id: str
    acquired_at: float
    is_dir: bool = False


class WorkspaceLockManager:
    """Manages granular shared-read and exclusive-write locks across workspaces and files.
    
    Prevents concurrent modifications (e.g. Agent A and Agent B modifying the same file
    or overlapping paths). Tracks locks per session for automatic cleanup on session termination.
    """

    def __init__(self) -> None:
        self._async_lock = asyncio.Lock()
        # Canonical path -> list of LockEntry
        self._locks: dict[Path, list[LockEntry]] = {}
        # session_id -> set of canonical Path
        self._session_locks: dict[str, set[Path]] = {}

    def _canonical_path(self, target: str | Path) -> Path:
        return _canonical(target)

    def _is_path_overlapping(self, path_a: Path, is_dir_a: bool, path_b: Path, is_dir_b: bool) -> bool:
        """Check if two canonical paths are identical or have an ancestor-descendant overlap."""
        if os.name == "nt":
            str_a = str(path_a).casefold()
            str_b = str(path_b).casefold()
        else:
            str_a = str(path_a)
            str_b = str(path_b)

        if str_a == str_b:
            return True

        # Check directory nesting
        sep = "\\" if os.name == "nt" else "/"
        if is_dir_a and str_b.startswith(str_a + sep):
            return True
        if is_dir_b and str_a.startswith(str_b + sep):
            return True

        return False

    async def acquire(
        self,
        target: str | Path,
        session_id: str,
        mode: LockMode = LockMode.WRITE,
        is_dir: bool | None = None,
        timeout: float = 5.0,
    ) -> bool:
        """Attempt to acquire a lock within timeout seconds.
        
        Raises WorkspaceConflictError if a conflicting lock is held by another session.
        """
        canon = self._canonical_path(target)
        target_is_dir = is_dir if is_dir is not None else (canon.is_dir() if canon.exists() else False)

        async with self._async_lock:
            # Check for existing conflicting locks across all paths
            for existing_path, entries in self._locks.items():
                for entry in entries:
                    if entry.session_id == session_id:
                        # Reentrant lock for same session
                        if entry.path == canon:
                            if mode == LockMode.WRITE and entry.mode == LockMode.READ:
                                # Upgrade lock request: check if other sessions also hold read locks
                                other_holders = [e for e in entries if e.session_id != session_id]
                                if other_holders:
                                    conflict = other_holders[0]
                                    raise WorkspaceConflictError(
                                        f"Cannot upgrade to WRITE lock on {canon}: held by session {conflict.session_id}",
                                        path=str(canon),
                                        mode=mode,
                                        held_by_session=conflict.session_id,
                                    )
                                entry.mode = LockMode.WRITE
                                return True
                            return True

                    # Overlapping paths between different sessions
                    if self._is_path_overlapping(canon, target_is_dir, entry.path, entry.is_dir):
                        # Conflict if either wants WRITE
                        if mode == LockMode.WRITE or entry.mode == LockMode.WRITE:
                            raise WorkspaceConflictError(
                                f"Lock conflict on {canon} ({mode.value}): currently held by session {entry.session_id} ({entry.mode.value})",
                                path=str(canon),
                                mode=mode,
                                held_by_session=entry.session_id,
                            )

            # No conflicts found; grant lock
            import time
            entry = LockEntry(
                path=canon,
                mode=mode,
                session_id=session_id,
                acquired_at=time.time(),
                is_dir=target_is_dir,
            )
            self._locks.setdefault(canon, []).append(entry)
            self._session_locks.setdefault(session_id, set()).add(canon)
            log.debug("Acquired lock %s (%s) for session %s", canon, mode.value, session_id)
            return True

    async def release(self, target: str | Path, session_id: str) -> bool:
        """Release a specific lock held by a session."""
        canon = self._canonical_path(target)
        async with self._async_lock:
            entries = self._locks.get(canon, [])
            self._locks[canon] = [e for e in entries if e.session_id != session_id]
            if not self._locks[canon]:
                self._locks.pop(canon, None)

            if session_id in self._session_locks:
                self._session_locks[session_id].discard(canon)
                if not self._session_locks[session_id]:
                    self._session_locks.pop(session_id, None)

            log.debug("Released lock %s for session %s", canon, session_id)
            return True

    async def release_session_locks(self, session_id: str) -> int:
        """Release all locks held by a session (called automatically on stop/exit/cleanup)."""
        async with self._async_lock:
            paths = list(self._session_locks.pop(session_id, set()))
            for path in paths:
                entries = self._locks.get(path, [])
                self._locks[path] = [e for e in entries if e.session_id != session_id]
                if not self._locks[path]:
                    self._locks.pop(path, None)

            count = len(paths)
            if count > 0:
                log.info("Released %d locks for session %s", count, session_id)
            return count

    async def get_held_locks(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Inspect all currently held locks."""
        async with self._async_lock:
            result = []
            for path, entries in self._locks.items():
                for entry in entries:
                    if session_id is None or entry.session_id == session_id:
                        result.append({
                            "path": str(entry.path),
                            "mode": entry.mode.value,
                            "session_id": entry.session_id,
                            "acquired_at": entry.acquired_at,
                            "is_dir": entry.is_dir,
                        })
            return result
