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


def _get_max_retries() -> int:
    """Load max validate retries from config."""
    try:
        from coordinator.config import load_config
        return int(load_config().get("max_validate_retries", 3))
    except Exception:
        return 3


def _get_timeout(key: str, default: int) -> int:
    """Load timeout value from config."""
    try:
        from coordinator.config import load_config
        return int(load_config().get(key, default))
    except Exception:
        return default


def _get_quality_thresholds() -> tuple[int, int, float]:
    """Load quality gate thresholds from config: (max_critical, max_high, min_coverage)."""
    try:
        from coordinator.config import load_config
        cfg = load_config()
        return (
            int(cfg.get("max_critical_issues", 0)),
            int(cfg.get("max_high_issues", 2)),
            float(cfg.get("min_coverage", 80.0)),
        )
    except Exception:
        return (0, 2, 80.0)


async def run_validate_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a validation task.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server
        daemon: Optional AgentDaemon instance for subprocess tracking.
        profile: Optional profile configuration from profiles.py.

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

    # v2 pipeline: Validate depends on Security + QA, not Dev directly.
    # Collect structured results from Security + QA, plus find dev_task_id.
    dev_task_id = None
    security_result: dict[str, Any] = {}
    qa_result: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for dep_id in dep_ids:
            try:
                resp = await client.get(f"{coordinator_url}/tasks/{dep_id}")
                resp.raise_for_status()
                dep_task = resp.json()
                dep_metadata = dep_task.get("metadata", {})
                dep_type = dep_task.get("type", "")
                logger.info("Checked dep %s: type=%s, dev_task_id=%s", dep_id[:8], dep_type, dep_metadata.get("dev_task_id", "N/A"))

                # Collect structured Security/QA results for quality gate
                if dep_type == "security":
                    security_result = dep_metadata
                elif dep_type == "qa":
                    qa_result = dep_metadata

                # Security/QA metadata contains dev_task_id
                if dep_metadata.get("dev_task_id"):
                    dev_task_id = dep_metadata["dev_task_id"]
                    logger.info("Found dev_task_id from %s metadata: %s", dep_type, dev_task_id[:8])
                # Fallback: if dep is Dev task directly
                elif dep_type == "dev":
                    dev_task_id = dep_id
                    logger.info("Dep is Dev task directly: %s", dev_task_id[:8])
            except Exception as e:
                logger.warning("Failed to fetch dep %s: %s", dep_id[:8], e)

        # If not found, walk chain: Security/QA depend on Dev
        if not dev_task_id:
            logger.info("dev_task_id not in metadata, walking dependency chain...")
            for dep_id in dep_ids:
                try:
                    resp = await client.get(f"{coordinator_url}/tasks/{dep_id}")
                    resp.raise_for_status()
                    dep_task = resp.json()
                    dep_deps = dep_task.get("depends_on", [])
                    for dd_id in dep_deps:
                        resp2 = await client.get(f"{coordinator_url}/tasks/{dd_id}")
                        resp2.raise_for_status()
                        dd_task = resp2.json()
                        if dd_task.get("type") == "dev":
                            dev_task_id = dd_id
                            logger.info("Found Dev via chain: %s", dev_task_id[:8])
                            break
                    if dev_task_id:
                        break
                except Exception as e:
                    logger.warning("Failed to walk chain for dep %s: %s", dep_id[:8], e)

        if not dev_task_id:
            logger.error("Could not find Dev task in dependency chain")
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            return {"error": "Could not find Dev task in dependency chain"}

        # Fetch dev task for worktree path
        logger.info("Fetching dev task %s for worktree", dev_task_id[:8])
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

    # ── Quality Gate: aggregate Security + QA structured results ──
    max_critical, max_high, min_coverage = _get_quality_thresholds()
    gate_verdict, gate_reasons = _quality_gate_check(
        security_result, qa_result,
        max_critical=max_critical, max_high=max_high, min_coverage=min_coverage,
    )
    write_progress(
        task_id,
        f"Quality gate: {gate_verdict} (reasons: {len(gate_reasons)})",
    )
    logger.info(
        "Quality gate verdict=%s reasons=%s security=%s qa=%s",
        gate_verdict,
        gate_reasons,
        {k: v for k, v in security_result.items() if k != "issues"},
        {k: v for k, v in qa_result.items() if k != "issues"},
    )

    # Build review text from gate results + optional Claude Code supplementary review
    review_parts = [
        f"# Validation Report\n",
        f"## Quality Gate Verdict: {gate_verdict}\n",
    ]
    if gate_reasons:
        review_parts.append("## Gate Findings\n")
        for reason in gate_reasons:
            review_parts.append(f"- {reason}")
        review_parts.append("")

    # Add structured Security summary
    if security_result:
        review_parts.append("## Security Results\n")
        review_parts.append(f"- CRITICAL: {security_result.get('critical_issues', 'N/A')}")
        review_parts.append(f"- HIGH: {security_result.get('high_issues', 'N/A')}")
        review_parts.append(f"- Verdict: {'PASS' if security_result.get('security_passed') else 'FAIL'}\n")
        for issue in security_result.get("issues", [])[:5]:
            review_parts.append(f"  - {issue}")

    # Add structured QA summary
    if qa_result:
        review_parts.append("## QA Results\n")
        review_parts.append(f"- Tests: {'PASS' if qa_result.get('test_passed') else 'FAIL'}")
        review_parts.append(f"- Coverage: {qa_result.get('coverage_pct', 0):.1f}% (threshold: 80%)")
        review_parts.append(f"- Verdict: {'PASS' if qa_result.get('qa_passed') else 'FAIL'}\n")

    # If gate passed, run a lightweight Claude Code review against design (supplementary)
    # If gate failed, skip Claude Code review — the structured results are sufficient
    if gate_verdict == "PASS" and dev_worktree:
        write_progress(task_id, "Gate passed, running supplementary design review...")
        prompt = _build_validation_prompt(
            dev_task_id=dev_task_id,
            design_task_id=design_task_id,
            validate_description=validate_task.get("description", ""),
            design_artifacts=design_artifacts,
        )
        result = await _run_opencode_validation(
            dev_worktree, prompt,
            daemon=daemon, task_id=task_id,
        )
        logger.info("Supplementary review finished: exit_code=%s", result.get("exit_code"))

        supplementary_text = result.get("stdout", "") + result.get("stderr", "")
        validation_file = Path(dev_worktree) / "validation.md"
        if validation_file.exists():
            supplementary_text = validation_file.read_text(encoding="utf-8")

        supplementary_status = _parse_validation_result(supplementary_text)
        if supplementary_status == "failed":
            gate_verdict = "FAIL"
            gate_reasons.append("Design review found issues (supplementary Claude Code check)")
            review_parts.append(f"## Supplementary Design Review\n\n{supplementary_text[:3000]}\n")
        else:
            review_parts.append("## Supplementary Design Review\n\nPASS — No issues found.\n")

    # Final review status
    review_status = "failed" if gate_verdict == "FAIL" else "passed"
    review_text = "\n".join(review_parts)

    # Write review artifact
    from coordinator.config import load_config, _default_workspace_dir

    cfg = load_config()
    artifact_dir = (
        Path(cfg.get("workspace_dir", _default_workspace_dir()))
        / str(task_id)
        / "artifacts"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "review.md").write_text(review_text[:10000], encoding="utf-8")

    # Handle failure: create retry tasks with specific failure direction
    if review_status == "failed":
        retry_info = validate_task.get("metadata", {}).get("retry_info", {})
        attempt = retry_info.get("attempt", 0) + 1

        if attempt <= _get_max_retries():
            logger.info(
                "Validation failed (attempt %d/%d), creating retry tasks",
                attempt,
                _get_max_retries(),
            )
            # Determine retry focus based on gate reasons
            security_failed = bool(security_result and not security_result.get("security_passed", True))
            qa_failed = bool(qa_result and not qa_result.get("qa_passed", True))

            try:
                await _create_targeted_retry_tasks(
                    coordinator_url=coordinator_url,
                    dev_task_id=dev_task_id,
                    validate_task_id=task_id,
                    design_task_id=design_task_id,
                    feedback=review_text[:4000],
                    attempt=attempt,
                    security_failed=security_failed,
                    qa_failed=qa_failed,
                )
            except Exception as e:
                logger.warning("Failed to create retry tasks: %s", e)
        else:
            logger.info(
                "Max retries (%d) reached, marking as failed", _get_max_retries()
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
            proc.communicate(), timeout=_get_timeout("claude_code_timeout", 1800)
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


def _quality_gate_check(
    security_result: dict[str, Any],
    qa_result: dict[str, Any],
    max_critical: int = 0,
    max_high: int = 2,
    min_coverage: float = 80.0,
) -> tuple[str, list[str]]:
    """Aggregate Security + QA structured results into a PASS/FAIL verdict.

    Returns (verdict, reasons) where verdict is "PASS" or "FAIL" and reasons
    is a list of human-readable strings explaining why the gate failed.
    Empty reasons means PASS.

    Thresholds (from quality-gate SKILL.md):
        - Security: 0 CRITICAL, ≤2 HIGH
        - QA: tests pass, coverage ≥80%
    """
    reasons: list[str] = []

    # ── Security checks ──
    if security_result:
        critical = security_result.get("critical_issues", 0)
        high = security_result.get("high_issues", 0)

        if critical > max_critical:
            reasons.append(
                f"Security: {critical} CRITICAL issue(s) found (max allowed: {max_critical})"
            )
        if high > max_high:
            reasons.append(
                f"Security: {high} HIGH issue(s) found (max allowed: {max_high})"
            )
        if not security_result.get("security_passed", True):
            if critical <= max_critical and high <= max_high:
                reasons.append("Security: overall check failed (unspecified reason)")
    else:
        # No security result — could be pipeline without security phase
        logger.info("No security result available, skipping security gate")

    # ── QA checks ──
    if qa_result:
        test_passed = qa_result.get("test_passed", False)
        coverage = qa_result.get("coverage_pct", 0.0)

        if not test_passed:
            exit_code = qa_result.get("test_exit_code", -1)
            reasons.append(f"QA: tests failed (exit code: {exit_code})")

        if coverage < min_coverage:
            reasons.append(
                f"QA: coverage {coverage:.1f}% below threshold {min_coverage}%"
            )

        if not qa_result.get("qa_passed", True):
            if test_passed and coverage >= min_coverage:
                reasons.append("QA: overall check failed (unspecified reason)")
    else:
        logger.info("No QA result available, skipping QA gate")

    verdict = "FAIL" if reasons else "PASS"
    return verdict, reasons


async def _create_targeted_retry_tasks(
    coordinator_url: str,
    dev_task_id: str,
    validate_task_id: str,
    design_task_id: str | None,
    feedback: str,
    attempt: int,
    security_failed: bool = False,
    qa_failed: bool = False,
) -> dict[str, Any]:
    """Create retry tasks with specific focus based on which gate check failed.

    Creates: dev_retry → (security_retry + qa_retry) → validate_retry
    The retry description tells dev exactly what to fix based on gate reasons.
    """
    import httpx

    # Fetch original dev task for context
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
        resp.raise_for_status()
        original_dev = resp.json()

    original_title = original_dev.get("title", "Dev task")

    # Build targeted fix instructions based on failure direction
    fix_instructions = ["Fix the following issues found during validation:\n"]
    if security_failed:
        fix_instructions.append(
            "## Security Issues (MUST FIX)\n"
            "Fix all CRITICAL and HIGH security findings. "
            "Common fixes: remove hardcoded secrets, add input validation, "
            "add CORS whitelist, add rate limiting.\n"
        )
    if qa_failed:
        fix_instructions.append(
            "## QA Issues (MUST FIX)\n"
            "Fix failing tests and/or increase coverage to ≥80%. "
            "Add unit tests for all new functions and edge cases.\n"
        )
    fix_instructions.append(f"## Detailed Feedback\n{feedback}")

    # 1. Create dev_retry
    dev_retry_body = {
        "type": "dev",
        "title": f"修复: {original_title} (第{attempt}次)",
        "description": "\n".join(fix_instructions),
        "depends_on": [dev_task_id],
        "metadata": {
            "is_retry": True,
            "original_dev_id": dev_task_id,
            "retry_attempt": attempt,
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{coordinator_url}/tasks", json=dev_retry_body)
        resp.raise_for_status()
        dev_retry_task = resp.json()

    # 2. Create security_retry (if security failed)
    retry_dep_ids = [dev_retry_task["id"]]
    if security_failed:
        sec_retry_body = {
            "type": "security",
            "title": f"安全重审: {original_title} (第{attempt}次)",
            "description": "Re-audit after security fixes. Threshold: 0 CRITICAL, ≤2 HIGH.",
            "depends_on": [dev_retry_task["id"]],
            "metadata": {
                "is_retry": True,
                "retry_attempt": attempt,
                "original_dev_id": dev_task_id,
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{coordinator_url}/tasks", json=sec_retry_body)
            resp.raise_for_status()
            sec_retry_task = resp.json()
            retry_dep_ids.append(sec_retry_task["id"])
        logger.info("Created security_retry: %s", sec_retry_task["id"][:8])

    # 3. Create qa_retry (if qa failed)
    if qa_failed:
        qa_retry_body = {
            "type": "qa",
            "title": f"QA重测: {original_title} (第{attempt}次)",
            "description": "Re-run tests after fixes. Coverage ≥80%.",
            "depends_on": [dev_retry_task["id"]],
            "metadata": {
                "is_retry": True,
                "retry_attempt": attempt,
                "original_dev_id": dev_task_id,
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{coordinator_url}/tasks", json=qa_retry_body)
            resp.raise_for_status()
            qa_retry_task = resp.json()
            retry_dep_ids.append(qa_retry_task["id"])
        logger.info("Created qa_retry: %s", qa_retry_task["id"][:8])

    # 4. Create validate_retry (depends on security_retry + qa_retry)
    validate_retry_body = {
        "type": "validate",
        "title": f"验证修复: {original_title} (第{attempt}次)",
        "description": f"Verify all fixes. Retry attempt {attempt}/{_get_max_retries()}.",
        "depends_on": retry_dep_ids,
        "metadata": {
            "is_retry": True,
            "retry_info": {
                "attempt": attempt,
                "original_validate_id": validate_task_id,
            },
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{coordinator_url}/tasks", json=validate_retry_body)
        resp.raise_for_status()
        validate_retry_task = resp.json()

    # 5. Resolve dependencies for all new tasks
    async with httpx.AsyncClient(timeout=30.0) as client:
        for task_id_to_resolve in [dev_retry_task["id"], validate_retry_task["id"]]:
            try:
                await client.post(f"{coordinator_url}/tasks/{task_id_to_resolve}/resolve-deps")
            except Exception as e:
                logger.warning("Failed to resolve deps for %s: %s", task_id_to_resolve[:8], e)

    # 6. Update deploy task dependency (same as original _create_retry_tasks)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{coordinator_url}/tasks", params={"status": "pending"},
        )
        resp.raise_for_status()
        all_pending = resp.json()

        for t in all_pending:
            if t.get("type") == "deploy" and validate_task_id in t.get("depends_on", []):
                await client.patch(
                    f"{coordinator_url}/tasks/{t['id']}",
                    json={"depends_on": [validate_retry_task["id"]]},
                )
                logger.info("Updated deploy %s → wait for validate_retry %s", t["id"][:8], validate_retry_task["id"][:8])
                break

    logger.info(
        "Created targeted retry: dev=%s security=%s qa=%s validate=%s",
        dev_retry_task["id"][:8],
        "yes" if security_failed else "no",
        "yes" if qa_failed else "no",
        validate_retry_task["id"][:8],
    )

    return {
        "dev_retry_id": dev_retry_task["id"],
        "validate_retry_id": validate_retry_task["id"],
    }


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
            f"Retry attempt {attempt}/{_get_max_retries()}.\n"
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
    # Use HTTP API instead of direct DB access to maintain consistency
    # (audit logging, SSE events, Kanban sync all go through the server).
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{coordinator_url}/tasks/{dev_retry_task['id']}/resolve-deps")
            if resp.status_code == 200:
                logger.info(
                    "Resolved dev_retry %s dependencies → %s",
                    dev_retry_task["id"][:8], resp.json().get("dependency_status"),
                )
            else:
                logger.warning("Failed to resolve dev_retry deps: status=%d", resp.status_code)

            resp = await client.post(f"{coordinator_url}/tasks/{validate_retry_task['id']}/resolve-deps")
            if resp.status_code == 200:
                logger.info(
                    "Resolved validate_retry %s dependencies → %s",
                    validate_retry_task["id"][:8], resp.json().get("dependency_status"),
                )
            else:
                logger.warning("Failed to resolve validate_retry deps: status=%d", resp.status_code)
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
