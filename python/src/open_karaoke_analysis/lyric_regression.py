"""Compare CER summaries only when their reference and evaluation settings match."""

import argparse
import json
from fractions import Fraction
from pathlib import Path

from .adapters import atomic_json
from .evaluation_summary import lyric_summary


def _validated_summary(value: dict) -> dict:
    """{
    責務: [保存済み歌詞サマリーの形式と集計の整合性を検証する]
    処理: [1: 共通形式へ再構成する, 2: 件数・率・絶対閾値判定を照合する]
    引数: [value: 非信頼のJSONオブジェクト]
    戻り値: [検証済みサマリー]
    }"""
    try:
        if type(value["format_version"]) is not int or value["format_version"] != 1:
            raise ValueError("Unsupported summary version")
        dataset, model, config = value["dataset"], value["model"], value["configuration"]
        metrics, gate = value["metrics"], value["gate"]
        counts = ("char_errors", "reference_chars", "word_errors", "reference_words")
        canonical = lyric_summary(
            {
                **config,
                "evaluator": value["evaluator"]["name"],
                "version": value["evaluator"]["version"],
                "cer": metrics["cer"],
                "wer": metrics["wer"],
                "totals": {key: metrics[key] for key in counts},
                "empty_reference_insertions": metrics["empty_reference_insertions"],
                "quality_gate_applied": gate["applied"],
                "passed": gate["passed"],
                "max_cer": gate["thresholds"]["max_cer"],
            },
            dataset_id=dataset["id"],
            dataset_version=dataset["version"],
            dataset_sha256=dataset["sha256"],
            reference_sha256=dataset["reference_sha256"],
            model_name=model["name"],
            model_version=model["version"],
            evaluation_kind=value["evaluation_kind"],
        )
        if canonical != value:
            raise ValueError("Summary fields do not match the supported schema")
        if metrics["empty_reference_insertions"] > metrics["char_errors"]:
            raise ValueError("Insertions exceed total character errors")
        for rate, errors, count in (
            ("cer", "char_errors", "reference_chars"),
            ("wer", "word_errors", "reference_words"),
        ):
            expected = (
                metrics[errors] / metrics[count]
                if metrics[count]
                else (0.0 if metrics[errors] == 0 else None)
            )
            if metrics[rate] != expected:
                raise ValueError(f"{rate} does not match aggregate counts")
        if gate["applied"] and gate["passed"] != (
            metrics["cer"] is not None
            and metrics["cer"] <= gate["thresholds"]["max_cer"]
            and metrics["empty_reference_insertions"] == 0
        ):
            raise ValueError("Absolute quality gate is inconsistent")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("Invalid or incomplete lyric summary") from error
    return canonical


def compare(baseline: dict, candidate: dict, *, max_cer_increase: float = 0.0) -> dict:
    """{
    責務: [同じ条件の歌詞CERを比較し回帰と絶対品質の失敗を返す]
    処理: [1: 入力と比較条件を検証する, 2: 整数集計から増分を厳密に比較する]
    引数: [baseline: 基準サマリー, candidate: 候補サマリー, max_cer_increase: 許容増分]
    戻り値: [回帰ゲートの集計結果。比較不能ならValueError]
    }"""
    if type(max_cer_increase) not in (int, float):
        raise ValueError("max_cer_increase must be a finite nonnegative number")
    try:
        tolerance = Fraction(str(max_cer_increase))
    except (ValueError, OverflowError) as error:
        raise ValueError("max_cer_increase must be a finite nonnegative number") from error
    if tolerance < 0:
        raise ValueError("max_cer_increase must be nonnegative")
    baseline, candidate = _validated_summary(baseline), _validated_summary(candidate)
    for key in ("evaluation_kind", "evaluator", "configuration"):
        if baseline[key] != candidate[key]:
            raise ValueError(f"Incompatible {key}")
    for key in ("id", "version", "reference_sha256"):
        if baseline["dataset"][key] != candidate["dataset"][key]:
            raise ValueError(f"Incompatible dataset.{key}")
    for key in ("reference_chars", "reference_words"):
        if baseline["metrics"][key] != candidate["metrics"][key]:
            raise ValueError(f"Incompatible {key}")
    if any(item["metrics"]["cer"] is None for item in (baseline, candidate)):
        raise ValueError("CER is unavailable; comparison cannot pass")
    before, after = (
        Fraction(item["metrics"]["char_errors"], item["metrics"]["reference_chars"] or 1)
        for item in (baseline, candidate)
    )
    regression_passed = after - before <= tolerance
    return {
        "format_version": 1,
        "evaluator": "lyric-cer-regression",
        "dataset": baseline["dataset"],
        "configuration": baseline["configuration"],
        "evaluation_kind": baseline["evaluation_kind"],
        "reference_chars": baseline["metrics"]["reference_chars"],
        "baseline": {
            "model": baseline["model"],
            "input_sha256": baseline["dataset"]["sha256"],
            "cer": float(before),
            "char_errors": baseline["metrics"]["char_errors"],
        },
        "candidate": {
            "model": candidate["model"],
            "input_sha256": candidate["dataset"]["sha256"],
            "cer": float(after),
            "char_errors": candidate["metrics"]["char_errors"],
            "quality_gate": candidate["gate"],
        },
        "cer_increase": float(after - before),
        "max_cer_increase": max_cer_increase,
        "regression_passed": regression_passed,
        "passed": regression_passed and candidate["gate"]["passed"] is not False,
    }


def _unique_object(pairs: list[tuple]) -> dict:
    """{
    責務: [JSONの重複キーを拒否する]
    処理: [1: 出現済みキーを調べる, 2: オブジェクトを作る]
    引数: [pairs: JSONのキーと値]
    戻り値: [重複のないオブジェクト]
    }"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _load(path: Path) -> dict:
    """{
    責務: [比較用サマリーをサイズ制限付きで読む]
    処理: [1: 64KiB以内を読む, 2: JSONを解釈する]
    引数: [path: サマリーの保存先]
    戻り値: [未検証のJSON値]
    }"""
    with path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("Summary exceeds 64 KiB")
    return json.loads(raw, object_pairs_hook=_unique_object)


def main() -> int:
    """{
    責務: [CER比較のCLIと終了コードを提供する]
    処理: [1: 入力を読む, 2: 比較する, 3: 集計を書き出し成否を返す]
    引数: []
    戻り値: [0: 合格, 1: 回帰または絶対閾値不合格。入力不正は2で終了]
    }"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cer-increase", type=float, default=0.0)
    args = parser.parse_args()
    try:
        if args.output.resolve() in (args.baseline.resolve(), args.candidate.resolve()):
            raise ValueError("Output must differ from both inputs")
        result = compare(
            _load(args.baseline), _load(args.candidate), max_cer_increase=args.max_cer_increase
        )
        atomic_json(args.output, result)
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
