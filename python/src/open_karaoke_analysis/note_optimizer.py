"""Replaceable temporal optimizer for scoring-note candidates."""

from __future__ import annotations

from typing import Protocol

from .note_candidates import HOP_SAMPLES, clamp

PITCH_CLASS_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
SCALE_WEIGHTS = {
    "major": {0: 1.30, 2: 1.00, 4: 1.15, 5: 0.95, 7: 1.20, 9: 0.95, 11: 0.90},
    "minor": {0: 1.30, 2: 0.95, 3: 1.15, 5: 1.00, 7: 1.20, 8: 0.95, 10: 0.95},
}


class NoteOptimizer(Protocol):
    id: str

    def optimize(
        self,
        candidates: list[dict],
        key_estimate: dict,
        repeated_sections: list[dict],
        *,
        min_note_samples: int,
    ) -> tuple[list[dict], list[dict], dict]: ...


def estimate_key(candidates: list[dict]) -> dict:
    histogram = [0.0] * 12
    for candidate in candidates:
        midi_note = int(round(candidate["base_midi"]))
        if not 0 <= midi_note <= 127:
            continue
        duration = max(1, candidate["end_sample"] - candidate["start_sample"])
        weight = duration * max(0.05, float(candidate["pitch_confidence"]))
        histogram[midi_note % 12] += weight

    total = sum(histogram)
    if total <= 0:
        return {
            "tonic_pitch_class": None,
            "tonic": None,
            "mode": None,
            "confidence": 0.0,
            "method": "weighted_pitch_class_profile_v1",
        }

    scored = []
    for mode, weights in SCALE_WEIGHTS.items():
        for tonic in range(12):
            score = 0.0
            for pitch_class, value in enumerate(histogram):
                degree = (pitch_class - tonic) % 12
                score += value * weights.get(degree, -0.45)
            scored.append((score, tonic, mode))
    scored.sort(reverse=True)
    best_score, tonic, mode = scored[0]
    second_score = scored[1][0]
    confidence = clamp((best_score - second_score) / max(total, abs(best_score), 1.0))
    return {
        "tonic_pitch_class": tonic,
        "tonic": PITCH_CLASS_NAMES[tonic],
        "mode": mode,
        "confidence": confidence,
        "score": best_score / total,
        "runner_up_score": second_score / total,
        "method": "weighted_pitch_class_profile_v1",
    }


def _in_scale(key_estimate: dict, midi_note: int) -> bool:
    tonic = key_estimate.get("tonic_pitch_class")
    mode = key_estimate.get("mode")
    if not isinstance(tonic, int) or mode not in SCALE_WEIGHTS:
        return False
    return (midi_note - tonic) % 12 in SCALE_WEIGHTS[mode]


def build_repetition_priors(candidates: list[dict], pairs: list[dict]) -> dict[str, dict]:
    priors: dict[str, dict] = {}
    for pair in pairs:
        source_start = pair["source_start_sample"]
        source_end = pair["source_end_sample"]
        repeat_start = pair["repeat_start_sample"]
        repeat_end = pair["repeat_end_sample"]
        source_candidates = [
            candidate
            for candidate in candidates
            if source_start
            <= (candidate["start_sample"] + candidate["end_sample"]) // 2
            < source_end
        ]
        repeat_candidates = [
            candidate
            for candidate in candidates
            if repeat_start
            <= (candidate["start_sample"] + candidate["end_sample"]) // 2
            < repeat_end
        ]
        if not source_candidates or not repeat_candidates:
            continue
        source_length = source_end - source_start
        repeat_length = repeat_end - repeat_start
        for candidate in repeat_candidates:
            center = (candidate["start_sample"] + candidate["end_sample"]) // 2
            relative = (center - repeat_start) / repeat_length
            mapped = source_start + relative * source_length
            source = min(
                source_candidates,
                key=lambda item: abs((item["start_sample"] + item["end_sample"]) / 2 - mapped),
            )
            priors[candidate["id"]] = {
                "midi_note": int(round(source["base_midi"])),
                "strength": clamp(pair["confidence"] * source["pitch_confidence"]),
                "pair_id": pair["id"],
                "source_candidate_id": source["id"],
            }
    return priors


def _merge_notes(left: dict, right: dict, *, keep_midi: int | None = None) -> dict:
    left_duration = left["end_sample"] - left["start_sample"]
    right_duration = right["end_sample"] - right["start_sample"]
    total = max(1, left_duration + right_duration)
    midi_note = (
        keep_midi
        if keep_midi is not None
        else left["midi_note"]
        if left_duration >= right_duration
        else right["midi_note"]
    )
    confidence = clamp(
        (left["pitch_confidence"] * left_duration + right["pitch_confidence"] * right_duration)
        / total
    )
    lyric_segment_id = (
        left["lyric_segment_id"]
        if left["lyric_segment_id"] == right["lyric_segment_id"]
        else left["lyric_segment_id"]
        if left_duration >= right_duration
        else right["lyric_segment_id"]
    )
    return {
        **left,
        "end_sample": right["end_sample"],
        "midi_note": midi_note,
        "pitch_confidence": confidence,
        "lyric_segment_id": lyric_segment_id,
        "boundary_after_probability": right["boundary_after_probability"],
        "boundary_after_reason": right["boundary_after_reason"],
        "candidate_ids": [*left["candidate_ids"], *right["candidate_ids"]],
        "normalization_actions": [
            *left["normalization_actions"],
            *right["normalization_actions"],
            "merged_expressive_fragment",
        ],
    }


def _merge_weak_equal_pitch(notes: list[dict]) -> list[dict]:
    index = 0
    while index + 1 < len(notes):
        left, right = notes[index], notes[index + 1]
        gap = right["start_sample"] - left["end_sample"]
        boundary_strength = max(
            float(left["boundary_after_probability"]),
            float(right["boundary_before_probability"]),
        )
        if (
            left["midi_note"] == right["midi_note"]
            and gap <= 2 * HOP_SAMPLES
            and boundary_strength < 0.85
        ):
            notes[index : index + 2] = [_merge_notes(left, right, keep_midi=left["midi_note"])]
            continue
        index += 1
    return notes


def _normalize_fragments(notes: list[dict], min_note_samples: int) -> list[dict]:
    if not notes:
        return []

    normalized = _merge_weak_equal_pitch([dict(note) for note in notes])
    index = 0
    while index < len(normalized):
        note = normalized[index]
        duration = note["end_sample"] - note["start_sample"]
        if duration >= min_note_samples or len(normalized) == 1:
            index += 1
            continue

        options = []
        if index > 0:
            left = normalized[index - 1]
            gap = note["start_sample"] - left["end_sample"]
            strength = max(
                float(left["boundary_after_probability"]),
                float(note["boundary_before_probability"]),
            )
            if gap <= 2 * HOP_SAMPLES and strength < 0.85:
                options.append((abs(left["midi_note"] - note["midi_note"]), "left"))
        if index + 1 < len(normalized):
            right = normalized[index + 1]
            gap = right["start_sample"] - note["end_sample"]
            strength = max(
                float(note["boundary_after_probability"]),
                float(right["boundary_before_probability"]),
            )
            if gap <= 2 * HOP_SAMPLES and strength < 0.85:
                options.append((abs(right["midi_note"] - note["midi_note"]), "right"))
        options = [option for option in options if option[0] <= 2]
        if not options:
            index += 1
            continue

        _, direction = min(options, key=lambda item: item[0])
        if direction == "left":
            merged = _merge_notes(normalized[index - 1], note)
            normalized[index - 1 : index + 1] = [merged]
            index = max(0, index - 1)
        else:
            merged = _merge_notes(note, normalized[index + 1])
            normalized[index : index + 2] = [merged]

    return _merge_weak_equal_pitch(normalized)


class TemporalDynamicProgrammingOptimizer:
    id = "temporal-dp-v1"

    def optimize(
        self,
        candidates: list[dict],
        key_estimate: dict,
        repeated_sections: list[dict],
        *,
        min_note_samples: int,
    ) -> tuple[list[dict], list[dict], dict]:
        if not candidates:
            return (
                [],
                [],
                {
                    "id": self.id,
                    "replaceable": True,
                    "candidate_count": 0,
                    "repetition_prior_count": 0,
                },
            )

        priors = build_repetition_priors(candidates, repeated_sections)
        dp: list[dict[int, tuple[float, int | None]]] = []
        local_details: list[dict[int, dict]] = []
        for index, candidate in enumerate(candidates):
            states: dict[int, tuple[float, int | None]] = {}
            details: dict[int, dict] = {}
            repeat = priors.get(candidate["id"])
            for hypothesis in candidate["hypotheses"]:
                midi_note = int(hypothesis["midi_note"])
                local = 2.0 * float(hypothesis["score"])
                key_bonus = (
                    0.08 * max(0.25, float(key_estimate.get("confidence", 0.0)))
                    if _in_scale(key_estimate, midi_note)
                    else -0.02
                )
                repetition_bonus = 0.0
                if repeat is not None:
                    if midi_note == repeat["midi_note"]:
                        repetition_bonus = 0.15 * repeat["strength"]
                    elif abs(midi_note - repeat["midi_note"]) in {12, 24}:
                        repetition_bonus = -0.08 * repeat["strength"]

                if index == 0:
                    score = local + key_bonus + repetition_bonus
                    states[midi_note] = (score, None)
                    details[midi_note] = {
                        "local_score": local,
                        "key_bonus": key_bonus,
                        "repetition_bonus": repetition_bonus,
                        "transition_penalty": 0.0,
                    }
                    continue

                previous_candidate = candidates[index - 1]
                gap = candidate["start_sample"] - previous_candidate["end_sample"]
                best = None
                best_detail = None
                for previous_midi, (previous_score, _) in dp[-1].items():
                    transition_penalty = 0.0
                    if gap <= 3 * HOP_SAMPLES:
                        interval = abs(midi_note - previous_midi)
                        if interval > 7:
                            transition_penalty += min(0.25, (interval - 7) * 0.015)
                        if interval in {12, 24}:
                            transition_penalty += 0.05
                    score = (
                        previous_score + local + key_bonus + repetition_bonus - transition_penalty
                    )
                    if best is None or score > best[0]:
                        best = (score, previous_midi)
                        best_detail = {
                            "local_score": local,
                            "key_bonus": key_bonus,
                            "repetition_bonus": repetition_bonus,
                            "transition_penalty": transition_penalty,
                        }
                assert best is not None and best_detail is not None
                states[midi_note] = best
                details[midi_note] = best_detail
            dp.append(states)
            local_details.append(details)

        last_midi = max(dp[-1], key=lambda midi: dp[-1][midi][0])
        selected = [last_midi]
        for index in range(len(candidates) - 1, 0, -1):
            previous = dp[index][selected[-1]][1]
            assert previous is not None
            selected.append(previous)
        selected.reverse()

        notes = []
        decisions = []
        for candidate, midi_note, state_details in zip(
            candidates, selected, local_details, strict=True
        ):
            hypothesis = next(
                item for item in candidate["hypotheses"] if item["midi_note"] == midi_note
            )
            confidence = clamp(0.55 * candidate["pitch_confidence"] + 0.45 * hypothesis["score"])
            repeat = priors.get(candidate["id"])
            notes.append(
                {
                    "start_sample": candidate["start_sample"],
                    "end_sample": candidate["end_sample"],
                    "midi_note": midi_note,
                    "pitch_confidence": confidence,
                    "lyric_segment_id": candidate.get("lyric_segment_id"),
                    "boundary_before_probability": candidate["boundary_before_probability"],
                    "boundary_after_probability": candidate["boundary_after_probability"],
                    "boundary_before_reason": candidate["boundary_before_reason"],
                    "boundary_after_reason": candidate["boundary_after_reason"],
                    "candidate_ids": [candidate["id"]],
                    "normalization_actions": [],
                }
            )
            decisions.append(
                {
                    "candidate_id": candidate["id"],
                    "selected_midi_note": midi_note,
                    "selected_sources": hypothesis["sources"],
                    "selected_hypothesis_score": hypothesis["score"],
                    "dp_components": state_details[midi_note],
                    "repetition_prior": repeat,
                }
            )

        normalized = _normalize_fragments(notes, min_note_samples)
        for index, note in enumerate(normalized, start=1):
            note["id"] = f"note-{index:04d}"

        return (
            normalized,
            decisions,
            {
                "id": self.id,
                "replaceable": True,
                "candidate_count": len(candidates),
                "output_note_count": len(normalized),
                "repetition_prior_count": len(priors),
                "short_fragment_threshold_samples": min_note_samples,
                "normalization_rules": [
                    "weak_same_pitch_boundary_merge",
                    "short_expressive_fragment_merge",
                    "octave_transition_soft_penalty",
                    "key_scale_soft_prior",
                    "repeated_section_soft_prior",
                ],
            },
        )
