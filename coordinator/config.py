"""Coordinator configuration loading.

Independent config — does not modify hermes_cli/config.py.
Loads from ~/.hermes/config.yaml under "orchestrator" key.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _hermes_home() -> Path:
    """Profile-safe Hermes home directory."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".hermes"


def _default_workspace_dir() -> str:
    """Platform-aware default workspace directory."""
    env = os.environ.get("HERMES_WORKSPACE")
    if env:
        return env
    return str(_hermes_home() / "workspace")


def load_config() -> dict[str, Any]:
    """Load orchestrator config from ~/.hermes/config.yaml."""
    cfg_path = _hermes_home() / "config.yaml"
    defaults = {
        "port": 9100,
        "db_path": str(_hermes_home() / "tasks.db"),
        "heartbeat_interval": 60,
        "stale_timeout": 120,
        "workspace_dir": _default_workspace_dir(),
        "max_retries": 0,
        "retry_delay": 30,
        "profile": "tdd-developer",
        "quality_gate_enabled": True,
        "min_coverage": 80.0,
        "max_critical_issues": 0,
        "max_high_issues": 2,
        "max_validate_retries": 3,
        "claude_code_timeout": 1800,
        "test_timeout": 600,
        "coverage_timeout": 300,
        "deploy_timeout": 3600,
    }
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8-sig") as f:
                raw = yaml.safe_load(f) or {}
            override = raw.get("orchestrator", {})
            if isinstance(override, dict):
                defaults.update(override)
        except Exception:
            pass

    return defaults


def load_llm_config() -> dict[str, Any]:
    """Load LLM configuration for Design Agent from config.yaml.

    Reads the `orchestrator.design_llm` section. Supports two modes:

    Mode 1 — Direct configuration:
        orchestrator:
          design_llm:
            model: claude-opus-4-8
            api_key_env: ANTHROPIC_API_KEY    # env var name (default)
            base_url: https://api.anthropic.com  # optional
            max_tokens: 8000

    Mode 2 — Reference a custom_provider:
        orchestrator:
          design_llm:
            provider: bailian         # references custom_providers[].name
            model: glm-5

    Falls back to ANTHROPIC_API_KEY env + claude-opus-4-8 if not configured.
    """
    import os
    cfg = load_config()
    llm = cfg.get("design_llm", {})
    if not isinstance(llm, dict):
        llm = {}

    result: dict[str, Any] = {
        "model": "claude-opus-4-8",
        "max_tokens": 8000,
        "api_key": None,
        "base_url": None,
    }

    # Mode 2: reference a custom_provider
    provider_name = llm.get("provider")
    if provider_name:
        cfg_path = _hermes_home() / "config.yaml"
        if cfg_path.exists():
            try:
                import yaml
                with open(cfg_path, encoding="utf-8-sig") as f:
                    raw = yaml.safe_load(f) or {}
                providers = raw.get("custom_providers", [])
                for p in providers:
                    if p.get("name") == provider_name:
                        result["base_url"] = p.get("base_url")
                        api_key = p.get("api_key")
                        if api_key:
                            result["api_key"] = api_key
                        # Resolve model name from provider's models dict
                        model_name = llm.get("model", "")
                        models = p.get("models", {})
                        if model_name and model_name in models:
                            result["model"] = models[model_name].get("name", model_name)
                        elif model_name:
                            result["model"] = model_name
                        break
            except Exception:
                pass
    else:
        # Mode 1: direct configuration
        if llm.get("model"):
            result["model"] = llm["model"]
        if llm.get("max_tokens"):
            result["max_tokens"] = int(llm["max_tokens"])
        if llm.get("base_url"):
            result["base_url"] = llm["base_url"]
        # API key from env var name or direct
        api_key_env = llm.get("api_key_env", "ANTHROPIC_API_KEY")
        result["api_key"] = os.environ.get(api_key_env)

    # Fallback: ANTHROPIC_API_KEY env var if not set yet
    if not result["api_key"]:
        result["api_key"] = os.environ.get("ANTHROPIC_API_KEY")

    return result
    """Load orchestrator config from ~/.hermes/config.yaml."""
    cfg_path = _hermes_home() / "config.yaml"
    defaults = {
        "port": 9100,
        "db_path": str(_hermes_home() / "tasks.db"),
        "heartbeat_interval": 60,
        "stale_timeout": 120,
        "workspace_dir": _default_workspace_dir(),
        "max_retries": 0,
        "retry_delay": 30,
        "profile": "tdd-developer",  # Default profile for dev tasks
        "quality_gate_enabled": True,
        "min_coverage": 80.0,
        "max_critical_issues": 0,
        "max_high_issues": 2,
    }
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8-sig") as f:
                raw = yaml.safe_load(f) or {}
            override = raw.get("orchestrator", {})
            if isinstance(override, dict):
                defaults.update(override)
        except Exception:
            pass

    return defaults


def init_memory_if_needed(workspace_dir: str) -> None:
    """Initialize default memories in workspace if not already present.

    Call this once at server startup, not on every load_config() call.
    """
    workspace = Path(workspace_dir)
    if workspace.exists():
        from coordinator.memory import init_default_memories
        memory_dir = workspace / "memory"
        if not memory_dir.exists():
            init_default_memories(workspace)


def get_profile_config(profile_name: str) -> dict[str, Any] | None:
    """Get profile configuration by name."""
    from coordinator.profiles import get_profile
    profile = get_profile(profile_name)
    if profile:
        return profile.to_dict()
    return None
