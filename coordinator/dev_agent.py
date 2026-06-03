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


def _get_timeout(key: str, default: int) -> int:
    """Load timeout value from config."""
    try:
        from coordinator.config import load_config
        return int(load_config().get(key, default))
    except Exception:
        return default


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
    heartbeat_task = asyncio.create_task(_send_heartbeat(task_id, coordinator_url))

    # Stream progress notes for the user
    from coordinator.progress import write_progress
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
    from coordinator.config import load_config, _default_workspace_dir
    cfg = load_config()
    workspace_dir = Path(cfg.get("workspace_dir", _default_workspace_dir())) / str(task_id) / "worktree"

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
    result = await _run_claude_code(
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

    # Cancel heartbeat task
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    return {
        "artifacts": {
            "commit_sha": commit_sha,
            "branch": branch,
            "diff_summary": diff[:4096] if diff else "",
            "test_results": test_results,
        },
        "metadata": {"worktree": str(workspace_dir)},
    }


async def _send_heartbeat(task_id: str, coordinator_url: str) -> None:
    """Send periodic heartbeats to prevent task timeout during long operations."""
    import httpx
    while True:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(f"{coordinator_url}/tasks/{task_id}/heartbeat")
                logger.debug("Sent heartbeat for task %s", task_id[:8])
        except Exception as e:
            logger.warning("Failed to send heartbeat for task %s: %s", task_id[:8], e)
        await asyncio.sleep(30)  # Send heartbeat every 30 seconds


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
    """Initialize a fresh empty git repo for the task."""
    subprocess.run(
        ["git", "-C", str(worktree_dir), "init", "-b", "main"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_dir), "config", "user.email", "hermes-agent@local"],
        capture_output=True, text=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(worktree_dir), "config", "user.name", "Hermes Agent"],
        capture_output=True, text=True, timeout=5,
    )
    # Initial empty commit
    subprocess.run(
        ["git", "-C", str(worktree_dir), "commit", "--allow-empty", "-m", "Initial commit"],
        capture_output=True, text=True, timeout=10,
    )


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


async def _run_claude_code(
    worktree_dir: Path,
    prompt: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run Claude Code CLI non-interactively.

    If daemon and task_id are provided, registers the spawned subprocess
    with the daemon so it can be killed on task cancellation/timeout.
    """
    import os
    import sys
    try:
        # On Windows, resolve to claude.exe directly (shell script won't work, cmd.exe breaks UTF-8)
        if sys.platform == "win32":
            which_result = subprocess.run(
                ["where", "claude"], capture_output=True, text=True, timeout=5
            )
            claude_exe = None
            for line in which_result.stdout.strip().split("\n"):
                line = line.strip()
                if line.lower().endswith("claude.exe") or line.lower().endswith("claude.cmd"):
                    claude_exe = line
                    break
            if claude_exe and claude_exe.lower().endswith(".cmd"):
                # cmd points to node_modules, resolve actual exe
                base_dir = os.path.dirname(claude_exe)
                exe_path = os.path.join(base_dir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
                if os.path.exists(exe_path):
                    claude_exe = exe_path
            if not claude_exe:
                claude_exe = "claude.exe"  # fallback to PATH lookup

            # Pass UTF-8 env to subprocess
            env = os.environ.copy()
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")

            proc = await asyncio.create_subprocess_exec(
                claude_exe, "-p", prompt, "--dangerously-skip-permissions",
                cwd=str(worktree_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", prompt, "--dangerously-skip-permissions",
                cwd=str(worktree_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        # Register subprocess for cancellation tracking
        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_get_timeout("claude_code_timeout", 1800))
        return {
            "stdout": stdout.decode()[:4096],
            "stderr": stderr.decode()[:4096],
            "exit_code": proc.returncode,
        }
    except FileNotFoundError:
        return {"error": "claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": "Claude Code timed out after 30 minutes"}


async def _get_git_info(worktree_dir: Path) -> tuple[str, str]:
    """Get git diff and latest commit SHA."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(worktree_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        commit_sha = sha.stdout.strip()
        if not commit_sha:
            return "", ""
        # Try diff against parent commit
        diff = subprocess.run(
            ["git", "-C", str(worktree_dir), "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True, timeout=10,
        )
        if diff.returncode != 0:
            # Fallback: show log of commits
            diff = subprocess.run(
                ["git", "-C", str(worktree_dir), "log", "--oneline", "--stat"],
                capture_output=True, text=True, timeout=10,
            )
        return diff.stdout.strip()[:4096], commit_sha
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_get_timeout("test_timeout", 600))
        return {
            "exit_code": proc.returncode,
            "output": stdout.decode()[:4096],
        }
    except Exception as e:
        return {"error": str(e)}
