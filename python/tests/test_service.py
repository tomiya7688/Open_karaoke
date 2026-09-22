import json
import threading
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from open_karaoke_analysis.adapters import AdapterRegistry, MockAdapter, resolve_artifact
from open_karaoke_analysis.contracts import ModelInfo, Role, ServiceError
from open_karaoke_analysis.service import create_app

TOKEN = "test-session-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN), headers=HEADERS) as session:
        yield session


def terminal(client, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.005)
    pytest.fail("Job did not finish")


def submit(client, options=None):
    response = client.post("/analysis/mock", json={"options": options or {}})
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    return response.json()["id"]


def test_health_and_models(client):
    health = client.get("/health").json()
    assert health["protocol_version"] == 1
    assert health["status"] == "ok"
    assert health["pid"] > 0
    assert client.get("/models").json()["models"][0]["role"] == "mock"
    assert TOKEN not in json.dumps(health)


def test_authentication_and_browser_origin(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN)) as session:
        assert session.get("/health").status_code == 401
        assert session.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert (
            session.get(
                "/health", headers={**HEADERS, "Origin": "https://example.test"}
            ).status_code
            == 403
        )


def test_result_is_written_and_deterministic(client, tmp_path):
    a = terminal(client, submit(client))
    b = terminal(client, submit(client))
    assert a["status"] == b["status"] == "completed"
    assert a["progress"] == 1
    assert a["error"] is None
    assert a["started_at"] and a["finished_at"]
    first = tmp_path / a["artifacts"][0]
    assert first.read_bytes() == (tmp_path / b["artifacts"][0]).read_bytes()
    assert json.loads(first.read_text())["mock"] is True
    assert a["artifacts"] != b["artifacts"]


def test_cancel_running_and_idempotent_terminal(client, tmp_path):
    job_id = submit(client, {"steps": 100, "delay_ms": 30})
    time.sleep(0.03)
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 200
    result = terminal(client, job_id)
    assert result["status"] == "cancelled"
    assert result["artifacts"] == []
    assert not list(tmp_path.rglob("result.json"))
    assert client.post(f"/jobs/{job_id}/cancel").json() == result


def test_cancel_queued(client):
    first = submit(client, {"steps": 100, "delay_ms": 10})
    second = submit(client)
    client.post(f"/jobs/{second}/cancel")
    client.post(f"/jobs/{first}/cancel")
    assert terminal(client, second)["status"] == "cancelled"


def test_failure_and_unknown_id(client):
    failed = terminal(client, submit(client, {"fail": True}))
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "mock_failure"
    assert failed["artifacts"] == []
    for suffix in ["", "/cancel"]:
        response = client.request("POST" if suffix else "GET", f"/jobs/{uuid4()}{suffix}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"


@pytest.mark.parametrize("role", ["pitch", "notes", "song"])
def test_unimplemented_roles_never_fake_success(client, role):
    response = client.post(f"/analysis/{role}", json={})
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


@pytest.mark.parametrize(
    "value",
    [
        "../secret.wav",
        "/secret.wav",
        "C:/secret.wav",
        "a/../../x",
        "a\\b",
        "a//b",
        "a/./b",
        "",
        "file:stream",
    ],
)
def test_paths_rejected(client, value):
    response = client.post("/analysis/mock", json={"input_artifact": value})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_path"


def test_existing_input_and_symlink(client, tmp_path):
    (tmp_path / "input.wav").write_bytes(b"fixture")
    assert client.post("/analysis/mock", json={"input_artifact": "input.wav"}).status_code == 202
    assert client.post("/analysis/mock", json={"input_artifact": "missing.wav"}).status_code == 404
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "input.wav")
    except OSError:
        pytest.skip("Symlinks require permission on this host")
    with pytest.raises(ServiceError):
        resolve_artifact(tmp_path, "link", must_exist=True)


def test_structured_validation_errors(client):
    assert client.post("/analysis/mock", json={"unknown": 1}).status_code == 422
    assert client.post("/analysis/mock", json={"options": {"steps": -1}}).status_code == 400
    assert client.post("/analysis/mock", json={"model_id": "missing"}).status_code == 404
    assert client.post("/analysis/lyrics", json={"model_id": "mock-v1"}).status_code == 400
    assert client.get("/jobs/not-a-uuid").status_code == 422
    malformed = client.post(
        "/analysis/mock", content="{", headers={"Content-Type": "application/json"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert client.post("/analysis/mock", content="x" * 65537).status_code == 413


def test_swappable_adapter_and_failure_cleanup(tmp_path):
    class Replacement(MockAdapter):
        info = ModelInfo(id="replacement", version="1", role=Role.MOCK)

        def analyze(self, context, options):
            context.write_json("partial.json", {"value": 42})
            raise RuntimeError("private-details-must-not-leak")

    with TestClient(create_app(tmp_path, TOKEN, [Replacement()]), headers=HEADERS) as session:
        assert session.get("/models").json()["models"][0]["id"] == "replacement"
        result = terminal(session, submit(session))
        assert result["error"]["code"] == "adapter_failure"
        assert "private-details" not in json.dumps(result)
        assert not list(tmp_path.rglob("partial.json"))
    with pytest.raises(ValueError):
        AdapterRegistry([MockAdapter(), MockAdapter()])


def test_queue_is_bounded(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN, capacity=1), headers=HEADERS) as session:
        job_id = submit(session, {"steps": 100, "delay_ms": 30})
        assert session.post("/analysis/mock", json={}).status_code == 429
        session.post(f"/jobs/{job_id}/cancel")
        terminal(session, job_id)
        assert terminal(session, submit(session))["status"] == "completed"


def test_shutdown_cancels_worker_and_stops_admission(tmp_path):
    started = threading.Event()
    stopped = threading.Event()

    class Cancellable(MockAdapter):
        def analyze(self, context, options):
            try:
                started.set()
                assert context.cancelled.wait(10), "Shutdown did not signal cancellation"
                context.checkpoint()
            finally:
                stopped.set()
            return []

    app = create_app(tmp_path, TOKEN, [Cancellable()])
    with TestClient(app, headers=HEADERS) as session:
        job_id = submit(session)
        deadline = time.monotonic() + 10
        while not started.is_set():
            response = session.get(f"/jobs/{job_id}")
            assert response.status_code == 200, response.text
            snapshot = response.json()
            assert snapshot["status"] in {"queued", "running"}, snapshot
            assert time.monotonic() < deadline, f"Worker did not start: {snapshot}"
            time.sleep(0.01)
        assert session.post("/shutdown").status_code == 200
        assert session.post("/analysis/mock", json={}).status_code == 503
    assert started.is_set() and stopped.is_set()
    assert app.state.jobs.get(UUID(job_id)).job.status == "cancelled"
