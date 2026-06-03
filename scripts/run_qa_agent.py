"""QA Agent entry point.

Usage:
    python scripts/run_qa_agent.py [--agent-id qa-1] [--coordinator-url http://localhost:9100]
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
    parser = argparse.ArgumentParser(description="QA Agent Daemon")
    parser.add_argument("--agent-id", default="qa-1")
    parser.add_argument("--coordinator-url", default="http://localhost:9100")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [qa] %(levelname)s %(message)s")

    from coordinator.qa_agent import run_qa_task
    from coordinator.agent_daemon import AgentDaemon

    class QADaemon(AgentDaemon):
        async def execute_task(self, task_id, *, profile=None):
            return await run_qa_task(task_id, self.coordinator_url, daemon=self, profile=profile)

    daemon = QADaemon(
        agent_type="qa",
        coordinator_url=args.coordinator_url,
        agent_id=args.agent_id,
    )
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())