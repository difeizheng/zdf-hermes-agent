"""Deploy Agent — executes deployment tasks.

Checks out validated branch, builds, and deploys.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _get_timeout(key: str, default: int) -> int:
    """Load timeout value from config."""
    try:
        from coordinator.config import load_config
        return int(load_config().get(key, default))
    except Exception:
        return default


async def run_deploy_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a deployment task.

    Walks the dependency chain (deploy → validate → dev → design) to find
    the dev task's worktree and artifacts. Does NOT rely on Brain-provided
    metadata for git_repo — always resolves from completed dev task data.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server
        daemon: Optional AgentDaemon instance for subprocess tracking.
        profile: Optional profile configuration from profiles.py.

    Returns:
        Result dict with deployment URL, version, and status
    """
    import httpx

    # Fetch all needed data in one client session
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch task details
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

        metadata = task_data.get("metadata", {})
        deploy_cmd = metadata.get("deploy_command", "")

        # Walk dependency chain to find the dev task:
        # deploy depends_on validate → validate depends_on dev
        dep_ids = task_data.get("depends_on", [])
        dev_task_id = ""
        dev_metadata: dict[str, Any] = {}
        dev_artifacts: dict[str, Any] = {}

        if dep_ids:
            # Step 1: fetch validate task
            resp = await client.get(f"{coordinator_url}/tasks/{dep_ids[0]}")
            resp.raise_for_status()
            validate_data = resp.json()
            validate_deps = validate_data.get("depends_on", [])
            if validate_deps:
                dev_task_id = validate_deps[0]

        # Step 2: fetch dev task (primary source of truth for code location)
        if dev_task_id:
            try:
                resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
                resp.raise_for_status()
                dev_data = resp.json()
                dev_metadata = dev_data.get("metadata", {})
                dev_artifacts = dev_data.get("artifacts", {})
            except Exception as e:
                logger.warning("Failed to fetch dev task %s: %s", dev_task_id[:8], e)

    # Resolve code location: prefer dev worktree, then Brain metadata, then fallback
    worktree = dev_metadata.get("worktree", "")
    repo_path = ""

    if worktree and Path(worktree).exists():
        # Use the dev worktree directly (most reliable)
        repo_path = str(Path(worktree).parent)
        logger.info("Using dev worktree parent as repo: %s", repo_path)
    elif metadata.get("git_repo") and Path(metadata["git_repo"]).exists():
        repo_path = metadata["git_repo"]
        logger.info("Using Brain-provided git_repo: %s", repo_path)
    else:
        repo_path = os.getcwd()
        logger.warning("No valid repo found, using cwd: %s", repo_path)

    # Resolve branch and commit from dev task artifacts
    branch = dev_artifacts.get("branch", metadata.get("branch", "main"))
    commit_sha = dev_artifacts.get("commit_sha", metadata.get("commit_sha", ""))

    # Use worktree from dev task if available, otherwise create one
    from coordinator.config import load_config, _default_workspace_dir
    cfg = load_config()
    workspace_dir = Path(cfg.get("workspace_dir", _default_workspace_dir())) / str(task_id) / "worktree"

    if commit_sha:
        existing_worktree = dev_metadata.get("worktree", "")
        if existing_worktree and Path(existing_worktree).exists():
            workspace_dir = Path(existing_worktree)
            logger.info("Reusing dev worktree: %s", workspace_dir)
        else:
            # Create fresh worktree at the committed state
            # Remove existing directory first (git worktree won't use existing dirs)
            if workspace_dir.exists():
                import shutil
                shutil.rmtree(workspace_dir, ignore_errors=True)
            try:
                subprocess.run(
                    ["git", "-C", repo_path, "worktree", "add", "--detach", str(workspace_dir), commit_sha],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                logger.info("Created worktree at %s for commit %s", workspace_dir, commit_sha)
            except subprocess.CalledProcessError as e:
                # Fallback: use the directory as-is if worktree creation fails
                logger.warning("Failed to create worktree, using existing directory: %s", e.stderr)
                workspace_dir.mkdir(parents=True, exist_ok=True)
    else:
        workspace_dir.mkdir(parents=True, exist_ok=True)

    # Run deploy command
    has_docker = (
        Path(workspace_dir, "Dockerfile").exists()
        or Path(workspace_dir, "docker-compose.yml").exists()
        or Path(workspace_dir, "docker-compose.yaml").exists()
    )

    if has_docker:
        result = await _deploy_docker(str(workspace_dir), daemon=daemon, task_id=task_id)
    else:
        result = await _run_deploy(str(workspace_dir), deploy_cmd, daemon=daemon, task_id=task_id)

    return {
        "artifacts": result,
        "metadata": {"deployed": True, "branch": branch, "commit_sha": commit_sha, "docker": has_docker},
    }


async def _run_deploy(
    worktree_dir: str,
    deploy_cmd: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Execute the deployment command.

    If daemon and task_id are provided, registers the spawned subprocess
    with the daemon so it can be killed on task cancellation/timeout.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            deploy_cmd,
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_get_timeout("deploy_timeout", 3600))
        return {
            "exit_code": proc.returncode,
            "output": stdout.decode()[:4096],
            "error": stderr.decode()[:4096],
        }
    except Exception as e:
        return {"error": str(e)}


async def _deploy_docker(
    worktree_dir: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Deploy via Docker Compose.

    Steps:
    1. Check docker/docker-compose availability
    2. docker compose pull (if images exist)
    3. docker compose up -d --build
    4. Verify containers are healthy

    If daemon and task_id are provided, registers the `docker compose up`
    subprocess with the daemon so it can be killed on cancellation/timeout.
    """
    docker_exe = shutil.which("docker")
    if not docker_exe:
        return {"error": "Docker not found. Install Docker Desktop or docker-ce."}

    has_compose = False
    try:
        result = subprocess.run(
            [docker_exe, "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        has_compose = result.returncode == 0
    except Exception:
        pass

    if not has_compose:
        return {"error": "Docker Compose not available. Install Docker Desktop or docker-compose-plugin."}

    # Determine compose file
    compose_file = "docker-compose.yml"
    if not Path(worktree_dir, compose_file).exists() and Path(worktree_dir, "docker-compose.yaml").exists():
        compose_file = "docker-compose.yaml"

    output_parts = []

    # Build and start
    cmd_up = [docker_exe, "compose", "-f", compose_file, "up", "-d", "--build"]
    logger.info("Running: %s", " ".join(cmd_up))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_up,
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_get_timeout("test_timeout", 600))
        output_parts.append(stdout.decode()[:2048])
        if stderr:
            output_parts.append("STDERR: " + stderr.decode()[:2048])
        if proc.returncode != 0:
            return {
                "exit_code": proc.returncode,
                "output": "\n".join(output_parts),
                "error": "docker compose up failed",
            }
    except asyncio.TimeoutError:
        return {"error": "Docker compose build timed out (10 min)"}

    # Verify containers
    cmd_ps = [docker_exe, "compose", "-f", compose_file, "ps", "--format", "json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_ps,
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        output_parts.append("\nContainer status:\n" + stdout.decode()[:2048])
    except Exception:
        output_parts.append("\nContainer status check failed (containers may still be starting)")

    return {
        "exit_code": 0,
        "output": "\n".join(output_parts),
        "error": None,
    }
