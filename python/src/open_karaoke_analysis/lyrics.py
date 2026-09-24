"""Whisper lyric candidates with a 48 kHz timeline and independently retained evidence."""

import hashlib
import json
import math
import re
import tempfile
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters import AnalysisContext, resolve_artifact
from .contracts import ModelInfo, Role, ServiceError
from .whisper_backend import WHISPER_VERSION, WhisperBackend

RATE = 48_000
CORE_SAMPLES = 28 * RATE
CONTEXT_SAMPLES = RATE
MAX_DURATION = 3600 * RATE
PIPELINE_VERSION = 1


class LyricOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    language: str | None = Field(default="ja", pattern=r"^[a-z]{2,3}$")
    device: Literal["cpu", "cuda"] = "cpu"
    beam_size: int = Field(default=5, ge=1, le=5)
    analysis_version: int = Field(default=1, ge=1, le=4_294_967_295)
    allow_model_download: bool = True
    reference_lyrics_artifact: str | None = Field(default=None, min_length=1, max_length=1024)


def sample_index(seconds: Any) -> int:
    """Round decimal seconds to the nearest 48 kHz sample, with ties upward."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ServiceError("invalid_asr_output", "ASR timestamps must be numeric")
    if not math.isfinite(seconds) or seconds < 0 or seconds > 3600:
        raise ServiceError("invalid_asr_output", "ASR timestamp is outside its valid range")
    return int((Decimal(str(seconds)) * RATE).to_integral_value(rounding=ROUND_HALF_UP))


def reference_lyrics(context: AnalysisContext, relative: str | None) -> dict | None:
    if relative is None:
        return None
    path = resolve_artifact(context.root, relative, must_exist=True)
    with path.open("rb") as stream:
        data = stream.read(65_537)
    if len(data) > 65_536:
        raise ServiceError("invalid_reference", "Reference lyrics exceed 64 KiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ServiceError("invalid_reference", "Reference lyrics must be UTF-8") from error
    return {
        "artifact": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "text": text,
        "used_for_decoding": False,
    }


def _diagnostics(segment: dict) -> tuple[dict, list[str]]:
    values = {}
    flags = []
    for key in ("avg_logprob", "no_speech_prob", "compression_ratio", "temperature"):
        value = segment.get(key)
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ServiceError("invalid_asr_output", "ASR diagnostics must be finite numbers")
            if key == "no_speech_prob" and not 0 <= value <= 1:
                raise ServiceError("invalid_asr_output", "Invalid ASR no-speech probability")
        values[key] = value
    if values["avg_logprob"] is not None and values["avg_logprob"] < -1:
        flags.append("low_logprob")
    if values["no_speech_prob"] is not None and values["no_speech_prob"] > 0.6:
        flags.append("suspected_nonspeech")
    if values["compression_ratio"] is not None and values["compression_ratio"] > 2.4:
        flags.append("high_compression_ratio")
    return values, flags


def _repetition(text: str) -> bool:
    compact = "".join(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())
    # A review signal only: real choruses and repeated syllables must not be deleted.
    return bool(re.search(r"(.{1,32}?)\1{3,}", compact))


def _candidates(result: dict, mono, offset: int, owner: int, duration: int, window: int):
    import numpy as np

    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        raise ServiceError("invalid_asr_output", "Whisper must return an ASR segment list")
    if len(result["segments"]) > 1000:
        raise ServiceError("invalid_asr_output", "Too many ASR segments in one window")
    candidates, observations = [], []
    for index, segment in enumerate(result["segments"]):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise ServiceError("invalid_asr_output", "Invalid ASR segment text")
        start = sample_index(segment.get("start"))
        end = sample_index(segment.get("end"))
        if start == end and not segment["text"].strip():
            observations.append(
                {"window": window, "segment": index, "disposition": "empty_segment"}
            )
            continue
        if start >= end:
            raise ServiceError("invalid_asr_output", "ASR segment must have positive duration")
        values, flags = _diagnostics(segment)
        record = {"window": window, "segment": index, "diagnostics": values, "flags": flags}
        observations.append(record)
        text = segment["text"].strip()
        if not text or start >= len(mono):
            record["disposition"] = "empty_or_outside_audio"
            continue
        if end > len(mono):
            end = len(mono)
            flags.append("timestamp_clamped")
        absolute_start, absolute_end = offset + start, offset + end
        midpoint = (absolute_start + absolute_end) // 2
        if not owner <= midpoint < min(owner + CORE_SAMPLES, duration):
            record["disposition"] = "other_window_owns_segment"
            continue
        if float(np.max(np.abs(mono[start:end]))) <= 1e-8:
            record["disposition"] = "digital_silence"
            flags.append("silence_hallucination")
            continue
        if _repetition(text):
            flags.append("repetition_review")
        if start < CONTEXT_SAMPLES or end > len(mono) - CONTEXT_SAMPLES:
            flags.append("window_boundary_review")
        record["disposition"] = "candidate"
        record["confidence_calibrated"] = False
        record["timing_kind"] = "coarse_asr_segment"
        candidate = {
            "text": text,
            "start_sample": absolute_start,
            "end_sample": min(absolute_end, duration),
            "text_confidence": None,
            "timing_confidence": None,
        }
        candidates.append((candidate, record))
    return candidates, observations


class LyricsAdapter:
    def __init__(self, backend_factory=None, *, model_name: str = "large-v3"):
        if model_name not in {"large-v3", "large-v3-turbo"}:
            raise ValueError("Unsupported lyric model")
        self.backend_factory = backend_factory or (lambda: WhisperBackend(model_name))
        self.info = ModelInfo(
            id=f"whisper-{model_name}",
            version=f"{model_name}@{WHISPER_VERSION}",
            role=Role.LYRICS,
            capabilities=["lyric_candidates", "ja", "multilingual", "raw_asr", "coarse_timestamps"],
        )

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return LyricOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid lyric transcription options") from error

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        context.checkpoint()
        if context.input_path is None:
            raise ServiceError("input_required", "A normalized vocals WAV is required")
        try:
            import numpy as np
            import soundfile as sf
            from scipy.signal import resample_poly
        except ImportError as error:
            raise ServiceError(
                "dependency_missing", "Install the analysis lyrics extra", 503
            ) from error
        backend = self.backend_factory()
        try:
            root = resolve_artifact(context.root, context.output_prefix)
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".lyrics-", dir=root) as temporary:
                snapshot = Path(temporary) / "vocals.wav"
                digest = hashlib.sha256()
                with context.input_path.open("rb") as source, snapshot.open("wb") as target:
                    while block := source.read(1024 * 1024):
                        context.checkpoint()
                        if target.tell() + len(block) > MAX_DURATION * 8 + 1_048_576:
                            raise ServiceError("invalid_audio", "Input WAV exceeds the size limit")
                        digest.update(block)
                        target.write(block)
                reference = reference_lyrics(context, options["reference_lyrics_artifact"])
                try:
                    audio = sf.SoundFile(snapshot)
                except (RuntimeError, OSError) as error:
                    raise ServiceError("invalid_audio", "Input is not a readable WAV") from error
                with audio:
                    if (
                        audio.format not in {"WAV", "WAVEX", "RF64"}
                        or audio.samplerate != RATE
                        or audio.channels not in {1, 2}
                        or audio.subtype != "FLOAT"
                        or not 0 < audio.frames <= MAX_DURATION
                    ):
                        raise ServiceError(
                            "invalid_audio", "Expected 48 kHz float32 mono/stereo WAV, <=1 h"
                        )
                    duration = audio.frames
                    raw_windows, candidates, observations = [], [], []
                    prepared = False
                    for window, owner in enumerate(range(0, duration, CORE_SAMPLES)):
                        context.checkpoint()
                        offset = max(0, owner - CONTEXT_SAMPLES)
                        stop = min(duration, owner + CORE_SAMPLES + CONTEXT_SAMPLES)
                        audio.seek(offset)
                        samples = audio.read(stop - offset, dtype="float32", always_2d=True)
                        if len(samples) != stop - offset or not np.isfinite(samples).all():
                            raise ServiceError(
                                "invalid_audio", "Input audio is truncated or contains NaN/infinity"
                            )
                        mono = samples.mean(axis=1)
                        raw_window = {
                            "offset_sample": offset,
                            "end_sample": stop,
                            "owner_sample": owner,
                        }
                        raw_windows.append(raw_window)
                        if float(np.max(np.abs(samples))) <= 1e-8:
                            raw_window["skipped"] = "digital_silence"
                        else:
                            # Avoid erasing an anti-phase stereo recording during mono conversion.
                            if float(np.max(np.abs(mono))) <= 1e-8:
                                mono = samples[:, 0].copy()
                                raw_window["downmix"] = "left_channel_phase_cancellation"
                            if not prepared:
                                backend.prepare(context, options)
                                prepared = True
                            context.checkpoint()
                            model_audio = resample_poly(mono, 1, 3).astype(np.float32)
                            result = backend.transcribe(model_audio, options)
                            context.checkpoint()
                            try:
                                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                            except (ValueError, TypeError) as error:
                                raise ServiceError(
                                    "invalid_asr_output", "ASR result is not finite JSON"
                                ) from error
                            if len(encoded.encode("utf-8")) > 1_048_576:
                                raise ServiceError(
                                    "invalid_asr_output", "ASR window result exceeds 1 MiB"
                                )
                            raw_window["result"] = result
                            found, records = _candidates(
                                result, mono, offset, owner, duration, window
                            )
                            candidates.extend(found)
                            observations.extend(records)
                        context.report(
                            0.05 + 0.9 * min(owner + CORE_SAMPLES, duration) / duration,
                            "transcribing_lyrics",
                        )
                model = backend.identity()
                segments, evidence_items = [], []
                ordered = sorted(candidates, key=lambda item: item[0]["start_sample"])
                for number, (candidate, record) in enumerate(ordered):
                    identifier = f"lyric-{number + 1:06d}"
                    candidate.update(id=identifier, source=f"{model['id']}:{model['version']}")
                    record["target_id"] = identifier
                    segments.append(candidate)
                    evidence_items.append({"target_id": identifier, "evidence": record})
                metadata = {
                    "model": model,
                    "pipeline_version": PIPELINE_VERSION,
                    "input_sha256": digest.hexdigest(),
                    "sample_rate": RATE,
                    "duration_samples": duration,
                    "inference_performed": prepared,
                    "options": options,
                    "candidate_only": True,
                }
                version = {"format_version": 1, "analysis_version": options["analysis_version"]}
                raw = {**version, **metadata, "windows": raw_windows, "reference_lyrics": reference}
                evidence_items.insert(0, {"target_id": "lyrics", "evidence": metadata})
                evidence = {**version, "items": evidence_items, "observations": observations}
                lyrics = {**version, "segments": segments}
                # JobManager publishes references only after all three files exist.
                return [
                    context.write_json(name, value)
                    for name, value in (
                        ("lyrics.json", lyrics),
                        ("lyrics_evidence.json", evidence),
                        ("asr_raw.json", raw),
                    )
                ]
        except OSError as error:
            raise ServiceError(
                "artifact_io", "Could not read or write lyric artifacts", 500
            ) from error
        finally:
            backend.close()
