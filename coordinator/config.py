"""Coordinator configuration loading.

Independent config — does not modify hermes_cli/config.py.
Loads from ~/.hermes/config.yaml under "orchestrator" key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _hermes_home() -> Path:
    """Profile-safe Hermes home directory."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".hermes"


def load_config() -> dict[str, Any]:
    """Load orchestrator config from ~/.hermes/config.yaml."""
    cfg_path = _hermes_home() / "config.yaml"
    defaults = {
        "port": 9100,
        "db_path": str(_hermes_home() / "tasks.db"),
        "heartbeat_interval": 60,
        "stale_timeout": 120,
        "artifact_dir": str(_hermes_home() / "tasks"),
        "max_retries": 0,
        "retry_delay": 30,
    }
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            override = raw.get("orchestrator", {})
            if isinstance(override, dict):
                defaults.update(override)
        except Exception:
            pass
    return defaults
