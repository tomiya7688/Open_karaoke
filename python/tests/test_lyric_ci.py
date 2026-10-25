import json
import sys
from pathlib import Path

import pytest

from open_karaoke_analysis import lyric_evaluator, lyric_regression

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("case", ["pass", "regression", "absolute", "reference"])
def test_ci_baseline_and_reports(tmp_path, monkeypatch, case):
    """{
    責務: [CIの固定baselineと公開CLIの合否・集計出力を確認する]
    処理: [1: 固定入力の候補を作る, 2: CLIで集計する, 3: 比較の成否と保存を確認する]
    引数: [tmp_path: 出力先, monkeypatch: CLI引数, case: 正常・回帰・絶対閾値・参照変更]
    戻り値: []
    }"""
    pairs = FIXTURES / "lyric_ci_pairs.json"
    if case != "pass":
        data = json.loads(pairs.read_text(encoding="utf-8"))
        if case == "reference":
            data[0]["reference"] = "こんばんは世界"
        else:
            data[0]["hypothesis"] = "間違い"
        pairs = tmp_path / "pairs.json"
        pairs.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    candidate, regression = tmp_path / "candidate.json", tmp_path / "regression.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lyric_evaluator",
            str(pairs),
            "--output",
            str(tmp_path / "detail.json"),
            "--summary-output",
            str(candidate),
            "--dataset-id",
            "lyric-ci",
            "--dataset-version",
            "v1",
            "--model-name",
            "synthetic-fixture",
            "--model-version",
            "v1",
            "--evaluation-kind",
            "synthetic",
            "--max-cer",
            "0.1",
        ],
    )
    if case == "pass":
        lyric_evaluator.main()
        actual = json.loads(candidate.read_text(encoding="utf-8"))
        baseline = json.loads((FIXTURES / "lyric_ci_baseline.json").read_text(encoding="utf-8"))
        assert actual == baseline
        assert actual["metrics"]["reference_chars"] == 17
        assert actual["metrics"]["reference_words"] == 5
    else:
        with pytest.raises(SystemExit) as failure:
            lyric_evaluator.main()
        assert failure.value.code == 1
        assert candidate.exists()  # Absolute failure still permits a regression report.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lyric_regression",
            str(FIXTURES / "lyric_ci_baseline.json"),
            str(candidate),
            "--output",
            str(regression),
            "--max-cer-increase",
            "1" if case == "absolute" else "0",
        ],
    )
    if case == "reference":
        with pytest.raises(SystemExit) as failure:
            lyric_regression.main()
        assert failure.value.code == 2
        assert not regression.exists()
    else:
        assert lyric_regression.main() == (0 if case == "pass" else 1)
        result = json.loads(regression.read_text(encoding="utf-8"))
        assert result["passed"] is (case == "pass")
        assert result["regression_passed"] is (case != "regression")
        assert (
            "utterances" not in result and "reference" not in result and "hypothesis" not in result
        )
