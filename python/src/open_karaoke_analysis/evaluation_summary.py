"""Versioned, aggregate-only evaluation results for comparing model runs."""

import hashlib
import math
import re


def _name(value: str, label: str) -> str:
    """
    {
      責務: [_name: dataset・modelの名前や版が空でない128文字以下の文字列か検証する]
      処理: [1: 文字列か確認する, 2: 空白のみでなく長さが上限内か確認する]
      引数: [value: 検証対象, label: エラー表示名]
      戻り値: [検証済みの文字列]
      エラー: [ValueError: 文字列でない、空、または長すぎる]
    }
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{label} must be a nonempty string of at most 128 characters")
    return value


def lyric_summary(
    report: dict,
    *,
    dataset_id: str,
    dataset_version: str,
    dataset_sha256: str,
    model_name: str,
    model_version: str,
    evaluation_kind: str,
    reference_sha256: str | None = None,
    detail_sha256: str | None = None,
) -> dict:
    """
    {
      責務: [lyric_summary: 歌詞評価レポートを、モデル比較用の集計だけの版付き要約へ変換する]
      処理: [
        1: dataset・model・SHA-256・評価種別のメタデータを検証する,
        2: 歌詞評価レポートの種別・版・指標・品質ゲート・集計値(率との整合を含む)・正規化を検証する,
        3: 個別の歌詞を含めず、集計値と設定だけの要約を組み立てる
      ]
      引数: [
        report: 歌詞evaluatorのレポート,
        dataset_id: dataset識別子,
        dataset_version: datasetの版,
        dataset_sha256: 評価入力のSHA-256,
        model_name: モデル名,
        model_version: モデルの版,
        evaluation_kind: synthetic または real_singing,
        reference_sha256: 任意の参照集合hash。省略した旧形式では参照集合を比較できない,
        detail_sha256: 任意。対で書く詳細レポートの生バイトのSHA-256。検証側が組の不整合を検出できる
      ]
      戻り値: [format_version 1の要約dict]
      エラー: [ValueError: メタデータやレポートが契約を満たさない]
    }
    """
    for label, value in (
        ("dataset_id", dataset_id),
        ("dataset_version", dataset_version),
        ("model_name", model_name),
        ("model_version", model_version),
    ):
        _name(value, label)
    if not isinstance(dataset_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", dataset_sha256) is None:
        raise ValueError("dataset_sha256 must be a lowercase SHA-256 digest")
    if reference_sha256 is not None and (
        not isinstance(reference_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", reference_sha256) is None
    ):
        raise ValueError("reference_sha256 must be a lowercase SHA-256 digest")
    if detail_sha256 is not None and (
        not isinstance(detail_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", detail_sha256) is None
    ):
        raise ValueError("detail_sha256 must be a lowercase SHA-256 digest")
    if evaluation_kind not in ("synthetic", "real_singing"):
        raise ValueError("evaluation_kind must be synthetic or real_singing")
    if not isinstance(report, dict) or report.get("evaluator") != "lyric-transcription":
        raise ValueError("Expected a lyric transcription report")
    if type(report.get("version")) is not int or report["version"] != 1:
        raise ValueError("Unsupported lyric evaluator version")

    if any(key not in report for key in ("cer", "wer")):
        raise ValueError("Missing lyric evaluation metrics")
    metrics = {key: report[key] for key in ("cer", "wer")}
    if any(
        value is not None
        and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        )
        for value in metrics.values()
    ):
        raise ValueError("Evaluation metrics must be finite numbers or null")
    applied, passed = report.get("quality_gate_applied"), report.get("passed")
    if type(applied) is not bool or (type(passed) is not bool and passed is not None):
        raise ValueError("Invalid quality gate result")
    if applied != (passed is not None):
        raise ValueError("Quality gate state is inconsistent")
    threshold = report.get("max_cer")
    if applied != (threshold is not None):
        raise ValueError("Quality gate threshold is inconsistent")
    if threshold is not None and (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 10
    ):
        raise ValueError("Invalid CER threshold")
    totals = report.get("totals")
    required_counts = ("char_errors", "reference_chars", "word_errors", "reference_words")
    if not isinstance(totals, dict) or any(
        type(totals.get(key)) is not int or totals[key] < 0 for key in required_counts
    ):
        raise ValueError("Invalid lyric evaluation totals")
    # 率は集計値で決まる(分母0は誤りも0なら0.0、それ以外はnull)。矛盾した要約を作らない。
    for rate, errors, count in (
        ("cer", "char_errors", "reference_chars"),
        ("wer", "word_errors", "reference_words"),
    ):
        if totals[count]:
            expected = totals[errors] / totals[count]
        else:
            expected = 0.0 if totals[errors] == 0 else None
        actual = metrics[rate]
        if (expected is None) != (actual is None) or (
            expected is not None and abs(actual - expected) > 1e-9
        ):
            raise ValueError(f"{rate} is inconsistent with the lyric evaluation totals")
    insertions = report.get("empty_reference_insertions")
    # 空の参照への挿入は文字誤りの一部なので、総数を超えない(lyric_regressionと同じ不変条件)。
    if type(insertions) is not int or not 0 <= insertions <= totals["char_errors"]:
        raise ValueError("Invalid empty-reference insertion count")
    if applied:
        # 評価器の合否の定義: CERが閾値以下で、空の参照への挿入がないこと。
        cer = metrics["cer"]
        if passed is not (cer is not None and cer <= threshold and insertions == 0):
            raise ValueError("Quality gate result is inconsistent with the lyric metrics")
    if report.get("normalization") not in ("NFKC+casefold+remove-punctuation", "none"):
        raise ValueError("Invalid lyric normalization")
    if report.get("wer_tokenizer") != "whitespace" or report.get("cer_whitespace") != "removed":
        raise ValueError("Unsupported lyric tokenization")
    dataset = {"id": dataset_id, "version": dataset_version, "sha256": dataset_sha256}
    if reference_sha256 is not None:
        dataset["reference_sha256"] = reference_sha256
    summary = {
        "format_version": 1,
        "evaluation_kind": evaluation_kind,
        "evaluator": {"name": "lyric-transcription", "version": 1},
        "dataset": dataset,
        "model": {"name": model_name, "version": model_version},
        "configuration": {
            "normalization": report["normalization"],
            "wer_tokenizer": "whitespace",
            "cer_whitespace": "removed",
        },
        "metrics": {
            **metrics,
            **{key: totals[key] for key in required_counts},
            "empty_reference_insertions": insertions,
        },
        "gate": {"applied": applied, "passed": passed, "thresholds": {"max_cer": threshold}},
    }
    if detail_sha256 is not None:
        summary["detail_sha256"] = detail_sha256
    return summary


def verify_detail_binding(summary: dict, detail_bytes: bytes) -> None:
    """
    {
      責務: [verify_detail_binding: 要約と詳細レポートが同じ実行で書かれた組か検証する]
      処理: [1: 要約のdetail_sha256を確認する, 2: 詳細レポートの生バイトのSHA-256と照合する]
      引数: [summary: 保存済みの要約, detail_bytes: 保存済みの詳細レポートの生バイト]
      戻り値: []
      エラー: [ValueError: 要約にdetail_sha256がない(旧形式)、または詳細レポートと一致しない]
    }
    """
    expected = summary.get("detail_sha256") if isinstance(summary, dict) else None
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("Summary does not record the detail report hash")
    if not isinstance(detail_bytes, (bytes, bytearray, memoryview)):
        raise ValueError("detail_bytes must be bytes-like")
    if hashlib.sha256(detail_bytes).hexdigest() != expected:
        raise ValueError("Detail report does not match the summary (mismatched pair)")
