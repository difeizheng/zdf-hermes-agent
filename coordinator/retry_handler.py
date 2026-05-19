"""Retry logic for failed tasks.

Configurable max_retries with exponential backoff.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RetryHandler:
    """Handles retrying failed tasks with configurable limits."""

    def __init__(
        self,
        max_retries: int = 0,
        retry_delay: int = 30,
        coordinator_url: str = "http://localhost:9100",
    ) -> None:
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.coordinator_url = coordinator_url.rstrip("/")
        self._retry_counts: dict[str, int] = {}

    async def handle_failure(self, task_id: str, task_type: str, error: str) -> bool:
        """Decide whether to retry a failed task.

        Returns True if a retry task was created.
        """
        import httpx

        count = self._retry_counts.get(task_id, 0)
        if count >= self.max_retries:
            logger.warning("Task %s exceeded max retries (%d)", task_id, self.max_retries)
            return False

        # Fetch original task to get description
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.coordinator_url}/tasks/{task_id}")
                resp.raise_for_status()
                task_data = resp.json()
        except Exception:
            logger.exception("Failed to fetch task %s for retry", task_id)
            return False

        # Create retry task with error context
        retry_title = f"[Retry {count + 1}] {task_data['title']}"
        retry_desc = f"Previous attempt failed with: {error}\n\nOriginal: {task_data['description']}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.coordinator_url}/tasks",
                    json={
                        "type": task_type,
                        "title": retry_title,
                        "description": retry_desc,
                        "depends_on": task_data.get("depends_on", []),
                        "metadata": {**task_data.get("metadata", {}), "retry_of": task_id},
                    },
                )
                resp.raise_for_status()
                self._retry_counts[task_id] = count + 1
                logger.info("Created retry task %d/%d for %s", count + 1, self.max_retries, task_id)
                return True
        except Exception:
            logger.exception("Failed to create retry task for %s", task_id)
            return False
