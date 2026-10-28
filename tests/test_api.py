import time

import pytest
from fastapi.testclient import TestClient

from sc2_harness.app import create_app
from sc2_harness.settings import Settings


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(tmp_path)), base_url="http://localhost") as value:
        yield value


def test_health_has_no_keys_and_lists_explicit_modes(tmp_path):
    settings = Settings(tmp_path, model_base_url="http://127.0.0.1:8000/v1", model_name="test",
                        model_api_key="never-public")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert "never-public" not in response.text
        assert {b["id"] for b in response.json()["capabilities"]["backends"]} == {"sc2", "sandbox"}


def test_api_run_evidence_and_frame_identity(client):
    response = client.post("/api/runs", json={"backend": "sandbox", "max_rounds": 3})
    assert response.status_code == 201
    run_id = response.json()["id"]
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"completed", "failed"}:
            break
        time.sleep(.03)
    assert run["status"] == "completed"
    assert client.get(f"/api/runs/{run_id}/frame").status_code == 404
    events = client.get(f"/api/runs/{run_id}/events").json()
    assert events["events"]
    assert client.get(f"/api/runs/{run_id}/events?after={events['cursor']}").json()["events"] == []
    artifacts = client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
    assert {a["name"] for a in artifacts} >= {"run.json", "events.jsonl", "metrics.json"}
    assert client.get(f"/api/runs/{run_id}/artifacts/.env").status_code == 404
    assert client.get(f"/api/runs/{run_id}/artifacts/..%5C.env").status_code == 404


def test_cross_origin_control_and_unvalidated_configs_rejected(client):
    assert client.post("/api/runs", json={"backend": "sandbox"},
                       headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.post("/api/runs", json={"map_name": "../../sensitive"}).status_code == 422
    assert client.get("/api/runs/missing").status_code == 404


def test_websocket_gets_authoritative_state(client):
    response = client.post("/api/runs", json={"backend": "sandbox", "max_rounds": 10})
    run_id = response.json()["id"]
    with client.websocket_connect(f"/api/runs/{run_id}/stream") as socket:
        value = socket.receive_json()
        assert value["type"] == "state" and value["data"]["id"] == run_id
