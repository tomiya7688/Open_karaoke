import json

from fastapi.testclient import TestClient

from open_karaoke_analysis.adapters import MockAdapter
from open_karaoke_analysis.contracts import Role
from open_karaoke_analysis.model_selection import (
    MAX_SELECTION_BYTES,
    SELECTION_NAME,
    read_selection,
    set_selection,
)
from open_karaoke_analysis.service import create_app

TOKEN = "selection-test-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ADAPTERS = {"mock-v1": MockAdapter()}


def package(root, folder="demo", **changes):
    value = {
        "manifest_version": 1,
        "id": folder,
        "name": "Demo",
        "role": "mock",
        "backend": "mock-v1",
        "version": "1",
    }
    value.update(changes)
    directory = root / "models" / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(value), encoding="utf-8")


def test_select_package_and_builtin_then_clear(tmp_path):
    package(tmp_path)
    assert read_selection(tmp_path, ADAPTERS) == {}
    assert set_selection(tmp_path, ADAPTERS, Role.MOCK, "demo") == {"mock": "demo"}
    assert read_selection(tmp_path, ADAPTERS) == {"mock": "demo"}
    assert set_selection(tmp_path, ADAPTERS, Role.MOCK, "mock-v1") == {"mock": "mock-v1"}
    assert set_selection(tmp_path, ADAPTERS, Role.MOCK, None) == {}
    assert not list(tmp_path.glob(".model_selection.*"))


def test_rejects_unknown_unavailable_and_wrong_role(tmp_path):
    package(tmp_path, "badbackend", backend="nope")
    package(tmp_path, "other", role="stems")
    for model_id in ("ghost", "badbackend", "../x", "other"):
        try:
            set_selection(tmp_path, ADAPTERS, Role.MOCK, model_id)
        except Exception as error:
            assert error.detail.code == "model_not_selectable"
        else:
            raise AssertionError(model_id)
    assert not (tmp_path / SELECTION_NAME).exists()


def test_stale_and_hostile_stored_values_are_ignored_but_kept(tmp_path):
    package(tmp_path)
    (tmp_path / SELECTION_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "selection": {
                    "mock": "demo",
                    "stems": "gone",
                    "lyrics": "../../etc/passwd",
                    "bogus": "demo",
                    "pitch": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    assert read_selection(tmp_path, ADAPTERS) == {"mock": "demo"}
    set_selection(tmp_path, ADAPTERS, Role.MOCK, "mock-v1")
    stored = json.loads((tmp_path / SELECTION_NAME).read_text(encoding="utf-8"))
    assert stored["selection"] == {"mock": "mock-v1", "stems": "gone"}


def test_corrupt_oversized_and_symlinked_files_read_as_empty(tmp_path):
    path = tmp_path / SELECTION_NAME
    for content in (
        b"not json",
        b"\xff\xfe",
        b'{"version":2,"selection":{"mock":"mock-v1"}}',
        b'{"version":1,"selection":[]}',
        b'{"version":1,"selection":{"mock":NaN}}',
        b'{"version":1,"selection":{"mock":"mock-v1"},"pad":"' + b"x" * MAX_SELECTION_BYTES + b'"}',
    ):
        path.write_bytes(content)
        assert read_selection(tmp_path, ADAPTERS) == {}
    path.write_text('{"version":1,"selection":{"mock":"mock-v1"}}', encoding="utf-8")
    assert read_selection(tmp_path, ADAPTERS) == {"mock": "mock-v1"}


def test_endpoints_require_token_and_validate(tmp_path):
    package(tmp_path)
    app = create_app(tmp_path, TOKEN, [MockAdapter()])
    with TestClient(app) as client:
        assert client.get("/model-selection").status_code == 401
        assert client.post("/model-selection", json={"role": "mock"}).status_code == 401
        assert client.get("/model-selection", headers=HEADERS).json() == {"selection": {}}
        ok = client.post(
            "/model-selection", headers=HEADERS, json={"role": "mock", "model_id": "demo"}
        )
        assert ok.status_code == 200 and ok.json() == {"selection": {"mock": "demo"}}
        assert client.get("/model-selection", headers=HEADERS).json() == {
            "selection": {"mock": "demo"}
        }
        bad = client.post(
            "/model-selection", headers=HEADERS, json={"role": "mock", "model_id": "ghost"}
        )
        assert bad.status_code == 409 and bad.json()["error"]["code"] == "model_not_selectable"
        for body in (
            {"role": "nope"},
            {"role": "mock", "extra": 1},
            {"role": "mock", "model_id": ""},
        ):
            response = client.post("/model-selection", headers=HEADERS, json=body)
            assert response.status_code == 422
        cleared = client.post("/model-selection", headers=HEADERS, json={"role": "mock"})
        assert cleared.json() == {"selection": {}}
        browser = client.get("/model-selection", headers={**HEADERS, "Origin": "http://x"})
        assert browser.status_code == 403
