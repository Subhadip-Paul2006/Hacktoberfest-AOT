"""Deterministic detection of AI agent loops, repeated errors, and cyclic tool executions."""
from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

# Normalize variable memory addresses (0x7fff...), line numbers in temporary files, and timestamps
MEMORY_ADDR_PATTERN = re.compile(r"0x[0-9a-fA-F]{6,}")
TIMESTAMP_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")


def normalize_fingerprint(text: str) -> str:
    """Produce a stable hash fingerprint of error message or command string."""
    if not text:
        return ""
    cleaned = MEMORY_ADDR_PATTERN.sub("0xADDR", text)
    cleaned = TIMESTAMP_PATTERN.sub("TIMESTAMP", cleaned)
    cleaned = " ".join(cleaned.split())
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


@dataclass
class LoopEvidence:
    loop_type: str  # "repeated_command", "repeated_error", "cycle_detected", "repeated_file_diff"
    count: int
    sample: str
    details: str


class AgentLoopDetector:
    """Tracks session execution events and flags stuck or looping agent states deterministically."""

    def __init__(
        self,
        command_threshold: int = 3,
        error_threshold: int = 3,
        cycle_length: int = 2,
        history_window: int = 20,
    ) -> None:
        self.command_threshold = command_threshold
        self.error_threshold = error_threshold
        self.cycle_length = cycle_length

        self._command_history: deque[str] = deque(maxlen=history_window)
        self._raw_commands: deque[str] = deque(maxlen=history_window)

        self._error_history: deque[str] = deque(maxlen=history_window)
        self._raw_errors: deque[str] = deque(maxlen=history_window)

        self._action_history: deque[str] = deque(maxlen=history_window)

    def record_command(self, cmd: str | Sequence[str]) -> LoopEvidence | None:
        """Record an executed command and return LoopEvidence if a loop is detected."""
        text = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        fp = normalize_fingerprint(text)
        self._command_history.append(fp)
        self._raw_commands.append(text)
        self._action_history.append(f"cmd:{fp}")

        # Check consecutive identical commands
        streak = 0
        for past_fp in reversed(self._command_history):
            if past_fp == fp:
                streak += 1
            else:
                break

        if streak >= self.command_threshold:
            return LoopEvidence(
                loop_type="repeated_command",
                count=streak,
                sample=text,
                details=f"Command repeated {streak} consecutive times without progress: {text[:100]}",
            )

        return self._check_cycles()

    def record_error(self, error: str) -> LoopEvidence | None:
        """Record an error and check for repeated identical failures."""
        if not error:
            return None
        fp = normalize_fingerprint(error)
        self._error_history.append(fp)
        self._raw_errors.append(error)
        self._action_history.append(f"err:{fp}")

        streak = 0
        for past_fp in reversed(self._error_history):
            if past_fp == fp:
                streak += 1
            else:
                break

        if streak >= self.error_threshold:
            return LoopEvidence(
                loop_type="repeated_error",
                count=streak,
                sample=error[:120],
                details=f"Identical error occurred {streak} consecutive times: {error[:120]}",
            )

        return self._check_cycles()

    def record_action(self, action_name: str) -> LoopEvidence | None:
        """Record a general tool/agent action name."""
        fp = normalize_fingerprint(action_name)
        self._action_history.append(f"act:{fp}")
        return self._check_cycles()

    def _check_cycles(self) -> LoopEvidence | None:
        """Detect 2-action or 3-action oscillation (e.g. A -> B -> A -> B -> A -> B)."""
        actions = list(self._action_history)
        n = len(actions)
        if n < 4:
            return None

        # Check cycle of length 2: (A, B) repeated 2+ times
        if n >= 4 and actions[-1] == actions[-3] and actions[-2] == actions[-4] and actions[-1] != actions[-2]:
            return LoopEvidence(
                loop_type="cycle_detected",
                count=2,
                sample=f"{actions[-2]} <-> {actions[-1]}",
                details="Agent is oscillating between two alternating actions without making forward progress",
            )

        # Check cycle of length 3: (A, B, C) repeated 2 times
        if n >= 6 and actions[-1] == actions[-4] and actions[-2] == actions[-5] and actions[-3] == actions[-6]:
            return LoopEvidence(
                loop_type="cycle_detected",
                count=2,
                sample=f"{actions[-3]} -> {actions[-2]} -> {actions[-1]}",
                details="Agent is stuck in a 3-step action cycle",
            )

        return None
