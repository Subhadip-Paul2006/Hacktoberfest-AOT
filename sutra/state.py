"""Small SQLite store for local device state and approved workspace roots."""
from __future__ import annotations
import sqlite3
from threading import RLock
from pathlib import Path

class StateStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS device (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS workspaces (path TEXT PRIMARY KEY)")
        self._db.commit()
    def get_device(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM device WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    def set_device(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO device(key,value) VALUES (?,?)", (key, value)); self._db.commit()
    def workspaces(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT path FROM workspaces ORDER BY path COLLATE NOCASE")]
    def add_workspace(self, path: str) -> bool:
        with self._lock:
            before = self._db.total_changes
            self._db.execute("INSERT OR IGNORE INTO workspaces(path) VALUES (?)", (path,)); self._db.commit()
            return self._db.total_changes > before
    def remove_workspace(self, path: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM workspaces WHERE path=?", (path,)); self._db.commit()
            return cur.rowcount > 0
    def close(self) -> None:
        with self._lock:
            self._db.close()
