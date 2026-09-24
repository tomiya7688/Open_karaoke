"""Offline 48 kHz F0 timeline generation with switchable detector fusion."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .adapters import AnalysisContext, resolve_artifact
from .contracts import ModelInfo, Role, ServiceError

RATE = 48_000
WINDOW_SAMPLES = 1_920
HOP_SAMPLES = 480
MAX_DURATION_SAMPLES = 30 * 60 * RATE
MODEL_ID = "f0-ensemble-v1"
MODEL_VERSION = "1.0.0"


class PitchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["single", "ensemble"] = "ensemble"
    detectors: list[Literal["yin", "nacf"]] = Field(
        default_factory=lambda: ["yin", "nacf"], min_length=1, max_length=2
    )
    min_frequency_hz: float = Field(default=55.0, ge=40.0, le=500.0)
    max_frequency_hz: float = Field(default=1760.0, ge=200.0, le=2400.0)
    energy_floor: float = Field(default=0.0001, gt=0.0, le=0.1)
    voiced_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    analysis_version: int = Field(default=1, ge=1, le=2_147_483_647)

    @model_validator(mode="after")
    def detector_configuration(self):
        if len(set(self.detectors)) != len(self.detectors):
            raise ValueError("Pitch detectors must not be duplicated")
        if self.mode == "single" and len(self.detectors) != 1:
            raise ValueError("Single-detector mode requires exactly one detector")
        if self.mode == "ensemble" and len(self.detectors) < 2:
            raise ValueError("Ensemble mode requires at least two detectors")
        if self.min_frequency_hz >= self.max_frequency_hz:
            raise ValueError("Minimum pitch must be below maximum pitch")
        return self


class PitchAdapter:
    info = ModelInfo(
        id=MODEL_ID,
        version=MODEL_VERSION,
        role=Role.PITCH,
        capabilities=[
            "48khz",
            "yin",
            "normalized-autocorrelation",
            "single-detector",
            "ensemble",
            "voiced-probability",
            "octave-consistency",
            "temporal-continuity",
            "evidence",
        ],
    )

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return PitchOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid pitch-analysis options") from error

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        if context.input_path is None:
            raise ServiceError("input_required", "Pitch analysis requires a vocals WAV artifact")
        options = self.validate_options(options)
        context.checkpoint()

        try:
            import numpy as np
            import soundfile as sf

            from .pitch_detectors import DETECTOR_FACTORIES
            from .pitch_fusion import fuse_frame
        except ImportError as error:
            raise ServiceError(
                "dependency_missing", "Install the analysis package with the pitch extra", 503
            ) from error

        output_root = resolve_artifact(context.root, context.output_prefix)
        output_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".pitch-", dir=output_root) as temp:
                snapshot = Path(temp) / "vocals.wav"
                digest = hashlib.sha256()
                with context.input_path.open("rb") as source, snapshot.open("xb") as target:
                    total_bytes = 0
                    while block := source.read(1024 * 1024):
                        context.checkpoint()
                        total_bytes += len(block)
                        if total_bytes > 2_500_000_000:
                            raise ServiceError("invalid_audio", "Pitch input exceeds size limit")
                        digest.update(block)
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())

                try:
                    audio = sf.SoundFile(snapshot)
                except (RuntimeError, OSError) as error:
                    raise ServiceError("invalid_audio", "Input is not a readable WAV") from error

                detectors = [DETECTOR_FACTORIES[name]() for name in options["detectors"]]
                frames: list[dict] = []
                evidence_frames: list[dict] = []
                previous_hz: float | None = None

                with audio:
                    if (
                        audio.format not in {"WAV", "WAVEX", "RF64"}
                        or audio.samplerate != RATE
                        or audio.subtype != "FLOAT"
                        or audio.channels not in {1, 2}
                        or not 0 < audio.frames <= MAX_DURATION_SAMPLES
                    ):
                        raise ServiceError(
                            "invalid_audio", "Expected <=30 min 48 kHz float32 mono/stereo WAV"
                        )
                    duration_samples = int(audio.frames)
                    frame_count = math.ceil(duration_samples / HOP_SAMPLES)

                    for index, start_sample in enumerate(
                        range(0, duration_samples, HOP_SAMPLES)
                    ):
                        context.checkpoint()
                        end_sample = min(start_sample + WINDOW_SAMPLES, duration_samples)
                        audio.seek(start_sample)
                        block = audio.read(
                            end_sample - start_sample, dtype="float32", always_2d=True
                        )
                        if len(block) != end_sample - start_sample:
                            raise ServiceError("invalid_audio", "Pitch input was truncated")
                        if not np.isfinite(block).all():
                            raise ServiceError(\n                                "invalid_audio", "Pitch input contains NaN or infinity"\n                            )

                        flags: list[str] = []
                        mono = block.mean(axis=1, dtype=np.float64)
                        if audio.channels == 2 and len(block):
                            channel_rms = np.sqrt(
                                np.mean(block.astype(np.float64) ** 2, axis=0)
                            )
                            mono_rms = float(np.sqrt(np.mean(mono * mono)))
                            loudest = int(np.argmax(channel_rms))
                            if (
                                float(channel_rms[loudest]) > options["energy_floor"]
                                and mono_rms < float(channel_rms[loudest]) * 0.1
                            ):
                                mono = block[:, loudest].astype(np.float64)
                                flags.append("channel_phase_cancellation_fallback")

                        if len(mono) < WINDOW_SAMPLES:
                            mono = np.pad(mono, (0, WINDOW_SAMPLES - len(mono)))
                        raw = [
                            detector.detect(
                                mono,
                                RATE,
                                options["min_frequency_hz"],
                                options["max_frequency_hz"],
                                options["energy_floor"],
                            )
                            for detector in detectors
                        ]

                        if options["mode"] == "single":
                            selected = raw[0]
                            voiced_probability = selected.voiced_probability
                            final_hz = (
                                selected.hz
                                if selected.hz is not None
                                and voiced_probability >= options["voiced_threshold"]
                                else None
                            )
                            confidence = selected.confidence if final_hz is not None else 0.0
                            fusion = {
                                "mode": "single",
                                "selected_detector": selected.detector,
                                "confidence_calibrated": False,
                                "voiced_probability_calibrated": False,
                            }
                        else:
                            final_hz, confidence, voiced_probability, fusion = fuse_frame(
                                raw,
                                previous_hz,
                                options["min_frequency_hz"],
                                options["max_frequency_hz"],
                                options["voiced_threshold"],
                            )

                        if final_hz is not None:
                            previous_hz = final_hz
                            midi = 69.0 + 12.0 * math.log2(final_hz / 440.0)
                        else:
                            midi = None

                        frame = {
                            "index": index,
                            "start_sample": start_sample,
                            "end_sample": end_sample,
                            "center_sample": (start_sample + end_sample) // 2,
                            "f0_hz": final_hz,
                            "midi": midi,
                            "confidence": confidence,
                            "voiced_probability": voiced_probability,
                            "voiced": final_hz is not None,
                        }
                        frames.append(frame)
                        evidence_frames.append(
                            {
                                "index": index,
                                "start_sample": start_sample,
                                "end_sample": end_sample,
                                "flags": flags,
                                "detectors": {
                                    observation.detector: observation.as_dict()
                                    for observation in raw
                                },
                                "fusion": fusion,
                            }
                        )
                        if index == frame_count - 1 or index % 50 == 0:
                            context.report(
                                0.05 + 0.9 * (index + 1) / frame_count,
                                "analyzing_pitch",
                            )

                version = {
                    "format_version": 1,
                    "analysis_version": options["analysis_version"],
                }
                detector_metadata = [
                    {
                        "id": detector.id,
                        "version": detector.version,
                        "reliability": detector.reliability,
                        "external_weights": False,
                    }
                    for detector in detectors
                ]
                metadata = {
                    "model": {"id": MODEL_ID, "version": MODEL_VERSION},
                    "sample_rate": RATE,
                    "window_samples": WINDOW_SAMPLES,
                    "hop_samples": HOP_SAMPLES,
                    "duration_samples": duration_samples,
                    "input_sha256": digest.hexdigest(),
                    "mode": options["mode"],
                    "detectors": detector_metadata,
                    "options": options,
                    "confidence_calibrated": False,
                    "voiced_probability_calibrated": False,
                    "real_singing_accuracy_verified": False,
                }
                timeline = {**version, **metadata, "frames": frames}
                evidence = {**version, **metadata, "frames": evidence_frames}
                context.report(0.98, "publishing_pitch")
                return [
                    context.write_json("pitch.json", timeline),
                    context.write_json("pitch_evidence.json", evidence),
                ]
        except ServiceError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise ServiceError(
                "pitch_analysis_failed", "Could not analyze pitch input", 500
            ) from error
