"""Sync coordinator tasks to Hermes Kanban for human visibility.

Optional layer — gracefully degrades if Kanban is unavailable.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_KANBAN_AVAILABLE = False
try:
    from hermes_cli import kanban_db as kb
    _KANBAN_AVAILABLE = True
except Exception:
    kb = None  # type: ignore[assignment]

_STATUS_MAP = {
    "pending": "triage",
    "running": "running",
    "completed": "done",
    "failed": "blocked",
    "cancelled": "archived",
    "timeout": "blocked",
}


def _get_conn():
    """Get Kanban connection, or None if unavailable."""
    if not _KANBAN_AVAILABLE:
        return None
    try:
        return kb.connect()
    except Exception:
        return None


def sync_create(task_id: str, task_type: str, title: str, description: str) -> None:
    """Mirror a new coordinator task in Kanban."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        kb.create_task(
            conn,
            title=f"[{task_type}] {title}",
            body=description,
            assignee=task_type,
            session_id=task_id,
            initial_status="blocked",  # blocked = waiting for agent to claim
        )
        # Immediately unblock to ready so it shows in Kanban as available
        task = _find_by_session(conn, task_id)
        if task:
            try:
                kb.unblock_task(conn, task.id)
            except Exception:
                pass
        logger.info("Kanban sync: created task %s", task_id)
    except Exception:
        logger.exception("Kanban sync create failed for %s", task_id)


def sync_status(task_id: str, status: str) -> None:
    """Mirror a coordinator task status change in Kanban."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        kanban_status = _STATUS_MAP.get(status, "triage")
        task = _find_by_session(conn, task_id)
        if task is None:
            return
        if kanban_status == "done":
            kb.complete_task(conn, task.id, summary=f"Coordinator task {task_id} completed")
        elif kanban_status == "blocked":
            kb.block_task(conn, task.id, reason=f"Coordinator task {task_id} {status}")
        elif kanban_status == "running":
            # Kanban has no explicit 'running' -> 'running' transition API,
            # task is already in running state from create
            pass
    except Exception:
        logger.exception("Kanban sync status failed for %s", task_id)


def _find_by_session(conn, task_id: str):
    """Find a Kanban task by session_id (which stores the coordinator task_id)."""
    if not _KANBAN_AVAILABLE:
        return None
    try:
        tasks = kb.list_tasks(conn, session_id=task_id)
        return tasks[0] if tasks else None
    except Exception:
        return None


def backfill_all() -> int:
    """Backfill all existing coordinator tasks into Kanban. Returns count synced."""
    if not _KANBAN_AVAILABLE:
        return 0
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        from coordinator.db import TaskDB
        from pathlib import Path
        from coordinator.config import load_config
        cfg = load_config()
        db = TaskDB(Path(cfg["db_path"]))
        tasks = db.list_tasks()
        count = 0
        for t in tasks:
            if _find_by_session(conn, str(t.id)) is None:
                kb.create_task(
                    conn,
                    title=f"[{t.type.value}] {t.title}",
                    body=t.description,
                    assignee=t.type.value,
                    session_id=str(t.id),
                    initial_status="blocked",
                )
                # Unblock to ready so pending tasks show in Kanban
                task = _find_by_session(conn, str(t.id))
                if task and t.status.value == "pending":
                    try:
                        kb.unblock_task(conn, task.id)
                    except Exception:
                        pass
                count += 1
        db.close()
        logger.info("Kanban backfill: %d tasks synced", count)
        return count
    except Exception:
        logger.exception("Kanban backfill failed")
        return 0
