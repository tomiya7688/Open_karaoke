import hashlib
import io

import pytest
from test_lyrics import setup_audio

from open_karaoke_analysis import whisper_backend as backend
from open_karaoke_analysis.adapters import AnalysisCancelled
from open_karaoke_analysis.contracts import ServiceError


class Response(io.BytesIO):
    url = "https://publisher.test/weights"


def test_streamed_download_is_verified_and_cached(tmp_path, monkeypatch):
    data = b"trusted-fixture"
    monkeypatch.setitem(backend.WEIGHTS, "tiny", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(backend.urllib.request, "urlopen", lambda *a, **k: Response(data))
    context = setup_audio(tmp_path)
    path = backend.verified_checkpoint(context, "tiny", True)
    assert path.read_bytes() == data
    assert backend.verified_checkpoint(context, "tiny", False) == path
    assert not list(tmp_path.rglob(".download-*"))


def test_corrupt_download_is_never_published(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.urllib.request, "urlopen", lambda *a, **k: Response(b"wrong"))
    with pytest.raises(ServiceError, match="invalid"):
        backend.verified_checkpoint(setup_audio(tmp_path), "tiny", True)
    assert not list(tmp_path.rglob("*.pt"))
    assert not list(tmp_path.rglob(".download-*"))


def test_cancelled_download_is_never_published(tmp_path, monkeypatch):
    context = setup_audio(tmp_path)

    class CancelResponse(Response):
        def read(self, size=-1):
            context.cancelled.set()
            return super().read(size)

    monkeypatch.setattr(backend.urllib.request, "urlopen", lambda *a, **k: CancelResponse(b"a"))
    with pytest.raises(AnalysisCancelled):
        backend.verified_checkpoint(context, "tiny", True)
    assert not list(tmp_path.rglob("*.pt"))
    assert not list(tmp_path.rglob(".download-*"))
