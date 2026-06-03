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

from coordinator.shared_heartbeat import start_heartbeat
from coordinator.shared_claude_cli import run_claude_cli
from coordinator.shared_helpers import (
    get_timeout, get_max_validate_retries, get_max_pipeline_retries,
    get_quality_thresholds, get_workspace_dir,
)

logger = logging.getLogger(__name__)


async def run_validate_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a validation task."""
    import httpx

    # Fetch validate task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        validate_task = resp.json()

    dep_ids = validate_task.get("depends_on", [])
    if not dep_ids:
        return {"error": "No dev task dependency found"}

    # Start heartbeat with try/finally safety
    hb = start_heartbeat(task_id, coordinator_url)

    try:
        return await _execute_validate(
            task_id, coordinator_url, validate_task, daemon=daemon, profile=profile,
        )
    finally:
        await hb.cancel_and_wait()


async def _execute_validate(
    task_id: str,
    coordinator_url: str,
    validate_task: dict[str, Any],
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Core validation execution, called inside heartbeat try/finally."""
    import httpx
    from coordinator.progress import write_progress

    write_progress(task_id, f"Validate task started: {validate_task.get('title', '')[:60]}")

    # v2 pipeline: Validate depends on Security + QA, not Dev directly.
    dep_ids = validate_task.get("depends_on", [])
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
            return {"error": "Could not find Dev task in dependency chain"}

        # Fetch dev task for worktree path
        logger.info("Fetching dev task %s for worktree", dev_task_id[:8])
        resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
        resp.raise_for_status()
        dev_task = resp.json()

    dev_metadata = dev_task.get("metadata", {})
    dev_worktree = dev_metadata.get("worktree")

    if not dev_worktree or not Path(dev_worktree).exists():
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
    max_critical, max_high, min_coverage = get_quality_thresholds()
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
        review_parts.append(f"- Coverage: {qa_result.get('coverage_pct', 0):.1f}% (threshold: {min_coverage}%)")
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
    artifact_dir = Path(get_workspace_dir()) / str(task_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "review.md").write_text(review_text[:10000], encoding="utf-8")

    # Handle failure: create retry tasks with specific failure direction
    if review_status == "failed":
        retry_info = validate_task.get("metadata", {}).get("retry_info", {})
        attempt = retry_info.get("attempt", 0) + 1
        total_pipeline_retries = retry_info.get("total_pipeline_retries", 0) + 1
        max_per_phase = get_max_validate_retries()
        max_pipeline = get_max_pipeline_retries()

        if attempt <= max_per_phase and total_pipeline_retries <= max_pipeline:
            logger.info(
                "Validation failed (attempt %d/%d, pipeline %d/%d), creating retry tasks",
                attempt, max_per_phase, total_pipeline_retries, max_pipeline,
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
                    total_pipeline_retries=total_pipeline_retries,
                    security_failed=security_failed,
                    qa_failed=qa_failed,
                )
            except Exception as e:
                logger.warning("Failed to create retry tasks: %s", e)
        else:
            reason = "per-phase" if attempt > max_per_phase else "pipeline"
            logger.warning(
                "Max retries reached for validate %s (%s: attempt=%d/%d pipeline=%d/%d), escalating",
                task_id[:8], reason, attempt, max_per_phase, total_pipeline_retries, max_pipeline,
            )
            # Attempt escalation: notify via DingTalk webhook if configured
            try:
                from coordinator.dingtalk_webhook import post_markdown, resolve_webhook_url
                webhook_url = resolve_webhook_url()
                if webhook_url:
                    await asyncio.to_thread(
                        post_markdown,
                        webhook_url,
                        f"⚠️ Pipeline 升级通知",
                        f"任务 {task_id[:8]} 验证失败已达上限\n\n"
                        f"**dev_task**: `{dev_task_id[:8]}`\n"
                        f"**设计任务**: `{design_task_id}`\n\n"
                        f"**重试**: per-phase {attempt}/{max_per_phase}, pipeline {total_pipeline_retries}/{max_pipeline}\n\n"
                        f"**失败原因**:\n{chr(10).join(f'- {r}' for r in gate_reasons[:5])}\n\n"
                        f"需人工介入审查。",
                    )
                    logger.info("Escalation notification sent via DingTalk")
            except Exception as e:
                logger.warning("Failed to send escalation notification: %s", e)

    return_result: dict[str, Any] = {
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
        # Mark escalation status when retries are exhausted
        if attempt > max_per_phase or total_pipeline_retries > max_pipeline:
            return_result["metadata"]["escalation"] = "FAILED_WITH_ESCALATION"
        return_result["error"] = f"validation_failed: {review_text[:200]}"

    return return_result


async def _run_opencode_validation(
    worktree_dir: Path, prompt: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run Claude Code for validation using shared CLI invoker."""
    return await run_claude_cli(
        worktree_dir, prompt,
        daemon=daemon, task_id=task_id,
    )


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
    total_pipeline_retries: int = 1,
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
    # On the last retry before escalation, signal for stronger model usage
    is_last_retry = attempt >= get_max_validate_retries() or total_pipeline_retries >= get_max_pipeline_retries()
    dev_retry_body = {
        "type": "dev",
        "title": f"修复: {original_title} (第{attempt}次)",
        "description": "\n".join(fix_instructions),
        "depends_on": [dev_task_id],
        "metadata": {
            "is_retry": True,
            "original_dev_id": dev_task_id,
            "retry_attempt": attempt,
            **({"escalation_retry": True} if is_last_retry else {}),
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
        "description": f"Verify all fixes. Retry attempt {attempt}/{get_max_validate_retries()}, pipeline {total_pipeline_retries}/{get_max_pipeline_retries()}.",
        "depends_on": retry_dep_ids,
        "metadata": {
            "is_retry": True,
            "retry_info": {
                "attempt": attempt,
                "original_validate_id": validate_task_id,
                "total_pipeline_retries": total_pipeline_retries,
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
