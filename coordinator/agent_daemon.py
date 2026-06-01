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
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(self) -> None:
        """Main daemon loop."""
        self._running = True
        # Bypass system proxy for local coordinator connections (Windows issue)
        import os
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        import sys
        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._stop)

        sse_url = f"{self.coordinator_url}/tasks/events?type={self.agent_type}"
        logger.info("Agent daemon starting: type=%s, id=%s", self.agent_type, self.agent_id)

        # Claim any leftover pending tasks on startup
        await self._recover_pending_tasks()

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
        """Claim, execute, submit. Retries on 409 Conflict (race with dispatcher).

        Tracks the active subprocess so it can be killed if the task is
        cancelled or times out while executing. A cancellation watcher polls
        the coordinator every 5s during execution and kills the subprocess
        if the task's status changes to cancelled/timeout.
        """
        max_retries = 5
        retry_delay = 1.0
        for attempt in range(max_retries):
            try:
                # Check if task was cancelled/timed out while we were waiting
                async with httpx.AsyncClient(timeout=30.0) as client:
                    check = await client.get(f"{self.coordinator_url}/tasks/{task_id}")
                    if check.status_code == 200:
                        task_data = check.json()
                        if task_data.get("status") in ("cancelled", "timeout"):
                            logger.info("Task %s already %s, skipping", task_id[:8], task_data["status"])
                            return

                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self.coordinator_url}/tasks/{task_id}/claim",
                        json={"agent_id": self.agent_id},
                    )
                    if resp.status_code == 409:
                        # Race: another agent or dispatcher claimed it first.
                        # Retry with backoff to give the winner time to finish.
                        if attempt < max_retries - 1:
                            logger.debug(
                                "Task %s claim conflict (attempt %d/%d), retrying in %.1fs",
                                task_id, attempt + 1, max_retries, retry_delay,
                            )
                            await asyncio.sleep(retry_delay * (2 ** attempt))
                            retry_delay *= 1.5
                            continue
                        logger.debug("Task %s not claimable after %d retries", task_id, max_retries)
                        return
                    if resp.status_code != 200:
                        logger.debug("Task %s not claimable (status=%d)", task_id, resp.status_code)
                        return

                # Start mid-execution cancellation watcher
                watcher = asyncio.create_task(self._watch_cancellation(task_id))

                try:
                    result = await self.execute_task(task_id)
                finally:
                    # Stop the watcher regardless of outcome
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass

                # Check if the result contains an error (top-level or inside artifacts)
                error_msg = None
                if result and result.get("error"):
                    error_msg = str(result["error"])
                elif result and result.get("artifacts"):
                    artifacts = result["artifacts"]
                    if isinstance(artifacts, dict):
                        if artifacts.get("exit_code") and artifacts["exit_code"] != 0:
                            error_msg = artifacts.get("error") or f"exit code {artifacts['exit_code']}"
                        elif artifacts.get("error") and artifacts["error"]:
                            error_msg = artifacts["error"]

                if error_msg:
                    await self._submit_error(task_id, error_msg)
                    return

                await self._submit(task_id, result)
                return  # Success
            except Exception as e:
                logger.exception("Task %s failed", task_id)
                await self._submit_error(task_id, str(e))
                return
            finally:
                # Clean up tracked subprocess
                self._active_processes.pop(task_id, None)

    async def _watch_cancellation(self, task_id: str) -> None:
        """Background watcher: polls task status; kills subprocess if cancelled/timeout.

        Runs concurrently with execute_task. On detecting a cancellation,
        kills the registered subprocess and returns. Catches and logs all
        errors so it never breaks the parent task.
        """
        try:
            while True:
                await asyncio.sleep(5)
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(f"{self.coordinator_url}/tasks/{task_id}")
                    if resp.status_code != 200:
                        continue
                    task_data = resp.json()
                    status = task_data.get("status")
                    if status in ("cancelled", "timeout"):
                        logger.info(
                            "Task %s status=%s detected during execution, killing subprocess",
                            task_id[:8], status,
                        )
                        await self.kill_active_task(task_id)
                        return
                except Exception as e:
                    logger.debug("Cancellation watcher poll error for %s: %s", task_id[:8], e)
        except asyncio.CancelledError:
            # Normal exit when execute_task completes
            raise
        except Exception:
            logger.exception("Cancellation watcher crashed for %s", task_id[:8])

    def register_subprocess(self, task_id: str, proc: asyncio.subprocess.Process) -> None:
        """Register a subprocess for a task so it can be killed on cancellation."""
        self._active_processes[task_id] = proc
        logger.debug("Registered subprocess for task %s (pid=%s)", task_id[:8], proc.pid)

    async def kill_active_task(self, task_id: str) -> bool:
        """Kill the subprocess associated with a task. Returns True if killed."""
        proc = self._active_processes.get(task_id)
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
                logger.info("Killed subprocess for task %s", task_id[:8])
                return True
            except Exception as e:
                logger.warning("Failed to kill subprocess for task %s: %s", task_id[:8], e)
        return False

    async def _submit(self, task_id: str, result: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{self.coordinator_url}/tasks/{task_id}/result",
                json={
                    "artifacts": result.get("artifacts"),
                    "metadata": result.get("metadata"),
                },
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

    async def _recover_pending_tasks(self) -> None:
        """Claim any pending tasks left over from previous runs."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.coordinator_url}/tasks",
                    params={"status": "pending", "type": self.agent_type},
                )
                resp.raise_for_status()
                tasks = resp.json()
            if not tasks:
                logger.info("No pending tasks to recover")
                return
            logger.info("Recovering %d pending tasks", len(tasks))
            for task in tasks:
                if not self._running:
                    break
                await self._claim_and_execute(task["id"])
        except Exception:
            logger.exception("Failed to recover pending tasks")
