import copy
import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.lyrics import LyricsAdapter, sample_index
from open_karaoke_analysis.whisper_backend import WhisperBackend, verified_checkpoint


class FixtureBackend:
    """Scripted ASR contract, never evidence of model or singer accuracy."""

    def __init__(self, result=None):
        self.result = result or {
            "text": "青い空",
            "language": "ja",
            "segments": [
                {
                    "start": 0.1,
                    "end": 0.8,
                    "text": " 青い空 ",
                    "tokens": [1, 2],
                    "avg_logprob": -0.2,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.1,
                }
            ],
        }
        self.calls = 0
        self.closed = False

    def identity(self):
        return {"id": "fixture-asr", "version": "1", "mock": True}

    def prepare(self, context, options):
        self.context = context

    def transcribe(self, audio, options):
        assert audio.dtype == np.float32
        assert audio.ndim == 1
        assert 0 < len(audio) <= 30 * 16000
        self.calls += 1
        return copy.deepcopy(self.result)

    def close(self):
        self.closed = True


def setup_audio(root, seconds=1, *, silence=False, rate=48000, channels=2, subtype="FLOAT"):
    samples = np.zeros((int(rate * seconds), channels), dtype=np.float32)
    if not silence:
        samples[:] = (0.1 * np.sin(np.arange(len(samples)) * 2 * np.pi * 220 / rate))[:, None]
    sf.write(root / "vocals.wav", samples, rate, subtype=subtype)
    return AnalysisContext(
        root, "jobs/test/lyrics", root / "vocals.wav", Event(), lambda p, s: None
    )


def run(context, backend=None, **options):
    backend = backend or FixtureBackend()
    adapter = LyricsAdapter(lambda: backend)
    paths = adapter.analyze(context, adapter.validate_options(options))
    return [json.loads((context.root / path).read_text(encoding="utf-8")) for path in paths]


def test_japanese_candidates_raw_evidence_and_exact_timeline(tmp_path):
    backend = FixtureBackend()
    expected = copy.deepcopy(backend.result)
    lyrics, evidence, raw = run(setup_audio(tmp_path), backend)
    assert lyrics["segments"][0]["start_sample"] == 4800
    assert lyrics["segments"][0]["end_sample"] == 38400
    assert lyrics["segments"][0]["text"] == "青い空"
    assert lyrics["segments"][0]["text_confidence"] is None
    assert raw["windows"][0]["result"] == expected
    assert evidence["items"][1]["evidence"]["diagnostics"]["avg_logprob"] == -0.2
    assert raw["inference_performed"] is True
    assert backend.closed
    assert not list(tmp_path.rglob(".lyrics-*"))


@pytest.mark.parametrize(
    "seconds,expected", [(0, 0), (0.02, 960), (0.00003125, 2), (28.1, 1348800)]
)
def test_sample_conversion(seconds, expected):
    assert sample_index(seconds) == expected


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "0.1", None])
def test_invalid_sample_conversion(value):
    with pytest.raises(ServiceError):
        sample_index(value)


@pytest.mark.parametrize(
    "options",
    [
        {"beam_size": 0},
        {"device": "auto"},
        {"language": "Japanese"},
        {"task": "translate"},
        {"analysis_version": True},
        {"allow_model_download": "yes"},
    ],
)
def test_invalid_options(options):
    with pytest.raises(ServiceError):
        LyricsAdapter().validate_options(options)


@pytest.mark.parametrize("language", [None, "ja", "en", "fr"])
def test_multilingual_option(language):
    assert LyricsAdapter().validate_options({"language": language})["language"] == language


def test_silence_does_not_load_a_model(tmp_path):
    class NoInference(FixtureBackend):
        def prepare(self, context, options):
            pytest.fail("Silence must not load/download weights")

    lyrics, evidence, raw = run(setup_audio(tmp_path, silence=True), NoInference())
    assert not lyrics["segments"]
    assert raw["inference_performed"] is False
    assert raw["windows"][0]["skipped"] == "digital_silence"


def test_silence_hallucination_is_retained_only_in_raw(tmp_path):
    context = setup_audio(tmp_path)
    samples, _ = sf.read(context.input_path, dtype="float32", always_2d=True)
    samples[4800:38400] = 0
    sf.write(context.input_path, samples, 48000, subtype="FLOAT")
    lyrics, evidence, raw = run(context)
    assert not lyrics["segments"]
    assert evidence["observations"][0]["disposition"] == "digital_silence"
    assert raw["windows"][0]["result"]["segments"][0]["text"] == " 青い空 "


def test_repetition_flags_do_not_remove_real_chorus(tmp_path):
    backend = FixtureBackend()
    backend.result["segments"][0]["text"] = "らららららら"
    lyrics, evidence, raw = run(setup_audio(tmp_path), backend)
    assert lyrics["segments"][0]["text"] == "らららららら"
    assert "repetition_review" in evidence["items"][1]["evidence"]["flags"]


def test_reference_is_preserved_but_not_used_as_recognized_text(tmp_path):
    (tmp_path / "reference.txt").write_text("別の歌詞", encoding="utf-8")
    lyrics, evidence, raw = run(setup_audio(tmp_path), reference_lyrics_artifact="reference.txt")
    assert raw["reference_lyrics"]["text"] == "別の歌詞"
    assert raw["reference_lyrics"]["used_for_decoding"] is False
    assert lyrics["segments"][0]["text"] == "青い空"


@pytest.mark.parametrize("reference", ["../outside.txt", "C:/outside.txt"])
def test_reference_path_validation(tmp_path, reference):
    with pytest.raises(ServiceError):
        run(setup_audio(tmp_path), reference_lyrics_artifact=reference)


@pytest.mark.parametrize("kwargs", [{"rate": 44100}, {"subtype": "PCM_16"}, {"channels": 3}])
def test_audio_contract(tmp_path, kwargs):
    with pytest.raises(ServiceError, match="48 kHz"):
        run(setup_audio(tmp_path, **kwargs))


def test_nonfinite_audio_rejected(tmp_path):
    context = setup_audio(tmp_path)
    sf.write(context.input_path, np.full((100, 2), np.nan), 48000, subtype="FLOAT")
    with pytest.raises(ServiceError, match="NaN"):
        run(context)


def test_antiphase_audio_is_not_mistaken_for_silence(tmp_path):
    context = setup_audio(tmp_path)
    samples, _ = sf.read(context.input_path, dtype="float32", always_2d=True)
    samples[:, 1] = -samples[:, 0]
    sf.write(context.input_path, samples, 48000, subtype="FLOAT")
    lyrics, _, raw = run(context)
    assert lyrics["segments"]
    assert raw["windows"][0]["downmix"] == "left_channel_phase_cancellation"


def test_chunk_ownership_uses_absolute_samples(tmp_path):
    backend = FixtureBackend()
    backend.result["segments"][0].update(start=1.1, end=1.8)
    lyrics, evidence, raw = run(setup_audio(tmp_path, seconds=57), backend)
    assert backend.calls == 3
    assert [s["start_sample"] for s in lyrics["segments"]] == [52800, 1348800, 2692800]
    assert all(s["end_sample"] <= 57 * 48000 for s in lyrics["segments"])
    assert raw["windows"][1]["offset_sample"] == 27 * 48000


def test_cancel_before_and_after_inference(tmp_path):
    context = setup_audio(tmp_path)
    context.cancelled.set()
    with pytest.raises(AnalysisCancelled):
        run(context)
    context.cancelled.clear()

    class CancelDuring(FixtureBackend):
        def transcribe(self, audio, options):
            self.context.cancelled.set()
            return super().transcribe(audio, options)

    backend = CancelDuring()
    with pytest.raises(AnalysisCancelled):
        run(context, backend)
    assert backend.closed
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob(".lyrics-*"))


@pytest.mark.parametrize(
    "change",
    [
        {"start": -1},
        {"end": 0},
        {"text": None},
        {"no_speech_prob": 2},
        {"avg_logprob": float("nan")},
    ],
)
def test_bad_backend_output_is_not_a_success(tmp_path, change):
    backend = FixtureBackend()
    backend.result["segments"][0].update(change)
    with pytest.raises(ServiceError):
        run(setup_audio(tmp_path), backend)
    assert backend.closed


def test_weights_are_offline_and_fail_closed(tmp_path):
    context = setup_audio(tmp_path)
    with pytest.raises(ServiceError, match="not installed"):
        verified_checkpoint(context, "tiny", False)
    path = tmp_path / "models/whisper/tiny.pt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"untrusted checkpoint")
    with pytest.raises(ServiceError, match="SHA-256"):
        verified_checkpoint(context, "tiny", True)


def test_model_identity_is_explicit():
    identity = WhisperBackend().identity()
    assert identity["id"] == "whisper-large-v3"
    assert len(identity["weights_sha256"]) == 64
    with pytest.raises(ValueError):
        WhisperBackend("arbitrary-url")


def test_import_is_lazy():
    code = (
        "import sys; before=set(sys.modules); "
        "from open_karaoke_analysis.lyrics import LyricsAdapter; "
        "LyricsAdapter(); assert not {'torch','whisper','numpy','soundfile'} "
        "& (sys.modules.keys() - before)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10)


def test_python_output_matches_shared_rust_fixture(tmp_path):
    lyrics, evidence, raw = run(setup_audio(tmp_path))
    root = Path(__file__).resolve().parents[2] / "core/tests/fixtures"
    expected = json.loads((root / "whisper_lyrics.json").read_text(encoding="utf-8"))
    assert lyrics == expected
    expected_evidence = json.loads((root / "whisper_evidence.json").read_text(encoding="utf-8"))
    assert evidence["items"][1] == expected_evidence["items"][1]


def test_upstream_empty_segment_is_preserved_as_observation(tmp_path):
    backend = FixtureBackend()
    backend.result["segments"].append({"text": "", "start": 0.9, "end": 0.9, "tokens": []})
    lyrics, evidence, raw = run(setup_audio(tmp_path), backend)
    assert len(lyrics["segments"]) == 1
    assert evidence["observations"][1]["disposition"] == "empty_segment"
    assert len(raw["windows"][0]["result"]["segments"]) == 2
