"""QA Agent — runs tests and coverage checks.

Runs tests in the dev agent's worktree, checks coverage threshold (≥80%).
Part of Phase 4 in the multi-agent pipeline (runs in parallel with Security).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from coordinator.shared_heartbeat import start_heartbeat
from coordinator.shared_helpers import get_timeout, get_min_coverage, get_workspace_dir

logger = logging.getLogger(__name__)


async def run_qa_task(
    task_id: str,
    coordinator_url: str,
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a QA task."""
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
        return await _execute_qa(
            task_id, coordinator_url, task_data, daemon=daemon, profile=profile,
        )
    finally:
        await hb.cancel_and_wait()


async def _execute_qa(
    task_id: str,
    coordinator_url: str,
    task_data: dict[str, Any],
    daemon: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Core QA execution, called inside heartbeat try/finally."""
    import httpx
    from coordinator.progress import write_progress

    write_progress(task_id, f"QA task started: {task_data.get('title', '')[:60]}")

    dep_ids = task_data.get("depends_on", [])
    if not dep_ids:
        return {"error": "No dev task dependency found"}
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

    # Detect project type and run appropriate tests
    write_progress(task_id, "Detecting project type...")
    project_type = _detect_project_type(dev_worktree)

    write_progress(task_id, f"Running tests for {project_type} project...")
    # For Python projects, run tests + coverage in a single invocation
    if project_type == "python":
        combined_results = await _run_tests_with_coverage(dev_worktree, daemon=daemon, task_id=task_id)
        test_results = combined_results["test"]
        coverage_results = combined_results["coverage"]
    else:
        test_results = await _run_tests(dev_worktree, project_type, daemon=daemon, task_id=task_id)
        write_progress(task_id, "Checking coverage...")
        coverage_results = await _run_coverage(dev_worktree, project_type)

    # Parse results
    test_passed = test_results.get("exit_code", 1) == 0
    coverage_pct = coverage_results.get("coverage", 0.0)
    coverage_passed = coverage_pct >= get_min_coverage()

    # Write QA artifact
    artifact_dir = Path(get_workspace_dir()) / str(task_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    qa_report = _build_qa_report(
        test_results=test_results,
        coverage_results=coverage_results,
        test_passed=test_passed,
        coverage_passed=coverage_passed,
    )
    (artifact_dir / "qa.md").write_text(qa_report, encoding="utf-8")

    # Determine overall pass/fail
    qa_passed = test_passed and coverage_passed

    return_result = {
        "artifacts": {
            "qa_report": str(artifact_dir / "qa.md"),
        },
        "metadata": {
            "test_passed": test_passed,
            "test_exit_code": test_results.get("exit_code", -1),
            "coverage_pct": coverage_pct,
            "coverage_passed": coverage_passed,
            "project_type": project_type,
            "qa_passed": qa_passed,
            "dev_task_id": dev_task_id,
        },
    }

    if not qa_passed:
        if not test_passed:
            return_result["error"] = f"tests_failed: exit_code={test_results.get('exit_code', -1)}"
        else:
            return_result["error"] = f"coverage_insufficient: {coverage_pct}% < {get_min_coverage()}%"

    # Auto-write QA learnings to memory
    memory_updates = _extract_qa_memories(test_passed, coverage_pct, project_type)
    if memory_updates:
        return_result["memory_updates"] = memory_updates

    return return_result


def _detect_project_type(worktree_dir: str) -> str:
    """Detect project type from directory contents."""
    path = Path(worktree_dir)

    if path.joinpath("pyproject.toml").exists():
        return "python"
    if path.joinpath("setup.py").exists():
        return "python"
    if path.joinpath("requirements.txt").exists():
        return "python"

    if path.joinpath("package.json").exists():
        pkg = path.joinpath("package.json")
        try:
            import json
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = data.get("dependencies", {})
            dev_deps = data.get("devDependencies", {})
            all_deps = {**deps, **dev_deps}

            if "react" in all_deps or "next" in all_deps:
                return "frontend"
            if "@nestjs/core" in all_deps or "express" in all_deps:
                return "nodejs"
            return "nodejs"
        except Exception:
            return "nodejs"

    if path.joinpath("Cargo.toml").exists():
        return "rust"

    if path.joinpath("go.mod").exists():
        return "golang"

    if path.joinpath("pom.xml").exists() or path.joinpath("build.gradle").exists():
        return "java"

    return "unknown"


async def _run_tests_with_coverage(
    worktree_dir: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Run pytest with coverage in a single invocation (avoids running tests twice).

    Returns dict with 'test' and 'coverage' sub-dicts for compatibility with
    the existing result processing logic.
    """
    import re
    try:
        cmd = ["pytest", "-v", "--tb=short", "--cov=.", "--cov-report=term-missing"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("test_timeout", 600))
        output = stdout.decode("utf-8", errors="replace")
        error_output = stderr.decode("utf-8", errors="replace")
        coverage = 0.0
        match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
        if match:
            coverage = float(match.group(1))

        return {
            "test": {
                "exit_code": proc.returncode,
                "output": output[:4096],
                "error": error_output[:2048],
            },
            "coverage": {
                "coverage": coverage,
                "output": output[:2048],
            },
        }
    except FileNotFoundError:
        return {
            "test": {"exit_code": -1, "error": "pytest not found"},
            "coverage": {"coverage": 0.0, "error": "pytest not found"},
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "test": {"exit_code": -1, "error": f"Tests timed out after {get_timeout('test_timeout', 600) // 60} minutes"},
            "coverage": {"coverage": 0.0, "error": "Tests timed out"},
        }
    except Exception as e:
        return {
            "test": {"exit_code": -1, "error": str(e)},
            "coverage": {"coverage": 0.0, "error": str(e)},
        }


async def _run_tests(
    worktree_dir: str,
    project_type: str,
    daemon: Any = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run tests based on project type."""
    try:
        if project_type == "python":
            cmd = ["pytest", "-v", "--tb=short"]
        elif project_type == "frontend":
            cmd = ["npm", "test"]
        elif project_type == "nodejs":
            cmd = ["npm", "test"]
        elif project_type == "rust":
            cmd = ["cargo", "test"]
        elif project_type == "golang":
            cmd = ["go", "test", "-v", "./..."]
        elif project_type == "java":
            cmd = ["mvn", "test"]
        else:
            # Fallback: try pytest or npm test
            if Path(worktree_dir, "pytest.ini").exists() or Path(worktree_dir, "pyproject.toml").exists():
                cmd = ["pytest", "-v"]
            elif Path(worktree_dir, "package.json").exists():
                cmd = ["npm", "test"]
            else:
                return {"exit_code": 0, "output": "No tests found (skipped)", "skipped": True}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("test_timeout", 600))
        return {
            "exit_code": proc.returncode,
            "output": stdout.decode("utf-8", errors="replace")[:4096],
            "error": stderr.decode("utf-8", errors="replace")[:2048],
        }
    except FileNotFoundError:
        return {"exit_code": -1, "error": f"Test runner not found for {project_type}"}
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {"exit_code": -1, "error": f"Tests timed out after {get_timeout('test_timeout', 600) // 60} minutes"}
    except Exception as e:
        return {"exit_code": -1, "error": str(e)}


async def _run_coverage(worktree_dir: str, project_type: str) -> dict[str, Any]:
    """Run coverage analysis based on project type."""
    try:
        if project_type == "python":
            cmd = ["pytest", "--cov=.", "--cov-report=term-missing"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=worktree_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("coverage_timeout", 300))
            output = stdout.decode("utf-8", errors="replace")

            # Parse coverage percentage from pytest-cov output
            import re
            match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
            if match:
                coverage = float(match.group(1))
            else:
                coverage = 0.0

            return {"coverage": coverage, "output": output[:2048]}

        elif project_type in ("frontend", "nodejs"):
            cmd = ["npm", "run", "coverage"] if Path(worktree_dir, "package.json").exists() else []
            if not cmd:
                return {"coverage": 0.0, "error": "No coverage script in package.json"}

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=worktree_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("coverage_timeout", 300))
            output = stdout.decode("utf-8", errors="replace")

            # Parse coverage from vitest/jest output
            import re
            match = re.search(r"All files[|\s]+(\d+\.?\d*)", output)
            if match:
                coverage = float(match.group(1))
            else:
                coverage = 0.0

            return {"coverage": coverage, "output": output[:2048]}

        elif project_type == "rust":
            # Rust coverage requires cargo-tarpaulin
            cmd = ["cargo", "tarpaulin", "--out", "Std"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=worktree_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("coverage_timeout", 300))
            output = stdout.decode("utf-8", errors="replace")

            import re
            match = re.search(r"(\d+\.?\d*)% coverage", output)
            if match:
                coverage = float(match.group(1))
            else:
                coverage = 0.0

            return {"coverage": coverage, "output": output[:2048]}

        elif project_type == "golang":
            cmd = ["go", "test", "-cover", "./..."]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=worktree_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=get_timeout("coverage_timeout", 300))
            output = stdout.decode("utf-8", errors="replace")

            # Parse Go coverage
            import re
            matches = re.findall(r"coverage:\s+(\d+\.?\d)%", output)
            if matches:
                coverage = max(float(m) for m in matches)
            else:
                coverage = 0.0

            return {"coverage": coverage, "output": output[:2048]}

        else:
            return {"coverage": 0.0, "error": f"Coverage not supported for {project_type}"}

    except FileNotFoundError:
        return {"coverage": 0.0, "error": f"Coverage tool not found for {project_type}"}
    except asyncio.TimeoutError:
        return {"coverage": 0.0, "error": "Coverage analysis timed out"}
    except Exception as e:
        return {"coverage": 0.0, "error": str(e)}


def _build_qa_report(
    test_results: dict[str, Any],
    coverage_results: dict[str, Any],
    test_passed: bool,
    coverage_passed: bool,
) -> str:
    """Build QA report in markdown format."""
    verdict = "PASS" if (test_passed and coverage_passed) else "REJECT"

    parts = [
        "# QA Report",
        "",
        f"## Verdict: {verdict}",
        "",
        "## Test Results",
        "",
        f"- Status: {test_results.get('exit_code', -1)} ({'PASS' if test_passed else 'FAIL'})",
        "",
        "### Test Output",
        "",
        test_results.get("output", "")[:2000],
        "",
        "## Coverage Results",
        "",
        f"- Coverage: {coverage_results.get('coverage', 0.0):.1f}% ({'PASS' if coverage_passed else 'FAIL'})",
        f"- Threshold: {get_min_coverage()}%",
        "",
        "### Coverage Output",
        "",
        coverage_results.get("output", "")[:1000],
        "",
        "## Summary",
        "",
        f"- Tests: {'PASS' if test_passed else 'FAIL'}",
        f"- Coverage: {'PASS' if coverage_passed else 'FAIL'} ({coverage_results.get('coverage', 0.0):.1f}%)",
        f"- Overall: {verdict}",
    ]

    if test_results.get("error"):
        parts.extend(["", "## Test Errors", "", test_results["error"]])

    if coverage_results.get("error"):
        parts.extend(["", "## Coverage Errors", "", coverage_results["error"]])

    return "\n".join(parts)


def _extract_qa_memories(
    test_passed: bool, coverage_pct: float, project_type: str,
) -> list[dict[str, str]]:
    """Extract memory-worthy patterns from QA results.

    Writes patterns when tests pass (best practices) and errors when they fail
    (lessons learned for future avoidance).
    """
    updates: list[dict[str, str]] = []

    if test_passed and coverage_pct >= get_min_coverage():
        # Success pattern — what worked
        updates.append({
            "category": "patterns",
            "name": f"qa-pass-{project_type}-{int(coverage_pct)}pct",
            "content": (
                f"QA passed for {project_type} project. Coverage: {coverage_pct:.1f}%. "
                f"Testing strategy: run pytest with --cov for Python, npm test for Node.js. "
                f"Threshold: {get_min_coverage()}% coverage, all tests passing."
            ),
        })
    elif not test_passed:
        updates.append({
            "category": "errors",
            "name": f"qa-tests-failed-{project_type}",
            "content": (
                f"QA tests failed for {project_type} project. "
                f"Ensure all new functionality has corresponding tests before submitting. "
                f"Run tests locally before pushing to catch failures early."
            ),
        })
    elif coverage_pct < get_min_coverage():
        updates.append({
            "category": "errors",
            "name": f"qa-coverage-low-{project_type}-{int(coverage_pct)}pct",
            "content": (
                f"Coverage {coverage_pct:.1f}% below threshold {get_min_coverage()}% for {project_type}. "
                f"Add unit tests for all new functions and edge cases. "
                f"Focus on uncovered branches and error handling paths."
            ),
        })

    return updates