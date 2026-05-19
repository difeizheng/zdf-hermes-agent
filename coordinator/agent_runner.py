"""Base agent runner for coordinator-managed agents.

Long-lived daemon that subscribes to SSE events, claims tasks,
executes them, and submits results.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any

import httpx

from coordinator.models import Task

logger = logging.getLogger(__name__)


class AgentRunner(ABC):
    """Base class for agent implementations."""

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
        self._heartbeat_thread: threading.Thread | None = None

    async def run(self) -> None:
        """Main loop: subscribe to SSE, claim tasks, execute, submit."""
        self._running = True
        sse_url = f"{self.coordinator_url}/tasks/events?type={self.agent_type}"

        while self._running:
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream("GET", sse_url) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            import json
                            payload = json.loads(line[5:])
                            if payload.get("type") == "created":
                                task_id = payload.get("task_id")
                                if task_id:
                                    await self._claim_and_execute(task_id)
            except (httpx.HTTPError, asyncio.CancelledError) as e:
                if self._running:
                    logger.warning("SSE connection lost, reconnecting in 5s: %s", e)
                    await asyncio.sleep(5)
                else:
                    break

    async def _claim_and_execute(self, task_id: str) -> None:
        """Try to claim a task, then execute it."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.coordinator_url}/tasks/{task_id}/claim",
                    json={"agent_id": self.agent_id},
                )
                if resp.status_code != 200:
                    logger.debug("Task %s not claimable (status %d)", task_id, resp.status_code)
                    return

                # Start heartbeat thread
                self._start_heartbeat(task_id)

                result = await self.execute_task(task_id)
                await self._submit_result(task_id, result)
        except Exception as e:
            logger.exception("Failed to execute task %s", task_id)
            await self._submit_error(task_id, str(e))
        finally:
            self._stop_heartbeat()

    async def _submit_result(self, task_id: str, result: dict[str, Any]) -> None:
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

    def _start_heartbeat(self, task_id: str) -> None:
        self._heartbeat_task_id = task_id
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(task_id,), daemon=True
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)

    def _heartbeat_loop(self, task_id: str) -> None:
        import time
        while self._running:
            time.sleep(self.heartbeat_interval)
            try:
                httpx.post(
                    f"{self.coordinator_url}/tasks/{task_id}/heartbeat",
                    timeout=10,
                )
            except Exception:
                pass

    @abstractmethod
    async def execute_task(self, task_id: str) -> dict[str, Any]:
        """Execute a task and return result dict with artifacts."""
        ...
