"""End-to-end integration tests for the coordinator."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coordinator.db import TaskDB
from coordinator.events import TaskEventBroadcaster
from coordinator.metrics import MetricsCollector


@pytest.fixture
def tmp_db() -> TaskDB:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = TaskDB(Path(path))
    yield db
    db.close()
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def client(tmp_db: TaskDB, tmp_path: Path) -> TestClient:
    """Create TestClient with test DB injected, lifespan disabled."""
    import coordinator.server as srv

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield

    test_app = FastAPI(title="Test Coordinator", lifespan=_noop_lifespan)
    test_app.router.routes = list(srv.app.routes)

    srv.db = tmp_db
    srv.broadcaster = TaskEventBroadcaster()
    srv.metrics = MetricsCollector()
    srv.cfg = {
        "port": 9100,
        "db_path": str(tmp_db.db_path),
        "heartbeat_interval": 60,
        "stale_timeout": 120,
        "artifact_dir": str(artifact_dir),
        "max_retries": 0,
        "retry_delay": 30,
    }

    return TestClient(test_app)


def _claim_and_complete(c: TestClient, task_id: str, agent_id: str, artifacts: dict) -> None:
    """Helper: claim a task and submit completion."""
    c.post(f"/tasks/{task_id}/claim", json={"agent_id": agent_id})
    c.post(f"/tasks/{task_id}/result", json={"artifacts": artifacts})


def _resolve_deps(c: TestClient, task_id: str) -> None:
    """Resolve dependencies without waiting for background monitor."""
    from coordinator.server import db as srv_db
    if srv_db:
        srv_db._resolve_dependencies(task_id)


@pytest.mark.integration
def test_full_chain(client: TestClient) -> None:
    """Create design → dev → validate → deploy chain, complete sequentially."""
    # 1. Create design task (no deps)
    design = client.post("/tasks", json={
        "type": "design", "title": "Design UMS", "description": "Design user management system",
    }).json()
    assert design["status"] == "pending"
    assert design["dependency_status"] == "satisfied"

    # 2. Create dev task (depends on design)
    dev = client.post("/tasks", json={
        "type": "dev", "title": "Dev UMS", "description": "Implement UMS",
        "depends_on": [design["id"]],
    }).json()
    assert dev["dependency_status"] == "blocked"

    # 3. Create validate task (depends on dev)
    validate = client.post("/tasks", json={
        "type": "validate", "title": "Validate UMS", "description": "Review UMS",
        "depends_on": [dev["id"]],
    }).json()
    assert validate["dependency_status"] == "blocked"

    # 4. Create deploy task (depends on validate)
    deploy = client.post("/tasks", json={
        "type": "deploy", "title": "Deploy UMS", "description": "Release UMS",
        "depends_on": [validate["id"]],
    }).json()
    assert deploy["dependency_status"] == "blocked"

    # 5. Complete design
    _claim_and_complete(client, design["id"], "design-1", {"prd.md": "design/prd.md"})
    _resolve_deps(client, design["id"])

    # 6. dev should now be ready
    tasks = client.get("/tasks?status=pending").json()
    ready_ids = [t["id"] for t in tasks if t.get("dependency_status") == "satisfied"]
    assert dev["id"] in ready_ids, f"dev task not ready. Pending tasks: {tasks}"

    # 7. Complete dev
    _claim_and_complete(client, dev["id"], "dev-1", {"src/": "code/"})
    _resolve_deps(client, dev["id"])

    # 8. validate should now be ready
    tasks = client.get("/tasks?status=pending").json()
    ready_ids = [t["id"] for t in tasks if t.get("dependency_status") == "satisfied"]
    assert validate["id"] in ready_ids

    # 9. Complete validate
    _claim_and_complete(client, validate["id"], "val-1", {"review.md": "review/"})
    _resolve_deps(client, validate["id"])

    # 10. deploy should now be ready
    tasks = client.get("/tasks?status=pending").json()
    ready_ids = [t["id"] for t in tasks if t.get("dependency_status") == "satisfied"]
    assert deploy["id"] in ready_ids

    # 11. Complete deploy
    _claim_and_complete(client, deploy["id"], "deploy-1", {"url": "https://ums.example.com"})

    # 12. Final state check
    deploy_final = client.get(f"/tasks/{deploy['id']}").json()
    assert deploy_final["status"] == "completed"


@pytest.mark.integration
def test_task_failure_keeps_deps_blocked(client: TestClient) -> None:
    """When a task fails, dependent tasks remain blocked."""
    design = client.post("/tasks", json={
        "type": "design", "title": "Design", "description": "Design the system",
    }).json()

    dev = client.post("/tasks", json={
        "type": "dev", "title": "Dev", "description": "Dev the system",
        "depends_on": [design["id"]],
    }).json()
    assert dev["dependency_status"] == "blocked"

    # Fail design
    client.post(f"/tasks/{design['id']}/claim", json={"agent_id": "design-1"})
    client.post(f"/tasks/{design['id']}/result", json={
        "error": "Design failed: timeout",
    })

    # dev should still be blocked
    dev_check = client.get(f"/tasks/{dev['id']}").json()
    assert dev_check["dependency_status"] == "blocked"


@pytest.mark.integration
def test_cancel_task(client: TestClient) -> None:
    """Cancel a pending task."""
    task = client.post("/tasks", json={
        "type": "design", "title": "To cancel", "description": "Please cancel this task",
    }).json()

    resp = client.patch(f"/tasks/{task['id']}", json={"status": "cancelled"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
