"""Agent daemon base class — long-lived process subscribing to SSE events."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class AgentDaemon:
    """Long-lived daemon that claims and executes tasks via SSE subscription."""

    def __init__(
        self,
        agent_type: str,
        coordinator_url: str,
        agent_id: str = "default",
        heartbeat_interval: int = 60,
    ) -> None:
        self.agent_type = agent_type
        self.coordinator_url = coordinator_url.rstrip("/")
        self.agent_id = agent_id
        self.heartbeat_interval = heartbeat_interval
        self._running = False

    async def run(self) -> None:
        """Main daemon loop."""
        self._running = True
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop)

        sse_url = f"{self.coordinator_url}/tasks/events?type={self.agent_type}"
        logger.info("Agent daemon starting: type=%s, id=%s", self.agent_type, self.agent_id)

        while self._running:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", sse_url) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not self._running:
                                break
                            if not line or not line.startswith("data:"):
                                continue
                            import json
                            payload = json.loads(line[5:])
                            if payload.get("type") == "created":
                                task_id = payload.get("task_id")
                                if task_id:
                                    await self._claim_and_execute(task_id)
            except (httpx.HTTPError, ConnectionError) as e:
                if self._running:
                    logger.warning("SSE connection lost, reconnecting in 5s: %s", e)
                    await asyncio.sleep(5)
                else:
                    break

        logger.info("Agent daemon stopped")

    def _stop(self) -> None:
        self._running = False

    async def _claim_and_execute(self, task_id: str) -> None:
        """Claim, execute, submit."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.coordinator_url}/tasks/{task_id}/claim",
                    json={"agent_id": self.agent_id},
                )
                if resp.status_code != 200:
                    logger.debug("Task %s not claimable", task_id)
                    return

            result = await self.execute_task(task_id)
            await self._submit(task_id, result)
        except Exception as e:
            logger.exception("Task %s failed", task_id)
            await self._submit_error(task_id, str(e))

    async def _submit(self, task_id: str, result: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{self.coordinator_url}/tasks/{task_id}/result",
                json={"artifacts": result.get("artifacts")},
            )

    async def _submit_error(self, task_id: str, error: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{self.coordinator_url}/tasks/{task_id}/result",
                json={"error": error},
            )

    async def execute_task(self, task_id: str) -> dict[str, Any]:
        """Override in subclass."""
        raise NotImplementedError
