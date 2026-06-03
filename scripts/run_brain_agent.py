"""Brain Agent — orchestrates the full 5-phase pipeline.

Creates and monitors: Design → Dev → Security + QA → Validate → Deploy

Usage:
    python scripts/run_brain_agent.py --task "Implement user management API"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

PIPELINE_PHASES = ["design", "dev", "security", "qa", "validate", "deploy"]


async def create_pipeline(
    task_title: str,
    task_description: str,
    coordinator_url: str,
    profile: str = "tdd-developer",
    quality_gate: bool = True,
) -> dict[str, Any]:
    """Create the full 5-phase pipeline and monitor execution.

    Returns aggregated results from all phases.
    """
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Phase 1: Design
        logger.info("Creating Design task...")
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json={
                "type": "design",
                "title": f"Design: {task_title}",
                "description": task_description,
            },
        )
        resp.raise_for_status()
        design_task = resp.json()
        design_id = design_task["id"]
        logger.info("Design task created: %s", design_id[:8])

        # Phase 2: Dev (depends on Design)
        logger.info("Creating Dev task...")
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json={
                "type": "dev",
                "title": f"Dev: {task_title}",
                "description": f"{task_description}\nProfile: {profile}\nQuality gate: {quality_gate}",
                "depends_on": [design_id],
            },
        )
        resp.raise_for_status()
        dev_task = resp.json()
        dev_id = dev_task["id"]
        logger.info("Dev task created: %s", dev_id[:8])

        # Phase 3+4: Security + QA (parallel, depends on Dev)
        logger.info("Creating Security task...")
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json={
                "type": "security",
                "title": f"Security: {task_title}",
                "description": f"Audit implementation. Threshold: 0 CRITICAL, <=2 HIGH.",
                "depends_on": [dev_id],
            },
        )
        resp.raise_for_status()
        security_task = resp.json()
        security_id = security_task["id"]
        logger.info("Security task created: %s", security_id[:8])

        logger.info("Creating QA task...")
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json={
                "type": "qa",
                "title": f"QA: {task_title}",
                "description": f"Run tests. Coverage >= 80%.",
                "depends_on": [dev_id],
            },
        )
        resp.raise_for_status()
        qa_task = resp.json()
        qa_id = qa_task["id"]
        logger.info("QA task created: %s", qa_id[:8])

        # Phase 5: Validate (depends on Security + QA)
        logger.info("Creating Validate task...")
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json={
                "type": "validate",
                "title": f"Validate: {task_title}",
                "description": f"Quality gate: Security PASS + QA PASS.",
                "depends_on": [security_id, qa_id],
            },
        )
        resp.raise_for_status()
        validate_task = resp.json()
        validate_id = validate_task["id"]
        logger.info("Validate task created: %s", validate_id[:8])

        # Phase 6: Deploy (depends on Validate)
        logger.info("Creating Deploy task...")
        resp = await client.post(
            f"{coordinator_url}/tasks",
            json={
                "type": "deploy",
                "title": f"Deploy: {task_title}",
                "description": f"Deploy validated implementation.",
                "depends_on": [validate_id],
            },
        )
        resp.raise_for_status()
        deploy_task = resp.json()
        deploy_id = deploy_task["id"]
        logger.info("Deploy task created: %s", deploy_id[:8])

    # Monitor pipeline progress
    task_ids = {
        "design": design_id,
        "dev": dev_id,
        "security": security_id,
        "qa": qa_id,
        "validate": validate_id,
        "deploy": deploy_id,
    }

    logger.info("Pipeline created. Monitoring progress...")
    results = await monitor_pipeline(task_ids, coordinator_url)

    return {
        "task_title": task_title,
        "task_ids": task_ids,
        "results": results,
    }


async def monitor_pipeline(
    task_ids: dict[str, str],
    coordinator_url: str,
    timeout: int = 3600,  # 1 hour max
) -> dict[str, Any]:
    """Monitor all tasks until complete or timeout."""
    import httpx
    import time

    start_time = time.time()
    results = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        while time.time() - start_time < timeout:
            all_done = True
            for phase, task_id in task_ids.items():
                if task_id in results:
                    continue  # Already completed

                resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
                resp.raise_for_status()
                task = resp.json()

                status = task.get("status")
                if status in ("completed", "failed", "cancelled", "timeout"):
                    results[task_id] = task
                    logger.info(
                        "Phase %s %s: %s (error: %s)",
                        phase,
                        status,
                        task_id[:8],
                        str(task.get("error", "none"))[:50],
                    )

                    # Check for failures - may need to stop pipeline
                    if status == "failed" and phase in ("design", "dev"):
                        logger.error("Critical phase failed, stopping pipeline")
                        return results
                else:
                    all_done = False

            if all_done:
                logger.info("Pipeline complete!")
                return results

            await asyncio.sleep(10)  # Poll every 10 seconds

    logger.error("Pipeline timed out after %d seconds", timeout)
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description="Brain Agent — Pipeline Orchestrator")
    parser.add_argument("--task", required=True, help="Task title/description")
    parser.add_argument("--coordinator-url", default="http://localhost:9100")
    parser.add_argument("--profile", default="tdd-developer")
    parser.add_argument("--quality-gate", action="store_true", default=True)
    parser.add_argument("--description", default="", help="Detailed task description")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [brain] %(levelname)s %(message)s",
    )

    task_title = args.task
    task_description = args.description or args.task

    logger.info("Starting pipeline for: %s", task_title)

    result = await create_pipeline(
        task_title=task_title,
        task_description=task_description,
        coordinator_url=args.coordinator_url,
        profile=args.profile,
        quality_gate=args.quality_gate,
    )

    # Print final results
    print("\n" + "=" * 60)
    print("PIPELINE RESULTS")
    print("=" * 60)

    for phase, task_id in result["task_ids"].items():
        task_data = result["results"].get(task_id, {})
        status = task_data.get("status", "unknown")
        error = task_data.get("error", "")

        if status == "completed":
            print(f"{phase.upper():10s} ✅ PASS")
            if phase == "qa":
                coverage = task_data.get("metadata", {}).get("coverage_pct", 0)
                print(f"           Coverage: {coverage}%")
            if phase == "security":
                critical = task_data.get("metadata", {}).get("critical_issues", 0)
                high = task_data.get("metadata", {}).get("high_issues", 0)
                print(f"           Issues: {critical} CRITICAL, {high} HIGH")
        elif status == "failed":
            print(f"{phase.upper():10s} ❌ FAIL")
            print(f"           Error: {error[:100]}")
        else:
            print(f"{phase.upper():10s} ⏳ {status}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())