"""Non-destructive lyric alignment and imported-reference timelines (Issue #8)."""

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters import AnalysisContext, resolve_artifact
from .alignment_backend import REVISION, JapaneseCtcBackend
from .alignment_text import (
    MAX_DURATION,
    RATE,
    map_reference,
    normalized_chars,
    read_lyrics,
    read_reference,
    reference_timeline,
)
from .contracts import ModelInfo, Role, ServiceError
from .ctc_alignment import align_tokens, frame_samples


class AlignmentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lyrics_artifact: str = Field(min_length=1, max_length=1024)
    reference_lyrics_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    language: Literal["ja"] = "ja"
    device: Literal["cpu", "cuda"] = "cpu"
    allow_model_download: bool = True
    unit_kind: Literal["character", "whitespace_word"] = "character"
    min_token_score: float = Field(default=0.05, ge=0, le=1)
    analysis_version: int = Field(default=1, ge=1, le=4_294_967_295)


def aggregate_units(text: str, characters: list[dict], kind: str, segment_id: str) -> list[dict]:
    if kind == "whitespace_word":
        ranges = [match.span() for match in re.finditer(r"\S+", text)]
    else:
        ranges = list(dict.fromkeys((char["char_start"], char["char_end"]) for char in characters))
    units = []
    for index, (start, end) in enumerate(ranges):
        chars = [c for c in characters if start <= c["char_start"] and c["char_end"] <= end]
        timed = bool(chars) and all(c["status"] == "aligned" for c in chars)
        units.append(
            {
                "id": f"{segment_id}:unit-{index + 1:04d}",
                "text": text[start:end],
                "char_start": start,
                "char_end": end,
                "unit_kind": kind,
                "start_sample": min(c["start_sample"] for c in chars) if timed else None,
                "end_sample": max(c["end_sample"] for c in chars) if timed else None,
                "status": "aligned" if timed else "unaligned",
                "score": min(c["score"] for c in chars) if timed else None,
                "timing_confidence": None,
            }
        )
    return units


def align_segment(
    segment: dict,
    emissions,
    vocabulary: dict,
    blank_id: int,
    options: dict,
    context: AnalysisContext,
) -> dict:
    chars = [
        {**char, "start_sample": None, "end_sample": None, "status": "unaligned", "score": None}
        for char in normalized_chars(segment["text"])
    ]
    line = {
        "id": segment["id"],
        "text": segment["text"],
        "characters": chars,
        "asr_start_sample": segment["start_sample"],
        "asr_end_sample": segment["end_sample"],
        "start_sample": None,
        "end_sample": None,
        "status": "unaligned",
        "timing_confidence": None,
        "flags": [],
    }
    reason = None
    if not chars:
        reason = "no_alignable_text"
    elif emissions is None:
        reason = "digital_silence_or_too_short"
    elif len(chars) > 512:
        reason = "text_window_too_large"
    elif any(char["char"] not in vocabulary for char in chars):
        # Do not silently discard unknown letters and force the remainder across their audio.
        reason = "out_of_vocabulary"
        line["unsupported_characters"] = sorted(
            {c["char"] for c in chars if c["char"] not in vocabulary}
        )
    else:
        spans = align_tokens(
            emissions, [vocabulary[c["char"]] for c in chars], blank_id, context.checkpoint
        )
        if spans is None:
            reason = "no_ctc_path"
        else:
            for char, span in zip(chars, spans, strict=True):
                start, end = frame_samples(
                    span.start_frame,
                    span.end_frame,
                    len(emissions),
                    segment["start_sample"],
                    segment["end_sample"],
                )
                char["score"] = span.score
                char["observed_span"] = {"start_sample": start, "end_sample": end}
                if span.score >= options["min_token_score"]:
                    char.update(start_sample=start, end_sample=end, status="aligned")
                else:
                    char["reason"] = "low_acoustic_score"
            if all(char["status"] == "aligned" for char in chars):
                line.update(
                    status="aligned",
                    start_sample=chars[0]["start_sample"],
                    end_sample=chars[-1]["end_sample"],
                )
            else:
                reason = "low_acoustic_score"
            line["frame_count"] = len(emissions)
    if reason:
        line["flags"].append(reason)
    line["words"] = aggregate_units(segment["text"], chars, options["unit_kind"], segment["id"])
    return line


class AlignmentAdapter:
    info = ModelInfo(
        id="wav2vec2-ja-alignment",
        version=f"ctc-v1@{REVISION}",
        role=Role.ALIGNMENT,
        capabilities=[
            "ja",
            "ctc_alignment",
            "line_timing",
            "character_timing",
            "whitespace_word_timing",
            "reference_mapping",
        ],
    )

    def __init__(self, backend_factory=None):
        self.backend_factory = backend_factory or JapaneseCtcBackend

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return AlignmentOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid lyric alignment options") from error

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
                "dependency_missing", "Install the analysis alignment extra", 503
            ) from error
        backend = self.backend_factory()
        try:
            original, lyrics_hash = read_lyrics(context, options["lyrics_artifact"])
            reference = read_reference(context, options["reference_lyrics_artifact"])
            mapping = map_reference(original["segments"], reference, context.checkpoint)
            root = resolve_artifact(context.root, context.output_prefix)
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".alignment-", dir=root) as temp:
                path = Path(temp) / "vocals.wav"
                digest = hashlib.sha256()
                with context.input_path.open("rb") as source, path.open("wb") as target:
                    while block := source.read(1024 * 1024):
                        context.checkpoint()
                        if target.tell() + len(block) > MAX_DURATION * 8 + 1_048_576:
                            raise ServiceError(
                                "invalid_audio", "Alignment input exceeds 1 h size limit"
                            )
                        digest.update(block)
                        target.write(block)
                try:
                    audio = sf.SoundFile(path)
                except (RuntimeError, OSError) as error:
                    raise ServiceError("invalid_audio", "Input is not a readable WAV") from error
                lines, prepared, inference_count = [], False, 0
                with audio:
                    if (
                        audio.format not in {"WAV", "WAVEX", "RF64"}
                        or audio.samplerate != RATE
                        or audio.subtype != "FLOAT"
                        or audio.channels not in {1, 2}
                        or not 0 < audio.frames <= MAX_DURATION
                    ):
                        raise ServiceError(
                            "invalid_audio", "Expected 48 kHz float32 mono/stereo WAV"
                        )
                    duration = audio.frames
                    if any(
                        s["end_sample"] > duration
                        or s["end_sample"] - s["start_sample"] > 30 * RATE
                        for s in original["segments"]
                    ):
                        raise ServiceError(
                            "invalid_lyrics", "ASR windows must fit audio and be <=30 s"
                        )
                    # Check the whole snapshot, including gaps outside the ASR spans.
                    for block in audio.blocks(blocksize=65_536, dtype="float32", always_2d=True):
                        context.checkpoint()
                        if not np.isfinite(block).all():
                            raise ServiceError("invalid_audio", "Audio contains NaN or infinity")
                    for index, segment in enumerate(original["segments"]):
                        context.checkpoint()
                        start, end = segment["start_sample"], segment["end_sample"]
                        audio.seek(start)
                        samples = audio.read(end - start, dtype="float32", always_2d=True)
                        if len(samples) != end - start:
                            raise ServiceError("invalid_audio", "Truncated audio")
                        mono, emissions, flags = samples.mean(axis=1), None, []
                        non_silent = float(np.max(np.abs(samples))) > 1e-8
                        if non_silent and float(np.max(np.abs(mono))) <= 1e-8:
                            mono = samples[:, 0].copy()
                            flags.append("left_channel_phase_cancellation")
                        if non_silent and len(mono) >= 1200 and normalized_chars(segment["text"]):
                            if not prepared:
                                backend.prepare(context, options)
                                prepared = True
                            context.checkpoint()
                            emissions = backend.emissions(
                                resample_poly(mono, 1, 3).astype(np.float32)
                            )
                            context.checkpoint()
                            inference_count += 1
                        line = align_segment(
                            segment,
                            emissions,
                            backend.vocabulary,
                            backend.blank_id,
                            options,
                            context,
                        )
                        if index and start < original["segments"][index - 1]["end_sample"]:
                            flags.append("overlapping_asr_windows_review")
                        line["flags"].extend(flags)
                        lines.append(line)
                        context.report(
                            0.1 + 0.8 * (index + 1) / len(original["segments"]), "aligning_lyrics"
                        )
                canonical, items = [], []
                for segment, line in zip(original["segments"], lines, strict=True):
                    timed = line["status"] == "aligned"
                    canonical.append(
                        {
                            "id": segment["id"],
                            "text": segment["text"],
                            "start_sample": line["start_sample"]
                            if timed
                            else segment["start_sample"],
                            "end_sample": line["end_sample"] if timed else segment["end_sample"],
                            "text_confidence": segment.get("text_confidence"),
                            "timing_confidence": None,
                            "source": self.info.id if timed else segment.get("source"),
                        }
                    )
                    items.append(
                        {
                            "target_id": segment["id"],
                            "evidence": {
                                "timing_kind": "ctc_forced" if timed else "coarse_asr_fallback",
                                "original_source": segment.get("source"),
                                "flags": line["flags"],
                                "confidence_calibrated": False,
                                "alignment": line,
                            },
                        }
                    )
                units = [unit for line in lines for unit in line["words"]]
                metadata = {
                    "model": backend.identity(),
                    "pipeline_version": 1,
                    "sample_rate": RATE,
                    "duration_samples": duration,
                    "input_sha256": digest.hexdigest(),
                    "lyrics_sha256": lyrics_hash,
                    "options": options,
                    "inference_performed": inference_count > 0,
                    "aligned_units": sum(u["status"] == "aligned" for u in units),
                    "total_units": len(units),
                    "candidate_only": True,
                    "singing_accuracy_verified": False,
                    "confidence_calibrated": False,
                }
                version = {"format_version": 1, "analysis_version": options["analysis_version"]}
                details = {
                    **version,
                    **metadata,
                    "asr_original": original,
                    "lines": lines,
                    "reference": reference,
                    "reference_mapping": mapping,
                    "reference_lines": reference_timeline(reference, mapping, lines)
                    if reference is not None
                    else [],
                }
                evidence = {
                    **version,
                    "items": [{"target_id": "lyrics", "evidence": metadata}, *items],
                }
                return [
                    context.write_json(name, value)
                    for name, value in (
                        ("lyrics.json", {**version, "segments": canonical}),
                        ("alignment.json", details),
                        ("alignment_evidence.json", evidence),
                    )
                ]
        except OSError as error:
            raise ServiceError(
                "artifact_io", "Could not read or write alignment artifacts", 500
            ) from error
        finally:
            backend.close()
