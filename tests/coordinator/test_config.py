"""Tests for coordinator.config.load_llm_config (Round 11 refactor).

The refactor: design agent's LLM config now reads Hermes'
``providers`` dict as the single source of truth, instead of
referencing the legacy ``custom_providers`` list (which new Hermes
configs no longer write).

Lookup priority:
  1. ``orchestrator.design_llm`` override (operator can pin a
     different model than Hermes' default)
  2. Hermes ``config.model`` + ``config.providers[provider_key]``
  3. Hard fallback: env ``ANTHROPIC_API_KEY`` + ``claude-opus-4-8``

Each test isolates config state via ``HERMES_HOME`` env var pointing
at a temp directory, so it never touches the real ``~/.hermes/``.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HERMES_HOME to a fresh temp dir; reload config modules."""
    tmp = Path(tempfile.mkdtemp(prefix="hermes_test_"))
    monkeypatch.setenv("HERMES_HOME", str(tmp))
    # hermes_cli.config caches by (mtime, size) keyed on config path —
    # but loading fresh modules in a fresh interpreter is safer.
    import hermes_cli.config
    importlib.reload(hermes_cli.config)
    import coordinator.config
    importlib.reload(coordinator.config)
    return tmp


def _write_config(home: Path, content: str) -> None:
    (home / "config.yaml").write_text(content, encoding="utf-8-sig")


def test_tier_3_hard_fallback_when_unconfigured(hermes_home: Path) -> None:
    """Empty config + no env → hard fallback (model, no key)."""
    from coordinator import config

    _write_config(hermes_home, "")
    monkey_env = os.environ.copy()
    monkey_env.pop("ANTHROPIC_API_KEY", None)
    for k, v in monkey_env.items():
        os.environ[k] = v

    result = config.load_llm_config()
    assert result["model"] == "claude-opus-4-8"
    assert result["max_tokens"] == 8000
    assert result["api_key"] in (None, "")  # env was stripped
    assert result["base_url"] is None


def test_tier_2_uses_hermes_providers_dict(hermes_home: Path) -> None:
    """When Hermes' providers dict is configured, the design agent
    reuses it — model + api_key + base_url all come from the same
    source. No need to also configure ``orchestrator.design_llm``."""
    from coordinator import config

    _write_config(
        hermes_home,
        """
providers:
  anthropic:
    api_key: sk-test-123
    base_url: https://api.anthropic.com
    models:
      claude-sonnet-4-6:
        name: claude-sonnet-4-6
model: anthropic/claude-sonnet-4-6
""",
    )

    result = config.load_llm_config()
    assert result["model"] == "claude-sonnet-4-6"
    assert result["api_key"] == "sk-test-123"
    assert result["base_url"] == "https://api.anthropic.com"


def test_tier_2_resolves_model_alias(hermes_home: Path) -> None:
    """If the provider's models dict maps alias → real name, the alias
    resolves. (e.g. provider says ``claude-sonnet-4-6: {name: claude-sonnet-4-6}``,
    the resolved config carries the real name.)"""
    from coordinator import config

    _write_config(
        hermes_home,
        """
providers:
  anthropic:
    api_key: sk-789
    base_url: https://api.anthropic.com
    models:
      opus-latest:
        name: claude-opus-4-8
model: anthropic/opus-latest
""",
    )

    result = config.load_llm_config()
    assert result["model"] == "claude-opus-4-8"
    assert result["api_key"] == "sk-789"


def test_tier_1_override_model(hermes_home: Path) -> None:
    """``orchestrator.design_llm.model`` overrides the Hermes default
    but inherits api_key/base_url from the Hermes provider (so the
    operator doesn't have to repeat themselves)."""
    from coordinator import config

    _write_config(
        hermes_home,
        """
providers:
  anthropic:
    api_key: sk-inherit
    base_url: https://api.anthropic.com
    models:
      claude-sonnet-4-6:
        name: claude-sonnet-4-6
      claude-opus-4-8:
        name: claude-opus-4-8
model: anthropic/claude-sonnet-4-6
orchestrator:
  design_llm:
    model: claude-opus-4-8
    max_tokens: 16000
""",
    )

    result = config.load_llm_config()
    assert result["model"] == "claude-opus-4-8"  # overridden
    assert result["max_tokens"] == 16000  # overridden
    assert result["api_key"] == "sk-inherit"  # inherited
    assert result["base_url"] == "https://api.anthropic.com"  # inherited


def test_tier_1b_override_via_provider_reference(hermes_home: Path) -> None:
    """``orchestrator.design_llm.provider: bailian`` references any
    Hermes provider by name, including third-party ones (DashScope,
    OpenRouter, etc.). The model alias is resolved through that
    provider's models dict."""
    from coordinator import config

    _write_config(
        hermes_home,
        """
providers:
  bailian:
    api_key: sk-bailian-abc
    base_url: https://dashscope.aliyuncs.com
    models:
      glm-5:
        name: glm-5
  anthropic:
    api_key: sk-anthropic
    base_url: https://api.anthropic.com
    models:
      claude-opus-4-8:
        name: claude-opus-4-8
model: anthropic/claude-opus-4-8
orchestrator:
  design_llm:
    provider: bailian
    model: glm-5
""",
    )

    result = config.load_llm_config()
    assert result["model"] == "glm-5"
    assert result["api_key"] == "sk-bailian-abc"
    assert result["base_url"] == "https://dashscope.aliyuncs.com"


def test_tier_1b_provider_ref_falls_back_to_legacy_list(hermes_home: Path) -> None:
    """Older Hermes configs use ``custom_providers: [...]`` (list).
    The provider ref must still resolve against that shape so users
    upgrading gradually don't break."""
    from coordinator import config

    _write_config(
        hermes_home,
        """
custom_providers:
  - name: legacy-llm
    api_key: sk-legacy
    base_url: https://legacy.example.com
    models:
      legacy-model:
        name: legacy-model-v1
model: anthropic/claude-opus-4-8
orchestrator:
  design_llm:
    provider: legacy-llm
    model: legacy-model
""",
    )

    result = config.load_llm_config()
    assert result["model"] == "legacy-model-v1"
    assert result["api_key"] == "sk-legacy"
    assert result["base_url"] == "https://legacy.example.com"


def test_unknown_provider_returns_fallback(hermes_home: Path) -> None:
    """If the override references a provider that doesn't exist,
    we degrade gracefully to the hard fallback (no exception)."""
    from coordinator import config

    _write_config(
        hermes_home,
        """
providers:
  anthropic:
    api_key: sk-1
    base_url: https://api.anthropic.com
    models:
      claude-opus-4-8:
        name: claude-opus-4-8
model: anthropic/claude-opus-4-8
orchestrator:
  design_llm:
    provider: nonexistent
    model: anything
""",
    )

    # Should not raise
    result = config.load_llm_config()
    assert "model" in result
    assert "api_key" in result
    assert "base_url" in result


def test_env_var_fallback_when_no_config(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTHROPIC_API_KEY env var is the last-resort api_key source."""
    from coordinator import config

    _write_config(hermes_home, "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-fallback")

    result = config.load_llm_config()
    assert result["api_key"] == "sk-env-fallback"
    assert result["model"] == "claude-opus-4-8"  # hard fallback model


def test_load_llm_config_does_not_raise_on_bad_yaml(hermes_home: Path) -> None:
    """A corrupted config.yaml must not break design agent startup
    (load_llm_config is called from the agent loop, not at boot)."""
    from coordinator import config

    # Write something that isn't valid YAML
    (hermes_home / "config.yaml").write_text("just-a-string-not-yaml: : :", encoding="utf-8-sig")

    # Should not raise
    result = config.load_llm_config()
    assert "model" in result
    assert "api_key" in result
    assert "base_url" in result


def test_load_llm_config_handles_model_as_dict(hermes_home: Path) -> None:
    """Hermes 2025+ allows ``model: {default: ..., provider: ...}``
    (a dict, not a string). We must not crash on that shape — we
    just fall through to the hard fallback if the dict form is
    present, because we can't tell which model to use."""
    from coordinator import config

    _write_config(
        hermes_home,
        """
providers:
  anthropic:
    api_key: sk-1
    base_url: https://api.anthropic.com
    models:
      claude-opus-4-8:
        name: claude-opus-4-8
model:
  default: claude-opus-4-8
  provider: anthropic
""",
    )

    # Should not raise; falls back because we can't parse dict model
    result = config.load_llm_config()
    assert "model" in result
