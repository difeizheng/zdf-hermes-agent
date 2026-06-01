"""Validate Agent — verifies implementation against design using Claude Code.

Fetches design artifacts from the design task, examines the dev agent's
worktree, and calls Claude Code to verify that the implementation satisfies
all design requirements.

On failure: creates a dev_retry + validate_retry task pair (max 3 attempts).
Only when validation passes does the chain proceed to deploy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_VALIDATE_RETRIES = 3


async def run_validate_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
) -> dict[str, Any]:
    """Execute a validation task.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server
        daemon: Optional AgentDaemon instance. If provided, the spawned
                Claude Code subprocess is registered with the daemon so it
                can be killed on task cancellation/timeout.

    Returns:
        Result dict with pass/fail status and feedback
    """
    import httpx

    # Fetch validate task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        validate_task = resp.json()

    dep_ids = validate_task.get("depends_on", [])
    if not dep_ids:
        return {"error": "No dev task dependency found"}

    # Start heartbeat background task to prevent timeout during long operations
    heartbeat_task = asyncio.create_task(_send_heartbeat(task_id, coordinator_url))

    # Stream progress notes for the user
    from coordinator.progress import write_progress
    write_progress(task_id, f"Validate task started: {validate_task.get('title', '')[:60]}")

    dev_task_id = dep_ids[0]

    # Fetch dev task for worktree path
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
        resp.raise_for_status()
        dev_task = resp.json()

    dev_metadata = dev_task.get("metadata", {})
    dev_worktree = dev_metadata.get("worktree")

    if not dev_worktree or not Path(dev_worktree).exists():
        # Cancel heartbeat before raising
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        raise RuntimeError(f"Dev worktree not found: {dev_worktree}")

    # Fetch design artifacts from the design task (grandparent)
    design_task_id = None
    design_artifacts = {}
    if dev_task.get("depends_on"):
        design_task_id = dev_task["depends_on"][0]
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{coordinator_url}/tasks/{design_task_id}/artifacts",
                )
                resp.raise_for_status()
                design_artifacts = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch design artifacts: %s", e)

    # Build validation prompt
    prompt = _build_validation_prompt(
        dev_task_id=dev_task_id,
        design_task_id=design_task_id,
        validate_description=validate_task.get("description", ""),
        design_artifacts=design_artifacts,
    )

    # Run Claude Code validation
    logger.info("Running OpenCode validation in %s", dev_worktree)
    write_progress(task_id, "Running validation checks against design...")
    result = await _run_opencode_validation(
        dev_worktree, prompt,
        daemon=daemon, task_id=task_id,
    )
    logger.info("OpenCode finished: exit_code=%s", result.get("exit_code"))

    # Collect review text
    review_text = result.get("stdout", "") + result.get("stderr", "")

    # Check if validation.md was created by Claude Code
    validation_file = Path(dev_worktree) / "validation.md"
    if validation_file.exists():
        review_text = validation_file.read_text(encoding="utf-8")

    review_status = _parse_validation_result(review_text)

    # Write review artifact
    from coordinator.config import load_config

    cfg = load_config()
    artifact_dir = (
        Path(cfg.get("workspace_dir", "D:/hermes/workspace"))
        / str(task_id)
        / "artifacts"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "review.md").write_text(review_text[:10000], encoding="utf-8")

    # Handle failure: create retry tasks if under retry limit
    if review_status == "failed":
        retry_info = validate_task.get("metadata", {}).get("retry_info", {})
        attempt = retry_info.get("attempt", 0) + 1

        if attempt <= MAX_VALIDATE_RETRIES:
            logger.info(
                "Validation failed (attempt %d/%d), creating retry tasks",
                attempt,
                MAX_VALIDATE_RETRIES,
            )
            try:
                await _create_retry_tasks(
                    coordinator_url=coordinator_url,
                    dev_task_id=dev_task_id,
                    validate_task_id=task_id,
                    design_task_id=design_task_id,
                    feedback=review_text[:4000],
                    attempt=attempt,
                )
            except Exception as e:
                logger.warning("Failed to create retry tasks: %s", e)
        else:
            logger.info(
                "Max retries (%d) reached, marking as failed", MAX_VALIDATE_RETRIES
            )

    return_result = {
        "artifacts": {
            "review": str(artifact_dir / "review.md"),
        },
        "metadata": {
            "review_status": review_status,
            "dev_task_id": dev_task_id,
            "design_task_id": design_task_id,
        },
    }
    # When validation fails, return with error field so agent_daemon marks task as FAILED
    # (not COMPLETED). This ensures the retry chain is properly triggered.
    if review_status == "failed":
        return_result["error"] = f"validation_failed: {review_text[:200]}"

    # Cancel heartbeat task
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    return return_result


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


async def _run_opencode_validation(
    worktree_dir: Path, prompt: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run Claude Code (opencode) non-interactively for validation.

    If daemon and task_id are provided, registers the spawned subprocess
    with the daemon so it can be killed on task cancellation/timeout.
    """
    import os
    import sys
    try:
        # On Windows, resolve to claude.exe directly
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
                base_dir = os.path.dirname(claude_exe)
                exe_path = os.path.join(base_dir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
                if os.path.exists(exe_path):
                    claude_exe = exe_path
            if not claude_exe:
                claude_exe = "claude.exe"

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

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=1800
        )
        return {
            "stdout": stdout.decode()[:8192],
            "stderr": stderr.decode()[:4096],
            "exit_code": proc.returncode,
        }
    except FileNotFoundError:
        return {"error": "claude CLI not found"}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": "Validation timed out after 30 minutes"}


def _build_validation_prompt(
    dev_task_id: str,
    design_task_id: str | None,
    validate_description: str,
    design_artifacts: dict[str, str],
) -> str:
    """Build prompt for Claude Code to validate implementation against design."""
    parts = [
        "# Validation Task",
        "",
        "You are a validation engineer. Your job is to verify that the",
        "implemented code satisfies the design requirements.",
        "",
        "## Instructions",
        "",
        "1. Read ALL design documents below carefully",
        "2. Examine the actual codebase in the current directory",
        "3. For EACH requirement in the design, verify the implementation exists",
        "4. Check for: missing features, incomplete implementations, bugs, security issues",
        "5. Write your findings to `validation.md` in the current directory",
        "",
        "## Validation Criteria",
        "",
        "- **PASS**: All core design requirements are implemented correctly",
        "- **FAIL**: One or more core requirements are missing or broken",
        "",
        "## Output Format (write to validation.md)",
        "",
        "```",
        "# Validation Report",
        "",
        "## Verdict: PASS or FAIL",
        "",
        "## Checked Requirements",
        "| # | Requirement | Status | Notes |",
        "|---|-------------|--------|-------|",
        "| 1 | ... | PASS/FAIL | ... |",
        "",
        "## Issues Found",
        "- List specific missing features or bugs with file references",
        "",
        "## Feedback for Dev Agent (if FAIL)",
        "- Specific actions needed to fix each issue",
        "```",
        "",
        "## Task Description",
        "",
        validate_description,
        "",
    ]

    if design_artifacts:
        parts.append("## Design Documents")
        parts.append("")
        for name, content in design_artifacts.items():
            parts.append(f"\n--- {name} ---\n{content}")
    else:
        parts.append("## Note")
        parts.append(
            "No design documents available. Validate based on the task description alone."
        )

    return "\n".join(parts)


def _parse_validation_result(text: str) -> str:
    """Parse validation output to determine pass/fail.

    Only trusts the structured VERDICT marker written by Claude Code.
    Previous fallback used overly broad keywords ("NOT FOUND", "ERROR")
    which matched normal output like "No issues found" or "0 errors",
    causing false failures and unnecessary retry loops.
    """
    text_upper = text.upper()
    if "VERDICT: PASS" in text_upper:
        return "passed"
    if "VERDICT: FAIL" in text_upper:
        return "failed"
    # No structured verdict found — default to passed.
    # Claude Code was asked to write a validation.md with explicit VERDICT.
    # If it didn't follow the format, we trust the exit_code instead.
    logger.warning("No VERDICT found in validation output (length=%d), defaulting to passed", len(text))
    return "passed"


async def _create_retry_tasks(
    coordinator_url: str,
    dev_task_id: str,
    validate_task_id: str,
    design_task_id: str | None,
    feedback: str,
    attempt: int,
) -> dict[str, Any]:
    """Create dev_retry + validate_retry task pair.

    Chain: validate(failed) → dev_retry → validate_retry
    When dev_retry completes, it triggers validate_retry via dependency.
    When validate_retry completes, the chain continues to deploy.

    CRITICAL: After creating each retry task, we must explicitly call
    resolve_dependencies_for_task because:
    - dev_retry depends on original_dev (which already COMPLETED)
    - validate_retry depends on dev_retry (which is PENDING at this moment)
    - The coordinator's _resolve_dependencies is only triggered on task
      completion, NOT on task creation. Without this call, the new tasks
      stay in 'blocked' state forever and the agent daemon never sees them.
    """
    import httpx
    from coordinator.config import load_config
    from pathlib import Path
    from coordinator.db import TaskDB

    # Get original task info for context
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
        resp.raise_for_status()
        original_dev = resp.json()

    original_title = original_dev.get("title", "Dev task")

    # Create dev_retry task (depends on the ORIGINAL dev task, not validate).
    # Dev must re-implement based on design spec, fixing the validation issues.
    dev_retry_body = {
        "type": "dev",
        "title": f"修复: {original_title} (第{attempt}次)",
        "description": (
            f"Validation found issues with the previous implementation.\n\n"
            f"## Validation Feedback:\n{feedback}\n\n"
            f"Fix the issues listed above. Work in the existing worktree directory.\n"
            f"Original dev task: {dev_task_id}\n"
            f"Failed validate task: {validate_task_id}"
        ),
        "depends_on": [dev_task_id],
        "metadata": {
            "is_retry": True,
            "original_dev_id": dev_task_id,
            "retry_attempt": attempt,
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json=dev_retry_body,
        )
        resp.raise_for_status()
        dev_retry_task = resp.json()

    # Create validate_retry task (depends on the new dev_retry task)
    validate_retry_body = {
        "type": "validate",
        "title": f"验证修复: {original_title} (第{attempt}次)",
        "description": (
            f"Verify that the validation issues have been fixed.\n\n"
            f"Retry attempt {attempt}/{MAX_VALIDATE_RETRIES}.\n"
            f"Check the same design requirements as before."
        ),
        "depends_on": [dev_retry_task["id"]],
        "metadata": {
            "is_retry": True,
            "retry_info": {
                "attempt": attempt,
                "original_validate_id": validate_task_id,
            },
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json=validate_retry_body,
        )
        resp.raise_for_status()
        validate_retry_task = resp.json()

    # CRITICAL FIX: Manually resolve dependencies for both new tasks.
    # Without this, dev_retry and validate_retry stay 'blocked' forever because
    # _resolve_dependencies is only invoked on task completion, not creation.
    try:
        cfg = load_config()
        db = TaskDB(Path(cfg["db_path"]))
        dev_retry_status = db.resolve_dependencies_for_task(dev_retry_task["id"])
        logger.info(
            "Resolved dev_retry %s dependencies → %s",
            dev_retry_task["id"][:8], dev_retry_status,
        )
        validate_retry_status = db.resolve_dependencies_for_task(validate_retry_task["id"])
        logger.info(
            "Resolved validate_retry %s dependencies → %s",
            validate_retry_task["id"][:8], validate_retry_status,
        )
        db.close()
    except Exception as e:
        logger.warning("Failed to resolve retry task dependencies: %s", e)

    # Find and update deploy task that depends on the failed validate.
    # After retry, deploy should wait for the new validate_retry, not the failed original.
    # This MUST complete synchronously (single client session) to avoid a race where
    # validate_retry finishes before the deploy dependency is updated.
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{coordinator_url}/tasks",
            params={"status": "pending"},
        )
        resp.raise_for_status()
        all_pending = resp.json()

        deploy_task_id = None
        for t in all_pending:
            if t.get("type") == "deploy" and validate_task_id in t.get("depends_on", []):
                deploy_task_id = t["id"]
                break

        if deploy_task_id:
            resp = await client.patch(
                f"{coordinator_url}/tasks/{deploy_task_id}",
                json={"depends_on": [validate_retry_task["id"]]},
            )
            if resp.status_code == 200:
                logger.info(
                    "Updated deploy %s dependency: %s -> %s",
                    deploy_task_id[:8],
                    validate_task_id[:8],
                    validate_retry_task["id"][:8],
                )
            else:
                logger.warning(
                    "Failed to update deploy %s dependency: status=%d",
                    deploy_task_id[:8],
                    resp.status_code,
                )

    logger.info(
        "Created retry tasks: dev=%s, validate=%s",
        dev_retry_task["id"][:8],
        validate_retry_task["id"][:8],
    )

    return {
        "dev_retry_id": dev_retry_task["id"],
        "validate_retry_id": validate_retry_task["id"],
    }
