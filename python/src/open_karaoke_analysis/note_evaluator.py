"""Regression evaluator for scoring-note generation."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .note_candidates import HOP_SAMPLES, RATE
from .notes import generate_score_documents


def evaluate_notes(reference: list[dict], predicted: list[dict], tolerance_samples: int) -> dict:
    if tolerance_samples < 0:
        raise ValueError("Tolerance must be non-negative")

    def valid(note: dict) -> bool:
        return (
            isinstance(note, dict)
            and isinstance(note.get("start_sample"), int)
            and not isinstance(note.get("start_sample"), bool)
            and isinstance(note.get("end_sample"), int)
            and not isinstance(note.get("end_sample"), bool)
            and 0 <= note["start_sample"] < note["end_sample"]
            and isinstance(note.get("midi_note"), int)
            and not isinstance(note.get("midi_note"), bool)
            and 0 <= note["midi_note"] <= 127
        )

    if not all(valid(note) for note in [*reference, *predicted]):
        raise ValueError("Notes require valid sample intervals and integer MIDI pitches")

    remaining = set(range(len(predicted)))
    pairs = []
    for expected_index, expected in enumerate(reference):
        candidates = []
        expected_duration = expected["end_sample"] - expected["start_sample"]
        for index in remaining:
            actual = predicted[index]
            onset_error = abs(actual["start_sample"] - expected["start_sample"])
            overlap = max(
                0,
                min(actual["end_sample"], expected["end_sample"])
                - max(actual["start_sample"], expected["start_sample"]),
            )
            overlap_ratio = overlap / max(
                1,
                min(expected_duration, actual["end_sample"] - actual["start_sample"]),
            )
            if onset_error <= tolerance_samples or overlap_ratio >= 0.5:
                candidates.append((onset_error, -overlap_ratio, index))
        if not candidates:
            continue
        _, _, index = min(candidates)
        remaining.remove(index)
        pairs.append((expected_index, index))

    exact_pairs = [
        (reference[expected], predicted[actual])
        for expected, actual in pairs
        if reference[expected]["midi_note"] == predicted[actual]["midi_note"]
    ]
    wrong_pitch_pairs = [
        (reference[expected], predicted[actual])
        for expected, actual in pairs
        if reference[expected]["midi_note"] != predicted[actual]["midi_note"]
    ]
    true_positive = len(exact_pairs)
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
        else 0.0
        if precision is not None and recall is not None
        else None
    )
    onset_errors = [
        abs(actual["start_sample"] - expected["start_sample"]) for expected, actual in exact_pairs
    ]
    offset_errors = [
        abs(actual["end_sample"] - expected["end_sample"]) for expected, actual in exact_pairs
    ]
    octave_errors = sum(
        abs(actual["midi_note"] - expected["midi_note"]) in {12, 24, 36}
        for expected, actual in wrong_pitch_pairs
    )
    temporal_matches = len(pairs)

    return {
        "reference_count": len(reference),
        "predicted_count": len(predicted),
        "temporal_matches": temporal_matches,
        "pitch_true_positive": true_positive,
        "pitch_false_positive": false_positive,
        "pitch_false_negative": false_negative,
        "pitch_precision": precision,
        "pitch_recall": recall,
        "pitch_f1": f1,
        "mean_abs_onset_error_ms": (
            sum(onset_errors) / len(onset_errors) / RATE * 1000.0 if onset_errors else None
        ),
        "mean_abs_offset_error_ms": (
            sum(offset_errors) / len(offset_errors) / RATE * 1000.0 if offset_errors else None
        ),
        "max_abs_onset_error_ms": max(onset_errors) / RATE * 1000.0 if onset_errors else None,
        "max_abs_offset_error_ms": max(offset_errors) / RATE * 1000.0 if offset_errors else None,
        "octave_error_count": octave_errors,
        "octave_error_rate": octave_errors / temporal_matches if temporal_matches else None,
    }


def _case_documents(case: dict) -> tuple[dict, dict]:
    case_id = case.get("id")
    duration = case.get("duration_samples")
    regions = case.get("pitch_regions")
    boundaries = case.get("note_boundaries", [])
    if (
        not isinstance(case_id, str)
        or not case_id
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration <= 0
        or not isinstance(regions, list)
        or not isinstance(boundaries, list)
    ):
        raise ValueError("Regression cases require id, duration and pitch_regions")

    frames = []
    event_frames = []
    frame_count = math.ceil(duration / HOP_SAMPLES)
    for index in range(frame_count):
        start = index * HOP_SAMPLES
        end = min(duration, start + HOP_SAMPLES)
        center = (start + end) // 2
        region = next(
            (
                item
                for item in regions
                if isinstance(item, dict)
                and isinstance(item.get("start_sample"), int)
                and isinstance(item.get("end_sample"), int)
                and item["start_sample"] <= center < item["end_sample"]
            ),
            None,
        )
        midi = None
        if region is not None and region.get("midi") is not None:
            base_midi = region["midi"]
            contour = region.get("contour_cents", [0.0])
            if (
                isinstance(base_midi, bool)
                or not isinstance(base_midi, (int, float))
                or not math.isfinite(float(base_midi))
                or not isinstance(contour, list)
                or not contour
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in contour
                )
            ):
                raise ValueError("Pitch-region contour is invalid")
            relative_index = max(0, (center - region["start_sample"]) // HOP_SAMPLES)
            midi = float(base_midi) + float(contour[relative_index % len(contour)]) / 100.0

        voiced = midi is not None
        frames.append(
            {
                "index": index,
                "start_sample": start,
                "end_sample": end,
                "center_sample": center,
                "f0_hz": None,
                "midi": midi,
                "confidence": 0.95 if voiced else 0.0,
                "voiced_probability": 0.95 if voiced else 0.0,
                "voiced": voiced,
            }
        )
        note_probability = max(
            (
                float(boundary["probability"])
                for boundary in boundaries
                if isinstance(boundary, dict)
                and isinstance(boundary.get("sample"), int)
                and isinstance(boundary.get("probability"), (int, float))
                and abs(boundary["sample"] - center) <= HOP_SAMPLES // 2
            ),
            default=0.0,
        )
        event_frames.append(
            {
                "index": index,
                "start_sample": start,
                "end_sample": end,
                "center_sample": center,
                "features": {},
                "note_boundary_probability": note_probability,
                "lyric_boundary_probability": 0.0,
                "silence_probability": 0.0 if voiced else 1.0,
                "breath_probability": 0.0,
            }
        )

    digest = f"fixture-{case_id}-00000000"
    pitch = {
        "format_version": 1,
        "analysis_version": 1,
        "model": {"id": "fixture-f0", "version": "1"},
        "sample_rate": RATE,
        "window_samples": 1_920,
        "hop_samples": HOP_SAMPLES,
        "duration_samples": duration,
        "input_sha256": digest,
        "frames": frames,
    }
    events = {
        "format_version": 1,
        "analysis_version": 1,
        "model": {"id": "fixture-events", "version": "1"},
        "sample_rate": RATE,
        "window_samples": 1_920,
        "hop_samples": HOP_SAMPLES,
        "duration_samples": duration,
        "input_sha256": digest,
        "frames": event_frames,
        "note_boundaries": boundaries,
        "lyric_boundaries": [],
    }
    return pitch, events


def evaluate_dataset(document: dict, tolerance_samples: int) -> dict:
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Score regression dataset requires non-empty cases")

    results = []
    aggregate_reference = []
    aggregate_predicted = []
    offset = 0
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("reference_notes"), list):
            raise ValueError("Each case requires reference_notes")
        pitch, events = _case_documents(case)
        notes, _, evidence = generate_score_documents(
            pitch,
            events,
            options=case.get("options"),
        )
        reference = case["reference_notes"]
        predicted = notes["notes"]
        results.append(
            {
                "id": case["id"],
                "metrics": evaluate_notes(reference, predicted, tolerance_samples),
                "predicted_notes": predicted,
                "key_estimate": evidence["key_estimate"],
            }
        )
        aggregate_reference.extend(
            [
                {
                    **note,
                    "start_sample": note["start_sample"] + offset,
                    "end_sample": note["end_sample"] + offset,
                }
                for note in reference
            ]
        )
        aggregate_predicted.extend(
            [
                {
                    **note,
                    "start_sample": note["start_sample"] + offset,
                    "end_sample": note["end_sample"] + offset,
                }
                for note in predicted
            ]
        )
        offset += pitch["duration_samples"] + RATE

    return {
        "dataset_version": document.get("dataset_version", "unknown"),
        "description": document.get("description"),
        "real_recordings": bool(document.get("real_recordings", False)),
        "real_singing_accuracy_verified": False,
        "tolerance_samples": tolerance_samples,
        "tolerance_ms": tolerance_samples / RATE * 1000.0,
        "cases": results,
        "aggregate": evaluate_notes(
            aggregate_reference,
            aggregate_predicted,
            tolerance_samples,
        ),
        "quality_gate_applied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tolerance-ms", type=float, default=30.0)
    parser.add_argument("--min-pitch-f1", type=float)
    parser.add_argument("--max-mean-onset-ms", type=float)
    parser.add_argument("--max-mean-offset-ms", type=float)
    parser.add_argument("--max-octave-error-rate", type=float)
    arguments = parser.parse_args()

    if not math.isfinite(arguments.tolerance_ms) or arguments.tolerance_ms < 0:
        return 2
    try:
        document = json.loads(arguments.input.read_text(encoding="utf-8"))
        report = evaluate_dataset(
            document,
            round(arguments.tolerance_ms * RATE / 1000.0),
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return 2

    thresholds = {
        "min_pitch_f1": arguments.min_pitch_f1,
        "max_mean_onset_ms": arguments.max_mean_onset_ms,
        "max_mean_offset_ms": arguments.max_mean_offset_ms,
        "max_octave_error_rate": arguments.max_octave_error_rate,
    }
    report["quality_gate_applied"] = any(value is not None for value in thresholds.values())
    report["quality_gate_passed"] = True
    aggregate = report["aggregate"]

    if arguments.min_pitch_f1 is not None:
        if not 0.0 <= arguments.min_pitch_f1 <= 1.0:
            return 2
        if aggregate["pitch_f1"] is None or aggregate["pitch_f1"] < arguments.min_pitch_f1:
            report["quality_gate_passed"] = False
    for name, metric in (
        ("max_mean_onset_ms", "mean_abs_onset_error_ms"),
        ("max_mean_offset_ms", "mean_abs_offset_error_ms"),
    ):
        threshold = thresholds[name]
        if threshold is not None:
            if not math.isfinite(threshold) or threshold < 0:
                return 2
            if aggregate[metric] is None or aggregate[metric] > threshold:
                report["quality_gate_passed"] = False
    if arguments.max_octave_error_rate is not None:
        if not 0.0 <= arguments.max_octave_error_rate <= 1.0:
            return 2
        if (
            aggregate["octave_error_rate"] is None
            or aggregate["octave_error_rate"] > arguments.max_octave_error_rate
        ):
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
