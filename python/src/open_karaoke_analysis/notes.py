"""High-accuracy scoring-note generation from pitch and Vocal Event artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters import AnalysisContext, resolve_artifact
from .contracts import ModelInfo, Role, ServiceError
from .note_candidates import HOP_SAMPLES, RATE, build_note_candidates
from .note_optimizer import TemporalDynamicProgrammingOptimizer, estimate_key

MODEL_ID = "score-note-optimizer-v1"
MODEL_VERSION = "1.0.1"


class NotesOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pitch_artifact: str = Field(min_length=1, max_length=1024)
    vocal_event_artifact: str = Field(min_length=1, max_length=1024)
    alignment_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    candidate_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    repeated_sections_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    optimizer: Literal["temporal-dp"] = "temporal-dp"
    voiced_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    bridge_unvoiced_ms: int = Field(default=20, ge=0, le=100)
    min_pitch_change_cents: float = Field(default=55.0, ge=20.0, le=300.0)
    strong_boundary_probability: float = Field(default=0.82, ge=0.0, le=1.0)
    min_note_ms: int = Field(default=70, ge=30, le=1000)
    analysis_version: int = Field(default=1, ge=1, le=2_147_483_647)


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _probability(value: Any) -> bool:
    return _finite_number(value) and 0.0 <= float(value) <= 1.0


def _validate_pitch(document: dict) -> None:
    if document.get("format_version") != 1:
        raise ValueError("Expected a version-1 pitch artifact")
    if document.get("sample_rate") != RATE or document.get("hop_samples") != HOP_SAMPLES:
        raise ValueError("Pitch artifact must use the canonical 48 kHz / 480-sample grid")
    duration = document.get("duration_samples")
    digest = document.get("input_sha256")
    frames = document.get("frames")
    window = document.get("window_samples", HOP_SAMPLES)
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration <= 0
        or not isinstance(digest, str)
        or len(digest) < 8
        or not isinstance(frames, list)
        or not frames
        or type(window) is not int
        or window not in {HOP_SAMPLES, 1920}
    ):
        raise ValueError("Pitch artifact metadata is invalid")
    if len(frames) != (duration + HOP_SAMPLES - 1) // HOP_SAMPLES:
        raise ValueError("Pitch frame count does not cover the song")

    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError("Pitch frames must be objects")
        start = frame.get("start_sample")
        end = frame.get("end_sample")
        center = frame.get("center_sample")
        if (
            type(frame.get("index")) is not int
            or frame["index"] != index
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or isinstance(center, bool)
            or not isinstance(center, int)
            or start != index * HOP_SAMPLES
            or end != min(start + window, duration)
            or center != (start + end) // 2
        ):
            raise ValueError("Pitch frame timeline is invalid")
        midi = frame.get("midi")
        if midi is not None and (not _finite_number(midi) or not 0.0 <= float(midi) <= 127.0):
            raise ValueError("Pitch MIDI values must be finite and in range")
        if not _probability(frame.get("confidence")) or not _probability(
            frame.get("voiced_probability")
        ):
            raise ValueError("Pitch confidence values must be in 0..1")


def _pitch_note_cells(pitch: dict) -> list[dict]:
    """Partition time at observation-center midpoints without editing source evidence.

    The production detector publishes overlapping 1920-sample observations every
    480 samples. Those windows are not note durations. Interior ownership edges
    are integer midpoints between adjacent observation centers; edge cells extend
    to song start/end. Legacy non-overlapping hop-cell fixtures stay unchanged.
    """
    frames = pitch["frames"]
    if pitch.get("window_samples", HOP_SAMPLES) == HOP_SAMPLES:
        return frames
    boundaries = [
        0,
        *[
            (previous["center_sample"] + current["center_sample"]) // 2
            for previous, current in zip(frames, frames[1:], strict=False)
        ],
        pitch["duration_samples"],
    ]
    return [
        {**frame, "start_sample": boundaries[index], "end_sample": boundaries[index + 1]}
        for index, frame in enumerate(frames)
    ]


def _validate_events(document: dict, pitch: dict) -> None:
    if document.get("format_version") != 1:
        raise ValueError("Expected a version-1 Vocal Event artifact")
    for key in ("sample_rate", "hop_samples", "duration_samples", "input_sha256"):
        if document.get(key) != pitch.get(key):
            raise ValueError("Pitch and Vocal Event artifacts do not describe the same audio/grid")
    frames = document.get("frames")
    pitch_frames = pitch["frames"]
    boundaries = document.get("note_boundaries")
    if (
        not isinstance(frames, list)
        or len(frames) != len(pitch_frames)
        or not isinstance(boundaries, list)
    ):
        raise ValueError("Vocal Event artifact timeline is invalid")

    # Event output intervals are hop cells, even when its acoustic analysis window
    # is 1920 samples. Compare shared starts/identity, not Pitch observation ends.
    for index, event in enumerate(frames):
        if (
            not isinstance(event, dict)
            or type(event.get("index")) is not int
            or event["index"] != index
        ):
            raise ValueError("Vocal Event frames must be indexed objects")
        start = index * HOP_SAMPLES
        end = min(start + HOP_SAMPLES, pitch["duration_samples"])
        expected = {"start_sample": start, "end_sample": end, "center_sample": (start + end) // 2}
        if any(
            type(event.get(key)) is not int or event[key] != value
            for key, value in expected.items()
        ):
            raise ValueError("Vocal Event frames must use the canonical hop-cell timeline")
        for field in (
            "note_boundary_probability",
            "lyric_boundary_probability",
            "silence_probability",
            "breath_probability",
        ):
            if not _probability(event.get(field)):
                raise ValueError(f"Vocal Event {field} must be in 0..1")

    previous = -1
    for boundary in boundaries:
        if not isinstance(boundary, dict):
            raise ValueError("Note boundaries must be objects")
        sample = boundary.get("sample")
        probability = boundary.get("probability")
        if (
            isinstance(sample, bool)
            or not isinstance(sample, int)
            or not 0 <= sample < pitch["duration_samples"]
            or sample < previous
            or not _probability(probability)
        ):
            raise ValueError("Note boundary timeline is invalid")
        previous = sample


def _validate_alignment(document: dict | None, pitch: dict) -> None:
    if document is None:
        return
    if document.get("format_version") != 1 or not isinstance(document.get("lines"), list):
        raise ValueError("Expected a version-1 alignment artifact")
    if document.get("duration_samples") not in {None, pitch["duration_samples"]}:
        raise ValueError("Alignment duration does not match pitch")
    if document.get("input_sha256") not in {None, pitch["input_sha256"]}:
        raise ValueError("Alignment input hash does not match pitch")
    for line in document["lines"]:
        if not isinstance(line, dict) or not isinstance(line.get("id"), str) or not line["id"]:
            raise ValueError("Alignment lines require stable ids")


def _validate_external_candidates(document: dict | None, pitch: dict) -> list[dict]:
    if document is None:
        return []
    if document.get("format_version") != 1:
        raise ValueError("Expected a version-1 candidate artifact")
    if document.get("sample_rate") not in {None, RATE}:
        raise ValueError("Candidate artifact must use 48 kHz sample indices")
    if document.get("duration_samples") not in {None, pitch["duration_samples"]}:
        raise ValueError("Candidate artifact duration does not match pitch")
    if document.get("input_sha256") not in {None, pitch["input_sha256"]}:
        raise ValueError("Candidate artifact input hash does not match pitch")
    provider = document.get("provider")
    if (
        not isinstance(provider, dict)
        or not isinstance(provider.get("id"), str)
        or not provider["id"]
    ):
        raise ValueError("Candidate artifact requires provider metadata")
    raw = document.get("candidates")
    if not isinstance(raw, list):
        raise ValueError("Candidate artifact requires a candidates list")

    candidates = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("External note candidates must be objects")
        start = item.get("start_sample")
        end = item.get("end_sample")
        midi = item.get("midi_note")
        confidence = item.get("confidence")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not 0 <= start < end <= pitch["duration_samples"]
            or isinstance(midi, bool)
            or not isinstance(midi, int)
            or not 0 <= midi <= 127
            or not _probability(confidence)
        ):
            raise ValueError("External candidate fields are invalid")
        candidates.append(
            {
                "id": (
                    item.get("id")
                    if isinstance(item.get("id"), str)
                    else f"external-{index + 1:04d}"
                ),
                "start_sample": start,
                "end_sample": end,
                "midi_note": midi,
                "confidence": float(confidence),
                "provider": provider["id"],
            }
        )
    return candidates


def _validate_repeated_sections(document: dict | None, pitch: dict) -> list[dict]:
    if document is None:
        return []
    if document.get("format_version") != 1 or not isinstance(document.get("pairs"), list):
        raise ValueError("Expected a version-1 repeated-section artifact")
    if document.get("duration_samples") not in {None, pitch["duration_samples"]}:
        raise ValueError("Repeated-section duration does not match pitch")
    pairs = []
    for index, item in enumerate(document["pairs"]):
        if not isinstance(item, dict):
            raise ValueError("Repeated-section pairs must be objects")
        source_start = item.get("source_start_sample")
        source_end = item.get("source_end_sample")
        repeat_start = item.get("repeat_start_sample")
        repeat_end = item.get("repeat_end_sample")
        confidence = item.get("confidence", 1.0)
        values = (source_start, source_end, repeat_start, repeat_end)
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in values)
            or not 0 <= source_start < source_end <= pitch["duration_samples"]
            or not 0 <= repeat_start < repeat_end <= pitch["duration_samples"]
            or repeat_start < source_start
            or not _probability(confidence)
        ):
            raise ValueError("Repeated-section pair fields are invalid")
        pairs.append(
            {
                "id": (
                    item.get("id") if isinstance(item.get("id"), str) else f"repeat-{index + 1:04d}"
                ),
                "source_start_sample": source_start,
                "source_end_sample": source_end,
                "repeat_start_sample": repeat_start,
                "repeat_end_sample": repeat_end,
                "confidence": float(confidence),
            }
        )
    return pairs


def _line_interval(line: dict) -> tuple[int, int] | None:
    start = line.get("start_sample")
    end = line.get("end_sample")
    if not (isinstance(start, int) and not isinstance(start, bool)):
        start = line.get("asr_start_sample")
    if not (isinstance(end, int) and not isinstance(end, bool)):
        end = line.get("asr_end_sample")
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and start < end
    ):
        return start, end
    return None


def assign_lyric_segments(candidates: list[dict], alignment: dict | None) -> None:
    if alignment is None:
        return
    lines = [
        (line, _line_interval(line))
        for line in alignment["lines"]
        if isinstance(line, dict) and isinstance(line.get("id"), str)
    ]
    for candidate in candidates:
        best = None
        for line, interval in lines:
            if interval is None:
                continue
            start, end = interval
            overlap = max(
                0,
                min(candidate["end_sample"], end) - max(candidate["start_sample"], start),
            )
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, line["id"])
        if best is not None:
            candidate["lyric_segment_id"] = best[1]


def generate_score_documents(
    pitch: dict,
    events: dict,
    *,
    alignment: dict | None = None,
    external_candidate_document: dict | None = None,
    repeated_sections_document: dict | None = None,
    options: dict[str, Any] | None = None,
) -> tuple[dict, dict, dict]:
    validated_options = NotesOptions.model_validate(
        {
            "pitch_artifact": "pitch.json",
            "vocal_event_artifact": "vocal_events.json",
            **(options or {}),
        }
    ).model_dump()
    _validate_pitch(pitch)
    _validate_events(events, pitch)
    _validate_alignment(alignment, pitch)
    external = _validate_external_candidates(external_candidate_document, pitch)
    repeated = _validate_repeated_sections(repeated_sections_document, pitch)

    candidates, boundary_decisions, candidate_summary = build_note_candidates(
        _pitch_note_cells(pitch),
        events["frames"],
        events["note_boundaries"],
        external,
        voiced_threshold=validated_options["voiced_threshold"],
        bridge_unvoiced_samples=round(validated_options["bridge_unvoiced_ms"] * RATE / 1000),
        min_pitch_change_cents=validated_options["min_pitch_change_cents"],
        strong_boundary_probability=validated_options["strong_boundary_probability"],
    )
    assign_lyric_segments(candidates, alignment)
    key_estimate = estimate_key(candidates)

    optimizer = TemporalDynamicProgrammingOptimizer()
    notes, optimizer_decisions, optimizer_summary = optimizer.optimize(
        candidates,
        key_estimate,
        repeated,
        min_note_samples=round(validated_options["min_note_ms"] * RATE / 1000),
    )

    canonical_notes = [
        {
            "id": note["id"],
            "start_sample": note["start_sample"],
            "end_sample": note["end_sample"],
            "midi_note": note["midi_note"],
            "pitch_confidence": note["pitch_confidence"],
            "lyric_segment_id": note["lyric_segment_id"],
        }
        for note in notes
    ]
    version = {
        "format_version": 1,
        "analysis_version": validated_options["analysis_version"],
    }
    candidate_document = {
        **version,
        "sample_rate": RATE,
        "duration_samples": pitch["duration_samples"],
        "input_sha256": pitch["input_sha256"],
        "provider_contract_version": 1,
        "candidate_sources": [
            "f0-event-baseline",
            *(
                [external_candidate_document["provider"]["id"]]
                if external_candidate_document is not None
                else []
            ),
        ],
        "candidates": candidates,
    }
    notes_document = {**version, "notes": canonical_notes}
    evidence_document = {
        **version,
        "model": {"id": MODEL_ID, "version": MODEL_VERSION},
        "sample_rate": RATE,
        "duration_samples": pitch["duration_samples"],
        "input_sha256": pitch["input_sha256"],
        "pitch_frame_policy": (
            "observation-center-cells-v1"
            if pitch.get("window_samples", HOP_SAMPLES) == 1920
            else "hop-cells-v1"
        ),
        "source_models": {
            "pitch": pitch.get("model"),
            "vocal_events": events.get("model"),
            "external_candidates": (
                external_candidate_document.get("provider")
                if external_candidate_document is not None
                else None
            ),
        },
        "options": validated_options,
        "key_estimate": key_estimate,
        "candidate_summary": candidate_summary,
        "boundary_decisions": boundary_decisions,
        "optimizer": optimizer_summary,
        "optimizer_decisions": optimizer_decisions,
        "repeated_section_hook": {
            "provided": repeated_sections_document is not None,
            "pair_count": len(repeated),
            "mode": "soft_local_prior",
        },
        "normalization": {
            "vibrato": "low-sustained-change boundaries are suppressed",
            "scoop_portamento": "edge transition frames are trimmed before pitch selection",
            "kobushi_short_motion": "short weak-boundary fragments merge into neighboring notes",
            "melisma": "sustained pitch changes with boundary evidence remain separate notes",
        },
        "candidate_provider_contract": {
            "version": 1,
            "future_sources": ["basic-pitch", "imported-midi", "other-note-transcribers"],
            "external_weights_bundled": False,
        },
        "confidence_calibrated": False,
        "real_singing_accuracy_verified": False,
    }
    return notes_document, candidate_document, evidence_document


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _read_json(
    context: AnalysisContext,
    relative: str,
    *,
    max_bytes: int,
    error_code: str,
) -> dict:
    path: Path = resolve_artifact(context.root, relative, must_exist=True)
    try:
        if path.stat().st_size > max_bytes:
            raise ServiceError(error_code, "Artifact exceeds size limit")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
        )
    except ServiceError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ServiceError(error_code, "Artifact is not valid JSON") from error
    if not isinstance(value, dict):
        raise ServiceError(error_code, "Artifact root must be an object")
    return value


class NotesAdapter:
    info = ModelInfo(
        id=MODEL_ID,
        version=MODEL_VERSION,
        role=Role.NOTES,
        capabilities=[
            "f0-ensemble-input",
            "vocal-event-input",
            "note-candidate-contract-v1",
            "key-scale-estimation",
            "lyric-evidence",
            "repeated-section-hook",
            "temporal-optimization",
            "expressive-motion-normalization",
            "notes-json",
            "replaceable-optimizer",
        ],
    )

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return NotesOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid score-generation options") from error

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        options = self.validate_options(options)
        context.checkpoint()
        context.report(0.05, "loading_score_evidence")
        try:
            pitch = _read_json(
                context,
                options["pitch_artifact"],
                max_bytes=128 * 1024 * 1024,
                error_code="invalid_pitch",
            )
            events = _read_json(
                context,
                options["vocal_event_artifact"],
                max_bytes=256 * 1024 * 1024,
                error_code="invalid_vocal_events",
            )
            alignment = (
                _read_json(
                    context,
                    options["alignment_artifact"],
                    max_bytes=128 * 1024 * 1024,
                    error_code="invalid_alignment",
                )
                if options["alignment_artifact"] is not None
                else None
            )
            external = (
                _read_json(
                    context,
                    options["candidate_artifact"],
                    max_bytes=128 * 1024 * 1024,
                    error_code="invalid_note_candidates",
                )
                if options["candidate_artifact"] is not None
                else None
            )
            repeated = (
                _read_json(
                    context,
                    options["repeated_sections_artifact"],
                    max_bytes=32 * 1024 * 1024,
                    error_code="invalid_repeated_sections",
                )
                if options["repeated_sections_artifact"] is not None
                else None
            )
            context.checkpoint()
            context.report(0.25, "building_note_candidates")
            notes, candidates, evidence = generate_score_documents(
                pitch,
                events,
                alignment=alignment,
                external_candidate_document=external,
                repeated_sections_document=repeated,
                options=options,
            )
            context.checkpoint()
            context.report(0.90, "optimizing_score_timeline")
            evidence["source_artifacts"] = {
                "pitch": options["pitch_artifact"],
                "vocal_events": options["vocal_event_artifact"],
                "alignment": options["alignment_artifact"],
                "external_candidates": options["candidate_artifact"],
                "repeated_sections": options["repeated_sections_artifact"],
            }
            context.report(0.98, "publishing_score")
            return [
                context.write_json("notes.json", notes),
                context.write_json("note_candidates.json", candidates),
                context.write_json("note_evidence.json", evidence),
            ]
        except ServiceError:
            raise
        except ValueError as error:
            raise ServiceError(
                "invalid_score_evidence",
                "Score-generation evidence is inconsistent",
            ) from error
        except (OSError, TypeError) as error:
            raise ServiceError(
                "score_generation_failed",
                "Could not generate scoring notes",
                500,
            ) from error
