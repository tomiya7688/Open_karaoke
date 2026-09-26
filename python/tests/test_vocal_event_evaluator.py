import json
from pathlib import Path

import pytest

from open_karaoke_analysis.vocal_event_evaluator import evaluate_boundaries, evaluate_dataset


def fixture():
    path = Path(__file__).parent / "fixtures" / "vocal_event_boundaries.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_boundary_evaluator_is_one_to_one_and_reports_f1():
    report = evaluate_boundaries([1000, 2000], [900, 1100, 2050], 150)
    assert report["true_positive"] == 2
    assert report["false_positive"] == 1
    assert report["false_negative"] == 0
    assert report["precision"] == pytest.approx(2 / 3)
    assert report["recall"] == 1.0
    assert report["f1"] == pytest.approx(0.8)


def test_annotated_fixture_covers_clean_low_quality_and_legato_cases():
    document = fixture()
    assert [case["id"] for case in document["cases"]] == [
        "clean-transitions",
        "low-quality-audio",
        "legato-melisma",
    ]
    report = evaluate_dataset(document, 1440)
    assert report["aggregate"]["note"]["f1"] > 0.9
    assert report["aggregate"]["lyric"]["f1"] == 1.0
    assert report["quality_gate_applied"] is False


def test_evaluator_rejects_invalid_timelines():
    with pytest.raises(ValueError):
        evaluate_boundaries([-1], [], 10)
    with pytest.raises(ValueError):
        evaluate_boundaries([], [True], 10)
    with pytest.raises(ValueError):
        evaluate_dataset({"cases": []}, 10)
