"""End-to-end pipeline tests for the coordinator.

Tests the full Design -> Dev -> Security -> QA -> Validate chain by:
1. Starting a real coordinator server (uvicorn on random port)
2. Calling each ``run_*_task`` function with mock LLM/subprocess
3. Simulating the daemon's claim -> execute -> submit cycle
4. Verifying metadata contracts between phases
5. Verifying the quality gate aggregation in validate_agent

These tests are NOT marked @pytest.mark.integration -- they use no
external APIs, only the local coordinator + mock LLM.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from coordinator.design_agent import run_design_task
from coordinator.dev_agent import run_dev_task
from coordinator.qa_agent import run_qa_task
from coordinator.security_agent import run_security_task
from coordinator.validate_agent import run_validate_task


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _create_task_chain(
    base: str, description: str = "E2E test: design a CRM system"
) -> dict[str, str]:
    """Create the 5-task dependency chain, return {name: task_id}."""
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        design = (
            await client.post(
                "/tasks",
                json={"type": "design", "title": "Design", "description": description},
            )
        ).json()

        dev = (
            await client.post(
                "/tasks",
                json={
                    "type": "dev",
                    "title": "Dev",
                    "description": "Implement the design from the architect phase",
                    "depends_on": [design["id"]],
                },
            )
        ).json()

        security = (
            await client.post(
                "/tasks",
                json={
                    "type": "security",
                    "title": "Security",
                    "description": "Run a full security audit on the codebase",
                    "depends_on": [dev["id"]],
                },
            )
        ).json()

        qa = (
            await client.post(
                "/tasks",
                json={
                    "type": "qa",
                    "title": "QA",
                    "description": "Run all tests and check coverage thresholds",
                    "depends_on": [dev["id"]],
                },
            )
        ).json()

        validate = (
            await client.post(
                "/tasks",
                json={
                    "type": "validate",
                    "title": "Validate",
                    "description": "Aggregate quality gate from security and QA results",
                    "depends_on": [security["id"], qa["id"]],
                },
            )
        ).json()

    return {
        "design": design["id"],
        "dev": dev["id"],
        "security": security["id"],
        "qa": qa["id"],
        "validate": validate["id"],
    }


async def _get_task(base: str, task_id: str) -> dict[str, Any]:
    """Fetch task from coordinator."""
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        return resp.json()


async def _resolve_deps(base: str, task_id: str) -> None:
    """Trigger dependency resolution for a task."""
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        await client.post(f"/tasks/{task_id}/resolve-deps")


async def _run_and_submit(
    base: str,
    task_id: str,
    agent_fn: Any,
    agent_id: str = "e2e-agent",
) -> dict[str, Any]:
    """Simulate daemon: claim -> run_*_task -> submit result.

    The real AgentDaemon does this in ``_claim_and_execute()``.
    ``run_*_task`` only returns a result dict -- it does NOT claim
    or submit to the coordinator.  We do that here.
    """
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        # Claim: pending -> running
        resp = await client.post(f"/tasks/{task_id}/claim", json={"agent_id": agent_id})
        resp.raise_for_status()

    # Execute the agent function
    result = await agent_fn(task_id, base)

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        # Submit result: running -> completed/failed
        error = result.get("error")
        resp = await client.post(
            f"/tasks/{task_id}/result",
            json={
                "artifacts": result.get("artifacts"),
                "metadata": result.get("metadata"),
                "error": error,
            },
        )
        resp.raise_for_status()

    return result


# ── Shared mock factories ────────────────────────────────────────────────────


async def _mock_design_api(
    system_prompt: str, user_content: str, *, description: str = ""
) -> dict[str, str]:
    """Mock design LLM: returns fixed artifacts (async to match _call_claude_api)."""
    return {
        "prd.md": "# PRD: E2E Test Project\n\n## Overview\nA test CRM system.\n",
        "architecture.md": "# Architecture\n\n## Components\n- API server\n- Database\n",
        "system_design.md": "# System Design\n\n## Data Model\n- users table\n",
    }


async def _mock_claude_code(
    worktree_dir: Path, prompt: str, daemon: Any = None, task_id: str | None = None
) -> dict[str, Any]:
    (worktree_dir / "app.py").write_text('"""App module."""\n', encoding="utf-8")
    return {"exit_code": 0, "stdout": "Code generated", "stderr": ""}


async def _mock_git_info(worktree_dir: Path) -> tuple[str, str]:
    return ("+1 line", "e2e-fake-commit-sha-1234567890abcdef")


async def _mock_security_scans(worktree_dir: str) -> dict[str, Any]:
    return {}


async def _mock_claude_security(
    worktree_dir: Path, prompt: str, daemon: Any = None, task_id: str | None = None
) -> dict[str, Any]:
    return {
        "exit_code": 0,
        "stdout": "VERDICT: PASS\nNo security issues found.\n",
        "stderr": "",
    }


async def _mock_tests_with_coverage(
    worktree_dir: str, daemon: Any = None, task_id: str | None = None
) -> dict[str, dict[str, Any]]:
    return {
        "test": {"exit_code": 0, "stdout": "5 passed", "stderr": ""},
        "coverage": {"coverage": 85.0, "stdout": "TOTAL 85%", "stderr": ""},
    }


async def _mock_tests_with_low_coverage(
    worktree_dir: str, daemon: Any = None, task_id: str | None = None
) -> dict[str, dict[str, Any]]:
    return {
        "test": {"exit_code": 0, "stdout": "5 passed", "stderr": ""},
        "coverage": {"coverage": 60.0, "stdout": "TOTAL 60%", "stderr": ""},
    }


async def _mock_claude_security_with_critical(
    worktree_dir: Path, prompt: str, daemon: Any = None, task_id: str | None = None
) -> dict[str, Any]:
    return {
        "exit_code": 1,
        "stdout": "VERDICT: REJECT\nCRITICAL | 1\nHIGH | 0\nHardcoded secret found",
        "stderr": "",
    }


def _apply_common_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply mock patches shared by all pipeline tests."""
    monkeypatch.setattr("coordinator.design_agent._call_claude_api", _mock_design_api)
    monkeypatch.setattr(
        "coordinator.design_agent._load_llm_config",
        lambda: {"api_key": "test-key", "model": "test-model"},
    )
    monkeypatch.setattr("coordinator.dev_agent.run_claude_cli", _mock_claude_code)
    monkeypatch.setattr("coordinator.dev_agent._get_git_info", _mock_git_info)
    monkeypatch.setattr("coordinator.dev_agent._get_local_docker_images", lambda: [])


def _apply_clean_security_qa_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply clean security + QA mocks (no issues, 85% coverage)."""
    monkeypatch.setattr("coordinator.security_agent._run_security_scans", _mock_security_scans)
    monkeypatch.setattr("coordinator.security_agent.run_claude_cli", _mock_claude_security)
    monkeypatch.setattr("coordinator.qa_agent._run_tests_with_coverage", _mock_tests_with_coverage)
    monkeypatch.setattr("coordinator.qa_agent._detect_project_type", lambda p: "python")


async def _mock_opencode_validation(
    worktree_dir: Path, prompt: str, daemon: Any = None, task_id: str | None = None
) -> dict[str, Any]:
    """Mock validate agent's Claude Code review — always pass."""
    return {
        "exit_code": 0,
        "stdout": "VERDICT: PASS\nAll design requirements met.\n",
        "stderr": "",
    }


# ── Async implementations ────────────────────────────────────────────────────


async def _test_full_pipeline_pass(base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_common_mocks(monkeypatch)
    _apply_clean_security_qa_mocks(monkeypatch)
    monkeypatch.setattr("coordinator.validate_agent.run_claude_cli", _mock_opencode_validation)

    ids = await _create_task_chain(base)

    # Design
    result = await _run_and_submit(base, ids["design"], run_design_task, "e2e-design")
    assert result.get("error") is None, f"Design failed: {result.get('error')}"
    design_task = await _get_task(base, ids["design"])
    assert design_task["status"] == "completed"

    # Dev
    await _resolve_deps(base, ids["design"])
    result = await _run_and_submit(base, ids["dev"], run_dev_task, "e2e-dev")
    assert result.get("error") is None, f"Dev failed: {result.get('error')}"
    dev_task = await _get_task(base, ids["dev"])
    assert dev_task["status"] == "completed"
    assert dev_task.get("metadata", {}).get("worktree") is not None

    # Security
    await _resolve_deps(base, ids["dev"])
    result = await _run_and_submit(base, ids["security"], run_security_task, "e2e-sec")
    assert result.get("error") is None, f"Security failed: {result.get('error')}"
    sec_task = await _get_task(base, ids["security"])
    assert sec_task["status"] == "completed"
    sec_meta = sec_task.get("metadata", {})
    assert sec_meta.get("security_passed") is True
    assert sec_meta.get("critical_issues", 1) == 0

    # QA
    await _resolve_deps(base, ids["dev"])
    result = await _run_and_submit(base, ids["qa"], run_qa_task, "e2e-qa")
    assert result.get("error") is None, f"QA failed: {result.get('error')}"
    qa_task = await _get_task(base, ids["qa"])
    assert qa_task["status"] == "completed"
    qa_meta = qa_task.get("metadata", {})
    assert qa_meta.get("test_passed") is True
    assert qa_meta.get("coverage_pct", 0) >= 80.0

    # Validate (no mocks -- real integration)
    await _resolve_deps(base, ids["security"])
    await _resolve_deps(base, ids["qa"])
    result = await _run_and_submit(base, ids["validate"], run_validate_task, "e2e-val")
    assert result.get("error") is None, f"Validate failed: {result.get('error')}"
    val_task = await _get_task(base, ids["validate"])
    assert val_task["status"] == "completed"

    # Verify no mock-echo contamination
    for key, value in (val_task.get("artifacts") or {}).items():
        if isinstance(value, str):
            assert "cors-whitelist" not in value, f"Mock echo in {key}"
            assert "no-hardcoded-secrets" not in value, f"Mock echo in {key}"


async def _test_pipeline_security_fails_gate(base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_common_mocks(monkeypatch)
    monkeypatch.setattr("coordinator.security_agent._run_security_scans", _mock_security_scans)
    monkeypatch.setattr("coordinator.security_agent.run_claude_cli", _mock_claude_security_with_critical)
    monkeypatch.setattr("coordinator.qa_agent._run_tests_with_coverage", _mock_tests_with_coverage)
    monkeypatch.setattr("coordinator.qa_agent._detect_project_type", lambda p: "python")

    ids = await _create_task_chain(base)

    await _run_and_submit(base, ids["design"], run_design_task, "e2e-design")
    await _resolve_deps(base, ids["design"])
    await _run_and_submit(base, ids["dev"], run_dev_task, "e2e-dev")

    # Security detects critical → task status = failed (correct behavior)
    await _resolve_deps(base, ids["dev"])
    sec_result = await _run_and_submit(base, ids["security"], run_security_task, "e2e-sec")
    sec_task = await _get_task(base, ids["security"])
    # Security task itself is failed (has error), but metadata is preserved
    assert sec_task["status"] == "failed"
    sec_meta = sec_task.get("metadata", {})
    assert sec_meta.get("critical_issues", 0) >= 1, f"Expected critical issues, got: {sec_meta}"

    # QA passes
    await _resolve_deps(base, ids["dev"])
    await _run_and_submit(base, ids["qa"], run_qa_task, "e2e-qa")

    # Validate should FAIL the gate (reads security metadata even from failed tasks)
    await _resolve_deps(base, ids["security"])
    await _resolve_deps(base, ids["qa"])
    val_result = await _run_and_submit(base, ids["validate"], run_validate_task, "e2e-val")

    # Gate should detect security failure
    val_meta = val_result.get("metadata", {})
    gate_verdict = val_meta.get("gate_verdict", "").upper()
    has_error = val_result.get("error") is not None
    assert has_error or "FAIL" in gate_verdict, (
        f"Gate should fail for security. verdict={gate_verdict}, error={val_result.get('error')}"
    )


async def _test_pipeline_low_coverage_fails_gate(base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_common_mocks(monkeypatch)
    monkeypatch.setattr("coordinator.security_agent._run_security_scans", _mock_security_scans)
    monkeypatch.setattr("coordinator.security_agent.run_claude_cli", _mock_claude_security)
    monkeypatch.setattr("coordinator.qa_agent._run_tests_with_coverage", _mock_tests_with_low_coverage)
    monkeypatch.setattr("coordinator.qa_agent._detect_project_type", lambda p: "python")

    ids = await _create_task_chain(base)

    await _run_and_submit(base, ids["design"], run_design_task, "e2e-design")
    await _resolve_deps(base, ids["design"])
    await _run_and_submit(base, ids["dev"], run_dev_task, "e2e-dev")
    await _resolve_deps(base, ids["dev"])
    await _run_and_submit(base, ids["security"], run_security_task, "e2e-sec")

    # QA with low coverage
    await _resolve_deps(base, ids["dev"])
    await _run_and_submit(base, ids["qa"], run_qa_task, "e2e-qa")
    qa_task = await _get_task(base, ids["qa"])
    assert qa_task.get("metadata", {}).get("coverage_pct", 0) < 80.0

    # Validate should fail
    await _resolve_deps(base, ids["security"])
    await _resolve_deps(base, ids["qa"])
    val_result = await _run_and_submit(base, ids["validate"], run_validate_task, "e2e-val")

    has_error = val_result.get("error") is not None
    gate_verdict = val_result.get("metadata", {}).get("gate_verdict", "").upper()
    assert has_error or "FAIL" in gate_verdict, (
        f"Gate should fail for low coverage. verdict={gate_verdict}, error={val_result.get('error')}"
    )


async def _test_pipeline_dev_worktree_missing(base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Security should fail gracefully when dev worktree is missing."""
    _apply_common_mocks(monkeypatch)
    monkeypatch.setattr("coordinator.security_agent._run_security_scans", _mock_security_scans)
    monkeypatch.setattr("coordinator.security_agent.run_claude_cli", _mock_claude_security)

    ids = await _create_task_chain(base)

    await _run_and_submit(base, ids["design"], run_design_task, "e2e-design")
    await _resolve_deps(base, ids["design"])
    await _run_and_submit(base, ids["dev"], run_dev_task, "e2e-dev")

    # Get the real worktree path, then make Path.exists return False for it
    dev_task = await _get_task(base, ids["dev"])
    worktree = dev_task.get("metadata", {}).get("worktree", "")
    real_exists = Path.exists

    def _patched_exists(self: Path) -> bool:
        if worktree and str(self) == worktree:
            return False
        return real_exists(self)

    monkeypatch.setattr("coordinator.security_agent.Path.exists", _patched_exists)

    # Security should fail gracefully — RuntimeError for missing worktree
    await _resolve_deps(base, ids["dev"])
    try:
        result = await _run_and_submit(base, ids["security"], run_security_task, "e2e-sec")
        # If it returned instead of raising, check for error
        assert result.get("error") is not None, (
            f"Security should have errored for missing worktree. Result: {result}"
        )
    except RuntimeError:
        pass  # Expected — security raises RuntimeError for missing worktree


# ── Sync test wrappers ───────────────────────────────────────────────────────
# NOTE: We wrap with asyncio.run() instead of @pytest.mark.asyncio because
# pytest-asyncio 1.3.0 (STRICT mode) swallows tracebacks on Windows.


def test_full_pipeline_pass(
    coordinator_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_test_full_pipeline_pass(coordinator_server, tmp_path, monkeypatch))


def test_pipeline_security_fails_gate(
    coordinator_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_test_pipeline_security_fails_gate(coordinator_server, tmp_path, monkeypatch))


def test_pipeline_low_coverage_fails_gate(
    coordinator_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_test_pipeline_low_coverage_fails_gate(coordinator_server, tmp_path, monkeypatch))


def test_pipeline_dev_worktree_missing_fails_gracefully(
    coordinator_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_test_pipeline_dev_worktree_missing(coordinator_server, tmp_path, monkeypatch))


def test_all_agent_modules_importable() -> None:
    """Prevent SyntaxError from slipping through."""
    import importlib

    modules = [
        "coordinator.design_agent",
        "coordinator.dev_agent",
        "coordinator.security_agent",
        "coordinator.qa_agent",
        "coordinator.validate_agent",
        "coordinator.deploy_agent",
        "coordinator.agent_daemon",
        "coordinator.profiles",
        "coordinator.config",
        "coordinator.db",
        "coordinator.server",
        "coordinator.events",
        "coordinator.metrics",
        "coordinator.models",
        "coordinator.progress",
        "coordinator.memory",
        "coordinator.kanban_sync",
    ]
    errors: list[str] = []
    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
        except SyntaxError as e:
            errors.append(f"{mod_name}: {e}")
        except Exception:
            pass

    assert not errors, "Syntax errors found:\n" + "\n".join(errors)
