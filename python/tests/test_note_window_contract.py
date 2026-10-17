"""Production analysis windows are observations, not disjoint note durations."""

from copy import deepcopy

import pytest

from open_karaoke_analysis.notes import (
    _pitch_note_cells,
    _validate_events,
    _validate_pitch,
    generate_score_documents,
)


def _documents(duration, window=1920):
    frames = []
    for index, start in enumerate(range(0, duration, 480)):
        end = min(start + window, duration)
        frames.append(
            {
                "index": index,
                "start_sample": start,
                "end_sample": end,
                "center_sample": (start + end) // 2,
                "midi": 69.0,
                "f0_hz": 440.0,
                "confidence": 0.95,
                "voiced_probability": 0.95,
                "voiced": True,
            }
        )
    pitch = {
        "format_version": 1,
        "analysis_version": 1,
        "sample_rate": 48_000,
        "hop_samples": 480,
        "window_samples": window,
        "duration_samples": duration,
        "input_sha256": "f" * 64,
        "frames": frames,
    }
    event_frames = []
    for frame in frames:
        start = frame["start_sample"]
        end = min(start + 480, duration)
        event_frames.append(
            {
                "index": frame["index"],
                "start_sample": start,
                "end_sample": end,
                "center_sample": (start + end) // 2,
                "note_boundary_probability": 0.0,
                "lyric_boundary_probability": 0.0,
                "silence_probability": 0.0,
                "breath_probability": 0.0,
            }
        )
    # Actual Event metadata declares its acoustic window, not its output cell width.
    events = {**pitch, "window_samples": 1920, "frames": event_frames, "note_boundaries": []}
    return pitch, events


@pytest.mark.parametrize("duration", [1, 479, 480, 481, 1919, 1920, 1921, 9600, 9601])
def test_window_ownership_is_a_partition_and_preserves_observation_centers(duration):
    pitch, events = _documents(duration)
    before = deepcopy(pitch)
    _validate_pitch(pitch)
    _validate_events(events, pitch)
    cells = _pitch_note_cells(pitch)
    assert pitch == before
    assert cells[0]["start_sample"] == 0
    assert cells[-1]["end_sample"] == duration
    assert sum(c["end_sample"] - c["start_sample"] for c in cells) == duration
    for index, cell in enumerate(cells):
        assert cell["start_sample"] <= cell["center_sample"] < cell["end_sample"]
        assert cell["center_sample"] == pitch["frames"][index]["center_sample"]
        if index:
            assert cells[index - 1]["end_sample"] == cell["start_sample"]


def test_existing_hop_cell_contract_stays_unchanged():
    pitch, events = _documents(9600, window=480)
    pitch.pop("window_samples")
    _validate_pitch(pitch)
    assert _pitch_note_cells(pitch) == pitch["frames"]
    notes, _, evidence = generate_score_documents(pitch, events)
    assert notes["notes"] and notes["notes"][0]["midi_note"] == 69
    assert evidence["pitch_frame_policy"] == "hop-cells-v1"


def test_window_end_does_not_extend_a_voiced_note_into_the_following_cells():
    pitch, events = _documents(48_000)
    for frame in pitch["frames"][10:]:
        frame.update(midi=None, f0_hz=None, confidence=0.0, voiced_probability=0.0, voiced=False)
    for frame in events["frames"][10:]:
        frame["silence_probability"] = 1.0
    before = deepcopy(pitch)
    cells = _pitch_note_cells(pitch)
    notes, _, evidence = generate_score_documents(
        pitch, events, options={"bridge_unvoiced_ms": 0, "min_note_ms": 30}
    )
    assert notes["notes"]
    assert notes["notes"][-1]["end_sample"] == cells[9]["end_sample"]
    assert notes["notes"][-1]["end_sample"] < pitch["frames"][9]["end_sample"]
    assert evidence["pitch_frame_policy"] == "observation-center-cells-v1"
    assert evidence["model"]["version"] == "1.0.1"
    assert pitch == before


@pytest.mark.parametrize("fault", ["window", "count", "end", "center", "index"])
def test_declared_windows_and_full_frame_grid_are_validated(fault):
    pitch, _ = _documents(9600)
    if fault == "window":
        pitch["window_samples"] = 1921
    elif fault == "count":
        pitch["frames"].pop()
    elif fault == "end":
        pitch["frames"][0]["end_sample"] += 1
    elif fault == "center":
        pitch["frames"][0]["center_sample"] += 1
    else:
        pitch["frames"][0]["index"] = False
    with pytest.raises(ValueError):
        _validate_pitch(pitch)


@pytest.mark.parametrize("fault", ["start", "end", "center", "index", "count", "hash"])
def test_event_grid_validation_is_not_weakened_for_overlapping_pitch(fault):
    pitch, events = _documents(9600)
    if fault in {"start", "end", "center"}:
        events["frames"][0][f"{fault}_sample"] += 1
    elif fault == "index":
        events["frames"][0]["index"] = False
    elif fault == "count":
        events["frames"].pop()
    else:
        events["input_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        _validate_events(events, pitch)


def test_event_output_cells_cannot_be_replaced_by_pitch_observation_windows():
    pitch, events = _documents(9600)
    for observation, event in zip(pitch["frames"], events["frames"], strict=True):
        event["end_sample"] = observation["end_sample"]
        event["center_sample"] = observation["center_sample"]
    with pytest.raises(ValueError):
        _validate_events(events, pitch)
