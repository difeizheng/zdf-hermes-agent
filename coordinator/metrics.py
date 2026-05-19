"""Observability metrics for the coordinator.

Simple counters exposed via /metrics endpoint.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class MetricsCollector:
    """Thread-safe metrics collector."""

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._task_counts: dict[str, int] = defaultdict(int)
        self._task_latencies: list[float] = []
        self._error_counts: dict[str, int] = defaultdict(int)
        self._start_time = time.time()

    def record_task_created(self, task_type: str) -> None:
        with self._lock:
            self._task_counts[f"created_{task_type}"] += 1

    def record_task_completed(self, task_type: str, latency_seconds: float) -> None:
        with self._lock:
            self._task_counts[f"completed_{task_type}"] += 1
            self._task_latencies.append(latency_seconds)

    def record_task_failed(self, task_type: str, error_type: str) -> None:
        with self._lock:
            self._task_counts[f"failed_{task_type}"] += 1
            self._error_counts[f"{task_type}_{error_type}"] += 1

    def record_task_timeout(self, task_type: str) -> None:
        with self._lock:
            self._task_counts[f"timeout_{task_type}"] += 1

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            uptime = time.time() - self._start_time
            avg_latency = (
                sum(self._task_latencies) / len(self._task_latencies)
                if self._task_latencies
                else 0
            )
            return {
                "uptime_seconds": round(uptime, 1),
                "task_counts": dict(self._task_counts),
                "total_tasks": sum(
                    v for k, v in self._task_counts.items()
                    if k.startswith(("created_", "completed_", "failed_", "timeout_"))
                ),
                "avg_latency_seconds": round(avg_latency, 2),
                "error_counts": dict(self._error_counts),
            }

    def format_prometheus(self) -> str:
        """Format metrics for Prometheus-style scraping."""
        snap = self.get_snapshot()
        lines = [
            f"# HELP hermes_coordinator_uptime Coordinator uptime in seconds",
            f"# TYPE hermes_coordinator_uptime gauge",
            f"hermes_coordinator_uptime {snap['uptime_seconds']}",
            f"# HELP hermes_coordinator_task_total Total tasks by status",
            f"# TYPE hermes_coordinator_task_total counter",
        ]
        for key, value in snap["task_counts"].items():
            lines.append(f'hermes_coordinator_task_total{{status="{key}"}} {value}')
        lines.extend([
            f"# HELP hermes_coordinator_avg_latency Average task latency",
            f"# TYPE hermes_coordinator_avg_latency gauge",
            f"hermes_coordinator_avg_latency {snap['avg_latency_seconds']}",
        ])
        return "\n".join(lines)
