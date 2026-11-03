import json
import time

from fastapi.testclient import TestClient

from open_karaoke_analysis.adapters import MockAdapter
from open_karaoke_analysis.contracts import ModelInfo, Role
from open_karaoke_analysis.service import create_app

TOKEN = "selection-job-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
FAST = {"steps": 1, "delay_ms": 0}


class SecondMock(MockAdapter):
    """
    {
      責務: [SecondMock: 既定とは別IDの同roleテスト用Adapter]
    }
    """

    info = ModelInfo(id="mock-second", version="1", role=Role.MOCK, capabilities=["fixture"])


def write_package(root, folder="demo", **changes):
    value = {
        "manifest_version": 1,
        "id": folder,
        "name": "Demo",
        "role": "mock",
        "backend": "mock-second",
        "version": "1",
    }
    value.update(changes)
    directory = root / "models" / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(value), encoding="utf-8")


def write_selection(root, selection):
    (root / "model_selection.json").write_text(
        json.dumps({"version": 1, "selection": selection}), encoding="utf-8"
    )


def run_job(root, body=None):
    app = create_app(root, TOKEN, [MockAdapter(), SecondMock()])
    with TestClient(app, headers=HEADERS) as client:
        response = client.post("/analysis/mock", json=body or {"options": FAST})
        assert response.status_code == 202, response.text
        accepted = response.json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{accepted['id']}").json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                return accepted, job
            time.sleep(0.005)
    raise AssertionError("job did not finish")


def test_no_selection_uses_default(tmp_path):
    accepted, job = run_job(tmp_path)
    for snapshot in (accepted, job):
        assert snapshot["model_id"] == "mock-v1"
        assert snapshot["model_source"] == "default"
        assert snapshot["selected_model_id"] is None
        assert snapshot["selection_fallback_reason"] is None
    assert job["status"] == "completed"


def test_builtin_selection_is_applied(tmp_path):
    write_selection(tmp_path, {"mock": "mock-second"})
    accepted, job = run_job(tmp_path)
    assert accepted["model_id"] == job["model_id"] == "mock-second"
    assert job["model_source"] == "selection"
    assert job["selected_model_id"] == "mock-second"


def test_package_selection_maps_to_manifest_backend(tmp_path):
    write_package(tmp_path)
    write_selection(tmp_path, {"mock": "demo"})
    accepted, job = run_job(tmp_path)
    assert accepted["model_id"] == job["model_id"] == "mock-second"
    assert job["model_source"] == "selection"
    assert job["selected_model_id"] == "demo"
    assert job["status"] == "completed"


def test_explicit_model_wins_over_selection(tmp_path):
    write_selection(tmp_path, {"mock": "mock-second"})
    _accepted, job = run_job(tmp_path, {"model_id": "mock-v1", "options": FAST})
    assert job["model_id"] == "mock-v1"
    assert job["model_source"] == "request"
    assert job["selected_model_id"] is None


def test_explicit_model_ignores_even_broken_selection(tmp_path):
    (tmp_path / "model_selection.json").write_bytes(b"not json")
    _accepted, job = run_job(tmp_path, {"model_id": "mock-second", "options": FAST})
    assert (job["model_id"], job["model_source"]) == ("mock-second", "request")


def test_unavailable_or_stale_choice_falls_back_and_is_reported(tmp_path):
    write_package(tmp_path, "badbackend", backend="nope")
    for stored in ("ghost", "badbackend", "demo-gone"):
        write_selection(tmp_path, {"mock": stored})
        _accepted, job = run_job(tmp_path)
        assert job["model_id"] == "mock-v1"
        assert job["model_source"] == "default_fallback"
        assert job["selected_model_id"] == stored
        assert job["selection_fallback_reason"]
        assert str(tmp_path) not in job["selection_fallback_reason"]
        assert job["status"] == "completed"


def test_wrong_role_and_hostile_values_fall_back_or_are_ignored(tmp_path):
    write_package(tmp_path, "stemsy", role="stems", backend="mock-second")
    write_selection(tmp_path, {"mock": "stemsy"})
    _accepted, job = run_job(tmp_path)
    assert (job["model_id"], job["model_source"]) == ("mock-v1", "default_fallback")
    write_selection(tmp_path, {"mock": "../../x"})
    _accepted, job = run_job(tmp_path)
    assert (job["model_id"], job["model_source"], job["selected_model_id"]) == (
        "mock-v1",
        "default",
        None,
    )


def test_selection_for_another_role_is_not_applied(tmp_path):
    write_selection(tmp_path, {"stems": "mock-second"})
    _accepted, job = run_job(tmp_path)
    assert (job["model_id"], job["model_source"]) == ("mock-v1", "default")


def test_unreadable_models_directory_falls_back(tmp_path):
    (tmp_path / "models").write_text("not a directory", encoding="utf-8")
    write_selection(tmp_path, {"mock": "demo"})
    _accepted, job = run_job(tmp_path)
    assert (job["model_id"], job["model_source"]) == ("mock-v1", "default_fallback")


def test_package_code_is_never_executed(tmp_path):
    write_package(tmp_path)
    marker = tmp_path / "executed.txt"
    (tmp_path / "models" / "demo" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')", encoding="utf-8"
    )
    write_selection(tmp_path, {"mock": "demo"})
    _accepted, job = run_job(tmp_path)
    assert job["selected_model_id"] == "demo"
    assert not marker.exists()
