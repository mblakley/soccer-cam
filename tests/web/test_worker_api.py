"""Tests for the master-side worker coordination API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from video_grouper.utils.config import TTTConfig
from video_grouper.web.auth_server import create_app
from video_grouper.web.worker_api import enqueue_task


# auth_server's middleware requires same-origin Origin/Referer on every
# state-changing method; bake it into the test client so each POST/PUT
# is treated as a same-origin browser request.
_SAME_ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture
def client(tmp_path):
    app = create_app(TTTConfig(), str(tmp_path), node_role="master")
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        yield c


@pytest.fixture
def storage(tmp_path):
    return tmp_path


def test_worker_api_not_mounted_in_standalone_mode(tmp_path):
    """The worker API only ships with role=master, never standalone."""
    app = create_app(TTTConfig(), str(tmp_path))  # default role
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        resp = c.post("/api/work/register", json={"node_id": "x"})
    assert resp.status_code == 404


def test_register_returns_token(client):
    resp = client.post(
        "/api/work/register",
        json={"node_id": "worker1", "capabilities": ["combine", "trim"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_id"] == "worker1"
    assert body["token"]
    assert len(body["token"]) > 16  # not empty / not trivial


def test_register_is_idempotent(client):
    """Re-registration (worker restart) returns the same token."""
    r1 = client.post(
        "/api/work/register",
        json={"node_id": "worker1", "capabilities": ["combine"]},
    )
    r2 = client.post(
        "/api/work/register",
        json={"node_id": "worker1", "capabilities": ["combine"]},
    )
    assert r1.json()["token"] == r2.json()["token"]


def test_next_requires_bearer_token(client):
    resp = client.get("/api/work/next")
    assert resp.status_code == 401


def test_next_returns_204_when_queue_empty(client):
    token = client.post(
        "/api/work/register",
        json={"node_id": "w", "capabilities": ["combine"]},
    ).json()["token"]
    resp = client.get("/api/work/next", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 204


def test_full_lifecycle_register_claim_complete(client, storage):
    token = client.post(
        "/api/work/register",
        json={"node_id": "w", "capabilities": ["combine"]},
    ).json()["token"]

    task_id = enqueue_task(storage, "combine", {"group_dir": "2026.04.20-14.30.00"})

    headers = {"authorization": f"Bearer {token}"}
    next_resp = client.get("/api/work/next", headers=headers)
    assert next_resp.status_code == 200
    offered = next_resp.json()
    assert offered["task_id"] == task_id
    assert offered["task_type"] == "combine"

    hb = client.post(f"/api/work/{task_id}/heartbeat", headers=headers)
    assert hb.status_code == 200

    done = client.post(
        f"/api/work/{task_id}/complete",
        headers=headers,
        json={"outputs": {"combined": "combined.mp4"}},
    )
    assert done.status_code == 200

    # No more tasks queued.
    next_resp2 = client.get("/api/work/next", headers=headers)
    assert next_resp2.status_code == 204


def test_capabilities_filter(client, storage):
    """A worker only gets tasks matching its advertised capabilities."""
    token_combine = client.post(
        "/api/work/register",
        json={"node_id": "w-cpu", "capabilities": ["combine", "trim"]},
    ).json()["token"]
    token_gpu = client.post(
        "/api/work/register",
        json={"node_id": "w-gpu", "capabilities": ["ball_tracking"]},
    ).json()["token"]

    enqueue_task(storage, "ball_tracking", {})
    enqueue_task(storage, "combine", {})

    # CPU worker gets the combine task, not the GPU one.
    cpu_task = client.get(
        "/api/work/next", headers={"authorization": f"Bearer {token_combine}"}
    ).json()
    assert cpu_task["task_type"] == "combine"

    # GPU worker gets the ball_tracking task.
    gpu_task = client.get(
        "/api/work/next", headers={"authorization": f"Bearer {token_gpu}"}
    ).json()
    assert gpu_task["task_type"] == "ball_tracking"


def test_fail_with_retry_requeues(client, storage):
    token = client.post(
        "/api/work/register",
        json={"node_id": "w", "capabilities": ["combine"]},
    ).json()["token"]
    task_id = enqueue_task(storage, "combine", {})
    headers = {"authorization": f"Bearer {token}"}

    client.get("/api/work/next", headers=headers)  # claim
    fail = client.post(
        f"/api/work/{task_id}/fail",
        headers=headers,
        json={"error": "boom", "retry": True},
    )
    assert fail.status_code == 200
    assert fail.json()["status"] == "queued"

    # Same task offered again on next poll.
    again = client.get("/api/work/next", headers=headers).json()
    assert again["task_id"] == task_id


def test_complete_rejects_unowned_task(client, storage):
    """Worker A can't complete a task assigned to worker B."""
    token_a = client.post(
        "/api/work/register",
        json={"node_id": "a", "capabilities": ["combine"]},
    ).json()["token"]
    token_b = client.post(
        "/api/work/register",
        json={"node_id": "b", "capabilities": ["combine"]},
    ).json()["token"]
    task_id = enqueue_task(storage, "combine", {})

    # A claims it.
    client.get("/api/work/next", headers={"authorization": f"Bearer {token_a}"})

    # B tries to complete it -> 404.
    resp = client.post(
        f"/api/work/{task_id}/complete",
        headers={"authorization": f"Bearer {token_b}"},
        json={"outputs": {}},
    )
    assert resp.status_code == 404
