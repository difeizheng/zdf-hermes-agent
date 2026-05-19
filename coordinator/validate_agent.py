"""Validate Agent — executes validation tasks (code review + testing).

Checks out Dev Agent's branch, runs review and tests, submits results.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_validate_task(task_id: str, coordinator_url: str) -> dict[str, Any]:
    """Execute a validation task.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server

    Returns:
        Result dict with review status and test results
    """
    import httpx

    # Fetch task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

    metadata = task_data.get("metadata", {})
    repo_path = metadata.get("git_repo", os.getcwd())

    # Fetch dev task results to get branch and commit
    dep_id = task_data.get("depends_on", [""])[0]
    branch = f"feature/{dep_id}"

    # Checkout branch
    worktree_dir = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "tasks" / str(task_id) / "worktree"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    await _checkout_branch(repo_path, branch, worktree_dir)

    # Run code review
    review = await _run_review(worktree_dir)

    # Run tests
    test_results = await _run_tests(worktree_dir, metadata.get("test_command"))

    # Write review artifact
    artifact_dir = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "tasks" / str(task_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "review.md").write_text(review, encoding="utf-8")

    return {
        "artifacts": {
            "review": str(artifact_dir / "review.md"),
            "test_results": test_results,
        },
        "metadata": {"review_status": "passed" if "FAIL" not in review.upper() else "failed"},
    }


async def _checkout_branch(repo_path: str, branch: str, worktree_dir: Path) -> None:
    """Checkout the dev agent's branch in a worktree."""
    subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", str(worktree_dir), branch],
        capture_output=True, text=True, timeout=30, check=True,
    )


async def _run_review(worktree_dir: Path) -> str:
    """Run automated code review checks."""
    checks = []
    try:
        # Run ruff/lint
        result = subprocess.run(
            ["ruff", "check", str(worktree_dir)],
            capture_output=True, text=True, timeout=60,
        )
        checks.append(f"## Lint Results\nExit code: {result.returncode}\n{result.stdout[:2000]}")
    except FileNotFoundError:
        checks.append("## Lint Results\nruff not found — skipping")

    try:
        # Run type check
        result = subprocess.run(
            ["pyright", str(worktree_dir)],
            capture_output=True, text=True, timeout=120,
        )
        checks.append(f"## Type Check Results\nExit code: {result.returncode}\n{result.stdout[:2000]}")
    except FileNotFoundError:
        checks.append("## Type Check Results\npyright not found — skipping")

    return "\n\n".join(checks) if checks else "# Review\nNo automated checks available."


async def _run_tests(worktree_dir: Path, test_cmd: str | None = None) -> dict[str, Any]:
    """Run tests in the worktree."""
    cmd = test_cmd or "pytest --tb=short -q"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(worktree_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        return {
            "exit_code": proc.returncode,
            "output": stdout.decode()[:4096],
        }
    except Exception as e:
        return {"error": str(e)}
