"""Orchestrator plugin configuration loading.

Independent from hermes_cli/config.py. Reads from ~/.hermes/config.yaml
under the "orchestrator" key, with env var expansion.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def load_orchestrator_config() -> dict[str, Any]:
    """Load orchestrator config from ~/.hermes/config.yaml."""
    cfg_path = _hermes_home() / "config.yaml"
    defaults: dict[str, Any] = {
        "enabled": False,
        "coordinator_url": "http://localhost:9100",
        "auto_respond": True,
        "max_concurrent_tasks": 3,
    }
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8-sig") as f:
                raw = yaml.safe_load(f) or {}
            override = raw.get("orchestrator", {})
            if isinstance(override, dict):
                # Expand env vars
                for k, v in override.items():
                    if isinstance(v, str):
                        override[k] = os.path.expandvars(v)
                defaults.update(override)
        except Exception:
            pass
    return defaults
