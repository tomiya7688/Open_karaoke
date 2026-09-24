import json
import time
from threading import Event

from fastapi.testclient import TestClient
from test_lyrics import FixtureBackend, setup_audio

from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.lyrics import LyricsAdapter
from open_karaoke_analysis.service import create_app

TOKEN = "lyric-test-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def terminal(client, identifier):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{identifier}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Lyric job did not finish")


def test_default_registry_and_actionable_missing_input(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN), headers=HEADERS) as client:
        models = client.get("/models").json()["models"]
        assert any(m["id"] == "whisper-large-v3" for m in models)
        response = client.post("/analysis/lyrics", json={})
        assert response.status_code == 202
        job = terminal(client, response.json()["id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "input_required"


def test_lyric_api_returns_owned_artifacts(tmp_path):
    setup_audio(tmp_path)
    adapter = LyricsAdapter(FixtureBackend)
    with TestClient(create_app(tmp_path, TOKEN, [adapter]), headers=HEADERS) as client:
        response = client.post("/analysis/lyrics", json={"input_artifact": "vocals.wav"})
        assert response.status_code == 202
        job = terminal(client, response.json()["id"])
        assert job["status"] == "completed"
        assert len(job["artifacts"]) == 3
        for artifact in job["artifacts"]:
            assert artifact.startswith(f"jobs/{job['instance_id']}/{job['id']}/")
            assert (tmp_path / artifact).is_file()
        lyrics = json.loads((tmp_path / job["artifacts"][0]).read_text(encoding="utf-8"))
        assert lyrics["segments"][0]["text"] == "青い空"


def test_failure_cleans_partial_files(tmp_path):
    setup_audio(tmp_path)

    class Broken(FixtureBackend):
        def transcribe(self, audio, options):
            self.context.write_json("partial.json", {"partial": True})
            raise ServiceError("inference_failed", "Fixture failure", 500)

    app = create_app(tmp_path, TOKEN, [LyricsAdapter(Broken)])
    with TestClient(app, headers=HEADERS) as client:
        response = client.post("/analysis/lyrics", json={"input_artifact": "vocals.wav"})
        job = terminal(client, response.json()["id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "inference_failed"
        assert job["artifacts"] == []
        assert not list(tmp_path.rglob("partial.json"))


def test_cancel_during_inference_cleans_owned_directory(tmp_path):
    setup_audio(tmp_path)
    started = Event()

    class Cancellable(FixtureBackend):
        def transcribe(self, audio, options):
            self.context.write_json("partial.json", {})
            started.set()
            self.context.cancelled.wait(5)
            self.context.checkpoint()
            return super().transcribe(audio, options)

    adapter = LyricsAdapter(Cancellable)
    with TestClient(create_app(tmp_path, TOKEN, [adapter]), headers=HEADERS) as client:
        response = client.post("/analysis/lyrics", json={"input_artifact": "vocals.wav"})
        identifier = response.json()["id"]
        assert started.wait(5)
        client.post(f"/jobs/{identifier}/cancel")
        assert terminal(client, identifier)["status"] == "cancelled"
        assert not list(tmp_path.rglob("partial.json"))
