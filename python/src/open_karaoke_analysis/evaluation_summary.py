"""Versioned, aggregate-only evaluation results for comparing model runs."""

import math
import re


def _name(value: str, label: str) -> str:
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
) -> dict:
    for label, value in (
        ("dataset_id", dataset_id),
        ("dataset_version", dataset_version),
        ("model_name", model_name),
        ("model_version", model_version),
    ):
        _name(value, label)
    if not isinstance(dataset_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", dataset_sha256) is None:
        raise ValueError("dataset_sha256 must be a lowercase SHA-256 digest")
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
    insertions = report.get("empty_reference_insertions")
    if type(insertions) is not int or insertions < 0:
        raise ValueError("Invalid empty-reference insertion count")
    if report.get("normalization") not in ("NFKC+casefold+remove-punctuation", "none"):
        raise ValueError("Invalid lyric normalization")
    if report.get("wer_tokenizer") != "whitespace" or report.get("cer_whitespace") != "removed":
        raise ValueError("Unsupported lyric tokenization")
    return {
        "format_version": 1,
        "evaluation_kind": evaluation_kind,
        "evaluator": {"name": "lyric-transcription", "version": 1},
        "dataset": {"id": dataset_id, "version": dataset_version, "sha256": dataset_sha256},
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
