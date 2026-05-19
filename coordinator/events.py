"""SSE event broadcaster for task orchestration.

Multiple subscribers, async-safe. Pattern adapted from mcp_serve.py EventBridge.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Optional

from coordinator.models import TaskEvent


class TaskEventBroadcaster:
    """Broadcast task events to SSE subscribers with optional filtering."""

    def __init__(self) -> None:
        self._subscribers: list[dict] = []
        self._last_event_id: int = 0
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        task_type: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> tuple[int, asyncio.Queue]:
        """Subscribe to events. Returns (last_event_id, queue).

        Callers can filter locally by task_type/task_id, or pass filters
        here for server-side filtering.
        """
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.append({
                "queue": queue,
                "task_type": task_type,
                "task_id": task_id,
            })
            last_id = self._last_event_id
        return last_id, queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers = [
                s for s in self._subscribers if s["queue"] is not queue
            ]

    async def publish(
        self,
        event_type: TaskEvent,
        task_id: str,
        task_type: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> int:
        """Publish an event to all matching subscribers. Returns event_id."""
        async with self._lock:
            self._last_event_id += 1
            event_id = self._last_event_id

        payload = {
            "event_id": event_id,
            "type": event_type.value,
            "task_id": task_id,
            "task_type": task_type,
            "data": data or {},
        }

        async with self._lock:
            for sub in self._subscribers:
                if sub["task_type"] and sub["task_type"] != task_type:
                    continue
                if sub["task_id"] and sub["task_id"] != task_id:
                    continue
                try:
                    sub["queue"].put_nowait(payload)
                except asyncio.QueueFull:
                    pass

        return event_id

    async def sse_stream(
        self,
        task_type: Optional[str] = None,
        task_id: Optional[str] = None,
        last_event_id: int = 0,
    ) -> AsyncIterator[str]:
        """Yield SSE-formatted strings for the event stream endpoint."""
        _, queue = await self.subscribe(task_type=task_type, task_id=task_id)
        try:
            while True:
                payload = await queue.get()
                yield (
                    f"id: {payload['event_id']}\n"
                    f"event: {payload['type']}\n"
                    f"data: {json.dumps(payload)}\n\n"
                )
        finally:
            await self.unsubscribe(queue)
