"""Shared policy and hysteresis for small control-plane network probes."""
from __future__ import annotations

import os
import math
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectivityPolicy:
    warning_rtt_ms: float = 800.0
    max_rtt_ms: float = 1500.0
    max_packet_loss_percent: float = 10.0
    request_warning_seconds: float = 3.0
    request_timeout_seconds: float = 7.0
    probe_interval_seconds: float = 5.0
    recovery_probe_count: int = 3
    failure_probe_count: int = 3

    @classmethod
    def from_environment(cls) -> "ConnectivityPolicy":
        def number(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return default if raw is None else float(raw)

        def count(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return default if raw is None else int(raw)

        policy = cls(
            warning_rtt_ms=number("NETWORK_WARNING_RTT_MS", 800.0),
            max_rtt_ms=number("NETWORK_MAX_RTT_MS", 1500.0),
            max_packet_loss_percent=number("NETWORK_MAX_PACKET_LOSS_PERCENT", 10.0),
            request_warning_seconds=number("CONTROL_REQUEST_WARNING_SECONDS", 3.0),
            request_timeout_seconds=number("CONTROL_REQUEST_TIMEOUT_SECONDS", 7.0),
            probe_interval_seconds=number("NETWORK_PROBE_INTERVAL_SECONDS", 5.0),
            recovery_probe_count=count("NETWORK_RECOVERY_PROBE_COUNT", 3),
            failure_probe_count=count("NETWORK_FAILURE_PROBE_COUNT", 3),
        )
        numeric_values = (policy.warning_rtt_ms, policy.max_rtt_ms, policy.max_packet_loss_percent,
                          policy.request_warning_seconds, policy.request_timeout_seconds,
                          policy.probe_interval_seconds)
        if (not all(math.isfinite(value) for value in numeric_values)
                or policy.warning_rtt_ms < 0 or policy.max_rtt_ms <= policy.warning_rtt_ms
                or not 0 <= policy.max_packet_loss_percent <= 100
                or policy.request_warning_seconds <= 0
                or policy.request_timeout_seconds <= policy.request_warning_seconds
                or policy.probe_interval_seconds <= 0
                or policy.recovery_probe_count < 1 or policy.failure_probe_count < 1):
            raise ValueError("invalid connectivity policy")
        return policy


class ConnectivityGuard:
    def __init__(self, policy: ConnectivityPolicy | None = None, window_size: int = 10):
        self.policy = policy or ConnectivityPolicy.from_environment()
        self.state = "DISCONNECTED"
        self.rtt_ms: float | None = None
        self.last_success_at: float | None = None
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self._probes: deque[bool] = deque(maxlen=max(1, window_size))

    @property
    def packet_loss_percent(self) -> float | None:
        if not self._probes:
            return None
        return 100.0 * sum(not success for success in self._probes) / len(self._probes)

    def record_success(self, rtt_ms: float, now: float) -> str:
        if rtt_ms < 0:
            raise ValueError("RTT cannot be negative")
        self.rtt_ms = rtt_ms
        self.last_success_at = now
        self._probes.append(True)
        quality_failed = (rtt_ms > self.policy.max_rtt_ms
                          or (self.packet_loss_percent is not None
                              and self.packet_loss_percent > self.policy.max_packet_loss_percent))
        if quality_failed:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self.state = ("UNUSABLE" if self.consecutive_failures >= self.policy.failure_probe_count
                          else "DEGRADED")
            return self.state
        self.consecutive_failures = 0
        self.consecutive_successes += 1
        if self.state in {"DISCONNECTED", "UNUSABLE", "RECOVERING"}:
            self.state = "RECOVERING"
            if self.consecutive_successes >= self.policy.recovery_probe_count:
                self.state = self._quality_state()
        else:
            self.state = self._quality_state()
        return self.state

    def record_failure(self) -> str:
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        self._probes.append(False)
        if self.consecutive_failures >= self.policy.failure_probe_count:
            self.state = "UNUSABLE"
        elif self.state == "DISCONNECTED":
            self.state = "DISCONNECTED"
        elif self.state != "UNUSABLE":
            self.state = "DEGRADED"
        return self.state

    def disconnected(self) -> None:
        self.state = "DISCONNECTED"
        self.rtt_ms = None
        self.consecutive_successes = 0
        self.consecutive_failures = 0

    def allows_request(self) -> bool:
        return self.state not in {"UNUSABLE", "DISCONNECTED"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 1),
            "packet_loss_percent": (None if self.packet_loss_percent is None
                                    else round(self.packet_loss_percent, 1)),
            "last_success_at": self.last_success_at,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "throughput": "UNKNOWN",
        }

    def _quality_state(self) -> str:
        if self.rtt_ms is None:
            return "DISCONNECTED"
        if (self.rtt_ms > self.policy.max_rtt_ms
                or (self.packet_loss_percent is not None
                    and self.packet_loss_percent > self.policy.max_packet_loss_percent)):
            return "DEGRADED" if self.consecutive_failures < self.policy.failure_probe_count else "UNUSABLE"
        if self.rtt_ms >= self.policy.warning_rtt_ms:
            return "DEGRADED"
        return "HEALTHY"
