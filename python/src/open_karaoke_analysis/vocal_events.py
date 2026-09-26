"""Shared Vocal Event Timeline for score generation and lyric-alignment refinement."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .adapters import AnalysisContext
from .contracts import ModelInfo, Role, ServiceError
from .vocal_event_features import (
    HOP_SAMPLES,
    RATE,
    WINDOW_SAMPLES,
    derive_silence_breath,
    extract_audio_features,
    read_alignment_evidence,
    read_note_onsets,
    read_pitch_features,
    sha256_file,
)
from .vocal_event_fusion import FEATURE_NAMES, fuse_frame, pick_boundaries

FeatureName = Literal[
    "f0_transition",
    "voiced_transition",
    "spectral_flux",
    "energy_delta",
    "acoustic_onset",
    "lyric_timing",
    "note_onset",
    "silence_breath",
]

MODEL_ID = "vocal-event-fusion-v1"
MODEL_VERSION = "1.0.0"


class VocalEventOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pitch_artifact: str = Field(min_length=1, max_length=1024)
    alignment_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    note_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    mode: Literal["single", "ensemble"] = "ensemble"
    features: list[FeatureName] = Field(
        default_factory=lambda: list(FEATURE_NAMES), min_length=1, max_length=len(FEATURE_NAMES)
    )
    boundary_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    min_boundary_distance_ms: int = Field(default=30, ge=10, le=500)
    analysis_version: int = Field(default=1, ge=1, le=2_147_483_647)

    @model_validator(mode="after")
    def configuration(self):
        if len(set(self.features)) != len(self.features):
            raise ValueError("Vocal Event features must not be duplicated")
        if self.mode == "single" and len(self.features) != 1:
            raise ValueError("Single-feature mode requires exactly one feature")
        if self.mode == "ensemble" and len(self.features) < 2:
            raise ValueError("Ensemble mode requires at least two features")
        return self


class VocalEventAdapter:
    info = ModelInfo(
        id=MODEL_ID,
        version=MODEL_VERSION,
        role=Role.EVENTS,
        capabilities=[
            "48khz",
            "f0_transition",
            "voiced_unvoiced_transition",
            "spectral_flux",
            "energy_envelope",
            "acoustic_onset",
            "alignment_timestamps",
            "note_onset_candidates",
            "silence_breath_candidates",
            "note_boundary_probability",
            "lyric_boundary_probability",
            "single_feature",
            "ensemble",
            "evidence",
        ],
    )

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return VocalEventOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid Vocal Event options") from error

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        options = self.validate_options(options)
        context.checkpoint()
        if context.input_path is None:
            raise ServiceError("input_required", "Vocal Event analysis requires vocals WAV")

        input_sha256 = sha256_file(context, context.input_path)
        audio = extract_audio_features(context)
        frame_count = len(audio["starts"])
        pitch = read_pitch_features(
            context,
            options["pitch_artifact"],
            duration_samples=audio["duration_samples"],
            input_sha256=input_sha256,
        )
        lyric_timing, linguistic_events, alignment_available = read_alignment_evidence(
            context,
            options["alignment_artifact"],
            duration_samples=audio["duration_samples"],
            frame_count=frame_count,
        )
        note_onset, note_events, notes_available = read_note_onsets(
            context,
            options["note_artifact"],
            duration_samples=audio["duration_samples"],
            frame_count=frame_count,
        )
        silence, breath, silence_breath = derive_silence_breath(
            audio["energy"], audio["spectral_flux"], pitch["voiced_probability"]
        )

        features = {
            "f0_transition": pitch["f0_transition"],
            "voiced_transition": pitch["voiced_transition"],
            "spectral_flux": audio["spectral_flux"],
            "energy_delta": audio["energy_delta"],
            "acoustic_onset": audio["acoustic_onset"],
            "lyric_timing": lyric_timing,
            "note_onset": note_onset,
            "silence_breath": silence_breath,
        }
        availability = {
            "f0_transition": True,
            "voiced_transition": True,
            "spectral_flux": True,
            "energy_delta": True,
            "acoustic_onset": True,
            "lyric_timing": alignment_available,
            "note_onset": notes_available,
            "silence_breath": True,
        }
        available_active = [name for name in options["features"] if availability[name]]
        required = 1 if options["mode"] == "single" else 2
        if len(available_active) < required:
            raise ServiceError(
                "insufficient_event_evidence",
                "Requested Vocal Event mode does not have enough available evidence",
            )

        note_probabilities = []
        lyric_probabilities = []
        canonical_frames = []
        evidence_frames = []
        for index, start_sample in enumerate(audio["starts"]):
            context.checkpoint()
            frame_features = {name: float(values[index]) for name, values in features.items()}
            note_probability, lyric_probability, fusion = fuse_frame(
                frame_features,
                options["features"],
                availability,
                options["mode"],
            )
            note_probabilities.append(note_probability)
            lyric_probabilities.append(lyric_probability)
            end_sample = min(start_sample + HOP_SAMPLES, audio["duration_samples"])
            canonical_frames.append(
                {
                    "index": index,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "center_sample": (start_sample + end_sample) // 2,
                    "features": frame_features,
                    "note_boundary_probability": note_probability,
                    "lyric_boundary_probability": lyric_probability,
                    "silence_probability": silence[index],
                    "breath_probability": breath[index],
                }
            )
            evidence_frames.append(
                {
                    "index": index,
                    "start_sample": start_sample,
                    "f0_hz": pitch["f0_hz"][index],
                    "voiced_probability": pitch["voiced_probability"][index],
                    "f0_delta_cents": pitch["f0_delta_cents"][index],
                    "energy_rms": audio["energy_rms"][index],
                    "spectral_flux_raw": audio["spectral_flux_raw"][index],
                    "phase_cancellation_fallback": audio["phase_fallback"][index],
                    "fusion": fusion,
                }
            )
            if index == frame_count - 1 or index % 200 == 0:
                context.report(
                    0.3 + 0.62 * (index + 1) / frame_count,
                    "fusing_vocal_events",
                )

        min_distance_samples = round(options["min_boundary_distance_ms"] * RATE / 1000)
        note_boundaries = pick_boundaries(
            note_probabilities,
            audio["starts"],
            options["boundary_threshold"],
            min_distance_samples,
        )
        lyric_boundaries = pick_boundaries(
            lyric_probabilities,
            audio["starts"],
            options["boundary_threshold"],
            min_distance_samples,
        )
        linguistic_kinds = sorted({event["kind"] for event in linguistic_events})
        metadata = {
            "model": {"id": MODEL_ID, "version": MODEL_VERSION},
            "sample_rate": RATE,
            "window_samples": WINDOW_SAMPLES,
            "hop_samples": HOP_SAMPLES,
            "duration_samples": audio["duration_samples"],
            "input_sha256": input_sha256,
            "mode": options["mode"],
            "active_features": options["features"],
            "feature_availability": availability,
            "boundary_threshold": options["boundary_threshold"],
            "min_boundary_distance_samples": min_distance_samples,
            "source_artifacts": {
                "pitch": options["pitch_artifact"],
                "alignment": options["alignment_artifact"],
                "notes": options["note_artifact"],
            },
            "linguistic_evidence_kinds": linguistic_kinds,
            "phoneme_or_syllable_evidence_available": bool(
                {"phoneme", "syllable"} & set(linguistic_kinds)
            ),
            "character_alignment_evidence_available": "character" in linguistic_kinds,
            "boundary_probability_calibrated": False,
            "real_singing_accuracy_verified": False,
        }
        version = {
            "format_version": 1,
            "analysis_version": options["analysis_version"],
        }
        timeline = {
            **version,
            **metadata,
            "frames": canonical_frames,
            "note_boundaries": note_boundaries,
            "lyric_boundaries": lyric_boundaries,
        }
        evidence = {
            **version,
            **metadata,
            "linguistic_events": linguistic_events,
            "note_onset_events": note_events,
            "frames": evidence_frames,
        }
        context.report(0.98, "publishing_vocal_events")
        return [
            context.write_json("vocal_events.json", timeline),
            context.write_json("vocal_event_evidence.json", evidence),
        ]
