import hashlib
import io
import json
from threading import Event

import pytest

from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis import alignment_backend as backend
from open_karaoke_analysis.contracts import ServiceError


class Response(io.BytesIO):
    url = "https://huggingface.co/fixture"


@pytest.fixture
def context(tmp_path):
    return AnalysisContext(tmp_path, "jobs/fixture", None, Event(), lambda *_: None)


def test_verified_snapshot_and_corruption(context, monkeypatch):
    payload = b"fixture-only-checkpoint"
    monkeypatch.setattr(backend, "WEIGHT_SHA256", hashlib.sha256(payload).hexdigest())
    def download(ctx, directory, name, limit):
        data = payload if name == "pytorch_model.bin" else b"{}"
        (directory / name).write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(backend, "download_file", download)
    directory, first = backend.model_snapshot(context, True)
    assert backend.model_snapshot(context, False)[1] == first
    (directory / "vocab.json").write_bytes(b"tampered")
    with pytest.raises(ServiceError, match="corrupt"):
        backend.model_snapshot(context, False)


def test_offline_and_incomplete_snapshot(context):
    with pytest.raises(ServiceError, match="downloads disabled"):
        backend.model_snapshot(context, False)
    directory = context.root / "models" / "alignment-ja" / backend.REVISION
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(json.dumps({"revision": backend.REVISION, "sha256": {}}))
    with pytest.raises(ServiceError, match="corrupt"):
        backend.model_snapshot(context, True)


@pytest.mark.parametrize("bad", ["hash", "size", "cancel"])
def test_download_failure_cleans_temporary(context, monkeypatch, bad):
    payload = b"fixture"
    class Opener:
        def open(self, request, timeout):
            assert backend.REVISION in request.full_url
            return Response(payload)
    monkeypatch.setattr(backend.urllib.request, "build_opener", lambda *_: Opener())
    if bad == "cancel":
        context.cancelled.set()
    with pytest.raises((ServiceError, AnalysisCancelled)):
        backend.download_file(context, context.root, "pytorch_model.bin", 1 if bad == "size" else 100)
    assert not list(context.root.glob(".download-*"))
    assert not (context.root / "pytorch_model.bin").exists()


def test_reject_downgrade_before_request():
    handler = backend.HttpsRedirect()
    with pytest.raises(ServiceError):
        handler.redirect_request(None, None, 302, "", {}, "http://example.test/model.bin")
