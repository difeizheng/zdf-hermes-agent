"""SQLite CRUD layer for task orchestration.

Follows hermes_state.py patterns: WAL mode, thread-safe, connection-level locking.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

from coordinator.models import (
    Task,
    TaskCreate,
    TaskEvent,
    TaskEventModel,
    TaskStatus,
)

_SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    artifacts       TEXT DEFAULT '{}',
    error           TEXT,
    assigned_to     TEXT,
    dependency_status TEXT DEFAULT 'blocked',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    last_heartbeat_at TIMESTAMP,
    metadata        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    data        TEXT DEFAULT '{}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type);
CREATE INDEX IF NOT EXISTS idx_tasks_dependency_status ON tasks(dependency_status);
CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);
"""


class TaskDB:
    """Thread-safe SQLite task store."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA_DDL)

    # -- helpers ----------------------------------------------------------------

    def _row_to_task(self, row: sqlite3.Row, deps: list[str]) -> Task:
        return Task(
            id=row["id"],
            type=row["type"],
            status=row["status"],
            title=row["title"],
            description=row["description"],
            depends_on=deps,
            artifacts=json.loads(row["artifacts"] or "{}"),
            error=row["error"],
            assigned_to=row["assigned_to"],
            dependency_status=row["dependency_status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # -- CRUD -------------------------------------------------------------------

    def create_task(self, task_in: TaskCreate) -> Task:
        with self._lock:
            task_id = str(uuid.uuid4())
            cur = self._conn.execute(
                "INSERT INTO tasks (id, type, title, description, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    task_in.type.value,
                    task_in.title,
                    task_in.description,
                    json.dumps(task_in.metadata),
                ),
            )
            # Insert dependencies
            for dep_id in task_in.depends_on:
                self._conn.execute(
                    "INSERT INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                    (task_id, dep_id),
                )
            # Resolve dependency status if no deps
            dep_status = "blocked" if task_in.depends_on else "satisfied"
            self._conn.execute(
                "UPDATE tasks SET dependency_status = ? WHERE id = ?",
                (dep_status, task_id),
            )
            self._conn.commit()
            return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: int | str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            deps = [
                d[0] for d in self._conn.execute(
                    "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
            ]
            return self._row_to_task(row, deps)

    def update_task_status(
        self,
        task_id: int | str,
        status: TaskStatus,
        assigned_to: Optional[str] = None,
    ) -> Optional[Task]:
        with self._lock:
            fields = []
            vals: list[object] = []
            if status == TaskStatus.RUNNING:
                fields.append("started_at = CURRENT_TIMESTAMP")
            if assigned_to is not None:
                fields.append("assigned_to = ?")
                vals.append(assigned_to)
            fields.append("status = ?")
            vals.append(status.value)
            vals.append(task_id)
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", vals
            )
            self._conn.commit()
            return self.get_task(task_id)

    def update_task_dependencies(self, task_id: int | str, depends_on: list[str]) -> Optional[Task]:
        """Replace all dependencies of task_id with new depends_on list. Recomputes dependency_status."""
        with self._lock:
            # Delete existing dependencies
            self._conn.execute(
                "DELETE FROM task_dependencies WHERE task_id = ?", (task_id,)
            )
            # Insert new dependencies
            for dep_id in depends_on:
                self._conn.execute(
                    "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                    (task_id, dep_id),
                )
            # Recompute dependency_status
            unsatisfied = self._conn.execute(
                "SELECT 1 FROM task_dependencies td "
                "JOIN tasks t ON t.id = td.depends_on "
                "WHERE td.task_id = ? AND t.status != 'completed' LIMIT 1",
                (task_id,),
            ).fetchone()
            dep_status = "blocked" if unsatisfied else "satisfied"
            self._conn.execute(
                "UPDATE tasks SET dependency_status = ? WHERE id = ?",
                (dep_status, task_id),
            )
            self._conn.commit()
            return self.get_task(task_id)

    def claim_task(self, task_id: int | str, agent_id: str) -> Optional[Task]:
        """Atomic CAS: pending → running with assigned_to. Only if all deps completed."""
        with self._lock:
            # Verify all dependencies are in terminal state (completed/cancelled/timeout/failed)
            # Note: failed/cancelled/timeout also unblock dependents (per resolve_dependencies_for_task)
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM task_dependencies td "
                "LEFT JOIN tasks t ON t.id = td.depends_on "
                "WHERE td.task_id = ? AND (t.status IS NULL OR t.status NOT IN ('completed', 'cancelled', 'timeout', 'failed'))",
                (task_id,),
            )
            incomplete = cur.fetchone()[0]
            if incomplete > 0:
                return None

            cur = self._conn.execute(
                "UPDATE tasks SET status = 'running', started_at = CURRENT_TIMESTAMP, "
                "assigned_to = ?, last_heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'pending'",
                (agent_id, task_id),
            )
            if cur.rowcount == 0:
                return None
            self._conn.commit()
            return self.get_task(task_id)

    def submit_result(
        self,
        task_id: int | str,
        artifacts: Optional[dict] = None,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[Task]:
        with self._lock:
            status = TaskStatus.FAILED if error else TaskStatus.COMPLETED
            vals: list = [status.value, task_id]
            fields = ["status = ?", "completed_at = CURRENT_TIMESTAMP"]
            if artifacts:
                fields.append("artifacts = ?")
                vals.insert(-1, json.dumps(artifacts))
            if error is not None:
                fields.append("error = ?")
                vals.insert(-1, error)
            if metadata is not None:
                fields.append("metadata = ?")
                vals.insert(-1, json.dumps(metadata))
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", vals
            )
            self._conn.commit()
            task = self.get_task(task_id)
            if task:
                # Always resolve dependencies on task completion or failure.
                # Terminal states (completed/failed) unblock downstream tasks
                # so the pipeline doesn't stall when Security/QA/Dev fail.
                self._resolve_dependencies_unlocked(task_id)
            return task

    def list_tasks(
        self,
        status: Optional[TaskStatus] = None,
        task_type: Optional[str] = None,
    ) -> list[Task]:
        with self._lock:
            query = "SELECT * FROM tasks WHERE 1=1"
            params: list = []
            if status:
                query += " AND status = ?"
                params.append(status.value)
            if task_type:
                query += " AND type = ?"
                params.append(task_type)
            query += " ORDER BY created_at DESC"
            rows = self._conn.execute(query, params).fetchall()
            tasks = []
            for row in rows:
                deps = [
                    d[0] for d in self._conn.execute(
                        "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                        (row["id"],),
                    ).fetchall()
                ]
                tasks.append(self._row_to_task(row, deps))
            return tasks

    def add_dependency(self, task_id: int | str, depends_on: int | str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                (task_id, depends_on),
            )
            self._conn.execute(
                "UPDATE tasks SET dependency_status = 'blocked' WHERE id = ?",
                (task_id,),
            )
            self._conn.commit()

    def timeout_and_resolve(self, timeout_seconds: int = 120) -> list[Task]:
        """Mark stale running tasks as TIMEOUT and resolve their dependents.

        Thread-safe wrapper that acquires the DB lock, finds stale tasks,
        updates their status, and resolves dependencies so dependents are
        not permanently blocked. Returns the list of tasks that were timed out.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' "
                "AND last_heartbeat_at IS NOT NULL "
                "AND (julianday('now') - julianday(last_heartbeat_at)) * 86400 > ?",
                (timeout_seconds,),
            ).fetchall()
            timed_out: list[Task] = []
            for row in rows:
                self._conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    (TaskStatus.TIMEOUT.value, row["id"]),
                )
                self._resolve_dependencies_unlocked(row["id"])
                # Fetch deps for the timed-out task
                deps = [
                    d[0] for d in self._conn.execute(
                        "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                        (row["id"],),
                    ).fetchall()
                ]
                timed_out.append(self._row_to_task(row, deps))
            self._conn.commit()
            return timed_out

    def _resolve_dependencies_unlocked(self, completed_task_id: int | str) -> None:
        """When a task completes or enters a terminal state, check if blocked tasks become satisfied.

        Terminal states: completed, cancelled, timeout, failed.
        This ensures that when an agent crashes (→ timeout), its dependents
        are not permanently blocked. The downstream agent's own retry logic
        will handle the missing work.

        IMPORTANT: Caller MUST hold self._lock before calling this method.
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info("_resolve_dependencies called for task: %s", completed_task_id)

        # Find all tasks that depend on the completed task
        rows = self._conn.execute(
            "SELECT td.task_id FROM task_dependencies td "
            "WHERE td.depends_on = ?",
            (completed_task_id,),
        ).fetchall()
        logger.info("_resolve_dependencies found %d dependent tasks", len(rows))
        for (dependent_id,) in rows:
            unsatisfied = self._conn.execute(
                "SELECT 1 FROM task_dependencies td "
                "JOIN tasks t ON t.id = td.depends_on "
                "WHERE td.task_id = ? AND t.status NOT IN ('completed', 'cancelled', 'timeout', 'failed')",
                (dependent_id,),
            ).fetchone()
            if unsatisfied is None:
                logger.info("All dependencies satisfied for task %s", dependent_id)
                self._conn.execute(
                    "UPDATE tasks SET dependency_status = 'satisfied' WHERE id = ?",
                    (dependent_id,),
                )
            else:
                logger.info("Task %s still has unsatisfied dependencies", dependent_id)
        self._conn.commit()

    def resolve_downstream_dependencies(self, completed_task_id: int | str) -> None:
        """Public, thread-safe wrapper: resolve dependents of a completed/cancelled task.

        Used by the server's PATCH endpoint when a task is cancelled, and by
        any other code path that needs to unblock downstream tasks without
        going through submit_result.
        """
        with self._lock:
            self._resolve_dependencies_unlocked(completed_task_id)

    def resolve_dependencies_for_task(self, task_id: int | str) -> str:
        """Recompute dependency_status for a specific task based on its deps' states.

        Used when a task is created AFTER its dependencies have already been
        satisfied (e.g., validate retry creates dev_retry after original_dev
        has already completed). Without this call, the new task stays in
        'blocked' state forever because _resolve_dependencies is only invoked
        on task completion, not on task creation.

        Terminal states: completed, cancelled, timeout, failed — all unblock dependents.

        Returns the new dependency_status ("satisfied" or "blocked").
        """
        import logging
        logger = logging.getLogger(__name__)
        with self._lock:
            unsatisfied = self._conn.execute(
                "SELECT 1 FROM task_dependencies td "
                "JOIN tasks t ON t.id = td.depends_on "
                "WHERE td.task_id = ? AND t.status NOT IN ('completed', 'cancelled', 'timeout', 'failed')",
                (task_id,),
            ).fetchone()
            dep_status = "blocked" if unsatisfied else "satisfied"
            self._conn.execute(
                "UPDATE tasks SET dependency_status = ? WHERE id = ?",
                (dep_status, task_id),
            )
            self._conn.commit()
            logger.info(
                "Manually resolved dependencies for task %s → %s",
                str(task_id)[:8], dep_status,
            )
            return dep_status

    def get_ready_tasks(self, task_type: Optional[str] = None) -> list[Task]:
        """Return pending tasks whose dependencies are all satisfied."""
        with self._lock:
            query = (
                "SELECT * FROM tasks "
                "WHERE status = 'pending' AND dependency_status = 'satisfied'"
            )
            params: list = []
            if task_type:
                query += " AND type = ?"
                params.append(task_type)
            query += " ORDER BY created_at ASC"
            rows = self._conn.execute(query, params).fetchall()
            tasks = []
            for row in rows:
                deps = [
                    d[0] for d in self._conn.execute(
                        "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                        (row["id"],),
                    ).fetchall()
                ]
                tasks.append(self._row_to_task(row, deps))
            return tasks

    def update_heartbeat(self, task_id: int | str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET last_heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
                (task_id,),
            )
            self._conn.commit()

    def get_stale_tasks(self, timeout_seconds: int = 120) -> list[Task]:
        """Return running tasks whose heartbeat exceeded timeout."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' "
                "AND last_heartbeat_at IS NOT NULL "
                "AND (julianday('now') - julianday(last_heartbeat_at)) * 86400 > ?",
                (timeout_seconds,),
            ).fetchall()
            tasks = []
            for row in rows:
                deps = [
                    d[0] for d in self._conn.execute(
                        "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                        (row["id"],),
                    ).fetchall()
                ]
                tasks.append(self._row_to_task(row, deps))
            return tasks

    # -- events -----------------------------------------------------------------

    def add_event(
        self,
        task_id: int | str,
        event_type: TaskEvent,
        data: Optional[dict] = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO task_events (task_id, type, data) VALUES (?, ?, ?)",
                (task_id, event_type.value, json.dumps(data or {})),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_events_since(
        self,
        task_id: int | str,
        event_id: int = 0,
    ) -> list[TaskEventModel]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_events "
                "WHERE task_id = ? AND id > ? ORDER BY id ASC",
                (task_id, event_id),
            ).fetchall()
            return [
                TaskEventModel(
                    id=r["id"],
                    task_id=r["task_id"],
                    type=r["type"],
                    data=json.loads(r["data"] or "{}"),
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def get_all_events_since(self, event_id: int = 0) -> list[TaskEventModel]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_events WHERE id > ? ORDER BY id ASC",
                (event_id,),
            ).fetchall()
            return [
                TaskEventModel(
                    id=r["id"],
                    task_id=r["task_id"],
                    type=r["type"],
                    data=json.loads(r["data"] or "{}"),
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # -- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()
