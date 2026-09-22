import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis import stem_backend
from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.stems import StemsAdapter


class FixtureBackend:
    """A test double, not a music-separation model."""

    calls = 0
    prepared = 0
    revision = "fixture-1"

    def identity(self, device):
        return {"id": "fixture-only", "version": self.revision, "device": device}

    def prepare(self, context, device, niter):
        type(self).prepared += 1

    def vocals(self, samples):
        type(self).calls += 1
        return samples * np.float32(0.25)

    def close(self):
        pass


def signal(frames=144_013):
    t = np.arange(frames, dtype=np.float64) / 48_000
    return np.stack(
        (0.2 * np.sin(2 * np.pi * 220 * t), 0.17 * np.sin(2 * np.pi * 329.6 * t)), axis=1
    ).astype(np.float32)


def context(root, prefix="jobs/test/one", *, source="input.wav", event=None, report=None):
    return AnalysisContext(
        root,
        prefix,
        root / source if source else None,
        event or Event(),
        report or (lambda p, s: None),
    )


def execute(root, *, prefix="jobs/test/one", options=None, backend=FixtureBackend):
    adapter = StemsAdapter(backend)
    paths = adapter.analyze(
        context(root, prefix),
        adapter.validate_options(
            options
            or {
                "chunk_seconds": 2,
                "overlap_seconds": 1,
            }
        ),
    )
    return paths, json.loads((root / paths[-1]).read_text())


@pytest.fixture(autouse=True)
def reset_fixture():
    FixtureBackend.calls = FixtureBackend.prepared = 0
    FixtureBackend.revision = "fixture-1"


@pytest.mark.parametrize("frames", [1, 2048, 48_001, 144_013, 300_007])
def test_exact_length_stereo_float_and_reconstruction(tmp_path, frames):
    mix = signal(frames)
    sf.write(tmp_path / "input.wav", mix, 48_000, subtype="FLOAT")
    paths, meta = execute(tmp_path)
    assert [Path(p).name for p in paths] == ["vocals.wav", "accompaniment.wav", "stems.json"]
    v, rate = sf.read(tmp_path / paths[0], dtype="float32", always_2d=True)
    b, _ = sf.read(tmp_path / paths[1], dtype="float32", always_2d=True)
    assert rate == meta["sample_rate"] == 48_000
    assert v.shape == b.shape == mix.shape
    assert meta["duration_samples"] == frames
    assert sf.info(tmp_path / paths[0]).subtype == "FLOAT"
    np.testing.assert_allclose(v + b, mix, atol=3e-8)
    assert meta["model"]["version"] == "fixture-1"
    assert not list(tmp_path.rglob(".accum"))
    assert not list(tmp_path.rglob(".working-*"))


def test_silence_finite_and_no_clipping(tmp_path):
    sf.write(tmp_path / "input.wav", np.zeros((4801, 2)), 48_000, subtype="FLOAT")
    paths, meta = execute(tmp_path)
    assert FixtureBackend.calls == 0
    assert meta["peak_amplitude"] == {"vocals": 0, "accompaniment": 0}
    assert np.count_nonzero(sf.read(tmp_path / paths[0])[0]) == 0
    sf.write(tmp_path / "input.wav", signal() * 20, 48_000, subtype="FLOAT")
    paths, meta = execute(tmp_path, prefix="jobs/test/loud")
    assert meta["exceeds_unity"]
    assert np.max(np.abs(sf.read(tmp_path / paths[1])[0])) > 1


def test_cache_hit_content_identity_and_no_hardlinks(tmp_path):
    sf.write(tmp_path / "input.wav", signal(), 48_000, subtype="FLOAT")
    first, a = execute(tmp_path)
    calls = FixtureBackend.calls
    second, b = execute(tmp_path, prefix="jobs/test/two")
    assert a["cache_hit"] is False and b["cache_hit"] is True
    assert a["cache_key"] == b["cache_key"]
    assert FixtureBackend.calls == calls
    assert (tmp_path / first[0]).read_bytes() == (tmp_path / second[0]).read_bytes()
    (tmp_path / first[0]).write_bytes(b"user-modified-private-result")
    third, c = execute(tmp_path, prefix="jobs/test/three")
    assert c["cache_hit"]
    assert (tmp_path / third[0]).read_bytes() == (tmp_path / second[0]).read_bytes()


def test_input_options_and_model_versions_invalidate_cache(tmp_path):
    sf.write(tmp_path / "input.wav", signal(48001), 48_000, subtype="FLOAT")
    _, a = execute(tmp_path)
    _, b = execute(tmp_path, prefix="jobs/test/two", options={"niter": 0})
    FixtureBackend.revision = "fixture-2"
    _, c = execute(tmp_path, prefix="jobs/test/three", options={"niter": 0})
    sf.write(tmp_path / "input.wav", signal(48001) * 0.5, 48_000, subtype="FLOAT")
    _, d = execute(tmp_path, prefix="jobs/test/four", options={"niter": 0})
    assert len({m["cache_key"] for m in (a, b, c, d)}) == 4
    assert all(not m["cache_hit"] for m in (a, b, c, d))


def test_corrupt_cache_is_not_trusted_or_overwritten(tmp_path):
    sf.write(tmp_path / "input.wav", signal(48001), 48_000, subtype="FLOAT")
    _, a = execute(tmp_path)
    cached = tmp_path / "cache/stems/v1" / a["cache_key"] / "vocals.wav"
    cached.write_bytes(b"damaged")
    paths, b = execute(tmp_path, prefix="jobs/test/two")
    assert not b["cache_hit"]
    assert sf.info(tmp_path / paths[0]).frames == 48001
    assert cached.read_bytes() == b"damaged"


@pytest.mark.parametrize(
    "options",
    [
        {"device": "bogus"},
        {"niter": 9},
        {"chunk_seconds": 0},
        {"chunk_seconds": 2, "overlap_seconds": 2},
        {"download_url": "bad"},
        {"chunk_seconds": "12"},
        {"niter": True},
    ],
)
def test_invalid_options_rejected(options):
    with pytest.raises(ServiceError, match="Invalid stem"):
        StemsAdapter().validate_options(options)


@pytest.mark.parametrize("rate,channels,frames", [(44100, 2, 100), (48000, 1, 100), (48000, 2, 0)])
def test_unnormalized_or_empty_audio_rejected(tmp_path, rate, channels, frames):
    sf.write(tmp_path / "input.wav", np.zeros((frames, channels)), rate, subtype="FLOAT")
    with pytest.raises(ServiceError) as error:
        execute(tmp_path)
    assert error.value.detail.code == "invalid_audio"


def test_missing_input_and_nonfinite_samples(tmp_path):
    with pytest.raises(ServiceError) as error:
        StemsAdapter().analyze(context(tmp_path, source=None), {})
    assert error.value.detail.code == "input_required"
    mix = signal(48001)
    mix[40, 1] = np.nan
    sf.write(tmp_path / "input.wav", mix, 48_000, subtype="FLOAT")
    with pytest.raises(ServiceError) as error:
        execute(tmp_path)
    assert error.value.detail.code == "invalid_audio"
    assert not (tmp_path / "jobs/test/one").exists()


def test_model_failure_and_cancellation_leave_no_partial_artifacts(tmp_path):
    event = Event()

    class CancelBackend(FixtureBackend):
        def vocals(self, samples):
            event.set()
            return samples

    sf.write(tmp_path / "input.wav", signal(), 48_000, subtype="FLOAT")
    adapter = StemsAdapter(CancelBackend)
    with pytest.raises(AnalysisCancelled):
        adapter.analyze(context(tmp_path, event=event), adapter.validate_options({}))
    assert not list(tmp_path.rglob("vocals.wav"))
    assert not (tmp_path / "jobs/test/one").exists()
    assert not list(tmp_path.rglob(".working-*"))

    class InvalidBackend(FixtureBackend):
        def vocals(self, samples):
            return np.zeros((len(samples) - 1, 2))

    with pytest.raises(ServiceError) as error:
        execute(tmp_path, backend=InvalidBackend)
    assert error.value.detail.code == "invalid_model_output"
    assert not list(tmp_path.rglob("vocals.wav"))


def test_symlinked_cache_is_rejected(tmp_path):
    sf.write(tmp_path / "input.wav", signal(48001), 48_000, subtype="FLOAT")
    outside = tmp_path / "unrelated"
    outside.mkdir()
    try:
        (tmp_path / "cache").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink permission unavailable")
    with pytest.raises(ServiceError) as error:
        execute(tmp_path)
    assert error.value.detail.code == "invalid_path"
    assert not list(outside.iterdir())


def test_startup_does_not_import_ml_or_download():
    code = (
        "import sys; before=set(sys.modules); "
        "from open_karaoke_analysis.stems import StemsAdapter; "
        "StemsAdapter(); assert not {'torch','torchaudio','openunmix','numpy'} & (sys.modules.keys() - before)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10)


def test_pinned_weight_download_validation_and_cancellation(tmp_path, monkeypatch):
    content = b"small-test-checkpoint"
    monkeypatch.setattr(stem_backend, "WEIGHT_BYTES", len(content))
    monkeypatch.setattr(stem_backend, "WEIGHT_SHA256", hashlib.sha256(content).hexdigest())

    class Response(io.BytesIO):
        url = "https://zenodo.org/test"

    calls = []

    def download(request, timeout):
        calls.append(request.full_url)
        return Response(content)

    monkeypatch.setattr(stem_backend.urllib.request, "urlopen", download)
    ctx = context(tmp_path)
    path = stem_backend.verified_weights(ctx)
    assert path.read_bytes() == content
    assert stem_backend.verified_weights(ctx) == path
    assert calls == [stem_backend.WEIGHT_URL]
    path.write_bytes(b"corrupted")
    with pytest.raises(ServiceError) as error:
        stem_backend.verified_weights(ctx)
    assert error.value.detail.code == "model_integrity"
    path.unlink()
    ctx.cancelled.set()
    with pytest.raises(AnalysisCancelled):
        stem_backend.verified_weights(ctx)
    assert not path.exists()
    assert not list(tmp_path.rglob(".download-*"))


def test_bad_download_never_published(tmp_path, monkeypatch):
    monkeypatch.setattr(stem_backend, "WEIGHT_BYTES", 3)
    monkeypatch.setattr(stem_backend, "WEIGHT_SHA256", "0" * 64)

    class Response(io.BytesIO):
        url = "https://zenodo.org/test"

    monkeypatch.setattr(stem_backend.urllib.request, "urlopen", lambda *a, **k: Response(b"abc"))
    with pytest.raises(ServiceError) as error:
        stem_backend.verified_weights(context(tmp_path))
    assert error.value.detail.code == "model_integrity"
    assert not list(tmp_path.rglob("*.pth"))
    assert not list(tmp_path.rglob(".download-*"))
