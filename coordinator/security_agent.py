"""Security Agent — executes security audit via Claude Code.

Runs security checks on the dev agent's worktree, produces security report.
Part of Phase 3 in the multi-agent pipeline (runs in parallel with QA).
"""

from __future__ import annotations

import asyncio
import logging
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

SECURITY_CHECKS = [
    "bandit -r . -f json -o security_report.json",
    "grep -r 'hardcoded' --include='*.py' --include='*.ts' --include='*.js' .",
]


async def run_security_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a security audit task.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server
        daemon: Optional AgentDaemon instance for subprocess tracking.
        profile: Optional profile configuration from profiles.py.

    Returns:
        Result dict with security report and issues found
    """
    import httpx

    # Fetch task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

    dep_ids = task_data.get("depends_on", [])
    if not dep_ids:
        return {"error": "No dev task dependency found"}

    # Start heartbeat background task
    heartbeat_task = asyncio.create_task(_send_heartbeat(task_id, coordinator_url))

    # Stream progress notes
    from coordinator.progress import write_progress
    write_progress(task_id, f"Security audit started: {task_data.get('title', '')[:60]}")

    dev_task_id = dep_ids[0]

    # Fetch dev task for worktree path
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{dev_task_id}")
        resp.raise_for_status()
        dev_task = resp.json()

    dev_metadata = dev_task.get("metadata", {})
    dev_worktree = dev_metadata.get("worktree")

    if not dev_worktree or not Path(dev_worktree).exists():
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        raise RuntimeError(f"Dev worktree not found: {dev_worktree}")

    # Run automated security scans first
    write_progress(task_id, "Running automated security scans...")
    scan_results = await _run_security_scans(dev_worktree)

    # Load memory context (security-specific errors from past projects)
    memory_context = ""
    try:
        from coordinator.config import load_config, _default_workspace_dir
        _cfg = load_config()
        _workspace = Path(_cfg.get("workspace_dir", _default_workspace_dir()))
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
    result = await _run_claude_security(
        dev_worktree, prompt,
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
    from coordinator.config import load_config, _default_workspace_dir

    cfg = load_config()
    artifact_dir = (
        Path(cfg.get("workspace_dir", _default_workspace_dir()))
        / str(task_id)
        / "artifacts"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "security.md").write_text(review_text[:10000], encoding="utf-8")

    # Determine pass/fail based on thresholds
    # PASS: 0 CRITICAL, ≤2 HIGH
    # REJECT: >0 CRITICAL or >2 HIGH
    security_passed = critical_count == 0 and high_count <= 2

    return_result = {
        "artifacts": {
            "security_report": str(artifact_dir / "security.md"),
        },
        "metadata": {
            "critical_issues": critical_count,
            "high_issues": high_count,
            "issues": issues[:10],  # Top 10 issues
            "security_passed": security_passed,
            "dev_task_id": dev_task_id,
        },
    }

    if not security_passed:
        return_result["error"] = f"security_failed: {critical_count} CRITICAL, {high_count} HIGH"

    # Auto-write security findings to memory for future prevention
    memory_updates = _extract_security_memories(issues, critical_count, high_count)
    if memory_updates:
        return_result["memory_updates"] = memory_updates

    # Cancel heartbeat task
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    return return_result


async def _send_heartbeat(task_id: str, coordinator_url: str) -> None:
    """Send periodic heartbeats to prevent task timeout."""
    import httpx
    while True:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(f"{coordinator_url}/tasks/{task_id}/heartbeat")
                logger.debug("Sent heartbeat for task %s", task_id[:8])
        except Exception as e:
            logger.warning("Failed to send heartbeat for task %s: %s", task_id[:8], e)
        await asyncio.sleep(30)


async def _run_security_scans(worktree_dir: str) -> dict[str, Any]:
    """Run automated security scanning tools."""
    results = {}

    # Run bandit if available
    try:
        proc = await asyncio.create_subprocess_exec(
            "bandit", "-r", ".", "-f", "json",
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_get_timeout("coverage_timeout", 120))
        if proc.returncode == 0:
            import json
            results["bandit"] = json.loads(stdout.decode())
        else:
            results["bandit"] = {"error": stderr.decode()[:500]}
    except FileNotFoundError:
        results["bandit"] = {"error": "bandit not installed"}
    except asyncio.TimeoutError:
        results["bandit"] = {"error": "bandit scan timed out"}
    except Exception as e:
        results["bandit"] = {"error": str(e)}

    # Check for hardcoded secrets (cross-platform, no grep dependency)
    try:
        import re
        secret_pattern = re.compile(
            r"(password|secret|api_key|token|credential)\s*=\s*['\"][^'\"]+['\"]",
            re.IGNORECASE,
        )
        findings: list[str] = []
        worktree_path = Path(worktree_dir)
        code_extensions = {".py", ".ts", ".js", ".tsx", ".jsx"}
        for code_file in worktree_path.rglob("*"):
            if code_file.suffix not in code_extensions:
                continue
            # Skip test/mock/vendor directories
            parts = code_file.parts
            if any(p in ("test", "tests", "__pycache__", "node_modules", ".git", "mock", "mocks", "vendor", "venv", ".venv") for p in parts):
                continue
            try:
                text = code_file.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if secret_pattern.search(line):
                        # Skip obvious test/mock patterns
                        line_stripped = line.strip().lower()
                        if any(kw in line_stripped for kw in ("fixture", "mock", "dummy", "example", "placeholder", "changeme", "test", "fake")):
                            continue
                        rel_path = code_file.relative_to(worktree_path)
                        findings.append(f"{rel_path}:{i}: {line.strip()}")
            except Exception:
                continue
        results["hardcoded_secrets"] = findings[:20]  # Cap at 20 findings
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
            parts.append(f"## Profile Behavior")
            parts.append("")
            parts.append(profile["behavior"])
            parts.append("")
        if profile.get("rules"):
            parts.append(f"Rules enforced: {', '.join(profile['rules'])}")
            parts.append("")

    # Inject memory context (known security errors to check for)
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
    ]

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


async def _run_claude_security(
    worktree_dir: Path,
    prompt: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run Claude Code for security review."""
    import os
    import sys
    try:
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

        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_get_timeout("claude_code_timeout", 1800))
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
        return {"error": "Security review timed out after 30 minutes"}


def _parse_security_result(text: str, scan_results: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Parse security output and scan results to count issues."""
    critical = 0
    high = 0
    issues = []

    # Parse Claude review text
    text_upper = text.upper()
    if "VERDICT: REJECT" in text_upper:
        # Count CRITICAL and HIGH from structured output
        import re
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

    # Add hardcoded secrets findings
    if scan_results.get("hardcoded_secrets"):
        for finding in scan_results["hardcoded_secrets"]:
            critical += 1
            issues.append(f"[CRITICAL] Hardcoded secret: {finding[:100]}")

    return critical, high, issues


def _extract_security_memories(
    issues: list[str], critical_count: int, high_count: int,
) -> list[dict[str, str]]:
    """Extract memory-worthy findings from security scan results.

    Only writes memories for actual issues found (not when clean).
    Each memory is a lesson that prevents the same error in future runs.
    """
    updates: list[dict[str, str]] = []

    # Extract distinct issue patterns (deduplicate by prefix)
    seen_categories: set[str] = set()
    for issue in issues[:10]:
        # Extract category from issue prefix like "[CRITICAL] Hardcoded secret:"
        # or "[Bandit HIGH] B106:"
        if "hardcoded secret" in issue.lower():
            cat = "hardcoded-secrets-found"
            if cat not in seen_categories:
                seen_categories.add(cat)
                updates.append({
                    "category": "errors",
                    "name": f"security-{cat}-{critical_count}c{high_count}h",
                    "content": f"Security audit found hardcoded secrets. Count: {critical_count} CRITICAL, {high_count} HIGH. "
                               f"All credentials must come from environment variables or secret managers. "
                               f"Check: password, secret, api_key, token, credential assignments in source code.",
                })
        elif "bandit" in issue.lower():
            # Extract bandit test ID if present
            import re
            test_id_match = re.search(r"(B\d+)", issue)
            test_id = test_id_match.group(1) if test_id_match else "unknown"
            cat = f"bandit-{test_id}"
            if cat not in seen_categories:
                seen_categories.add(cat)
                issue_text = issue.split("] ", 1)[-1] if "] " in issue else issue
                updates.append({
                    "category": "errors",
                    "name": f"security-bandit-{test_id}",
                    "content": f"Bandit security scan found: {issue_text}. "
                               f"Fix this pattern before submitting code.",
                })

    return updates