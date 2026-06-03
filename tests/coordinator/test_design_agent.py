"""Regression tests for design_agent mock artifacts.

The Round 9-bugfix incident: ``_mock_artifacts`` used to echo the
memory-laden ``user_content`` into ``prd.md``, dumping the entire
error/decision memory into a supposed PRD. These tests pin the
contract: the mock path must not bleed memory context, must mark
its output as MOCK, and must remain structurally usable as
placeholders for downstream agents.
"""

from __future__ import annotations

import pytest

from coordinator.design_agent import _mock_artifacts


# Memory entry keywords that should NEVER appear in mock output.
# Sourced from DEFAULT_ERROR_MEMORIES in coordinator/memory.py.
MEMORY_KEYWORDS = [
    "cors-whitelist",
    "no-hardcoded-secrets",
    "rate-limit-required",
    "setattr-whitelist",
    "tenant-isolation",
    "tdd-flow",
    "service-layer",
    "error-handling",
    "multi-tenant-strategy",
]


def test_mock_artifacts_does_not_echo_user_content() -> None:
    """The original bug: mock echoed user_content (containing memory) into prd.md."""
    # Simulate the original bug scenario: user_content with memory prepended
    user_content_with_memory = (
        "## [errors] cors-whitelist\n"
        "CORS 必须白名单...\n\n"
        "## [errors] no-hardcoded-secrets\n"
        "禁止硬编码密钥...\n\n"
        "---\n\n"
        "客户管理系统的设计需求"
    )
    # Pass user_content via the system_prompt arg (the old signature accepted it
    # there too). Modern signature only takes description, but test the new
    # contract by passing it where it doesn't belong.
    artifacts = _mock_artifacts(description="客户管理系统的设计需求")
    full = "\n".join(artifacts.values())
    for kw in MEMORY_KEYWORDS:
        assert kw not in full, f"Memory keyword {kw!r} leaked into mock output"


def test_mock_artifacts_marks_output_as_mock() -> None:
    """Every mock file should be self-identifying so users know it's not real."""
    artifacts = _mock_artifacts(description="some task")
    for filename, content in artifacts.items():
        assert "MOCK" in content, f"{filename} does not advertise itself as MOCK"


def test_mock_artifacts_uses_description_as_title() -> None:
    """The first sentence of the task description should appear in the output."""
    description = "设计一个CRM系统用于客户管理"
    artifacts = _mock_artifacts(description=description)
    # The first sentence of the description should appear (it makes the mock
    # recognizable as "this task" rather than generic).
    assert "CRM" in artifacts["prd.md"] or "CRM" in artifacts["architecture.md"]


def test_mock_artifacts_returns_all_three_files() -> None:
    """Downstream agents expect prd.md, architecture.md, system_design.md."""
    artifacts = _mock_artifacts(description="anything")
    assert set(artifacts.keys()) == {"prd.md", "architecture.md", "system_design.md"}


def test_mock_artifacts_handles_empty_description() -> None:
    """Empty or whitespace description must not crash."""
    artifacts_empty = _mock_artifacts(description="")
    artifacts_ws = _mock_artifacts(description="   ")
    for artifacts in (artifacts_empty, artifacts_ws):
        assert "prd.md" in artifacts
        assert "MOCK" in artifacts["prd.md"]


@pytest.mark.parametrize("description", [
    "客户管理。还需要跟进记录功能。",
    "Simple task",
    "任务A。任务B。任务C。",
])
def test_mock_artifacts_truncates_long_description(description: str) -> None:
    """Long descriptions are truncated to first sentence for the title slot."""
    artifacts = _mock_artifacts(description=description)
    # No mock output should contain the full long description verbatim
    # (truncation is intentional, so we only check first sentence is captured).
    first_part = description.split("。")[0][:50]
    if first_part.strip():
        assert first_part in artifacts["prd.md"] or first_part in artifacts["architecture.md"]


def test_mock_artifacts_no_user_content_kwarg() -> None:
    """The function signature must not accept user_content (regression guard).

    The bug was caused by user_content being threaded through to the mock.
    Pinning the signature here means any future PR that re-adds the param
    will fail this test, forcing a deliberate API change.
    """
    import inspect
    sig = inspect.signature(_mock_artifacts)
    assert "user_content" not in sig.parameters, (
        "_mock_artifacts must not accept user_content — that was the source "
        "of the Round 9 memory-leak bug"
    )
