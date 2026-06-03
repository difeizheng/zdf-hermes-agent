"""Shared agent helpers — config loading utilities used across all agents.

Replaces the per-agent _get_timeout / _get_min_coverage / _get_quality_thresholds
copies (5+ duplicates). Single source of truth for config access patterns.
"""

from __future__ import annotations

from typing import Any

from coordinator.config import load_config, _default_workspace_dir


def get_timeout(key: str, default: int) -> int:
    """Load timeout value from config (cached per call)."""
    try:
        return int(load_config().get(key, default))
    except Exception:
        return default


def get_min_coverage() -> float:
    """Load minimum coverage threshold from config."""
    try:
        return float(load_config().get("min_coverage", 80.0))
    except Exception:
        return 80.0


def get_quality_thresholds() -> tuple[int, int, float]:
    """Load quality gate thresholds: (max_critical, max_high, min_coverage)."""
    try:
        cfg = load_config()
        return (
            int(cfg.get("max_critical_issues", 0)),
            int(cfg.get("max_high_issues", 2)),
            float(cfg.get("min_coverage", 80.0)),
        )
    except Exception:
        return (0, 2, 80.0)


def get_max_validate_retries() -> int:
    """Load max validate retries from config."""
    try:
        return int(load_config().get("max_validate_retries", 3))
    except Exception:
        return 3


def get_workspace_dir(cfg: dict[str, Any] | None = None) -> str:
    """Get workspace directory from config or default."""
    if cfg is None:
        cfg = load_config()
    return cfg.get("workspace_dir") or _default_workspace_dir()
