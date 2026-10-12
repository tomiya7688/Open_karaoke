from pathlib import Path

import pytest

from open_karaoke_analysis.note_evaluator import evaluate_dataset, evaluate_notes

FIXTURE = Path(__file__).parent / "fixtures" / "score_notes_reference.json"


def test_note_metric_counts_octave_errors():
    reference = [{"start_sample": 0, "end_sample": 4800, "midi_note": 60}]
    predicted = [{"start_sample": 0, "end_sample": 4800, "midi_note": 72}]
    report = evaluate_notes(reference, predicted, tolerance_samples=480)
    assert report["pitch_f1"] == 0.0
    assert report["octave_error_count"] == 1
    assert report["octave_error_rate"] == 1.0


def test_hand_authored_score_fixture_regression():
    import json

    report = evaluate_dataset(
        json.loads(FIXTURE.read_text(encoding="utf-8")),
        tolerance_samples=1440,
    )
    assert report["real_recordings"] is False
    assert report["real_singing_accuracy_verified"] is False
    assert report["aggregate"]["pitch_f1"] == pytest.approx(1.0)
    assert report["aggregate"]["mean_abs_onset_error_ms"] == pytest.approx(0.0)
    assert report["aggregate"]["mean_abs_offset_error_ms"] == pytest.approx(0.0)
    assert report["aggregate"]["octave_error_rate"] == pytest.approx(0.0)


def test_note_metric_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        evaluate_notes(
            [{"start_sample": 100, "end_sample": 10, "midi_note": 60}],
            [],
            tolerance_samples=480,
        )
    with pytest.raises(ValueError):
        evaluate_notes([], [], tolerance_samples=-1)
