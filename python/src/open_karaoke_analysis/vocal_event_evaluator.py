"""Precision/recall/F1 evaluator for annotated Vocal Event boundaries."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

RATE = 48_000


def evaluate_boundaries(
    reference: list[int],
    predicted: list[int],
    tolerance_samples: int,
) -> dict:
    if tolerance_samples < 0:
        raise ValueError("Boundary tolerance must be non-negative")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in reference
    ):
        raise ValueError("Reference boundaries must be non-negative integer samples")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in predicted
    ):
        raise ValueError("Predicted boundaries must be non-negative integer samples")

    remaining = set(range(len(predicted)))
    errors = []
    true_positive = 0
    for expected in reference:
        candidates = [
            (abs(predicted[index] - expected), index)
            for index in remaining
            if abs(predicted[index] - expected) <= tolerance_samples
        ]
        if not candidates:
            continue
        error, index = min(candidates)
        remaining.remove(index)
        true_positive += 1
        errors.append(error)

    false_positive = len(predicted) - true_positive
    false_negative = len(reference) - true_positive
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    )
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "reference_count": len(reference),
        "predicted_count": len(predicted),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mean_abs_error_ms": (sum(errors) / len(errors) / RATE * 1000.0 if errors else None),
        "max_abs_error_ms": max(errors) / RATE * 1000.0 if errors else None,
    }


def evaluate_dataset(document: dict, tolerance_samples: int) -> dict:
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Annotated boundary dataset requires non-empty cases")

    results = []
    aggregate = {
        "note": {"reference": [], "predicted": []},
        "lyric": {"reference": [], "predicted": []},
    }
    offset = 0
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("Each annotated case requires an id")
        case_result = {"id": case["id"]}
        for kind in ("note", "lyric"):
            boundary = case.get(kind)
            if not isinstance(boundary, dict):
                raise ValueError(f"Case {case['id']} is missing {kind} annotations")
            reference = boundary.get("reference")
            predicted = boundary.get("predicted")
            if not isinstance(reference, list) or not isinstance(predicted, list):
                raise ValueError("Boundary annotations must be lists")
            case_result[kind] = evaluate_boundaries(reference, predicted, tolerance_samples)
            aggregate[kind]["reference"].extend(value + offset for value in reference)
            aggregate[kind]["predicted"].extend(value + offset for value in predicted)
        duration = case.get("duration_samples", RATE)
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ValueError("Case duration must be a positive integer")
        offset += duration + tolerance_samples * 4 + RATE
        results.append(case_result)

    return {
        "dataset_version": document.get("dataset_version", "unknown"),
        "tolerance_samples": tolerance_samples,
        "tolerance_ms": tolerance_samples / RATE * 1000.0,
        "cases": results,
        "aggregate": {
            kind: evaluate_boundaries(
                values["reference"], values["predicted"], tolerance_samples
            )
            for kind, values in aggregate.items()
        },
        "quality_gate_applied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tolerance-ms", type=float, default=30.0)
    parser.add_argument("--min-note-f1", type=float)
    parser.add_argument("--min-lyric-f1", type=float)
    arguments = parser.parse_args()

    if not math.isfinite(arguments.tolerance_ms) or arguments.tolerance_ms < 0:
        return 2
    try:
        document = json.loads(arguments.input.read_text(encoding="utf-8"))
        tolerance_samples = round(arguments.tolerance_ms * RATE / 1000.0)
        report = evaluate_dataset(document, tolerance_samples)
    except (OSError, ValueError, json.JSONDecodeError):
        return 2

    gates = {
        "note": arguments.min_note_f1,
        "lyric": arguments.min_lyric_f1,
    }
    report["quality_gate_applied"] = any(value is not None for value in gates.values())
    report["quality_gate_passed"] = True
    for kind, threshold in gates.items():
        if threshold is None:
            continue
        if not 0.0 <= threshold <= 1.0:
            return 2
        actual = report["aggregate"][kind]["f1"]
        if actual is None or actual < threshold:
            report["quality_gate_passed"] = False

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0 if report["quality_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
