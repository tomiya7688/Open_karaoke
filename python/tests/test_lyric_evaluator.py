import json
import subprocess
import sys

import pytest

from open_karaoke_analysis.lyric_evaluator import edit_distance, evaluate


def test_japanese_cer_and_explicit_whitespace_wer():
    report = evaluate([{"reference": "青い空", "hypothesis": "青い海"}])
    assert report["cer"] == pytest.approx(1 / 3)
    assert report["wer"] == 1.0
    assert report["wer_tokenizer"] == "whitespace"
    assert report["passed"] is None
    assert report["quality_gate_applied"] is False


def test_normalization_and_exact_mode():
    pair = [{"reference": "ＡＢＣ、 あ！", "hypothesis": "abc あ"}]
    assert evaluate(pair)["cer"] == 0
    assert evaluate(pair, normalized=False)["cer"] > 0


def test_micro_average_not_utterance_average():
    report = evaluate(
        [
            {"reference": "あ", "hypothesis": "い"},
            {"reference": "あいうえお", "hypothesis": "あいうえお"},
        ]
    )
    assert report["cer"] == pytest.approx(1 / 6)


@pytest.mark.parametrize("hypothesis,expected", [("", 0), ("幻覚", None)])
def test_empty_reference(hypothesis, expected):
    report = evaluate([{"reference": "", "hypothesis": hypothesis}], max_cer=1.0)
    assert report["cer"] == expected
    assert report["empty_reference_insertions"] == len(hypothesis)
    assert report["passed"] is (not hypothesis)
    json.dumps(report, allow_nan=False)


def test_repetition_can_exceed_one_hundred_percent_error():
    report = evaluate([{"reference": "ら", "hypothesis": "らららら"}], max_cer=0.5)
    assert report["cer"] == 3
    assert report["passed"] is False


@pytest.mark.parametrize(
    "a,b,distance", [("abc", "ac", 1), ("", "ab", 2), ("ab", "", 2), ("abc", "xyz", 3)]
)
def test_edit_distance(a, b, distance):
    assert edit_distance(a, b) == distance


def test_work_budget_and_invalid_threshold():
    with pytest.raises(ValueError):
        edit_distance("a" * 3000, "b" * 3000)
    with pytest.raises(ValueError):
        evaluate([{"reference": "", "hypothesis": ""}], max_cer=float("nan"))
    with pytest.raises(ValueError):
        evaluate([{"id": "x", "reference": "", "hypothesis": ""}] * 2)


def test_cli_quality_failure_exit_code(tmp_path):
    source = tmp_path / "pairs.json"
    output = tmp_path / "metrics.json"
    source.write_text(
        json.dumps([{"reference": "青い空", "hypothesis": "青い海"}]), encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_karaoke_analysis.lyric_evaluator",
            str(source),
            "--output",
            str(output),
            "--max-cer",
            "0.1",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(output.read_text())["passed"] is False
