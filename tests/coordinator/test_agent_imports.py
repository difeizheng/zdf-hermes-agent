"""Smoke tests for agent entry modules.

Catches SyntaxError, import-time, and missing-function regressions that
slip past the 32-test coordinator suite. Each agent is launched by
`scripts/run_<type>_agent.py`, which does `from coordinator.<type>_agent
import run_<type>_task`. If that import line breaks at runtime, the
agent dies immediately on startup — exactly the failure mode the v2
security_agent.py:283 missing-paren bug exposed in Round 9.

These tests would have caught that bug. They are deliberately cheap
(no fixtures, no I/O) so the cost of running them is negligible.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Awaitable, Callable

import pytest


# (module_name, expected_entry_function_name)
AGENT_MODULES: list[tuple[str, str]] = [
    ("coordinator.design_agent", "run_design_task"),
    ("coordinator.dev_agent", "run_dev_task"),
    ("coordinator.security_agent", "run_security_task"),
    ("coordinator.qa_agent", "run_qa_task"),
    ("coordinator.validate_agent", "run_validate_task"),
    ("coordinator.deploy_agent", "run_deploy_task"),
]


@pytest.mark.parametrize(("module_name", "entry_name"), AGENT_MODULES)
def test_agent_module_imports(module_name: str, entry_name: str) -> None:
    """Module imports without SyntaxError or ImportError.

    The bare ``importlib.import_module`` triggers the same import path
    that ``scripts/run_<type>_agent.py`` uses, so a syntax-level bug
    in any of these files is caught here before deploy.
    """
    module = importlib.import_module(module_name)
    assert module is not None
    assert hasattr(module, entry_name), (
        f"{module_name} is missing the expected entry function {entry_name!r}"
    )


@pytest.mark.parametrize(("module_name", "entry_name"), AGENT_MODULES)
def test_agent_entry_is_coroutine_factory(
    module_name: str, entry_name: str
) -> None:
    """The entry function is async (matches how agent_daemon invokes it).

    All run_*_task functions are defined ``async def``. Verifying
    ``asyncio.iscoroutinefunction`` guards against an accidental
    ``def`` regression that would break the daemon's await chain.
    """
    module = importlib.import_module(module_name)
    entry: Callable[..., Awaitable[object]] = getattr(module, entry_name)
    assert asyncio.iscoroutinefunction(entry), (
        f"{module_name}.{entry_name} must be ``async def``; the daemon awaits it"
    )
