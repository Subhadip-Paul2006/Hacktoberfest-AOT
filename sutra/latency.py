"""Bounded in-memory latency samples for operational status and debugging."""
from __future__ import annotations

from collections import defaultdict, deque
from threading import RLock
from typing import Any


class LatencyMetrics:
    def __init__(self, sample_limit: int = 1000):
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=sample_limit))
        self._lock = RLock()

    def record(self, name: str, duration_ms: float) -> None:
        with self._lock:
            self._samples[name].append(max(0.0, duration_ms))

    def summary(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            sample_sets = {name: sorted(values) for name, values in self._samples.items()}
        result = {}
        for name, values in sample_sets.items():
            if not values:
                continue
            result[name] = {
                "count": len(values),
                "p50_ms": round(_percentile(values, 0.50), 3),
                "p95_ms": round(_percentile(values, 0.95), 3),
                "p99_ms": round(_percentile(values, 0.99), 3),
            }
        return result


def _percentile(values: list[float], percentile: float) -> float:
    index = max(0, min(len(values) - 1, int((len(values) - 1) * percentile + 0.5)))
    return values[index]
