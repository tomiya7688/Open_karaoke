"""Alignment boundary error/coverage evaluator; unavailable timings never count as zero error."""

import argparse
import json
import math
from pathlib import Path

from .adapters import atomic_json


def _span(value: dict) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise ValueError("A span must be an object")
    start, end = value.get("start_sample"), value.get("end_sample")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= 172_800_000:
        raise ValueError("Expected integer 48 kHz sample spans")
    return start, end


def evaluate(dataset: dict, *, max_mean_ms=None, max_p95_ms=None, min_coverage=None) -> dict:
    for limit in (max_mean_ms, max_p95_ms):
        if limit is not None and (isinstance(limit, bool) or not math.isfinite(limit) or limit < 0):
            raise ValueError("Error thresholds must be finite and nonnegative")
    if min_coverage is not None and (
        isinstance(min_coverage, bool)
        or not math.isfinite(min_coverage)
        or not 0 <= min_coverage <= 1
    ):
        raise ValueError("Coverage must be between zero and one")
    if (
        not isinstance(dataset, dict)
        or not isinstance(dataset.get("dataset_version"), str)
        or not dataset["dataset_version"]
        or not isinstance(dataset.get("items"), list)
        or not 0 < len(dataset["items"]) <= 10_000
    ):
        raise ValueError("Expected a named, nonempty, bounded alignment dataset")
    ids, errors, missing = set(), [], []
    for item in dataset["items"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid evaluator row")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("Evaluator IDs must be nonempty and unique")
        ids.add(identifier)
        expected = _span(item.get("reference"))
        actual = item.get("hypothesis")
        if actual is None:
            missing.append(identifier)
            continue
        observed = _span(actual)
        errors.extend(abs(a - b) / 48 for a, b in zip(expected, observed, strict=True))
    errors.sort()
    mean = sum(errors) / len(errors) if errors else None
    if errors:
        position = 0.95 * (len(errors) - 1)
        low, high = math.floor(position), math.ceil(position)
        p95 = errors[low] + (errors[high] - errors[low]) * (position - low)
    else:
        p95 = None
    coverage = (len(ids) - len(missing)) / len(ids)
    applied = any(value is not None for value in (max_mean_ms, max_p95_ms, min_coverage))
    required_coverage = (1.0 if min_coverage is None else min_coverage) if applied else None
    passed = not applied or (
        bool(errors)
        and coverage >= required_coverage
        and (max_mean_ms is None or mean <= max_mean_ms)
        and (max_p95_ms is None or p95 <= max_p95_ms)
    )
    return {
        "evaluator": "lyric-alignment",
        "version": 1,
        "sample_rate": 48_000,
        "dataset_version": dataset["dataset_version"],
        "reference_units": len(ids),
        "matched_units": len(ids) - len(missing),
        "missing_ids": missing,
        "coverage": coverage,
        "mean_boundary_error_ms": mean,
        "p95_boundary_error_ms": p95,
        "p95_method": "linear",
        "quality_gate_applied": applied,
        "passed": passed,
        "thresholds": {
            "max_mean_ms": max_mean_ms,
            "max_p95_ms": max_p95_ms,
            "min_coverage": required_coverage,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-mean-ms", type=float)
    parser.add_argument("--max-p95-ms", type=float)
    parser.add_argument("--min-coverage", type=float)
    args = parser.parse_args()
    try:
        with args.dataset.open("rb") as stream:
            raw = stream.read(4_194_305)
        if len(raw) > 4_194_304:
            raise ValueError("Dataset is too large")
        result = evaluate(
            json.loads(raw),
            max_mean_ms=args.max_mean_ms,
            max_p95_ms=args.max_p95_ms,
            min_coverage=args.min_coverage,
        )
        atomic_json(args.output, result)
        return 0 if result["passed"] else 1
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Invalid alignment evaluation: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
