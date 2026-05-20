"""HTTP endpoint tests for the coordinator FastAPI server."""

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

    # Save original globals to restore after test
    original_db = srv.db
    original_broadcaster = srv.broadcaster
    original_metrics = srv.metrics
    original_cfg = srv.cfg

    # Inject test values
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

    # Use the live app directly — do NOT copy/mutate routes.
    # The global srv.app references route functions that read the
    # module-level globals (db, broadcaster, cfg) we just swapped.
    client = TestClient(srv.app)

    yield client

    # Restore original globals
    srv.db = original_db
    srv.broadcaster = original_broadcaster
    srv.metrics = original_metrics
    srv.cfg = original_cfg


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_task(client: TestClient) -> None:
    resp = client.post("/tasks", json={
        "type": "design",
        "title": "Test design",
        "description": "Design a user management system",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["title"] == "Test design"
    assert data["type"] == "design"


def test_get_task(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "dev",
        "title": "Get test",
        "description": "Test get task endpoint",
    })
    task_id = create_resp.json()["id"]

    resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == task_id


def test_get_task_not_found(client: TestClient) -> None:
    resp = client.get("/tasks/nonexistent-id")
    assert resp.status_code == 404


def test_list_tasks(client: TestClient) -> None:
    client.post("/tasks", json={
        "type": "design", "title": "T1", "description": "Design one",
    })
    client.post("/tasks", json={
        "type": "dev", "title": "T2", "description": "Dev task one",
    })

    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = client.get("/tasks?type=design")
    assert len(resp.json()) == 1
    assert resp.json()[0]["type"] == "design"


def test_claim_task(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "design", "title": "Claim test", "description": "Design a system",
    })
    task_id = create_resp.json()["id"]

    resp = client.post(f"/tasks/{task_id}/claim", json={"agent_id": "agent-1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


def test_double_claim_returns_409(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "dev", "title": "Double claim", "description": "Dev task implementation",
    })
    task_id = create_resp.json()["id"]

    client.post(f"/tasks/{task_id}/claim", json={"agent_id": "agent-1"})
    resp = client.post(f"/tasks/{task_id}/claim", json={"agent_id": "agent-2"})
    assert resp.status_code == 409


def test_submit_result(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "validate", "title": "Submit test", "description": "Validate code changes",
    })
    task_id = create_resp.json()["id"]
    client.post(f"/tasks/{task_id}/claim", json={"agent_id": "agent-1"})

    resp = client.post(f"/tasks/{task_id}/result", json={
        "artifacts": {"review.md": "/path/to/review.md"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_submit_result_error(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "deploy", "title": "Error test", "description": "Deploy to production",
    })
    task_id = create_resp.json()["id"]
    client.post(f"/tasks/{task_id}/claim", json={"agent_id": "agent-1"})

    resp = client.post(f"/tasks/{task_id}/result", json={
        "error": "Deployment failed: connection refused",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


def test_heartbeat(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "design", "title": "Heartbeat test", "description": "Design a system",
    })
    task_id = create_resp.json()["id"]
    client.post(f"/tasks/{task_id}/claim", json={"agent_id": "agent-1"})

    resp = client.post(f"/tasks/{task_id}/heartbeat")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_update_task(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "design", "title": "Update test", "description": "Design a system",
    })
    task_id = create_resp.json()["id"]

    resp = client.patch(f"/tasks/{task_id}", json={"status": "cancelled"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_artifact_upload_download(client: TestClient) -> None:
    create_resp = client.post("/tasks", json={
        "type": "design", "title": "Artifact test", "description": "Design a system",
    })
    task_id = create_resp.json()["id"]

    resp = client.put(
        f"/tasks/{task_id}/artifacts/prd.md",
        content=b"# PRD\nUser management system",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    resp = client.get(f"/tasks/{task_id}/artifacts")
    assert resp.status_code == 200
    assert "prd.md" in resp.json()
    assert "User management system" in resp.json()["prd.md"]


def test_metrics(client: TestClient) -> None:
    client.post("/tasks", json={
        "type": "design", "title": "Metrics test", "description": "Design a system",
    })

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "hermes_coordinator" in resp.text or "task" in resp.text.lower()


def test_create_task_with_deps(client: TestClient) -> None:
    design = client.post("/tasks", json={
        "type": "design", "title": "Parent", "description": "Design a system",
    })
    design_id = design.json()["id"]

    resp = client.post("/tasks", json={
        "type": "dev",
        "title": "Child",
        "description": "Dev task implementation",
        "depends_on": [design_id],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["dependency_status"] == "blocked"
