"""Validate Agent entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Agent Daemon")
    parser.add_argument("--agent-id", default="validate-1")
    parser.add_argument("--coordinator-url", default="http://localhost:9100")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [validate] %(levelname)s %(message)s")

    from coordinator.validate_agent import run_validate_task
    from coordinator.agent_daemon import AgentDaemon

    class ValidateDaemon(AgentDaemon):
        async def execute_task(self, task_id):
            return await run_validate_task(task_id, self.coordinator_url)

    daemon = ValidateDaemon(
        agent_type="validate",
        coordinator_url=args.coordinator_url,
        agent_id=args.agent_id,
    )
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())
