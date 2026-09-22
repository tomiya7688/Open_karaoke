import json
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from test_alignment import FixtureBackend

from open_karaoke_analysis.alignment import AlignmentAdapter
from open_karaoke_analysis.service import create_app

TOKEN = "alignment-test-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def wait(session, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = session.get(f"/jobs/{identifier}").json()
        if job["status"] in {"completed", "cancelled", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Alignment job did not finish")


def inputs(root):
    sf.write(root / "vocals.wav", np.ones(48000) * 0.1, 48000, subtype="FLOAT")
    lyrics = {"format_version": 1, "analysis_version": 1, "segments": [
        {"id": "s", "text": "あい", "start_sample": 0, "end_sample": 48000}]}
    (root / "lyrics.json").write_text(json.dumps(lyrics), encoding="utf-8")
    return {"input_artifact": "vocals.wav", "options": {"lyrics_artifact": "lyrics.json"}}


def test_default_registration_and_no_startup_model_import(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN), headers=HEADERS) as session:
        models = session.get("/models").json()["models"]
        assert any(model["id"] == "wav2vec2-ja-alignment" for model in models)
        assert session.post("/analysis/alignment", json={}).status_code == 400


def test_api_results_and_isolated_output(tmp_path):
    body = inputs(tmp_path)
    with TestClient(create_app(tmp_path, TOKEN, [AlignmentAdapter(FixtureBackend)]),
                    headers=HEADERS) as session:
        response = session.post("/analysis/alignment", json=body)
        assert response.status_code == 202
        job = wait(session, response.json()["id"])
        assert job["status"] == "completed", job
        assert len(job["artifacts"]) == 3
        assert all(path.startswith("jobs/") and (tmp_path / path).is_file() for path in job["artifacts"])
        report = json.loads((tmp_path / job["artifacts"][1]).read_text(encoding="utf-8"))
        assert report["aligned_units"] == 2
        assert (tmp_path / "lyrics.json").is_file()


def test_cancel_worker_cleanup(tmp_path):
    body = inputs(tmp_path)
    class Slow(FixtureBackend):
        def prepare(self, context, options):
            context.write_json("partial.json", {"test": True})
            context.cancelled.wait(4)
            context.checkpoint()
    with TestClient(create_app(tmp_path, TOKEN, [AlignmentAdapter(Slow)]), headers=HEADERS) as session:
        identifier = session.post("/analysis/alignment", json=body).json()["id"]
        time.sleep(0.03)
        session.post(f"/jobs/{identifier}/cancel")
        assert wait(session, identifier)["status"] == "cancelled"
    assert not list((tmp_path / "jobs").rglob("*.json"))


def test_failure_is_structured_and_cleans_outputs(tmp_path):
    body = inputs(tmp_path)
    class Broken(FixtureBackend):
        def emissions(self, samples):
            raise RuntimeError("private_details")
    with TestClient(create_app(tmp_path, TOKEN, [AlignmentAdapter(Broken)]), headers=HEADERS) as session:
        identifier = session.post("/analysis/alignment", json=body).json()["id"]
        job = wait(session, identifier)
        assert job["status"] == "failed" and job["artifacts"] == []
        assert job["error"]["code"] == "adapter_failure"
        assert "private_details" not in json.dumps(job)
    assert not list((tmp_path / "jobs").rglob("*.json"))
