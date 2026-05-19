"""Deploy Agent — executes deployment tasks.

Checks out validated branch, builds, and deploys.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def run_deploy_task(task_id: str, coordinator_url: str) -> dict[str, Any]:
    """Execute a deployment task.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server

    Returns:
        Result dict with deployment URL, version, and status
    """
    import httpx

    # Fetch task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

    metadata = task_data.get("metadata", {})
    repo_path = metadata.get("git_repo", os.getcwd())
    deploy_cmd = metadata.get("deploy_command", "echo 'no deploy command'")

    # Fetch validate task results
    dep_id = task_data.get("depends_on", [""])[0]
    branch = f"feature/{dep_id}"

    # Checkout branch
    worktree_dir = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "tasks" / str(task_id) / "worktree"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", str(worktree_dir), branch],
        capture_output=True, text=True, timeout=30, check=True,
    )

    # Run deploy command
    result = await _run_deploy(str(worktree_dir), deploy_cmd)

    return {
        "artifacts": result,
        "metadata": {"deployed": True, "branch": branch},
    }


async def _run_deploy(worktree_dir: str, deploy_cmd: str) -> dict[str, Any]:
    """Execute the deployment command."""
    try:
        proc = await asyncio.create_subprocess_shell(
            deploy_cmd,
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
        return {
            "exit_code": proc.returncode,
            "output": stdout.decode()[:4096],
            "error": stderr.decode()[:4096],
        }
    except Exception as e:
        return {"error": str(e)}
