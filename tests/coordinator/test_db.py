"""SQLite CRUD tests for TaskDB."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from coordinator.db import TaskDB
from coordinator.models import TaskCreate, TaskEvent, TaskStatus, TaskType


@pytest.fixture
def tmp_db() -> TaskDB:
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    db = TaskDB(Path(path))
    yield db
    db.close()
    Path(path).unlink(missing_ok=True)


def test_create_and_get(tmp_db: TaskDB) -> None:
    task = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Test design",
            description="Design a thing",
        )
    )
    assert task.id is not None
    assert task.title == "Test design"
    assert task.status == TaskStatus.PENDING
    assert task.dependency_status == "satisfied"  # no deps

    got = tmp_db.get_task(task.id)
    assert got is not None
    assert got.id == task.id


def test_create_with_dependencies(tmp_db: TaskDB) -> None:
    t1 = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Design",
            description="Design first",
        )
    )
    t2 = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DEV,
            title="Dev",
            description="Dev second",
            depends_on=[str(t1.id)],
        )
    )
    assert t2.dependency_status == "blocked"
    got = tmp_db.get_task(t2.id)
    assert got is not None
    assert got.depends_on == [str(t1.id)]


def test_claim_task(tmp_db: TaskDB) -> None:
    t = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Claim me",
            description="To be claimed",
        )
    )
    claimed = tmp_db.claim_task(t.id, "agent-1")
    assert claimed is not None
    assert claimed.status == TaskStatus.RUNNING
    assert claimed.assigned_to == "agent-1"

    # Double claim fails
    double = tmp_db.claim_task(t.id, "agent-2")
    assert double is None


def test_submit_result(tmp_db: TaskDB) -> None:
    t = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Design task",
            description="Design something",
        )
    )
    tmp_db.claim_task(t.id, "agent-1")
    result = tmp_db.submit_result(
        t.id,
        artifacts={"prd": "/path/to/prd.md"},
    )
    assert result is not None
    assert result.status == TaskStatus.COMPLETED
    assert result.artifacts == {"prd": "/path/to/prd.md"}


def test_submit_result_failure(tmp_db: TaskDB) -> None:
    t = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DEV,
            title="Dev task",
            description="Code something",
        )
    )
    tmp_db.claim_task(t.id, "agent-1")
    result = tmp_db.submit_result(t.id, error="build failed")
    assert result is not None
    assert result.status == TaskStatus.FAILED
    assert result.error == "build failed"


def test_dependency_resolution(tmp_db: TaskDB) -> None:
    t1 = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Design",
            description="Design first",
        )
    )
    t2 = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DEV,
            title="Dev",
            description="Dev second",
            depends_on=[str(t1.id)],
        )
    )
    assert t2.dependency_status == "blocked"

    # Complete t1
    tmp_db.claim_task(t1.id, "agent-1")
    tmp_db.submit_result(t1.id, artifacts={"prd": "/prd.md"})

    # t2 should now be satisfied
    ready = tmp_db.get_ready_tasks()
    assert any(r.id == t2.id for r in ready)


def test_get_ready_tasks(tmp_db: TaskDB) -> None:
    t1 = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="No deps",
            description="Design task with no deps",
        )
    )
    t2 = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="With deps",
            description="Design task with deps",
            depends_on=[str(t1.id)],
        )
    )
    ready = tmp_db.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == t1.id


def test_list_tasks(tmp_db: TaskDB) -> None:
    tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Design 1",
            description="Design task one",
        )
    )
    tmp_db.create_task(
        TaskCreate(
            type=TaskType.DEV,
            title="Dev 1",
            description="Dev task one",
        )
    )
    all_tasks = tmp_db.list_tasks()
    assert len(all_tasks) == 2

    design = tmp_db.list_tasks(task_type="design")
    assert len(design) == 1
    assert design[0].type.value == "design"


def test_heartbeat(tmp_db: TaskDB) -> None:
    t = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Heartbeat",
            description="Test heartbeat",
        )
    )
    tmp_db.claim_task(t.id, "agent-1")
    import time
    time.sleep(0.1)
    tmp_db.update_heartbeat(t.id)
    got = tmp_db.get_task(t.id)
    assert got is not None
    assert got.last_heartbeat_at is not None


def test_events(tmp_db: TaskDB) -> None:
    t = tmp_db.create_task(
        TaskCreate(
            type=TaskType.DESIGN,
            title="Event test",
            description="Test events",
        )
    )
    eid = tmp_db.add_event(t.id, TaskEvent.CREATED, {"title": "Event test"})
    events = tmp_db.get_events_since(t.id, 0)
    assert len(events) == 1
    assert events[0].type == TaskEvent.CREATED

    all_events = tmp_db.get_all_events_since(0)
    assert len(all_events) == 1
    assert all_events[0].id == eid
