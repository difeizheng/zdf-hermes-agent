"""Dev Agent entry point.

Usage:
    python scripts/run_dev_agent.py [--agent-id dev-1] [--coordinator-url http://localhost:9100]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Dev Agent Daemon")
    parser.add_argument("--agent-id", default="dev-1")
    parser.add_argument("--coordinator-url", default="http://localhost:9100")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [dev] %(levelname)s %(message)s")

    from coordinator.dev_agent import run_dev_task
    from coordinator.agent_daemon import AgentDaemon

    class DevDaemon(AgentDaemon):
        async def execute_task(self, task_id):
            return await run_dev_task(task_id, self.coordinator_url)

    daemon = DevDaemon(
        agent_type="dev",
        coordinator_url=args.coordinator_url,
        agent_id=args.agent_id,
    )
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())
