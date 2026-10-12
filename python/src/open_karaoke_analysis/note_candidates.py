"""Candidate generation for scoring-note recovery."""

from __future__ import annotations

import math
from collections.abc import Sequence

RATE = 48_000
HOP_SAMPLES = 480


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("Median requires at least one value")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def weighted_median(values: Sequence[tuple[float, float]]) -> float:
    weighted = sorted((float(value), max(0.0, float(weight))) for value, weight in values)
    if not weighted:
        raise ValueError("Weighted median requires at least one value")
    total = sum(weight for _, weight in weighted)
    if total <= 1e-12:
        return _median([value for value, _ in weighted])
    target = total / 2.0
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if cumulative >= target:
            return value
    return weighted[-1][0]


def _frame_midi(frame: dict, voiced_threshold: float) -> float | None:
    midi = frame.get("midi")
    voiced = frame.get("voiced_probability", 0.0)
    if (
        isinstance(midi, (int, float))
        and not isinstance(midi, bool)
        and math.isfinite(float(midi))
        and isinstance(voiced, (int, float))
        and not isinstance(voiced, bool)
        and float(voiced) >= voiced_threshold
    ):
        return float(midi)
    return None


def _window_median(
    frames: Sequence[dict],
    sample: int,
    *,
    before: bool,
    voiced_threshold: float,
    radius_frames: int = 5,
) -> float | None:
    low = sample - radius_frames * HOP_SAMPLES
    high = sample + radius_frames * HOP_SAMPLES
    values = []
    for frame in frames:
        center = int(frame["center_sample"])
        if before and low <= center < sample:
            midi = _frame_midi(frame, voiced_threshold)
            if midi is not None:
                values.append(midi)
        elif not before and sample <= center < high:
            midi = _frame_midi(frame, voiced_threshold)
            if midi is not None:
                values.append(midi)
    return _median(values) if values else None


def _near_event_strength(event_frames: Sequence[dict], sample: int, field: str) -> float:
    nearest = [
        float(frame.get(field, 0.0))
        for frame in event_frames
        if abs(int(frame["center_sample"]) - sample) <= HOP_SAMPLES
        and isinstance(frame.get(field, 0.0), (int, float))
        and not isinstance(frame.get(field, 0.0), bool)
    ]
    return max(nearest, default=0.0)


def _external_boundaries(external_candidates: Sequence[dict]) -> list[dict]:
    ordered = sorted(
        external_candidates, key=lambda item: (item["start_sample"], item["end_sample"])
    )
    boundaries = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        gap = current["start_sample"] - previous["end_sample"]
        different_pitch = current["midi_note"] != previous["midi_note"]
        strength = min(previous["confidence"], current["confidence"])
        if strength >= 0.60 and (different_pitch or gap >= 2 * HOP_SAMPLES):
            boundaries.append(
                {
                    "sample": current["start_sample"],
                    "probability": strength,
                    "source": "external_candidate_transition",
                    "provider": current.get("provider"),
                }
            )
    return boundaries


def decide_boundaries(
    pitch_frames: Sequence[dict],
    event_frames: Sequence[dict],
    note_boundaries: Sequence[dict],
    external_candidates: Sequence[dict],
    *,
    voiced_threshold: float,
    min_pitch_change_cents: float,
    strong_boundary_probability: float,
) -> list[dict]:
    raw = [
        {
            "sample": int(boundary["sample"]),
            "probability": float(boundary["probability"]),
            "source": "vocal_event",
        }
        for boundary in note_boundaries
    ]
    raw.extend(_external_boundaries(external_candidates))
    by_sample: dict[int, dict] = {}
    for boundary in raw:
        sample = boundary["sample"]
        existing = by_sample.get(sample)
        if existing is None or boundary["probability"] > existing["probability"]:
            by_sample[sample] = boundary

    decisions = []
    for sample, boundary in sorted(by_sample.items()):
        before = _window_median(
            pitch_frames, sample, before=True, voiced_threshold=voiced_threshold
        )
        after = _window_median(
            pitch_frames, sample, before=False, voiced_threshold=voiced_threshold
        )
        pitch_change_cents = (
            abs(after - before) * 100.0 if before is not None and after is not None else None
        )
        silence = _near_event_strength(event_frames, sample, "silence_probability")
        breath = _near_event_strength(event_frames, sample, "breath_probability")
        source = boundary["source"]
        strong = boundary["probability"] >= strong_boundary_probability
        acoustic_break = silence >= 0.55 or breath >= 0.65
        sustained_pitch_change = (
            pitch_change_cents is not None and pitch_change_cents >= min_pitch_change_cents
        )
        external_transition = (
            source == "external_candidate_transition" and boundary["probability"] >= 0.8
        )
        accepted = strong or acoustic_break or sustained_pitch_change or external_transition
        reason = (
            "strong_boundary"
            if strong
            else "silence_or_breath"
            if acoustic_break
            else "sustained_pitch_change"
            if sustained_pitch_change
            else "external_transition"
            if external_transition
            else "expressive_motion_suppressed"
        )
        decisions.append(
            {
                **boundary,
                "accepted": accepted,
                "reason": reason,
                "pitch_change_cents": pitch_change_cents,
                "silence_probability": silence,
                "breath_probability": breath,
            }
        )
    return decisions


def _voiced_runs(
    pitch_frames: Sequence[dict],
    *,
    voiced_threshold: float,
    bridge_unvoiced_samples: int,
) -> list[tuple[int, int]]:
    active = [
        index
        for index, frame in enumerate(pitch_frames)
        if _frame_midi(frame, voiced_threshold) is not None
    ]
    if not active:
        return []
    max_missing = max(0, bridge_unvoiced_samples // HOP_SAMPLES)
    runs = []
    first = previous = active[0]
    for index in active[1:]:
        missing = index - previous - 1
        if missing > max_missing:
            runs.append(
                (
                    int(pitch_frames[first]["start_sample"]),
                    int(pitch_frames[previous]["end_sample"]),
                )
            )
            first = index
        previous = index
    runs.append(
        (
            int(pitch_frames[first]["start_sample"]),
            int(pitch_frames[previous]["end_sample"]),
        )
    )
    return runs


def _segment_hypotheses(
    base_midi: float,
    pitch_confidence: float,
    start_sample: int,
    end_sample: int,
    external_candidates: Sequence[dict],
) -> list[dict]:
    scores: dict[int, float] = {}
    sources: dict[int, set[str]] = {}

    nearest = int(round(base_midi))
    for midi_note in range(max(0, nearest - 1), min(127, nearest + 1) + 1):
        distance_cents = abs(base_midi - midi_note) * 100.0
        score = pitch_confidence * math.exp(-0.5 * (distance_cents / 45.0) ** 2)
        if score > 0.005:
            scores[midi_note] = max(scores.get(midi_note, 0.0), score)
            sources.setdefault(midi_note, set()).add("f0_robust_median")

    duration = end_sample - start_sample
    for external in external_candidates:
        overlap = max(
            0,
            min(end_sample, external["end_sample"]) - max(start_sample, external["start_sample"]),
        )
        if overlap <= 0:
            continue
        denominator = max(1, min(duration, external["end_sample"] - external["start_sample"]))
        overlap_ratio = min(1.0, overlap / denominator)
        if overlap_ratio < 0.25:
            continue
        midi_note = external["midi_note"]
        score = clamp(external["confidence"] * (0.55 + 0.45 * overlap_ratio))
        combined = 1.0 - (1.0 - scores.get(midi_note, 0.0)) * (1.0 - 0.85 * score)
        scores[midi_note] = clamp(combined)
        sources.setdefault(midi_note, set()).add(external.get("provider") or "external")

    if not scores:
        scores[nearest] = pitch_confidence
        sources[nearest] = {"f0_robust_median"}

    return [
        {
            "midi_note": midi_note,
            "score": clamp(score),
            "sources": sorted(sources[midi_note]),
        }
        for midi_note, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def build_note_candidates(
    pitch_frames: Sequence[dict],
    event_frames: Sequence[dict],
    note_boundaries: Sequence[dict],
    external_candidates: Sequence[dict],
    *,
    voiced_threshold: float,
    bridge_unvoiced_samples: int,
    min_pitch_change_cents: float,
    strong_boundary_probability: float,
) -> tuple[list[dict], list[dict], dict]:
    decisions = decide_boundaries(
        pitch_frames,
        event_frames,
        note_boundaries,
        external_candidates,
        voiced_threshold=voiced_threshold,
        min_pitch_change_cents=min_pitch_change_cents,
        strong_boundary_probability=strong_boundary_probability,
    )
    accepted = {item["sample"]: item for item in decisions if item["accepted"]}
    runs = _voiced_runs(
        pitch_frames,
        voiced_threshold=voiced_threshold,
        bridge_unvoiced_samples=bridge_unvoiced_samples,
    )

    candidates = []
    suppressed = sum(not item["accepted"] for item in decisions)
    for run_index, (run_start, run_end) in enumerate(runs):
        split_points = [sample for sample in accepted if run_start < sample < run_end]
        points = [run_start, *sorted(set(split_points)), run_end]
        for start_sample, end_sample in zip(points, points[1:], strict=False):
            frames = [
                frame
                for frame in pitch_frames
                if start_sample <= int(frame["center_sample"]) < end_sample
                and _frame_midi(frame, voiced_threshold) is not None
            ]
            if not frames:
                continue

            trim = 2 if len(frames) >= 15 else 1 if len(frames) >= 7 else 0
            steady = (
                frames[trim : len(frames) - trim]
                if trim and len(frames) > 2 * trim
                else frames
            )
            weighted = []
            confidence_weighted = []
            for frame in steady:
                midi = _frame_midi(frame, voiced_threshold)
                if midi is None:
                    continue
                confidence = float(frame.get("confidence", 0.0))
                voiced_probability = float(frame.get("voiced_probability", 0.0))
                weight = max(0.01, confidence * voiced_probability)
                weighted.append((midi, weight))
                confidence_weighted.append((confidence * voiced_probability, weight))
            if not weighted:
                continue

            base_midi = weighted_median(weighted)
            total_weight = sum(weight for _, weight in weighted)
            mean_support = (
                sum(value * weight for value, weight in confidence_weighted) / total_weight
            )
            deviations = [abs(midi - base_midi) * 100.0 for midi, _ in weighted]
            median_deviation = _median(deviations)
            stability = math.exp(-0.5 * (median_deviation / 65.0) ** 2)
            pitch_confidence = clamp(mean_support * (0.65 + 0.35 * stability))
            spread_cents = (
                (max(midi for midi, _ in weighted) - min(midi for midi, _ in weighted)) * 100.0
            )

            before = accepted.get(start_sample)
            after = accepted.get(end_sample)
            hypotheses = _segment_hypotheses(
                base_midi,
                pitch_confidence,
                start_sample,
                end_sample,
                external_candidates,
            )
            candidates.append(
                {
                    "id": f"candidate-{len(candidates) + 1:04d}",
                    "run_index": run_index,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "base_midi": base_midi,
                    "pitch_confidence": pitch_confidence,
                    "pitch_spread_cents": spread_cents,
                    "steady_frame_count": len(steady),
                    "transition_trimmed_frames": len(frames) - len(steady),
                    "boundary_before_probability": (
                        float(before["probability"]) if before is not None else 1.0
                    ),
                    "boundary_after_probability": (
                        float(after["probability"]) if after is not None else 1.0
                    ),
                    "boundary_before_reason": (
                        before["reason"] if before is not None else "voiced_run_edge"
                    ),
                    "boundary_after_reason": (
                        after["reason"] if after is not None else "voiced_run_edge"
                    ),
                    "hypotheses": hypotheses,
                    "lyric_segment_id": None,
                }
            )

    return (
        candidates,
        decisions,
        {
            "voiced_runs": len(runs),
            "accepted_boundaries": len(accepted),
            "suppressed_boundaries": suppressed,
            "normalization": [
                "robust_median_pitch",
                "transition_frame_trimming",
                "low_sustained-change_boundary_suppression",
            ],
        },
    )
