import json
import time

import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from test_stems import FixtureBackend, signal

from open_karaoke_analysis.service import create_app
from open_karaoke_analysis.stems import StemsAdapter

TOKEN = "stem-test-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def wait_terminal(session, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = session.get(f"/jobs/{job_id}").json()
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    pytest.fail("Stem job did not finish")


def test_stem_api_outputs_and_cache(tmp_path):
    sf.write(tmp_path / "input.wav", signal(48001), 48000, subtype="FLOAT")
    app = create_app(tmp_path, TOKEN, [StemsAdapter(FixtureBackend)])
    with TestClient(app, headers=HEADERS) as session:
        assert session.get("/models").json()["models"][0]["role"] == "stems"
        for hit in [False, True]:
            submitted = session.post("/analysis/stems", json={"input_artifact": "input.wav"})
            assert submitted.status_code == 202
            result = wait_terminal(session, submitted.json()["id"])
            assert result["status"] == "completed", result
            assert len(result["artifacts"]) == 3
            meta = json.loads((tmp_path / result["artifacts"][-1]).read_text())
            assert meta["cache_hit"] == hit
            assert meta["duration_samples"] == 48001


def test_default_registry_stems_and_missing_input(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN), headers=HEADERS) as session:
        models = session.get("/models").json()["models"]
        assert any(m["id"] == "umxhq-vocals" and m["role"] == "stems" for m in models)
        response = session.post("/analysis/stems", json={})
        assert response.status_code == 202
        result = wait_terminal(session, response.json()["id"])
        assert result["error"]["code"] == "input_required"
        assert result["artifacts"] == []


def test_stem_api_cancellation_cleans_private_output(tmp_path):
    class Slow(FixtureBackend):
        def prepare(self, context, device, niter):
            context.cancelled.wait(5)
            context.checkpoint()

    sf.write(tmp_path / "input.wav", signal(48001), 48000, subtype="FLOAT")
    with TestClient(create_app(tmp_path, TOKEN, [StemsAdapter(Slow)]), headers=HEADERS) as session:
        job_id = session.post("/analysis/stems", json={"input_artifact": "input.wav"}).json()["id"]
        session.post(f"/jobs/{job_id}/cancel")
        result = wait_terminal(session, job_id)
        assert result["status"] == "cancelled"
        assert result["artifacts"] == []
    assert not list(tmp_path.rglob("vocals.wav"))
