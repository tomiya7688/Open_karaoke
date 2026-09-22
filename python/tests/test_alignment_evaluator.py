import json
import subprocess
import sys

import pytest

from open_karaoke_analysis.alignment_evaluator import evaluate


def dataset():
    return {
        "dataset_version": "scripted-boundaries-v1",
        "items": [
            {
                "id": "a",
                "reference": {"start_sample": 0, "end_sample": 4800},
                "hypothesis": {"start_sample": 480, "end_sample": 5760},
            },
            {
                "id": "b",
                "reference": {"start_sample": 9600, "end_sample": 14400},
                "hypothesis": None,
            },
        ],
    }


def test_errors_and_coverage():
    report = evaluate(dataset())
    assert report["mean_boundary_error_ms"] == 15
    assert report["p95_boundary_error_ms"] == 19.5
    assert report["coverage"] == 0.5
    assert report["quality_gate_applied"] is False
    assert not evaluate(dataset(), max_mean_ms=20)["passed"]
    assert evaluate(dataset(), max_mean_ms=20, min_coverage=0.5)["passed"]
    assert not evaluate(dataset(), max_p95_ms=19, min_coverage=0.5)["passed"]


def test_missing_never_improves_error():
    data = dataset()
    data["items"][0]["hypothesis"] = None
    report = evaluate(data, max_mean_ms=100, min_coverage=0)
    assert report["mean_boundary_error_ms"] is None
    assert report["passed"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"max_mean_ms": float("nan")},
        {"max_p95_ms": -1},
        {"min_coverage": 1.1},
        {"min_coverage": True},
    ],
)
def test_bad_thresholds(options):
    with pytest.raises(ValueError):
        evaluate(dataset(), **options)


def test_duplicates_and_noninteger_samples():
    data = dataset()
    data["items"][1]["id"] = "a"
    with pytest.raises(ValueError):
        evaluate(data)
    data = dataset()
    data["items"][0]["reference"]["start_sample"] = 0.0
    with pytest.raises(ValueError):
        evaluate(data)
    with pytest.raises(ValueError):
        evaluate({"dataset_version": "empty", "items": []})


def test_cli_threshold_exit(tmp_path):
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(dataset()), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_karaoke_analysis.alignment_evaluator",
            str(source),
            str(output),
            "--max-mean-ms",
            "1",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(output.read_text())["passed"] is False
