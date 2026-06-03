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

from coordinator.shared_heartbeat import start_heartbeat
from coordinator.shared_claude_cli import run_claude_cli
from coordinator.shared_helpers import get_timeout, get_workspace_dir

logger = logging.getLogger(__name__)


def _get_local_docker_images() -> list[str]:
    """Get list of locally available Docker image names (e.g., 'python:3.12-slim')."""
    try:
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            images = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            # Filter to commonly usable base images (Python, Node, etc.)
            return [img for img in images if not img.startswith("<")]
        return []
    except Exception:
        return []


async def run_dev_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a dev task via Claude Code CLI.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server
        daemon: Optional AgentDaemon instance for subprocess tracking.
        profile: Optional profile configuration from profiles.py.

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

    # Start heartbeat background task to prevent timeout during long operations
    hb = start_heartbeat(task_id, coordinator_url)

    try:
        return await _execute_dev(
            task_id, coordinator_url, task_data, daemon=daemon, profile=profile,
        )
    finally:
        await hb.cancel_and_wait()


async def _execute_dev(
    task_id: str,
    coordinator_url: str,
    task_data: dict[str, Any],
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Core dev execution, called inside heartbeat try/finally."""
    from coordinator.progress import write_progress

    # Stream progress notes for the user
    write_progress(task_id, f"Dev task started: {task_data.get('title', '')[:60]}")

    # Fetch dependency artifacts (design docs)
    design_artifacts = await _fetch_dependency_artifacts(
        task_data.get("depends_on", []), coordinator_url
    )

    # Load memory context from previous phases (errors to avoid, patterns to follow)
    memory_context = ""
    try:
        from coordinator.config import load_config, _default_workspace_dir
        _cfg = load_config()
        _workspace = Path(_cfg.get("workspace_dir", _default_workspace_dir()))
        from coordinator.memory import load_memory_context
        memory_context = load_memory_context(_workspace, categories=["errors", "patterns"])
    except Exception as e:
        logger.warning("Failed to load memory context: %s", e)

    # Check if this is a retry task - if so, try to reuse the original dev's worktree
    metadata = task_data.get("metadata", {})
    description = task_data["description"]
    is_retry = metadata.get("is_retry", False)
    original_dev_id = metadata.get("original_dev_id", "")
    existing_worktree = ""
    original_dev: dict[str, Any] = {}
    if is_retry and original_dev_id:
        try:
            async with httpx.AsyncClient(timeout=30.0) as retry_client:
                resp = await retry_client.get(f"{coordinator_url}/tasks/{original_dev_id}")
                resp.raise_for_status()
                original_dev = resp.json()
            existing_worktree = original_dev.get("metadata", {}).get("worktree", "")
            if existing_worktree and Path(existing_worktree).exists():
                logger.info("Reusing worktree from original dev %s: %s", original_dev_id[:8], existing_worktree)
        except Exception as e:
            logger.warning("Failed to fetch original dev worktree: %s", e)

    # Build prompt — inject profile behavior if available
    prompt = _build_dev_prompt(description, design_artifacts, profile=profile, memory_context=memory_context)
    write_progress(task_id, f"Loaded {len(design_artifacts)} design artifact(s)")

    # Determine project directory
    repo_path = metadata.get("git_repo")

    # Create workspace directory
    workspace_dir = Path(get_workspace_dir()) / str(task_id) / "worktree"

    # For retry tasks, reuse existing worktree if available
    if existing_worktree:
        workspace_dir = Path(existing_worktree)
        branch = original_dev.get("artifacts", {}).get("branch", "main") if original_dev else "main"
        logger.info("Reusing existing worktree: %s", workspace_dir)
    else:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        # If a specific git_repo is provided, use worktree from that repo
        # Otherwise create a fresh git repo for this task
        if repo_path:
            branch = f"feature/{task_id}"
            logger.info("Using worktree from repo %s, branch %s", repo_path, branch)
            await _create_worktree(repo_path, branch, workspace_dir)
        else:
            branch = "main"
            logger.info("Initializing fresh git repo in %s", workspace_dir)
            await _init_fresh_repo(workspace_dir)

    # Run Claude Code
    logger.info("Invoking claude code in %s", workspace_dir)
    write_progress(task_id, f"Invoking Claude Code in {workspace_dir.name}...")
    result = await run_claude_cli(
        workspace_dir, prompt,
        daemon=daemon, task_id=task_id,
    )
    logger.info("Claude code finished: exit_code=%s", result.get("exit_code"))

    # Get git diff and commit SHA
    diff, commit_sha = await _get_git_info(workspace_dir)
    logger.info("Git info: commit_sha=%s", commit_sha[:10] if commit_sha else "none")
    if commit_sha:
        write_progress(task_id, f"Code committed: {commit_sha[:10]}")
    else:
        write_progress(task_id, "No commit was made (code may not have changed)", level="warn")

    # Run tests if configured
    test_results = None
    test_cmd = metadata.get("test_command")
    if test_cmd and shutil.which(test_cmd.split()[0]):
        test_results = await _run_tests(workspace_dir, test_cmd)

    return {
        "artifacts": {
            "commit_sha": commit_sha,
            "branch": branch,
            "diff_summary": diff[:4096] if diff else "",
            "test_results": test_results,
        },
        "metadata": {"worktree": str(workspace_dir)},
    }


async def _fetch_dependency_artifacts(dep_ids: list[str], coordinator_url: str) -> dict[str, str]:
    """Fetch design artifacts from dependency tasks."""
    import httpx

    artifacts = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for dep_id in dep_ids:
            try:
                resp = await client.get(f"{coordinator_url}/tasks/{dep_id}/artifacts")
                resp.raise_for_status()
                artifacts.update(resp.json())
            except Exception as e:
                logger.warning("Failed to fetch artifacts for dep %s: %s", dep_id, e)
    return artifacts


def _build_dev_prompt(
    description: str,
    design_artifacts: dict[str, str],
    profile: dict[str, Any] | None = None,
    memory_context: str = "",
) -> str:
    """Build the prompt for Claude Code."""
    local_images = _get_local_docker_images()
    image_hint = ""
    if local_images:
        # Filter to Python/Node images commonly used as base images
        base_images = [img for img in local_images if any(img.startswith(f"{r}:") for r in ["python", "node", "golang", "rust"])]
        if base_images:
            image_hint = f"\nLocally available Docker base images (prefer these): {', '.join(base_images)}"

    parts = [
        f"Task: {description}",
        "",
    ]

    # Inject profile behavior as role guidance
    if profile:
        parts.append("## Agent Profile")
        parts.append("")
        if profile.get("behavior"):
            parts.append(f"Behavior: {profile['behavior']}")
        if profile.get("rules"):
            parts.append(f"Rules enforced: {', '.join(profile['rules'])}")
        if profile.get("output"):
            parts.append(f"Expected output: {profile['output']}")
        parts.append("")

    # Inject memory context (errors to avoid, patterns from past projects)
    if memory_context:
        parts.append(memory_context)
        parts.append("")

    if design_artifacts:
        parts.append("Design documents from the design phase:")
        for name, content in design_artifacts.items():
            parts.append(f"\n--- {name} ---\n{content}")
    if image_hint:
        parts.append(image_hint)
    parts.extend([
        "",
        "Implement the feature described above. Follow these rules:",
    ])
    if design_artifacts:
        parts.append("1. Follow existing code style and architecture patterns from the design documents")
        parts.append("2. Write tests for new functionality")
        parts.append("3. Commit changes with a clear commit message")
        parts.append("4. Do not modify unrelated files")
        parts.append("5. Generate a Dockerfile for the project (multi-stage build, production-ready). CRITICAL: You MUST use a locally cached Docker image that is already on this machine. Check 'docker images' output and pick an image from the AVAILABLE IMAGES list above. NEVER pull a new image from the internet - only use images confirmed by 'docker images'.")
        parts.append("6. Generate a docker-compose.yml for local development (app + database + any dependencies)")
    else:
        parts.append("1. Create a clean project structure for this feature")
        parts.append("2. Write tests for all functionality")
        parts.append("3. Commit changes with clear commit messages")
        parts.append("4. Generate a Dockerfile (multi-stage build, production-ready). CRITICAL: You MUST use a locally cached Docker image that is already on this machine. Check 'docker images' output and pick an image from the AVAILABLE IMAGES list above. NEVER pull a new image from the internet - only use images confirmed by 'docker images'.")
        parts.append("5. Generate a docker-compose.yml for local development")
    return "\n".join(parts)


async def _init_fresh_repo(worktree_dir: Path) -> None:
    """Initialize a fresh empty git repo for the task (non-blocking)."""
    async def _git(*args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(worktree_dir), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)

    await _git("init", "-b", "main")
    await _git("config", "user.email", "hermes-agent@local")
    await _git("config", "user.name", "Hermes Agent")
    await _git("commit", "--allow-empty", "-m", "Initial commit")


async def _create_worktree(repo_path: str, branch: str, worktree_dir: Path) -> None:
    """Create a git worktree for isolated development (non-blocking)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "fetch", "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "worktree", "add", str(worktree_dir), "-b", branch, "origin/main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"Worktree creation failed: {stderr.decode('utf-8', errors='replace')}")
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("Worktree creation failed: %s", e)
        raise


async def _get_git_info(worktree_dir: Path) -> tuple[str, str]:
    """Get git diff and latest commit SHA (non-blocking)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(worktree_dir), "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        commit_sha = stdout.decode("utf-8", errors="replace").strip()
        if not commit_sha:
            return "", ""
        # Try diff against parent commit
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(worktree_dir), "diff", "--stat", "HEAD~1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            # Fallback: show log of commits
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(worktree_dir), "log", "--oneline", "--stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="replace")
        return output.strip()[:4096], commit_sha
    except Exception:
        return "", ""


async def _run_tests(worktree_dir: Path, test_cmd: str) -> dict[str, Any]:
    """Run the configured test command.

    Uses shlex.split to safely parse the command string into arguments,
    preventing shell injection from user-controlled test_command metadata.
    """
    import shlex
    try:
        args = shlex.split(test_cmd, posix=True)
        if not args:
            return {"error": "Empty test command"}
        # Validate the command runner is a known test tool
        runner = Path(args[0]).stem  # strip path prefix
        _ALLOWED_RUNNERS = {
            "pytest", "python", "python3", "npm", "npx", "yarn",
            "cargo", "go", "mvn", "gradle", "make", "tox",
        }
        if runner not in _ALLOWED_RUNNERS:
            return {"error": f"Disallowed test runner: {runner}. Allowed: {sorted(_ALLOWED_RUNNERS)}"}
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(worktree_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("test_timeout", 600))
        return {
            "exit_code": proc.returncode,
            "output": stdout.decode("utf-8", errors="replace")[:4096],
        }
    except Exception as e:
        return {"error": str(e)}
