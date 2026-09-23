"""Bounded CTC Viterbi alignment with explicit blank/repeated-label states."""

from dataclasses import dataclass

from .contracts import ServiceError

MAX_CELLS = 4_000_000


@dataclass(frozen=True)
class TokenSpan:
    start_frame: int
    end_frame: int
    score: float


def align_tokens(log_probs, tokens: list[int], blank_id: int, checkpoint) -> list[TokenSpan] | None:
    """Return one emitting-frame span per token, or None when no valid CTC path exists.

    Leading/trailing frames are blank, not silently free. Repeated labels must be
    separated by a blank; holding a nonblank state extends its duration. Scores
    are mean emission posteriors, not calibrated timestamp correctness.
    """
    import numpy as np

    values = np.asarray(log_probs, dtype=np.float64)
    if values.ndim != 2 or not 0 < values.shape[0] <= 2000 or not 0 < values.shape[1] <= 8192:
        raise ServiceError("invalid_emissions", "Invalid CTC emission dimensions")
    frames, classes = values.shape
    if (
        not np.isfinite(values).all()
        or np.any(values > 1e-5)
        or not np.allclose(np.exp(values).sum(axis=1), 1, atol=1e-4)
        or type(blank_id) is not int
        or not 0 <= blank_id < classes
        or any(type(t) is not int or t == blank_id or not 0 <= t < classes for t in tokens)
    ):
        raise ServiceError(
            "invalid_emissions", "Expected normalized CTC log probabilities and labels"
        )
    if not tokens:
        return []
    states = 2 * len(tokens) + 1
    if frames * states > MAX_CELLS:
        raise ServiceError(
            "alignment_too_large", "CTC alignment exceeds the bounded trellis budget"
        )
    required = len(tokens) + sum(a == b for a, b in zip(tokens, tokens[1:], strict=False))
    if frames < required:
        return None
    labels = np.full(states, blank_id, dtype=np.int64)
    labels[1::2] = tokens
    can_skip = np.zeros(states, dtype=bool)
    can_skip[2:] = (labels[2:] != blank_id) & (labels[2:] != labels[:-2])
    previous = np.full(states, -np.inf)
    previous[0] = 0
    trace = np.zeros((frames, states), dtype=np.uint8)
    for frame in range(frames):
        if frame % 32 == 0:
            checkpoint()
        one = np.concatenate(([-np.inf], previous[:-1]))
        two = np.concatenate(([-np.inf, -np.inf], previous[:-2]))
        two[~can_skip] = -np.inf
        candidates = np.stack((previous, one, two))
        trace[frame] = candidates.argmax(axis=0)
        previous = candidates.max(axis=0) + values[frame, labels]
    state = states - 1 if previous[-1] >= previous[-2] else states - 2
    if not np.isfinite(previous[state]):
        return None
    positions: list[list[int]] = [[] for _ in tokens]
    for frame in range(frames - 1, -1, -1):
        if state % 2:
            positions[state // 2].append(frame)
        state -= int(trace[frame, state])
    checkpoint()
    if any(not item for item in positions):
        return None
    return [
        TokenSpan(min(pos), max(pos) + 1, float(np.exp(values[pos, token]).mean()))
        for pos, token in zip(positions, tokens, strict=True)
    ]


def frame_samples(
    start_frame: int, end_frame: int, frame_count: int, start_sample: int, end_sample: int
) -> tuple[int, int]:
    """Integer ratio mapping; precision remains limited by acoustic frame resolution."""
    if not 0 <= start_frame < end_frame <= frame_count or not 0 <= start_sample < end_sample:
        raise ValueError("Invalid frame/sample range")
    length = end_sample - start_sample
    start = start_sample + (2 * start_frame * length + frame_count) // (2 * frame_count)
    end = start_sample + (2 * end_frame * length + frame_count) // (2 * frame_count)
    return start, min(end, end_sample)
