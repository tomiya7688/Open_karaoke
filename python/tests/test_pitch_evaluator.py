import pytest

from open_karaoke_analysis.pitch_evaluator import evaluate, synthetic_benchmark


def test_pitch_evaluator_counts_missing_as_gross_error():
    report = evaluate([220.0, 220.0, None], [220.0, None, 110.0])
    assert report["voiced_recall"] == 0.5
    assert report["gross_pitch_error_rate"] == 0.5
    assert report["unvoiced_false_positive_rate"] == 1.0


def test_synthetic_benchmark_reports_each_detector_and_ensemble():
    report = synthetic_benchmark()
    assert set(report["detectors"]) == {"yin", "nacf"}
    assert report["ensemble"]["median_abs_cents"] < 5.0
    assert report["ensemble"]["gross_pitch_error_rate"] <= 0.05
    assert report["octave_recovery_fixture"]["passed"] is True
    assert report["real_singing_accuracy_verified"] is False
    assert report["detectors"]["nacf"]["octave_error_rate"] >= 0.0


def test_evaluator_requires_equal_nonempty_timelines():
    with pytest.raises(ValueError):
        evaluate([], [])
    with pytest.raises(ValueError):
        evaluate([220.0], [220.0, 220.0])
