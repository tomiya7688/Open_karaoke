import hashlib
import json
import math
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.vocal_event_features import HOP_SAMPLES, RATE
from open_karaoke_analysis.vocal_event_fusion import FEATURE_NAMES
from open_karaoke_analysis.vocal_events import VocalEventAdapter


def write_pitch(tmp_path, duration, frequency_at):
    path = tmp_path / "vocals.wav"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    frames = []
    for index, start in enumerate(range(0, duration, HOP_SAMPLES)):
        hz = frequency_at(start)
        probability = 0.95 if hz is not None else 0.02
        end = min(start + 1920, duration)
        frames.append(
            {
                "index": index,
                "start_sample": start,
                "end_sample": end,
                "center_sample": (start + end) // 2,
                "f0_hz": hz,
                "midi": 69 + 12 * math.log2(hz / 440) if hz else None,
                "confidence": 0.9 if hz else 0.0,
                "voiced_probability": probability,
                "voiced": hz is not None,
            }
        )
    document = {
        "format_version": 1,
        "analysis_version": 1,
        "model": {"id": "fixture-pitch", "version": "1"},
        "sample_rate": RATE,
        "window_samples": 1920,
        "hop_samples": HOP_SAMPLES,
        "duration_samples": duration,
        "input_sha256": digest,
        "mode": "ensemble",
        "detectors": [],
        "options": {},
        "confidence_calibrated": False,
        "voiced_probability_calibrated": False,
        "real_singing_accuracy_verified": False,
        "frames": frames,
    }
    (tmp_path / "pitch.json").write_text(json.dumps(document), encoding="utf-8")


def write_alignment(tmp_path):
    document = {
        "format_version": 1,
        "analysis_version": 1,
        "lines": [
            {
                "id": "line-1",
                "start_sample": 4800,
                "end_sample": 28800,
                "status": "aligned",
                "characters": [
                    {
                        "id": "char-1",
                        "status": "aligned",
                        "start_sample": 4800,
                        "end_sample": 9600,
                        "score": 0.95,
                    },
                    {
                        "id": "char-2",
                        "status": "aligned",
                        "start_sample": 16800,
                        "end_sample": 21600,
                        "score": 0.9,
                    },
                ],
                "words": [],
                "syllables": [
                    {
                        "id": "syllable-1",
                        "status": "aligned",
                        "start_sample": 16800,
                        "end_sample": 21600,
                        "score": 0.92,
                    }
                ],
            }
        ],
    }
    (tmp_path / "alignment.json").write_text(json.dumps(document), encoding="utf-8")


def write_notes(tmp_path):
    document = {
        "format_version": 1,
        "analysis_version": 1,
        "notes": [
            {"id": "note-1", "start_sample": 4800, "pitch_confidence": 0.9},
            {"id": "note-2", "start_sample": 16800, "pitch_confidence": 0.95},
        ],
    }
    (tmp_path / "notes.json").write_text(json.dumps(document), encoding="utf-8")


def stepped_audio(duration=38400, noise=0.0):
    samples = np.arange(duration, dtype=np.float64)
    audio = np.zeros(duration, dtype=np.float64)
    first = (samples >= 4800) & (samples < 16800)
    second = (samples >= 16800) & (samples < 28800)
    audio[first] = 0.45 * np.sin(2 * np.pi * 220 * samples[first] / RATE)
    audio[second] = 0.45 * np.sin(2 * np.pi * 440 * samples[second] / RATE)
    if noise:
        audio += np.random.default_rng(41).normal(0.0, noise, duration)
    return audio.astype(np.float32)


def setup_step_case(tmp_path, noise=0.0):
    audio = stepped_audio(noise=noise)
    sf.write(tmp_path / "vocals.wav", audio, RATE, subtype="FLOAT")
    write_pitch(
        tmp_path,
        len(audio),
        lambda sample: None
        if sample < 4800 or sample >= 28800
        else 220.0
        if sample < 16800
        else 440.0,
    )
    write_alignment(tmp_path)
    write_notes(tmp_path)
    return len(audio)


def run_adapter(tmp_path, options):
    context = AnalysisContext(
        tmp_path,
        "jobs/events/fixture",
        tmp_path / "vocals.wav",
        Event(),
        lambda *_: None,
    )
    adapter = VocalEventAdapter()
    paths = adapter.analyze(context, adapter.validate_options(options))
    return [json.loads((tmp_path / path).read_text(encoding="utf-8")) for path in paths]


def nearest(boundaries, sample):
    return min(boundaries, key=lambda item: abs(item["sample"] - sample))


def test_ensemble_publishes_shared_features_and_boundaries(tmp_path):
    setup_step_case(tmp_path)
    timeline, evidence = run_adapter(
        tmp_path,
        {
            "pitch_artifact": "pitch.json",
            "alignment_artifact": "alignment.json",
            "note_artifact": "notes.json",
            "boundary_threshold": 0.45,
        },
    )
    assert timeline["sample_rate"] == RATE
    assert timeline["hop_samples"] == HOP_SAMPLES
    assert timeline["mode"] == "ensemble"
    assert timeline["boundary_probability_calibrated"] is False
    assert timeline["real_singing_accuracy_verified"] is False
    assert timeline["phoneme_or_syllable_evidence_available"] is True
    assert timeline["character_alignment_evidence_available"] is True
    assert set(timeline["frames"][0]["features"]) == set(FEATURE_NAMES)
    for frame in timeline["frames"]:
        assert all(0.0 <= value <= 1.0 for value in frame["features"].values())
        assert 0.0 <= frame["note_boundary_probability"] <= 1.0
        assert 0.0 <= frame["lyric_boundary_probability"] <= 1.0

    for expected in [4800, 16800, 28800]:
        assert abs(nearest(timeline["note_boundaries"], expected)["sample"] - expected) <= 960
    for expected in [4800, 16800]:
        assert abs(nearest(timeline["lyric_boundaries"], expected)["sample"] - expected) <= 960
    assert evidence["source_artifacts"]["pitch"] == "pitch.json"
    assert len(evidence["note_onset_events"]) == 2


def test_single_feature_mode_is_directly_comparable(tmp_path):
    setup_step_case(tmp_path)
    timeline, _ = run_adapter(
        tmp_path,
        {
            "pitch_artifact": "pitch.json",
            "mode": "single",
            "features": ["f0_transition"],
            "boundary_threshold": 0.3,
        },
    )
    for frame in timeline["frames"]:
        expected = frame["features"]["f0_transition"]
        assert frame["note_boundary_probability"] == pytest.approx(expected)
        assert frame["lyric_boundary_probability"] == pytest.approx(expected)
    transition = timeline["frames"][round(16800 / HOP_SAMPLES)]
    assert transition["features"]["f0_transition"] > 0.9


def test_low_quality_audio_remains_finite_and_uses_pitch_evidence(tmp_path):
    setup_step_case(tmp_path, noise=0.08)
    timeline, _ = run_adapter(
        tmp_path,
        {
            "pitch_artifact": "pitch.json",
            "boundary_threshold": 0.4,
        },
    )
    values = [
        frame["note_boundary_probability"]
        for frame in timeline["frames"]
    ]
    assert np.isfinite(values).all()
    assert abs(nearest(timeline["note_boundaries"], 16800)["sample"] - 16800) <= 960


def test_legato_melisma_does_not_turn_small_pitch_motion_into_many_boundaries(tmp_path):
    duration = 33600
    samples = np.arange(duration, dtype=np.float64)
    phase = 2 * np.pi * (220.0 * samples / RATE + 20.0 * (samples / RATE) ** 2)
    audio = (0.45 * np.sin(phase)).astype(np.float32)
    sf.write(tmp_path / "vocals.wav", audio, RATE, subtype="FLOAT")

    def frequency(sample):
        return 220.0 + 40.0 * sample / duration

    write_pitch(tmp_path, duration, frequency)
    timeline, _ = run_adapter(
        tmp_path,
        {
            "pitch_artifact": "pitch.json",
            "boundary_threshold": 0.55,
        },
    )
    assert max(frame["features"]["f0_transition"] for frame in timeline["frames"]) < 0.2
    assert len(timeline["note_boundaries"]) <= 2


def test_mismatched_pitch_audio_is_rejected(tmp_path):
    setup_step_case(tmp_path)
    pitch = json.loads((tmp_path / "pitch.json").read_text(encoding="utf-8"))
    pitch["input_sha256"] = "0" * 64
    (tmp_path / "pitch.json").write_text(json.dumps(pitch), encoding="utf-8")
    with pytest.raises(ServiceError, match="Pitch timeline"):
        run_adapter(tmp_path, {"pitch_artifact": "pitch.json"})


def test_unavailable_single_feature_and_invalid_options(tmp_path):
    setup_step_case(tmp_path)
    with pytest.raises(ServiceError, match="enough available evidence"):
        run_adapter(
            tmp_path,
            {
                "pitch_artifact": "pitch.json",
                "mode": "single",
                "features": ["note_onset"],
            },
        )
    adapter = VocalEventAdapter()
    for options in [
        {},
        {
            "pitch_artifact": "pitch.json",
            "mode": "single",
            "features": ["f0_transition", "spectral_flux"],
        },
        {"pitch_artifact": "pitch.json", "mode": "ensemble", "features": ["f0_transition"]},
        {"pitch_artifact": "pitch.json", "features": ["spectral_flux", "spectral_flux"]},
        {"pitch_artifact": "pitch.json", "unknown": True},
    ]:
        with pytest.raises(ServiceError):
            adapter.validate_options(options)


def test_cancellation_before_publication(tmp_path):
    setup_step_case(tmp_path)
    cancelled = Event()
    cancelled.set()
    context = AnalysisContext(
        tmp_path,
        "jobs/events/cancelled",
        tmp_path / "vocals.wav",
        cancelled,
        lambda *_: None,
    )
    adapter = VocalEventAdapter()
    with pytest.raises(AnalysisCancelled):
        adapter.analyze(context, adapter.validate_options({"pitch_artifact": "pitch.json"}))
    assert not (tmp_path / "jobs").exists()
