import json
import math
from threading import Event

import numpy as np
import pytest
import soundfile as sf

from open_karaoke_analysis.adapters import AnalysisCancelled, AnalysisContext
from open_karaoke_analysis.contracts import ServiceError
from open_karaoke_analysis.pitch import HOP_SAMPLES, RATE, WINDOW_SAMPLES, PitchAdapter
from open_karaoke_analysis.pitch_detectors import (
    NormalizedAutocorrelationDetector,
    PitchObservation,
    YinDetector,
)
from open_karaoke_analysis.pitch_fusion import cents_between, fuse_frame


def tone(frequency, seconds=0.25, harmonic=True):
    samples = np.arange(round(seconds * RATE), dtype=np.float64)
    audio = 0.55 * np.sin(2 * np.pi * frequency * samples / RATE)
    if harmonic:
        audio += 0.22 * np.sin(2 * np.pi * frequency * 2 * samples / RATE)
        audio += 0.10 * np.sin(2 * np.pi * frequency * 3 * samples / RATE)
    return audio.astype(np.float32)


def observation(detector, hz, confidence=0.9, voiced=0.9, reliability=1.0):
    return PitchObservation(detector, hz, confidence, voiced, reliability, 0.3, {})


@pytest.mark.parametrize("frequency", [70.0, 110.0, 220.0, 440.0, 880.0])
def test_yin_tracks_synthetic_tones(frequency):
    detector = YinDetector()
    frame = tone(frequency, WINDOW_SAMPLES / RATE)
    result = detector.detect(frame, RATE, 55.0, 1760.0, 0.0001)
    assert result.hz is not None
    assert abs(cents_between(result.hz, frequency)) < 10.0
    assert result.confidence > 0.8
    assert 0.0 <= result.voiced_probability <= 1.0


def test_detectors_report_unvoiced_silence():
    frame = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    for detector in [YinDetector(), NormalizedAutocorrelationDetector()]:
        result = detector.detect(frame, RATE, 55.0, 1760.0, 0.0001)
        assert result.hz is None
        assert result.voiced_probability == 0.0


def test_ensemble_corrects_isolated_octave_error_using_continuity():
    sequence = [
        [observation("yin", 220.0, 0.99, 0.99), observation("nacf", 220.0, 0.9, 0.9, 0.85)],
        [observation("yin", 440.0, 0.55, 0.75), observation("nacf", 220.0, 0.95, 0.95, 0.85)],
        [observation("yin", 220.0, 0.99, 0.99), observation("nacf", 220.0, 0.9, 0.9, 0.85)],
    ]
    previous = None
    selected = []
    evidence = []
    for frame in sequence:
        hz, _, _, item = fuse_frame(frame, previous, 55.0, 1760.0, 0.55)
        selected.append(hz)
        evidence.append(item)
        if hz is not None:
            previous = hz
    assert all(hz is not None and abs(cents_between(hz, 220.0)) < 50.0 for hz in selected)
    assert evidence[1]["corrections"]["yin"] == -1


def test_single_and_ensemble_option_validation():
    adapter = PitchAdapter()
    assert adapter.validate_options({"mode": "single", "detectors": ["yin"]})["mode"] == "single"
    assert adapter.validate_options({})["detectors"] == ["yin", "nacf"]
    for options in [
        {"mode": "single", "detectors": ["yin", "nacf"]},
        {"mode": "ensemble", "detectors": ["yin"]},
        {"detectors": ["yin", "yin"]},
        {"min_frequency_hz": 500.0, "max_frequency_hz": 200.0},
        {"unknown": True},
    ]:
        with pytest.raises(ServiceError):
            adapter.validate_options(options)


def run_adapter(tmp_path, audio, options=None, channels=1):
    if channels == 2:
        audio = np.column_stack([audio, audio])
    sf.write(tmp_path / "vocals.wav", audio, RATE, subtype="FLOAT")
    context = AnalysisContext(
        tmp_path,
        "jobs/pitch/fixture",
        tmp_path / "vocals.wav",
        Event(),
        lambda *_: None,
    )
    paths = PitchAdapter().analyze(context, PitchAdapter().validate_options(options or {}))
    return [json.loads((tmp_path / path).read_text(encoding="utf-8")) for path in paths]


def test_adapter_publishes_normalized_timeline_and_evidence(tmp_path):
    timeline, evidence = run_adapter(tmp_path, tone(220.0, 0.12))
    assert timeline["sample_rate"] == RATE
    assert timeline["hop_samples"] == HOP_SAMPLES
    assert timeline["window_samples"] == WINDOW_SAMPLES
    assert timeline["mode"] == "ensemble"
    assert len(timeline["frames"]) == math.ceil(timeline["duration_samples"] / HOP_SAMPLES)
    assert timeline["frames"][0]["start_sample"] == 0
    assert timeline["frames"][0]["center_sample"] > 0
    voiced = [frame for frame in timeline["frames"] if frame["voiced"]]
    assert voiced
    assert abs(cents_between(voiced[0]["f0_hz"], 220.0)) < 20.0
    assert evidence["input_sha256"] == timeline["input_sha256"]
    assert set(evidence["frames"][0]["detectors"]) == {"yin", "nacf"}
    assert timeline["confidence_calibrated"] is False
    assert timeline["real_singing_accuracy_verified"] is False


def test_single_detector_mode_is_not_silently_fused(tmp_path):
    timeline, evidence = run_adapter(
        tmp_path,
        tone(880.0, 0.08),
        {"mode": "single", "detectors": ["nacf"]},
    )
    assert timeline["mode"] == "single"
    assert evidence["frames"][0]["fusion"]["mode"] == "single"
    assert evidence["frames"][0]["fusion"]["selected_detector"] == "nacf"


def test_phase_cancelled_stereo_uses_channel_fallback(tmp_path):
    audio = tone(220.0, 0.08)
    stereo = np.column_stack([audio, -audio])
    sf.write(tmp_path / "vocals.wav", stereo, RATE, subtype="FLOAT")
    context = AnalysisContext(
        tmp_path,
        "jobs/pitch/phase",
        tmp_path / "vocals.wav",
        Event(),
        lambda *_: None,
    )
    paths = PitchAdapter().analyze(context, PitchAdapter().validate_options({}))
    evidence = json.loads((tmp_path / paths[1]).read_text(encoding="utf-8"))
    assert "channel_phase_cancellation_fallback" in evidence["frames"][0]["flags"]


def test_invalid_audio_and_cancellation(tmp_path):
    sf.write(tmp_path / "bad.wav", np.zeros(1000), 44100, subtype="FLOAT")
    context = AnalysisContext(
        tmp_path, "jobs/pitch/bad", tmp_path / "bad.wav", Event(), lambda *_: None
    )
    with pytest.raises(ServiceError, match="48 kHz"):
        PitchAdapter().analyze(context, PitchAdapter().validate_options({}))

    sf.write(tmp_path / "vocals.wav", tone(220.0, 0.08), RATE, subtype="FLOAT")
    cancelled = Event()
    cancelled.set()
    context = AnalysisContext(
        tmp_path, "jobs/pitch/cancelled", tmp_path / "vocals.wav", cancelled, lambda *_: None
    )
    with pytest.raises(AnalysisCancelled):
        PitchAdapter().analyze(context, PitchAdapter().validate_options({}))
