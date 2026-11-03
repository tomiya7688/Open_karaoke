import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from open_karaoke_analysis.adapters import MockAdapter
from open_karaoke_analysis.model_packages import MAX_MANIFEST_BYTES, discover_packages
from open_karaoke_analysis.service import create_app

TOKEN = "package-test-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ADAPTERS = {"mock-v1": MockAdapter()}


def manifest(**changes):
    value = {
        "manifest_version": 1,
        "id": "demo",
        "name": "Demo",
        "role": "mock",
        "backend": "mock-v1",
        "version": "1",
        "languages": ["ja"],
        "license": "MIT",
        "capabilities": {"timestamps": True},
    }
    value.update(changes)
    return value


def put(root, folder, content, raw=False):
    directory = root / "models" / folder
    directory.mkdir(parents=True, exist_ok=True)
    text = content if raw else json.dumps(content)
    (directory / "manifest.json").write_text(text, encoding="utf-8")


def by_id(entries):
    return {e.id: e for e in entries}


def test_valid_package_is_available_and_weight_only_folders_are_ignored(tmp_path):
    put(tmp_path, "demo", manifest())
    (tmp_path / "models" / "whisper").mkdir()
    entries = discover_packages(tmp_path, ADAPTERS)
    assert [e.id for e in entries] == ["demo"]
    entry = entries[0]
    assert entry.status == "available"
    assert entry.origin == "user_supplied"
    assert entry.error is None
    assert entry.license == "MIT"
    assert entry.capabilities == {"timestamps": True}


def test_missing_models_directory_is_empty(tmp_path):
    assert discover_packages(tmp_path, ADAPTERS) == []


def test_invalid_manifests_never_stop_discovery(tmp_path):
    put(tmp_path, "good", manifest(id="good"))
    put(tmp_path, "badjson", "{not json", raw=True)
    put(tmp_path, "nan", '{"manifest_version": 1, "x": NaN}', raw=True)
    put(tmp_path, "missing", {"manifest_version": 1, "id": "missing"})
    put(tmp_path, "badid", manifest(id="Bad Id"))
    put(tmp_path, "wrongfolder", manifest(id="other"))
    put(tmp_path, "list", "[1]", raw=True)
    put(tmp_path, "huge", "x" * (MAX_MANIFEST_BYTES + 1), raw=True)
    (tmp_path / "models" / "binary").mkdir()
    (tmp_path / "models" / "binary" / "manifest.json").write_bytes(b"\xff\xfe\x00")
    entries = by_id(discover_packages(tmp_path, ADAPTERS))
    assert entries["good"].status == "available"
    for name in ("badjson", "nan", "missing", "badid", "wrongfolder", "list", "huge", "binary"):
        assert entries[name].status == "invalid", name
        assert entries[name].error
        assert str(tmp_path) not in entries[name].error


def test_deeply_nested_manifest_does_not_fail_the_whole_listing(tmp_path):
    put(tmp_path, "good", manifest(id="good"))
    put(tmp_path, "deep", "[" * 20000 + "]" * 20000, raw=True)
    entries = by_id(discover_packages(tmp_path, ADAPTERS))
    assert entries["good"].status == "available"
    assert entries["deep"].status == "invalid"


def test_future_manifest_version_is_reported_as_unsupported(tmp_path):
    put(tmp_path, "future", manifest(id="future", manifest_version=2))
    entry = discover_packages(tmp_path, ADAPTERS)[0]
    assert entry.status == "invalid"
    assert entry.error == "manifest_version is not supported"


def test_backend_and_role_must_match_an_installed_adapter(tmp_path):
    put(tmp_path, "nobackend", manifest(id="nobackend", backend="unknown-model"))
    put(tmp_path, "wrongrole", manifest(id="wrongrole", role="pitch"))
    entries = by_id(discover_packages(tmp_path, ADAPTERS))
    assert entries["nobackend"].status == "unsupported_backend"
    assert entries["wrongrole"].status == "role_mismatch"


def test_builtin_id_collision_is_a_duplicate(tmp_path):
    put(tmp_path, "mock-v1", manifest(id="mock-v1"))
    assert discover_packages(tmp_path, ADAPTERS)[0].status == "duplicate"


def test_manifest_read_is_bounded_even_if_stat_lies(tmp_path, monkeypatch):
    put(tmp_path, "big", "x" * (MAX_MANIFEST_BYTES * 4), raw=True)
    original = Path.stat

    def small_stat(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.name == "manifest.json":
            return os.stat_result((result.st_mode, *result[1:6], 10, *result[7:]))
        return result

    monkeypatch.setattr(Path, "stat", small_stat)
    entry = discover_packages(tmp_path, ADAPTERS)[0]
    assert entry.status == "invalid" and "64 KiB" in entry.error


def test_long_folder_names_are_shortened_in_the_listing(tmp_path):
    put(tmp_path, "x" * 200, "{", raw=True)
    entry = discover_packages(tmp_path, ADAPTERS)[0]
    assert entry.status == "invalid"
    assert len(entry.id) == 64


def test_symlinked_manifest_is_rejected(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(manifest(id="linked")), encoding="utf-8")
    directory = tmp_path / "models" / "linked"
    directory.mkdir(parents=True)
    try:
        (directory / "manifest.json").symlink_to(target)
    except OSError:
        return  # Windows without symlink privilege cannot create the fixture.
    entry = discover_packages(tmp_path, ADAPTERS)[0]
    assert entry.status == "invalid"


def test_package_directory_count_is_bounded(tmp_path):
    for index in range(300):
        put(tmp_path, f"p{index:03}", manifest(id=f"p{index:03}"))
    assert len(discover_packages(tmp_path, ADAPTERS)) == 256


def test_service_lists_packages_and_requires_the_session_token(tmp_path):
    put(tmp_path, "demo", manifest(backend="mock-v1"))
    put(tmp_path, "broken", "{", raw=True)
    app = create_app(tmp_path, TOKEN)
    with TestClient(app) as anonymous:
        assert anonymous.get("/model-packages").status_code == 401
    with TestClient(app, headers=HEADERS) as client:
        response = client.get("/model-packages")
        assert response.status_code == 200
        packages = {p["id"]: p for p in response.json()["packages"]}
        assert packages["demo"]["status"] == "available"
        assert packages["broken"]["status"] == "invalid"
        assert client.get("/health").status_code == 200
