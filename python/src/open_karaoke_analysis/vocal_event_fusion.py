"""Transparent feature fusion for shared vocal-event boundary probabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

FEATURE_NAMES = (
    "f0_transition",
    "voiced_transition",
    "spectral_flux",
    "energy_delta",
    "acoustic_onset",
    "lyric_timing",
    "note_onset",
    "silence_breath",
)

NOTE_WEIGHTS = {
    "f0_transition": 0.28,
    "voiced_transition": 0.15,
    "spectral_flux": 0.10,
    "energy_delta": 0.08,
    "acoustic_onset": 0.18,
    "lyric_timing": 0.05,
    "note_onset": 0.28,
    "silence_breath": 0.12,
}

LYRIC_WEIGHTS = {
    "f0_transition": 0.04,
    "voiced_transition": 0.12,
    "spectral_flux": 0.08,
    "energy_delta": 0.08,
    "acoustic_onset": 0.12,
    "lyric_timing": 0.42,
    "note_onset": 0.04,
    "silence_breath": 0.16,
}


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _weighted_probability(
    frame_features: Mapping[str, float],
    active_features: Sequence[str],
    availability: Mapping[str, bool],
    weights: Mapping[str, float],
    mode: str,
) -> tuple[float, dict[str, float]]:
    available = [
        name for name in active_features if availability.get(name, False) and weights.get(name, 0) > 0
    ]
    if not available:
        return 0.0, {}
    if mode == "single":
        name = available[0]
        value = clamp01(frame_features[name])
        return value, {name: value}

    denominator = sum(weights[name] for name in available)
    contributions = {
        name: clamp01(frame_features[name]) * weights[name] / denominator for name in available
    }
    return clamp01(sum(contributions.values())), contributions


def fuse_frame(
    frame_features: Mapping[str, float],
    active_features: Sequence[str],
    availability: Mapping[str, bool],
    mode: str,
) -> tuple[float, float, dict]:
    note, note_contributions = _weighted_probability(
        frame_features, active_features, availability, NOTE_WEIGHTS, mode
    )
    lyric, lyric_contributions = _weighted_probability(
        frame_features, active_features, availability, LYRIC_WEIGHTS, mode
    )
    return note, lyric, {
        "mode": mode,
        "active_features": list(active_features),
        "note_contributions": note_contributions,
        "lyric_contributions": lyric_contributions,
        "boundary_probability_calibrated": False,
    }


def pick_boundaries(
    probabilities: Sequence[float],
    centers: Sequence[int],
    threshold: float,
    min_distance_samples: int,
) -> list[dict]:
    if len(probabilities) != len(centers):
        raise ValueError("Boundary probability and center timelines must match")
    candidates = []
    for index, probability in enumerate(probabilities):
        left = probabilities[index - 1] if index else -1.0
        right = probabilities[index + 1] if index + 1 < len(probabilities) else -1.0
        if probability >= threshold and probability >= left and probability >= right:
            candidates.append(
                {"index": index, "sample": int(centers[index]), "probability": float(probability)}
            )

    selected: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: item["probability"], reverse=True):
        if all(
            abs(candidate["sample"] - existing["sample"]) >= min_distance_samples
            for existing in selected
        ):
            selected.append(candidate)
    selected.sort(key=lambda item: item["sample"])
    return selected
