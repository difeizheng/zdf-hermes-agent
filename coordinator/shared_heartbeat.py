"""Shared heartbeat — single implementation replacing 6 copies across agents.

All agent daemons create a background heartbeat task to prevent the
coordinator from marking long-running tasks as stale. Previously each
agent (design, dev, security, qa, validate) duplicated this ~15-line
function. Now they import from here.

Usage::

    from coordinator.shared_heartbeat import start_heartbeat

    hb = start_heartbeat(task_id, coordinator_url)
    try:
        ...  # do work
    finally:
        await hb.cancel_and_wait()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class HeartbeatHandle:
    """Cancellable heartbeat wrapper with try/finally safety."""

    def __init__(self, task: asyncio.Task[None]) -> None:
        self._task = task

    async def cancel_and_wait(self) -> None:
        """Cancel the heartbeat and await cleanup."""
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def start_heartbeat(
    task_id: str,
    coordinator_url: str,
    interval: int = 30,
) -> HeartbeatHandle:
    """Start a background heartbeat task. Returns a handle for safe cancellation.

    The caller MUST call ``await handle.cancel_and_wait()`` in a ``finally``
    block to prevent leaking the background coroutine.
    """
    coro = _heartbeat_loop(task_id, coordinator_url, interval)
    task = asyncio.create_task(coro)
    return HeartbeatHandle(task)


async def _heartbeat_loop(
    task_id: str,
    coordinator_url: str,
    interval: int,
) -> None:
    """Send periodic heartbeats to prevent task timeout."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(f"{coordinator_url}/tasks/{task_id}/heartbeat")
                logger.debug("Sent heartbeat for task %s", task_id[:8])
        except Exception as e:
            logger.warning("Failed to send heartbeat for task %s: %s", task_id[:8], e)
        await asyncio.sleep(interval)
