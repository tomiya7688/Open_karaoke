"""Confidence, octave-consistency and temporal-continuity fusion for F0 candidates."""

from __future__ import annotations

import math

from .pitch_detectors import PitchObservation


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def cents_between(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        raise ValueError("Pitch values must be positive")
    return 1200.0 * math.log2(a / b)


def fuse_frame(
    observations: list[PitchObservation],
    previous_hz: float | None,
    min_hz: float,
    max_hz: float,
    voiced_threshold: float,
) -> tuple[float | None, float, float, dict]:
    """Choose one F0 using octave-related candidates and previous-frame continuity.

    The score is deliberately transparent rather than learned. Detector confidence and
    voiced probability are treated as uncalibrated evidence, not probabilities of correctness.
    """

    valid = [
        observation
        for observation in observations
        if observation.hz is not None and observation.voiced_probability > 0.05
    ]
    if not valid:
        return (
            None,
            0.0,
            0.0,
            {
                "mode": "ensemble",
                "reason": "no_voiced_candidates",
                "states": [],
                "corrections": {},
            },
        )

    states: set[float] = set()
    for observation in valid:
        assert observation.hz is not None
        for octave_shift in range(-2, 3):
            candidate = observation.hz * (2.0**octave_shift)
            if min_hz <= candidate <= max_hz:
                states.add(round(candidate, 9))

    detector_weight = sum(observation.reliability * observation.confidence for observation in valid)
    voiced_probability = (
        sum(
            observation.reliability * observation.confidence * observation.voiced_probability
            for observation in valid
        )
        / detector_weight
        if detector_weight > 1e-12
        else 0.0
    )
    total_support_weight = sum(
        observation.reliability * observation.confidence * observation.voiced_probability
        for observation in valid
    )

    scored: list[dict] = []
    for state in states:
        support = 0.0
        shift_penalty = 0.0
        direct_anchor = 0.0
        for observation in valid:
            assert observation.hz is not None
            octave_shift = int(round(math.log2(state / observation.hz)))
            octave_shift = min(2, max(-2, octave_shift))
            adjusted = observation.hz * (2.0**octave_shift)
            error_cents = abs(cents_between(state, adjusted))
            weight = (
                observation.reliability * observation.confidence * observation.voiced_probability
            )
            similarity = math.exp(-0.5 * (error_cents / 45.0) ** 2)
            support += weight * similarity
            shift_penalty += weight * 0.08 * abs(octave_shift)
            if abs(cents_between(state, observation.hz)) < 50.0:
                direct_anchor += 0.08 * weight

        continuity_penalty = 0.0
        if previous_hz is not None:
            continuity_penalty = 0.55 * min(abs(cents_between(state, previous_hz)) / 600.0, 2.0)
        score = support - shift_penalty + direct_anchor - continuity_penalty
        scored.append(
            {
                "hz": state,
                "score": score,
                "support": support,
                "shift_penalty": shift_penalty,
                "direct_anchor": direct_anchor,
                "continuity_penalty": continuity_penalty,
            }
        )

    scored.sort(key=lambda item: (item["score"], item["support"], item["hz"]), reverse=True)
    chosen = scored[0]
    confidence = (
        _clamp(chosen["support"] / total_support_weight) if total_support_weight > 1e-12 else 0.0
    )
    chosen_hz = float(chosen["hz"]) if voiced_probability >= voiced_threshold else None

    corrections: dict[str, int | None] = {}
    if chosen_hz is not None:
        for observation in valid:
            assert observation.hz is not None
            shift = int(round(math.log2(chosen_hz / observation.hz)))
            shifted = observation.hz * (2.0**shift)
            corrections[observation.detector] = (
                shift if abs(cents_between(chosen_hz, shifted)) <= 100.0 else None
            )

    return (
        chosen_hz,
        confidence,
        _clamp(voiced_probability),
        {
            "mode": "ensemble",
            "previous_hz": previous_hz,
            "selected_state_hz": float(chosen["hz"]),
            "selected_score": chosen["score"],
            "confidence_calibrated": False,
            "voiced_probability_calibrated": False,
            "corrections": corrections,
            "states": scored[: min(8, len(scored))],
        },
    )
