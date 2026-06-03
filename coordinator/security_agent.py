"""Security Agent — executes security audit via Claude Code.

Runs security checks on the dev agent's worktree, produces security report.
Part of Phase 3 in the multi-agent pipeline (runs in parallel with QA).
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from coordinator.shared_heartbeat import start_heartbeat
from coordinator.shared_claude_cli import run_claude_cli
from coordinator.shared_helpers import get_timeout, get_workspace_dir

logger = logging.getLogger(__name__)


async def run_security_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a security audit task."""
    import httpx

    # Fetch task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

    dep_ids = task_data.get("depends_on", [])
    if not dep_ids:
        return {"error": "No dev task dependency found"}

    # Start heartbeat with try/finally safety
    hb = start_heartbeat(task_id, coordinator_url)

    try:
        return await _execute_security(
            task_id, coordinator_url, task_data, daemon=daemon, profile=profile,
        )
    finally:
        await hb.cancel_and_wait()


async def _execute_security(
    task_id: str,
    coordinator_url: str,
    task_data: dict[str, Any],
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Core security execution, called inside heartbeat try/finally."""
    import httpx
    from coordinator.progress import write_progress

    write_progress(task_id, f"Security audit started: {task_data.get('title', '')[:60]}")

    dep_ids = task_data.get("depends_on", [])
    dev_task_id = dep_ids[0]

    # Fetch dev task for worktree path
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
        resp.raise_for_status()
        dev_task = resp.json()

    dev_metadata = dev_task.get("metadata", {})
    dev_worktree = dev_metadata.get("worktree")

    if not dev_worktree or not Path(dev_worktree).exists():
        raise RuntimeError(f"Dev worktree not found: {dev_worktree}")

    # Run automated security scans first
    write_progress(task_id, "Running automated security scans...")
    scan_results = await _run_security_scans(dev_worktree)

    # Load memory context (security-specific errors from past projects)
    memory_context = ""
    try:
        _workspace = Path(get_workspace_dir())
        from coordinator.memory import load_memory_context
        memory_context = load_memory_context(_workspace, categories=["errors"])
    except Exception as e:
        logger.warning("Failed to load memory context: %s", e)

    # Build security audit prompt with scan results
    prompt = _build_security_prompt(
        dev_task_id=dev_task_id,
        scan_results=scan_results,
        task_description=task_data.get("description", ""),
        profile=profile,
        memory_context=memory_context,
    )

    # Run Claude Code for deep security review
    write_progress(task_id, "Running Claude security review...")
    result = await run_claude_cli(
        Path(dev_worktree), prompt,
        daemon=daemon, task_id=task_id,
    )
    logger.info("Security review finished: exit_code=%s", result.get("exit_code"))

    # Collect security report
    review_text = result.get("stdout", "") + result.get("stderr", "")

    # Check if security.md was created
    security_file = Path(dev_worktree) / "security.md"
    if security_file.exists():
        review_text = security_file.read_text(encoding="utf-8")

    # Parse security issues
    critical_count, high_count, issues = _parse_security_result(review_text, scan_results)

    # Write security artifact
    artifact_dir = Path(get_workspace_dir()) / str(task_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "security.md").write_text(review_text[:10000], encoding="utf-8")

    # Determine pass/fail based on thresholds
    security_passed = critical_count == 0 and high_count <= 2

    return_result: dict[str, Any] = {
        "artifacts": {
            "security_report": str(artifact_dir / "security.md"),
        },
        "metadata": {
            "critical_issues": critical_count,
            "high_issues": high_count,
            "issues": issues[:10],
            "security_passed": security_passed,
            "dev_task_id": dev_task_id,
        },
    }

    if not security_passed:
        return_result["error"] = f"security_failed: {critical_count} CRITICAL, {high_count} HIGH"

    # Auto-write security findings to memory
    memory_updates = _extract_security_memories(issues, critical_count, high_count)
    if memory_updates:
        return_result["memory_updates"] = memory_updates

    return return_result


async def _run_security_scans(worktree_dir: str) -> dict[str, Any]:
    """Run automated security scanning tools."""
    results: dict[str, Any] = {}

    # Run bandit if available
    try:
        proc = await asyncio.create_subprocess_exec(
            "bandit", "-r", ".", "-f", "json",
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("coverage_timeout", 120))
        if proc.returncode == 0:
            import json
            results["bandit"] = json.loads(stdout.decode("utf-8", errors="replace"))
        else:
            results["bandit"] = {"error": stderr.decode("utf-8", errors="replace")[:500]}
    except FileNotFoundError:
        results["bandit"] = {"error": "bandit not installed"}
    except asyncio.TimeoutError:
        results["bandit"] = {"error": "bandit scan timed out"}
    except Exception as e:
        results["bandit"] = {"error": str(e)}

    # Check for hardcoded secrets (cross-platform, no grep dependency)
    try:
        secret_pattern = re.compile(
            r"(password|secret|api_key|token|credential)\s*=\s*['\"][^'\"]+['\"]",
            re.IGNORECASE,
        )
        findings: list[str] = []
        worktree_path = Path(worktree_dir)
        code_extensions = {".py", ".ts", ".js", ".tsx", ".jsx"}
        skip_dirs = {"test", "tests", "__pycache__", "node_modules", ".git", "mock", "mocks", "vendor", "venv", ".venv"}
        for code_file in worktree_path.rglob("*"):
            if code_file.suffix not in code_extensions:
                continue
            if any(p in skip_dirs for p in code_file.parts):
                continue
            try:
                text = code_file.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if secret_pattern.search(line):
                        line_stripped = line.strip().lower()
                        if any(kw in line_stripped for kw in ("fixture", "mock", "dummy", "example", "placeholder", "changeme", "test", "fake")):
                            continue
                        rel_path = code_file.relative_to(worktree_path)
                        findings.append(f"{rel_path}:{i}: {line.strip()}")
            except Exception:
                continue
        results["hardcoded_secrets"] = findings[:20]
    except Exception:
        results["hardcoded_secrets"] = []

    return results


def _build_security_prompt(
    dev_task_id: str,
    scan_results: dict[str, Any],
    task_description: str,
    profile: dict[str, Any] | None = None,
    memory_context: str = "",
) -> str:
    """Build prompt for Claude Code security review."""
    parts = [
        "# Security Audit Task",
        "",
        "You are a security auditor. Your job is to find security vulnerabilities.",
        "",
    ]

    # Inject profile behavior
    if profile:
        if profile.get("behavior"):
            parts.append("## Profile Behavior")
            parts.append("")
            parts.append(profile["behavior"])
            parts.append("")
        if profile.get("rules"):
            parts.append(f"Rules enforced: {', '.join(profile['rules'])}")
            parts.append("")

    # Inject memory context
    if memory_context:
        parts.append(memory_context)
        parts.append("")

    parts.extend([
        "## Instructions",
        "",
        "1. Review the codebase in the current directory",
        "2. Focus on: authentication, authorization, input validation, SQL injection, XSS, secrets",
        "3. Check automated scan results below for known issues",
        "4. Write findings to `security.md` in the current directory",
        "",
        "## Severity Classification",
        "",
        "- CRITICAL: Exploit allows data breach or system takeover",
        "- HIGH: Significant security risk requiring immediate fix",
        "- MEDIUM: Security weakness that should be addressed",
        "- LOW: Minor security concern or code quality issue",
        "",
        "## PASS/REJECT Thresholds",
        "",
        "- PASS: 0 CRITICAL issues, ≤2 HIGH issues",
        "- REJECT: >0 CRITICAL issues or >2 HIGH issues",
        "",
        "## Output Format (write to security.md)",
        "",
        "```",
        "# Security Audit Report",
        "",
        "## Verdict: PASS or REJECT",
        "",
        "## Issues Summary",
        "| Severity | Count |",
        "|----------|-------|",
        "| CRITICAL | N |",
        "| HIGH | N |",
        "",
        "## Issues Found",
        "### [CRITICAL/HIGH/MEDIUM/LOW] Issue Title",
        "- File: path/to/file.py:line",
        "- Description: ...",
        "- Recommendation: ...",
        "",
        "## Recommendations",
        "- List of actionable security improvements",
        "```",
        "",
        "## Task Description",
        "",
        task_description,
        "",
        "## Automated Scan Results",
        "",
    ])

    if scan_results.get("bandit"):
        bandit_data = scan_results["bandit"]
        if isinstance(bandit_data, dict) and "results" in bandit_data:
            parts.append("### Bandit Findings")
            for issue in bandit_data["results"][:10]:
                parts.append(f"- {issue.get('test_id', 'N/A')}: {issue.get('issue_text', '')}")
                parts.append(f"  File: {issue.get('filename', '')}:{issue.get('line_number', '')}")
        else:
            parts.append(f"### Bandit: {bandit_data}")

    if scan_results.get("hardcoded_secrets"):
        parts.append("### Hardcoded Secrets Check")
        for finding in scan_results["hardcoded_secrets"][:5]:
            parts.append(f"- {finding}")

    return "\n".join(parts)


def _parse_security_result(text: str, scan_results: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Parse security output and scan results to count issues."""
    critical = 0
    high = 0
    issues: list[str] = []

    # Parse Claude review text
    text_upper = text.upper()
    if "VERDICT: REJECT" in text_upper:
        critical_match = re.search(r"CRITICAL\s*\|\s*(\d+)", text)
        high_match = re.search(r"HIGH\s*\|\s*(\d+)", text)
        if critical_match:
            critical = int(critical_match.group(1))
        if high_match:
            high = int(high_match.group(1))

    # Add bandit findings if available
    bandit_data = scan_results.get("bandit", {})
    if isinstance(bandit_data, dict) and "results" in bandit_data:
        for issue in bandit_data["results"]:
            severity = issue.get("issue_severity", "LOW").upper()
            if severity == "HIGH":
                high += 1
                issues.append(f"[Bandit HIGH] {issue.get('issue_text', '')}")
            elif severity == "MEDIUM":
                issues.append(f"[Bandit MEDIUM] {issue.get('issue_text', '')}")

    # Add hardcoded secrets findings — count as HIGH, not CRITICAL,
    # to avoid false positives inflating the gate (audit P7 fix).
    if scan_results.get("hardcoded_secrets"):
        for finding in scan_results["hardcoded_secrets"]:
            high += 1
            issues.append(f"[HIGH] Hardcoded secret: {finding[:100]}")

    return critical, high, issues


def _extract_security_memories(
    issues: list[str], critical_count: int, high_count: int,
) -> list[dict[str, str]]:
    """Extract memory-worthy findings from security scan results."""
    updates: list[dict[str, str]] = []
    seen_categories: set[str] = set()

    for issue in issues[:10]:
        if "hardcoded secret" in issue.lower():
            cat = "hardcoded-secrets-found"
            if cat not in seen_categories:
                seen_categories.add(cat)
                updates.append({
                    "category": "errors",
                    "name": f"security-{cat}-{critical_count}c{high_count}h",
                    "content": (
                        f"Security audit found hardcoded secrets. Count: {critical_count} CRITICAL, {high_count} HIGH. "
                        f"All credentials must come from environment variables or secret managers. "
                        f"Check: password, secret, api_key, token, credential assignments in source code."
                    ),
                })
        elif "bandit" in issue.lower():
            test_id_match = re.search(r"(B\d+)", issue)
            test_id = test_id_match.group(1) if test_id_match else "unknown"
            cat = f"bandit-{test_id}"
            if cat not in seen_categories:
                seen_categories.add(cat)
                issue_text = issue.split("] ", 1)[-1] if "] " in issue else issue
                updates.append({
                    "category": "errors",
                    "name": f"security-bandit-{test_id}",
                    "content": f"Bandit security scan found: {issue_text}. Fix this pattern before submitting code.",
                })

    return updates
