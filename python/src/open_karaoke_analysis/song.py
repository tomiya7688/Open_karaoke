"""One cancellable offline Job composing existing adapters; never writes SongStore."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .adapters import AdapterRegistry, AnalysisContext, resolve_artifact, validate_adapter_options
from .contracts import ModelInfo, Role, ServiceError

RATE = 48_000
MAX_SAMPLES = 30 * 60 * RATE
MAX_AUDIO_BYTES = MAX_SAMPLES * 8 + 1_048_576
STAGES = ("stems", "lyrics", "alignment", "pitch", "events", "notes")
Stage = Literal["stems", "lyrics", "alignment", "pitch", "events", "notes"]
OUTPUTS = {
    "stems": ("vocals.wav", "accompaniment.wav", "stems.json"),
    "lyrics": ("lyrics.json", "asr_raw.json"),
    "alignment": ("lyrics.json", "alignment.json", "alignment_evidence.json"),
    "pitch": ("pitch.json",),
    "events": ("vocal_events.json",),
    "notes": ("notes.json", "note_evidence.json"),
}


class SongOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=1024)
    artist: str | None = Field(default=None, max_length=1024)
    analysis_version: int = Field(default=1, ge=1, le=2_147_483_647)
    reference_lyrics_artifact: str | None = Field(default=None, min_length=1, max_length=1024)
    models: dict[Stage, str] = Field(default_factory=dict)
    stage_options: dict[Stage, dict[str, Any]] = Field(default_factory=dict)


def _json(context: AnalysisContext, relative: str, limit: int = 256 * 1024 * 1024) -> dict:
    path = resolve_artifact(context.root, relative, must_exist=True)
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("JSON size limit")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("JSON root")
        # Disallow NaN/infinity, including values buried inside evidence.
        json.dumps(value, allow_nan=False)
        return value
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise ServiceError("invalid_song_artifact", "Invalid or oversized stage JSON") from error


def _digest(path: Path, context: AnalysisContext, limit: int = MAX_AUDIO_BYTES) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            context.checkpoint()
            size += len(block)
            if size > limit:
                raise ServiceError("song_size_limit", "Song artifact exceeds its size limit")
            digest.update(block)
    return digest.hexdigest(), size


def _snapshot(context: AnalysisContext, source: Path, name: str, limit: int) -> str:
    relative = f"{context.output_prefix}/source/{name}"
    target = resolve_artifact(context.root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        while block := reader.read(1024 * 1024):
            context.checkpoint()
            size += len(block)
            if size > limit:
                raise ServiceError("song_size_limit", "Song input exceeds its size limit")
            writer.write(block)
    context.checkpoint()
    return relative


def _audio(context: AnalysisContext, relative: str, expected_frames: int | None = None) -> int:
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as error:
        raise ServiceError(
            "dependency_missing", "Install the song/pitch analysis dependencies", 503
        ) from error
    path = resolve_artifact(context.root, relative, must_exist=True)
    try:
        with sf.SoundFile(path) as audio:
            if (
                audio.format not in {"WAV", "WAVEX"}
                or audio.subtype != "FLOAT"
                or audio.samplerate != RATE
                or audio.channels != 2
                or not 0 < audio.frames <= MAX_SAMPLES
                or (expected_frames is not None and audio.frames != expected_frames)
            ):
                raise ServiceError(
                    "invalid_audio", "Song audio must be matching 48 kHz stereo float WAV, <=30 min"
                )
            for block in audio.blocks(blocksize=65_536, dtype="float32", always_2d=True):
                context.checkpoint()
                if not np.isfinite(block).all():
                    raise ServiceError("invalid_audio", "Song audio contains NaN or infinity")
            return audio.frames
    except (RuntimeError, OSError) as error:
        raise ServiceError("invalid_audio", "Song input is not a readable WAV") from error


def _canonical(document: dict, key: str, duration: int, version: int) -> list[dict]:
    if document.get("format_version") != 1 or document.get("analysis_version") != version:
        raise ServiceError("song_version_mismatch", "Canonical output version differs from song")
    rows = document.get(key)
    if not isinstance(rows, list):
        raise ServiceError("invalid_song_artifact", "Canonical output is missing its rows")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ServiceError("invalid_song_artifact", "Canonical rows need stable IDs")
        start, end = row.get("start_sample"), row.get("end_sample")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration:
            raise ServiceError("song_timeline_mismatch", "Canonical span is outside song timeline")
        if row["id"] in seen:
            raise ServiceError("invalid_song_artifact", "Duplicate canonical ID")
        seen.add(row["id"])
        for name in ("text_confidence", "timing_confidence", "pitch_confidence"):
            probability = row.get(name)
            if probability is not None and (
                type(probability) not in {int, float} or not 0 <= probability <= 1
            ):
                raise ServiceError("invalid_song_artifact", "Invalid canonical confidence")
        if key == "segments" and not isinstance(row.get("text"), str):
            raise ServiceError("invalid_song_artifact", "Lyric text is required")
        if key == "notes" and (
            type(row.get("midi_note")) is not int or not 0 <= row["midi_note"] <= 127
        ):
            raise ServiceError("invalid_song_artifact", "Invalid canonical MIDI note")
    return rows


class SongAdapter:
    """Compose adapters in the same inference worker, not nested queued HTTP Jobs."""

    info = ModelInfo(
        id="song-pipeline-v1",
        version="1.0.0",
        role=Role.SONG,
        capabilities=["orchestration", "48khz", "song-bundle", "stage-provenance"],
    )

    def __init__(self, registry: AdapterRegistry):
        self.registry = registry

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        """
        {
          責務: [validate_options: 全曲解析の設定を検証する]
          処理: [
            1: スキーマ検証を共通関数 validate_adapter_options へ委譲する,
            2: 空のタイトル・不正なモデルID・パイプラインが所有する設定の上書きを拒否する
          ]
          引数: [options: 利用者が指定した設定]
          戻り値: [検証済みの設定dict]
          エラー: [ServiceError: invalid_options。設定が契約を満たさない]
        }
        """
        message = "Invalid full-song options"
        parsed = validate_adapter_options(SongOptions, options, message)
        reason = None
        if not parsed["title"].strip():
            reason = "Empty title"
        elif any(not model or len(model) > 128 for model in parsed["models"].values()):
            reason = "Invalid model ID"
        elif any(
            name.endswith("_artifact") or name == "analysis_version"
            for values in parsed["stage_options"].values()
            for name in values
        ):
            reason = "Pipeline-owned wiring cannot be overridden"
        if reason is not None:
            # 利用者向けの文言は役割共通のまま、原因は例外連鎖に残す(変更前と同じ扱い)。
            raise ServiceError("invalid_options", message) from ValueError(reason)
        return parsed

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        options = self.validate_options(options)
        context.checkpoint()
        if context.input_path is None:
            raise ServiceError("input_required", "A normalized source WAV is required")
        # Select every stage before inference; never fall back to mock when a model is absent.
        selected = {
            stage: self.registry.select(Role(stage), options["models"].get(stage))
            for stage in STAGES
        }
        original = _snapshot(context, context.input_path, "original_48k.wav", MAX_AUDIO_BYTES)
        duration = _audio(context, original)
        original_path = resolve_artifact(context.root, original, must_exist=True)
        source_hash, _ = _digest(original_path, context)
        reference = None
        if options["reference_lyrics_artifact"] is not None:
            source = resolve_artifact(
                context.root, options["reference_lyrics_artifact"], must_exist=True
            )
            reference = _snapshot(context, source, "reference.txt", 65_536)
            try:
                resolve_artifact(context.root, reference).read_text(encoding="utf-8-sig")
            except UnicodeError as error:
                raise ServiceError("invalid_reference", "Reference lyrics must be UTF-8") from error
        prefix = {stage: f"{context.output_prefix}/stages/{stage}" for stage in STAGES}
        vocals = f"{prefix['stems']}/vocals.wav"
        wiring = {
            "stems": {},
            "lyrics": {"reference_lyrics_artifact": reference},
            "alignment": {
                "lyrics_artifact": f"{prefix['lyrics']}/lyrics.json",
                "reference_lyrics_artifact": reference,
            },
            "pitch": {},
            "events": {
                "pitch_artifact": f"{prefix['pitch']}/pitch.json",
                "alignment_artifact": f"{prefix['alignment']}/alignment.json",
            },
            "notes": {
                "pitch_artifact": f"{prefix['pitch']}/pitch.json",
                "vocal_event_artifact": f"{prefix['events']}/vocal_events.json",
                "alignment_artifact": f"{prefix['alignment']}/alignment.json",
            },
        }
        prepared = {}
        for stage in STAGES:
            values = {**options["stage_options"].get(stage, {}), **wiring[stage]}
            if stage != "stems":
                values["analysis_version"] = options["analysis_version"]
            prepared[stage] = selected[stage].validate_options(values)
        artifacts, stages, vocal_hash = [original], [], None
        observed = {original: _digest(original_path, context)}
        if reference is not None:
            artifacts.append(reference)
            observed[reference] = _digest(resolve_artifact(context.root, reference), context)
        for index, stage in enumerate(STAGES):
            context.checkpoint()
            context.report(index / 7, f"song.{stage}")
            child = AnalysisContext(
                context.root,
                prefix[stage],
                original_path
                if stage == "stems"
                else resolve_artifact(context.root, vocals, must_exist=True),
                context.cancelled,
                lambda progress, name, i=index, s=stage: context.report(
                    (i + min(1.0, max(0.0, progress))) / 7, f"song.{s}.{name}"
                ),
            )
            try:
                produced = selected[stage].analyze(child, prepared[stage])
            except ServiceError as error:
                raise ServiceError(
                    error.detail.code,
                    f"song.{stage}: {error.detail.message}",
                    error.status,
                    error.detail.retryable,
                ) from error
            context.checkpoint()
            if (
                not isinstance(produced, list)
                or not 1 <= len(produced) <= 32
                or any(not isinstance(item, str) for item in produced)
                or len(set(produced)) != len(produced)
            ):
                raise ServiceError("invalid_song_artifact", "Invalid stage artifact list")
            names = set()
            for relative in produced:
                if not relative.startswith(prefix[stage] + "/"):
                    raise ServiceError("invalid_song_artifact", "Stage returned unowned artifact")
                name = relative[len(prefix[stage]) + 1 :]
                if not re.fullmatch(r"[A-Za-z0-9_-]+\.(json|wav)", name):
                    raise ServiceError("invalid_song_artifact", "Unexpected artifact filename")
                path = resolve_artifact(context.root, relative, must_exist=True)
                observed[relative] = _digest(path, context)
                names.add(name)
            if not set(OUTPUTS[stage]) <= names:
                raise ServiceError("invalid_song_artifact", "Stage is missing required outputs")
            if stage == "stems":
                _audio(context, vocals, duration)
                _audio(context, f"{prefix[stage]}/accompaniment.wav", duration)
                vocal_hash, _ = _digest(resolve_artifact(context.root, vocals), context)
                if _json(context, f"{prefix[stage]}/stems.json").get("input_sha256") != source_hash:
                    raise ServiceError("song_input_mismatch", "Stems used a different snapshot")
            if stage in {"lyrics", "alignment", "pitch", "events", "notes"}:
                name = {
                    "lyrics": "asr_raw.json",
                    "alignment": "alignment.json",
                    "pitch": "pitch.json",
                    "events": "vocal_events.json",
                    "notes": "note_evidence.json",
                }[stage]
                metadata = _json(context, f"{prefix[stage]}/{name}")
                if (
                    metadata.get("input_sha256") != vocal_hash
                    or metadata.get("sample_rate") != RATE
                    or metadata.get("duration_samples") != duration
                ):
                    raise ServiceError("song_input_mismatch", "Stage audio identity differs")
            artifacts.extend(produced)
            stages.append(
                {
                    "role": stage,
                    "model": selected[stage].info.model_dump(mode="json"),
                    "options": prepared[stage],
                    "artifacts": [
                        "provenance/" + name[len(context.output_prefix) + 1 :] for name in produced
                    ],
                    "worker_artifacts": produced,
                }
            )
        lyrics = _json(context, f"{prefix['alignment']}/lyrics.json")
        notes = _json(context, f"{prefix['notes']}/notes.json")
        segments = _canonical(lyrics, "segments", duration, options["analysis_version"])
        rows = _canonical(notes, "notes", duration, options["analysis_version"])
        ids = {segment["id"] for segment in segments}
        if any(
            row.get("lyric_segment_id") is not None and row["lyric_segment_id"] not in ids
            for row in rows
        ):
            raise ServiceError("song_reference_mismatch", "Note refers to a missing lyric segment")
        assets = []
        for relative in artifacts:
            path = resolve_artifact(context.root, relative, must_exist=True)
            digest, size = _digest(path, context)
            if (digest, size) != observed[relative]:
                raise ServiceError("song_input_changed", "An earlier stage artifact changed")
            destination = "provenance/" + relative[len(context.output_prefix) + 1 :]
            assets.append(
                {"source": relative, "destination": destination, "sha256": digest, "bytes": size}
            )
        for source, destination in (
            (original, "audio/original_48k.wav"),
            (vocals, "audio/vocals.wav"),
            (f"{prefix['stems']}/accompaniment.wav", "audio/accompaniment.wav"),
        ):
            descriptor = next(asset for asset in assets if asset["source"] == source)
            assets.append({**descriptor, "destination": destination})
        identity = {
            "pipeline_version": 1,
            "source_sha256": source_hash,
            "reference_sha256": next(
                (a["sha256"] for a in assets if a["source"] == reference), None
            ),
            "stages": [
                {
                    "model": stage["model"],
                    "options": {
                        key: value
                        for key, value in stage["options"].items()
                        if not key.endswith("_artifact")
                    },
                }
                for stage in stages
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        provenance = {
            **identity,
            "analysis_fingerprint": fingerprint,
            "vocals_sha256": vocal_hash,
            "stages": stages,
            "singing_accuracy_verified": False,
        }
        version = {"format_version": 1, "analysis_version": options["analysis_version"]}
        dependencies = {
            "original": [],
            "vocals": ["original"],
            "accompaniment": ["original"],
            "lyrics": ["vocals"],
            "notes": ["vocals", "lyrics"],
            "evidence": ["lyrics", "notes"],
        }
        bundle = {
            "song": {
                **version,
                "song_id": context.output_prefix.rsplit("/", 1)[-1],
                "title": options["title"],
                "artist": options["artist"],
                "sample_rate": RATE,
                "duration_samples": duration,
                "files": {
                    "original": "audio/original_48k.wav",
                    "vocals": "audio/vocals.wav",
                    "accompaniment": "audio/accompaniment.wav",
                    "lyrics": "analysis/lyrics.json",
                    "notes": "analysis/notes.json",
                    "evidence": "analysis/evidence.json",
                },
                "artifacts": {
                    kind: {
                        "generation": 1,
                        "valid": True,
                        "depends_on": required,
                        "analysis_version": options["analysis_version"],
                    }
                    for kind, required in dependencies.items()
                },
            },
            "lyrics": lyrics,
            "notes": notes,
            "evidence": {**version, "items": [{"target_id": "song", "evidence": provenance}]},
        }
        context.report(0.98, "song.publishing")
        result = context.write_json(
            "song-result.json", {"format_version": 1, "bundle": bundle, "assets": assets}
        )
        return [*artifacts, result]
