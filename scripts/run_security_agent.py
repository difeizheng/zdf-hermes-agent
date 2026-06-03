"""Security Agent entry point.

Usage:
    python scripts/run_security_agent.py [--agent-id security-1] [--coordinator-url http://localhost:9100]
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
    parser = argparse.ArgumentParser(description="Security Agent Daemon")
    parser.add_argument("--agent-id", default="security-1")
    parser.add_argument("--coordinator-url", default="http://localhost:9100")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [security] %(levelname)s %(message)s")

    from coordinator.security_agent import run_security_task
    from coordinator.agent_daemon import AgentDaemon

    class SecurityDaemon(AgentDaemon):
        async def execute_task(self, task_id, *, profile=None):
            return await run_security_task(task_id, self.coordinator_url, daemon=self, profile=profile)

    daemon = SecurityDaemon(
        agent_type="security",
        coordinator_url=args.coordinator_url,
        agent_id=args.agent_id,
    )
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())