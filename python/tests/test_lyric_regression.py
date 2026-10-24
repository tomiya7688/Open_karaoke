import copy
import json
import subprocess
import sys

import pytest

from open_karaoke_analysis.evaluation_summary import lyric_summary
from open_karaoke_analysis.lyric_evaluator import evaluate
from open_karaoke_analysis.lyric_regression import compare


def _summary(errors=0, *, reference="abcdefghij", max_cer=None):
    """{
    責務: [固定参照に対する合成サマリーを作る]
    処理: [1: 既知の誤り数を作る, 2: 既存評価器と集計形式へ渡す]
    引数: [errors: 置換数, reference: 参照, max_cer: 絶対閾値]
    戻り値: [合成サマリー]
    }"""
    return lyric_summary(
        evaluate(
            [{"reference": reference, "hypothesis": "x" * errors + reference[errors:]}],
            max_cer=max_cer,
        ),
        dataset_id="fixture",
        dataset_version="1",
        dataset_sha256="a" * 64,
        model_name="fixture-model",
        model_version="1",
        evaluation_kind="synthetic",
        reference_sha256="b" * 64,
    )


@pytest.mark.parametrize(
    "before,after,limit,passed",
    [(0, 1, 0, False), (2, 1, 0, True), (3, 4, 0.1, True), (3, 5, 0.1, False)],
)
def test_regression_and_exact_tolerance(before, after, limit, passed):
    """{
    責務: [回帰・改善・小数閾値の境界を確認する]
    処理: [1: 異なるモデルの集計を比較する, 2: 判定と追跡情報を確認する]
    引数: [before: 基準誤り, after: 候補誤り, limit: 増分上限, passed: 期待判定]
    戻り値: []
    }"""
    baseline, candidate = _summary(before), _summary(after)
    candidate["model"]["version"], candidate["dataset"]["sha256"] = "2", "c" * 64
    original = copy.deepcopy(candidate)
    result = compare(baseline, candidate, max_cer_increase=limit)
    assert result["passed"] is passed
    assert result["candidate"]["model"]["version"] == "2"
    assert result["candidate"]["input_sha256"] == "c" * 64
    assert result["baseline"]["char_errors"] == before
    assert result["candidate"]["char_errors"] == after
    assert result["reference_chars"] == 10
    assert result["cer_increase"] == pytest.approx((after - before) / 10)
    assert candidate == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["dataset"].update(reference_sha256="c" * 64),
        lambda s: s["dataset"].update(version="2"),
        lambda s: s["configuration"].update(normalization="none"),
        lambda s: s.update(evaluation_kind="real_singing"),
        lambda s: s["metrics"].update(cer=0.5),
        lambda s: s["metrics"].update(reference_chars=11),
        lambda s: s["dataset"].pop("reference_sha256"),
        lambda s: s.update(format_version=True),
        lambda s: s["metrics"].update(char_errors=True),
        lambda s: s.update(unknown="ignored?"),
        lambda s: s["gate"].update(passed=True),
    ],
)
def test_incompatible_or_inconsistent_summary_is_rejected(mutate):
    """{
    責務: [比較不能な条件や改変された集計を成功扱いにしない]
    処理: [1: 一つの契約を破る, 2: 比較拒否を確認する]
    引数: [mutate: 不正入力への変更]
    戻り値: []
    }"""
    candidate = _summary()
    mutate(candidate)
    with pytest.raises(ValueError):
        compare(_summary(), candidate)


@pytest.mark.parametrize("limit", [True, -0.1, float("nan"), float("inf"), None])
def test_invalid_tolerance_is_rejected(limit):
    """{
    責務: [不正な増分上限を拒否する]
    処理: [1: 上限を渡す, 2: 検証エラーを確認する]
    引数: [limit: 不正な上限]
    戻り値: []
    }"""
    with pytest.raises(ValueError):
        compare(_summary(), _summary(), max_cer_increase=limit)


def test_unavailable_cer_and_failed_absolute_gate_cannot_pass():
    """{
    責務: [未定義CERと既存の絶対閾値の失敗を保持する]
    処理: [1: 空参照への挿入を拒否する, 2: 回帰内でも絶対閾値失敗を返す]
    引数: []
    戻り値: []
    }"""
    with pytest.raises(ValueError, match="unavailable"):
        compare(_summary(reference=""), _summary(1, reference=""))
    result = compare(_summary(3), _summary(3, max_cer=0.2))
    assert result["regression_passed"] is True
    assert result["passed"] is False


@pytest.mark.parametrize(
    "case,code",
    [("pass", 0), ("fail", 1), ("duplicate", 2), ("large", 2), ("overwrite", 2), ("mismatch", 2)],
)
def test_cli_exit_and_input_protection(tmp_path, case, code):
    """{
    責務: [公開CLIの終了値・保存・入力保護を確認する]
    処理: [1: 入力を保存する, 2: 別プロセスで比較する, 3: 結果と原本を照合する]
    引数: [tmp_path: 作業先, case: 入力条件, code: 期待終了値]
    戻り値: []
    }"""
    baseline, candidate, output = (tmp_path / n for n in ("base.json", "next.json", "out.json"))
    baseline.write_text(json.dumps(_summary()), encoding="utf-8")
    data = _summary(1 if case == "fail" else 0)
    if case == "mismatch":
        data["dataset"]["reference_sha256"] = "c" * 64
    raw = json.dumps(data)
    if case == "duplicate":
        raw = raw.replace('"format_version": 1', '"format_version": 1, "format_version": 1')
    if case == "large":
        raw = " " * 65_537
    candidate.write_text(raw, encoding="utf-8")
    original = baseline.read_bytes()
    target = baseline if case == "overwrite" else output
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_karaoke_analysis.lyric_regression",
            str(baseline),
            str(candidate),
            "--output",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == code, result.stderr
    assert baseline.read_bytes() == original
    assert output.exists() is (code != 2)
    if output.exists():
        assert json.loads(output.read_text(encoding="utf-8"))["passed"] is (code == 0)
