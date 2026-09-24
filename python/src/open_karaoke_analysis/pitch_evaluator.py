"""Synthetic F0 detector/ensemble regression evaluator."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from .pitch_detectors import (
    NormalizedAutocorrelationDetector,
    PitchObservation,
    YinDetector,
)
from .pitch_fusion import cents_between, fuse_frame

RATE = 48_000
WINDOW_SAMPLES = 1_920
DEFAULT_VOICED_THRESHOLD = 0.55


def _tone(frequency: float, seed: int) -> np.ndarray:
    time = np.arange(WINDOW_SAMPLES, dtype=np.float64) / RATE
    rng = np.random.default_rng(seed)
    return (
        0.55 * np.sin(2.0 * math.pi * frequency * time)
        + 0.22 * np.sin(2.0 * math.pi * frequency * 2.0 * time)
        + 0.10 * np.sin(2.0 * math.pi * frequency * 3.0 * time)
        + rng.normal(0.0, 0.002, WINDOW_SAMPLES)
    )


def _prediction(observation: PitchObservation, threshold: float) -> float | None:
    if observation.hz is None or observation.voiced_probability < threshold:
        return None
    return observation.hz


def evaluate(reference: list[float | None], predicted: list[float | None]) -> dict:
    if len(reference) != len(predicted) or not reference:
        raise ValueError("Reference and prediction timelines must have the same nonzero length")

    voiced_reference = 0
    unvoiced_reference = 0
    voiced_hits = 0
    false_voiced = 0
    cents_errors: list[float] = []
    gross_errors = 0
    octave_errors = 0

    for expected, actual in zip(reference, predicted, strict=True):
        if expected is None:
            unvoiced_reference += 1
            false_voiced += actual is not None
            continue

        voiced_reference += 1
        if actual is None:
            gross_errors += 1
            continue

        voiced_hits += 1
        error = abs(cents_between(actual, expected))
        cents_errors.append(error)
        if error > 50.0:
            gross_errors += 1
        octave_count = int(round(error / 1200.0))
        if octave_count >= 1 and abs(error - octave_count * 1200.0) <= 100.0:
            octave_errors += 1

    return {
        "frames": len(reference),
        "voiced_reference_frames": voiced_reference,
        "unvoiced_reference_frames": unvoiced_reference,
        "voiced_recall": voiced_hits / voiced_reference if voiced_reference else None,
        "unvoiced_false_positive_rate": (
            false_voiced / unvoiced_reference if unvoiced_reference else None
        ),
        "median_abs_cents": float(np.median(cents_errors)) if cents_errors else None,
        "p95_abs_cents": (float(np.percentile(cents_errors, 95)) if cents_errors else None),
        "gross_pitch_error_rate": gross_errors / voiced_reference if voiced_reference else None,
        "octave_error_rate": octave_errors / voiced_reference if voiced_reference else None,
    }


def synthetic_benchmark() -> dict:
    frequencies = [float(item) for item in np.geomspace(70.0, 1000.0, 24)]
    reference: list[float | None] = frequencies + [None] * 8
    detectors = [YinDetector(), NormalizedAutocorrelationDetector()]
    by_detector: dict[str, list[float | None]] = {detector.id: [] for detector in detectors}
    ensemble: list[float | None] = []
    previous_hz: float | None = None

    for index, expected in enumerate(reference):
        frame = (
            _tone(expected, 1000 + index)
            if expected is not None
            else np.zeros(WINDOW_SAMPLES, dtype=np.float64)
        )
        observations = [
            detector.detect(frame, RATE, 55.0, 1760.0, 0.0001) for detector in detectors
        ]
        for observation in observations:
            by_detector[observation.detector].append(
                _prediction(observation, DEFAULT_VOICED_THRESHOLD)
            )
        selected, _, _, _ = fuse_frame(
            observations,
            previous_hz,
            55.0,
            1760.0,
            DEFAULT_VOICED_THRESHOLD,
        )
        ensemble.append(selected)
        if selected is not None:
            previous_hz = selected

    octave_fixture = [
        [
            PitchObservation("yin", 220.0, 0.99, 0.99, 1.0, 0.4, {}),
            PitchObservation("nacf", 220.0, 0.90, 0.90, 0.85, 0.4, {}),
        ],
        [
            PitchObservation("yin", 440.0, 0.55, 0.75, 1.0, 0.4, {}),
            PitchObservation("nacf", 220.0, 0.95, 0.95, 0.85, 0.4, {}),
        ],
        [
            PitchObservation("yin", 220.0, 0.99, 0.99, 1.0, 0.4, {}),
            PitchObservation("nacf", 220.0, 0.90, 0.90, 0.85, 0.4, {}),
        ],
    ]
    recovered: list[float | None] = []
    previous = None
    evidence = []
    for observations in octave_fixture:
        selected, _, _, frame_evidence = fuse_frame(
            observations, previous, 55.0, 1760.0, DEFAULT_VOICED_THRESHOLD
        )
        recovered.append(selected)
        evidence.append(frame_evidence)
        if selected is not None:
            previous = selected

    return {
        "dataset": "generated-harmonic-tone-sweep-v1",
        "sample_rate": RATE,
        "window_samples": WINDOW_SAMPLES,
        "external_recordings": False,
        "real_singing_accuracy_verified": False,
        "confidence_calibrated": False,
        "detectors": {name: evaluate(reference, values) for name, values in by_detector.items()},
        "ensemble": evaluate(reference, ensemble),
        "octave_recovery_fixture": {
            "reference_hz": [220.0, 220.0, 220.0],
            "predicted_hz": recovered,
            "passed": all(
                actual is not None and abs(cents_between(actual, 220.0)) < 50.0
                for actual in recovered
            ),
            "evidence": evidence,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-ensemble-median-cents", type=float)
    parser.add_argument("--max-ensemble-gross-rate", type=float)
    arguments = parser.parse_args()

    report = synthetic_benchmark()
    content = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(content, encoding="utf-8", newline="\n")
    else:
        print(content, end="")

    ensemble = report["ensemble"]
    if arguments.max_ensemble_median_cents is not None and (
        ensemble["median_abs_cents"] is None
        or ensemble["median_abs_cents"] > arguments.max_ensemble_median_cents
    ):
        return 1
    if arguments.max_ensemble_gross_rate is not None and (
        ensemble["gross_pitch_error_rate"] is None
        or ensemble["gross_pitch_error_rate"] > arguments.max_ensemble_gross_rate
    ):
        return 1
    if not report["octave_recovery_fixture"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
