"""Dev Agent — executes dev tasks via Claude Code CLI.

Reads design artifacts from design task, creates git worktree,
invokes Claude Code non-interactively, and submits results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_dev_task(task_id: str, coordinator_url: str) -> dict[str, Any]:
    """Execute a dev task via Claude Code CLI.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server

    Returns:
        Result dict with commit SHA, changed files, test results
    """
    import httpx

    # Fetch task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

    description = task_data["description"]
    metadata = task_data.get("metadata", {})

    # Fetch dependency artifacts (design docs)
    design_artifacts = await _fetch_dependency_artifacts(
        task_data.get("depends_on", []), coordinator_url
    )

    # Build prompt
    prompt = _build_dev_prompt(description, design_artifacts)

    # Get git repo path from metadata
    repo_path = metadata.get("git_repo", os.getcwd())
    branch = f"feature/{task_id}"

    # Create worktree
    _home = os.environ.get("HERMES_HOME")
    if not _home:
        _home = os.environ.get("HOME") or os.path.expanduser("~") + "/.hermes"
    worktree_dir = Path(_home) / "tasks" / str(task_id) / "worktree"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    await _create_worktree(repo_path, branch, worktree_dir)

    # Run Claude Code
    result = await _run_claude_code(worktree_dir, prompt)

    # Get git diff and commit SHA
    diff, commit_sha = await _get_git_info(worktree_dir)

    # Run tests if configured
    test_results = None
    test_cmd = metadata.get("test_command")
    if test_cmd and shutil.which(test_cmd.split()[0]):
        test_results = await _run_tests(worktree_dir, test_cmd)

    return {
        "artifacts": {
            "commit_sha": commit_sha,
            "branch": branch,
            "diff_summary": diff[:4096] if diff else "",
            "test_results": test_results,
        },
        "metadata": {"worktree": str(worktree_dir)},
    }


async def _fetch_dependency_artifacts(dep_ids: list[str], coordinator_url: str) -> dict[str, str]:
    """Fetch design artifacts from dependency tasks."""
    import httpx

    artifacts = {}
    for dep_id in dep_ids:
        try:
            resp = httpx.get(f"{coordinator_url}/tasks/{dep_id}/artifacts", timeout=30)
            resp.raise_for_status()
            artifacts.update(resp.json())
        except Exception as e:
            logger.warning("Failed to fetch artifacts for dep %s: %s", dep_id, e)
    return artifacts


def _build_dev_prompt(description: str, design_artifacts: dict[str, str]) -> str:
    """Build the prompt for Claude Code."""
    parts = [
        f"Task: {description}",
        "",
        "Design documents from the design phase:",
    ]
    for name, content in design_artifacts.items():
        parts.append(f"\n--- {name} ---\n{content}")
    parts.extend([
        "",
        "Implement the feature described above. Follow these rules:",
        "1. Follow existing code style and architecture patterns",
        "2. Write tests for new functionality",
        "3. Commit changes with a clear commit message",
        "4. Do not modify unrelated files",
    ])
    return "\n".join(parts)


async def _create_worktree(repo_path: str, branch: str, worktree_dir: Path) -> None:
    """Create a git worktree for isolated development."""
    try:
        subprocess.run(
            ["git", "-C", repo_path, "fetch", "origin"],
            capture_output=True, text=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "add", str(worktree_dir), "-b", branch, "origin/main"],
            capture_output=True, text=True, timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning("Worktree creation failed: %s", e.stderr)
        raise


async def _run_claude_code(worktree_dir: Path, prompt: str) -> dict[str, Any]:
    """Run Claude Code CLI non-interactively."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            cwd=str(worktree_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        return {
            "stdout": stdout.decode()[:4096],
            "stderr": stderr.decode()[:4096],
            "exit_code": proc.returncode,
        }
    except FileNotFoundError:
        return {"error": "claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"}
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "Claude Code timed out after 30 minutes"}


async def _get_git_info(worktree_dir: Path) -> tuple[str, str]:
    """Get git diff and latest commit SHA."""
    try:
        diff = subprocess.run(
            ["git", "-C", str(worktree_dir), "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True, timeout=10,
        )
        sha = subprocess.run(
            ["git", "-C", str(worktree_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return diff.stdout.strip(), sha.stdout.strip()
    except Exception:
        return "", ""


async def _run_tests(worktree_dir: Path, test_cmd: str) -> dict[str, Any]:
    """Run the configured test command."""
    try:
        proc = await asyncio.create_subprocess_shell(
            test_cmd,
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
