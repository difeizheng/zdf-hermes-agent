"""Coordinator configuration loading.

Independent config — does not modify hermes_cli/config.py.
Loads from ~/.hermes/config.yaml under "orchestrator" key.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _hermes_home() -> Path:
    """Profile-safe Hermes home directory.

    Prefers hermes_constants.get_hermes_home() which handles
    profile-aware paths and data-corruption warnings. Falls back
    to HERMES_HOME env var then ~/.hermes.
    """
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except ImportError:
        pass
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
            logger.warning("Config load failed", exc_info=True)

    return defaults


def load_llm_config() -> dict[str, Any]:
    """Load LLM configuration for Design Agent from config.yaml.

    Single source of truth: the Hermes ``providers`` dict in
    ``~/.hermes/config.yaml`` (the same config Hermes itself uses for
    ``config.model``). This avoids forcing operators to configure the
    design agent's LLM in a separate place.

    Lookup priority (highest to lowest):

    1. ``orchestrator.design_llm`` explicit override — power users can
       pin the design agent to a different model than Hermes' default
       (e.g. force opus for design while chat uses sonnet).
    2. Hermes ``config.model`` + ``config.providers[provider_key]`` —
       the normal case. If ``config.model`` is e.g. ``anthropic/
       claude-opus-4-8``, the matching provider's ``api_key``,
       ``base_url``, and model alias are returned.
    3. Hard fallback: ``ANTHROPIC_API_KEY`` env var + ``claude-opus-4-8``.

    Returns a dict with keys ``model``, ``api_key``, ``base_url``,
    ``max_tokens``. Never raises — missing config degrades gracefully
    to the hard fallback so a misconfigured design agent doesn't
    block the rest of the pipeline.
    """
    import os

    fallback: dict[str, Any] = {
        "model": "claude-opus-4-8",
        "max_tokens": 8000,
        "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        "base_url": None,
    }

    # Tier 1: explicit orchestrator.design_llm override.
    orch_cfg = load_config()
    design_llm = orch_cfg.get("design_llm", {}) if isinstance(orch_cfg, dict) else {}
    if not isinstance(design_llm, dict):
        design_llm = {}

    # Tier 1a: provider reference (resolved against Hermes providers list).
    provider_ref = design_llm.get("provider")
    if provider_ref:
        provider = _find_hermes_provider(provider_ref)
        if provider:
            return _llm_from_provider(
                provider,
                requested_model=design_llm.get("model", ""),
                max_tokens=int(design_llm.get("max_tokens", 8000)),
            )

    # Tier 1b: direct fields (no provider reference). These OVERRIDE
    # whatever Hermes defaults to — but only the fields the user
    # actually set. Unset fields (api_key, base_url) are inherited
    # from the matching Hermes provider so the operator doesn't have
    # to repeat themselves.
    if design_llm.get("model") or design_llm.get("base_url") or design_llm.get("api_key_env"):
        # First, get the Hermes baseline for the default model so we
        # have api_key / base_url to fall back on.
        baseline = _load_hermes_default_llm() or dict(fallback)
        result = dict(baseline)
        if design_llm.get("model"):
            result["model"] = design_llm["model"]
        if design_llm.get("max_tokens"):
            result["max_tokens"] = int(design_llm["max_tokens"])
        if design_llm.get("base_url"):
            result["base_url"] = design_llm["base_url"]
        api_key_env = design_llm.get("api_key_env", "ANTHROPIC_API_KEY")
        env_key = os.environ.get(api_key_env)
        if env_key:
            result["api_key"] = env_key
        return result

    # Tier 2: Hermes default model + matching provider.
    baseline = _load_hermes_default_llm()
    if baseline is not None:
        # Apply max_tokens override from orchestrator.design_llm if set
        if design_llm.get("max_tokens"):
            baseline["max_tokens"] = int(design_llm["max_tokens"])
        return baseline
    return fallback


def _load_hermes_default_llm() -> dict[str, Any] | None:
    """Resolve Hermes' default ``config.model`` against its providers dict.

    Returns None if Hermes config isn't available or has no model
    configured, signaling the caller to use the hard fallback.
    """
    try:
        from hermes_cli.config import load_config as _hermes_load_config
        hermes_cfg = _hermes_load_config() or {}
    except Exception:
        return None

    model_str = str(hermes_cfg.get("model", "")).strip()
    if not model_str:
        return None

    if "/" in model_str:
        provider_key, model_alias = model_str.split("/", 1)
    else:
        provider_key, model_alias = "anthropic", model_str

    provider = _find_hermes_provider(provider_key, hermes_cfg=hermes_cfg)
    if not provider:
        return None

    return _llm_from_provider(
        provider,
        requested_model=model_alias,
        max_tokens=8000,
    )


def _find_hermes_provider(
    provider_ref: str,
    hermes_cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Look up a provider by key/name in Hermes' normalized providers list.

    Reads the on-disk Hermes config (``providers`` dict and the legacy
    ``custom_providers`` list) and returns the first matching entry.
    Returns None if not configured.
    """
    if hermes_cfg is None:
        try:
            from hermes_cli.config import load_config as _hermes_load_config
            hermes_cfg = _hermes_load_config() or {}
        except Exception:
            return None

    # Normalize the new ``providers: {name: {...}}`` dict into the legacy
    # list shape, then scan both. providers_dict_to_custom_providers is
    # the official Hermes helper for this — using it keeps us in sync
    # with whatever schema changes Hermes ships.
    try:
        from hermes_cli.config import providers_dict_to_custom_providers
        providers = providers_dict_to_custom_providers(hermes_cfg.get("providers", {}))
    except Exception:
        providers = []

    # Also include the legacy list in case it's still in use.
    legacy = hermes_cfg.get("custom_providers", [])
    if isinstance(legacy, list):
        providers = providers + legacy

    ref = provider_ref.strip().lower()
    for p in providers:
        if not isinstance(p, dict):
            continue
        if str(p.get("provider_key", "")).lower() == ref:
            return p
        if str(p.get("name", "")).lower() == ref:
            return p
    return None


def _llm_from_provider(
    provider: dict[str, Any],
    *,
    requested_model: str = "",
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """Convert a normalized Hermes provider entry into the design-agent LLM dict.

    Resolves the model name through the provider's ``models`` alias map
    (e.g. ``claude-opus-4-8`` -> ``{"name": "claude-opus-4-8"}``). Falls
    back to the alias as-is if the provider has no models map.
    """
    result: dict[str, Any] = {
        "model": requested_model or "claude-opus-4-8",
        "max_tokens": max_tokens,
        "api_key": provider.get("api_key") or None,
        "base_url": provider.get("base_url") or None,
    }

    if requested_model:
        models_map = provider.get("models", {}) or {}
        if requested_model in models_map:
            entry = models_map[requested_model]
            if isinstance(entry, dict) and entry.get("name"):
                result["model"] = entry["name"]

    # api_key in config may be a literal or an env-var name; resolve here
    # so callers always get a usable key or None.
    raw_key = result["api_key"]
    if raw_key and isinstance(raw_key, str) and not raw_key.startswith("sk-") and not raw_key.startswith("sk_"):
        import os
        resolved = os.environ.get(raw_key)
        if resolved:
            result["api_key"] = resolved

    return result


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
