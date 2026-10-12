"""Independent acoustic, pitch, lyric and note evidence for Vocal Event fusion."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .adapters import AnalysisContext, resolve_artifact
from .contracts import ServiceError

RATE = 48_000
WINDOW_SAMPLES = 1_920
HOP_SAMPLES = 480
MAX_DURATION_SAMPLES = 30 * 60 * RATE


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant: {value}")


def read_json_artifact(
    context: AnalysisContext,
    relative: str,
    *,
    max_bytes: int,
    error_code: str,
) -> dict:
    path = resolve_artifact(context.root, relative, must_exist=True)
    try:
        if path.stat().st_size > max_bytes:
            raise ServiceError(error_code, "Evidence artifact exceeds size limit")
        raw = json.loads(path.read_bytes(), parse_constant=_reject_constant)
    except ServiceError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise ServiceError(error_code, "Evidence artifact is not valid JSON") from error
    if not isinstance(raw, dict):
        raise ServiceError(error_code, "Evidence artifact root must be an object")
    return raw


def sha256_file(context: AnalysisContext, path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                context.checkpoint()
                digest.update(block)
    except OSError as error:
        raise ServiceError("artifact_io", "Could not hash vocal-event input", 500) from error
    return digest.hexdigest()


def _normalize_positive(values):
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    positive = array[array > 1e-12]
    if positive.size == 0:
        return np.zeros_like(array)
    scale = float(np.percentile(positive, 95))
    if scale <= 1e-12:
        return np.zeros_like(array)
    return np.clip(array / scale, 0.0, 1.0)


def extract_audio_features(context: AnalysisContext) -> dict:
    if context.input_path is None:
        raise ServiceError("input_required", "Vocal Event analysis requires a vocals WAV artifact")
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as error:
        raise ServiceError(
            "dependency_missing", "Install the analysis package with the events extra", 503
        ) from error

    try:
        audio = sf.SoundFile(context.input_path)
    except (RuntimeError, OSError) as error:
        raise ServiceError("invalid_audio", "Input is not a readable WAV") from error

    rms_values = []
    flux_values = []
    rise_values = []
    phase_fallback = []
    previous_spectrum = None
    previous_db = None
    window = np.hanning(WINDOW_SAMPLES)

    with audio:
        if (
            audio.format not in {"WAV", "WAVEX", "RF64"}
            or audio.samplerate != RATE
            or audio.subtype != "FLOAT"
            or audio.channels not in {1, 2}
            or not 0 < audio.frames <= MAX_DURATION_SAMPLES
        ):
            raise ServiceError("invalid_audio", "Expected <=30 min 48 kHz float32 mono/stereo WAV")
        duration_samples = int(audio.frames)
        starts = list(range(0, duration_samples, HOP_SAMPLES))
        for index, start in enumerate(starts):
            context.checkpoint()
            end = min(start + WINDOW_SAMPLES, duration_samples)
            audio.seek(start)
            block = audio.read(end - start, dtype="float32", always_2d=True)
            if len(block) != end - start:
                raise ServiceError("invalid_audio", "Vocal Event input was truncated")
            if not np.isfinite(block).all():
                raise ServiceError("invalid_audio", "Vocal Event input contains NaN or infinity")

            mono = block.mean(axis=1, dtype=np.float64)
            fallback = False
            if audio.channels == 2 and len(block):
                channel_rms = np.sqrt(np.mean(block.astype(np.float64) ** 2, axis=0))
                mono_rms = float(np.sqrt(np.mean(mono * mono)))
                loudest = int(np.argmax(channel_rms))
                if float(channel_rms[loudest]) > 1e-5 and mono_rms < channel_rms[loudest] * 0.1:
                    mono = block[:, loudest].astype(np.float64)
                    fallback = True

            if len(mono) < WINDOW_SAMPLES:
                mono = np.pad(mono, (0, WINDOW_SAMPLES - len(mono)))
            rms = float(np.sqrt(np.mean(mono * mono)))
            db = 20.0 * math.log10(max(rms, 1e-8))
            spectrum = np.abs(np.fft.rfft(mono * window))
            spectrum /= max(float(np.sum(spectrum)), 1e-12)
            if previous_spectrum is None:
                flux = 0.0
            else:
                positive = np.maximum(spectrum - previous_spectrum, 0.0)
                flux = float(np.sqrt(np.mean(positive * positive)))
            rise = max(0.0, db - previous_db) if previous_db is not None else 0.0

            rms_values.append(rms)
            flux_values.append(flux)
            rise_values.append(rise)
            phase_fallback.append(fallback)
            previous_spectrum = spectrum
            previous_db = db

            if index % 200 == 0:
                context.report(0.03 + 0.25 * (index + 1) / len(starts), "extracting_acoustics")

    energy = _normalize_positive(rms_values)
    spectral_flux = _normalize_positive(flux_values)
    energy_rise = _normalize_positive(rise_values)
    energy_delta_raw = [0.0]
    for current, previous in zip(rms_values[1:], rms_values[:-1], strict=True):
        current_db = 20.0 * math.log10(max(current, 1e-8))
        previous_db = 20.0 * math.log10(max(previous, 1e-8))
        energy_delta_raw.append(abs(current_db - previous_db))
    energy_delta = _normalize_positive(energy_delta_raw)
    acoustic_onset = np.clip(0.65 * spectral_flux + 0.35 * energy_rise, 0.0, 1.0)

    return {
        "duration_samples": duration_samples,
        "starts": starts,
        "energy_rms": [float(value) for value in rms_values],
        "energy": [float(value) for value in energy],
        "spectral_flux_raw": [float(value) for value in flux_values],
        "spectral_flux": [float(value) for value in spectral_flux],
        "energy_delta": [float(value) for value in energy_delta],
        "acoustic_onset": [float(value) for value in acoustic_onset],
        "phase_fallback": phase_fallback,
    }


def read_pitch_features(
    context: AnalysisContext,
    relative: str,
    *,
    duration_samples: int,
    input_sha256: str,
) -> dict:
    import numpy as np

    document = read_json_artifact(
        context, relative, max_bytes=128 * 1024 * 1024, error_code="invalid_pitch"
    )
    frames = document.get("frames")
    if (
        document.get("format_version") != 1
        or document.get("sample_rate") != RATE
        or document.get("hop_samples") != HOP_SAMPLES
        or document.get("duration_samples") != duration_samples
        or document.get("input_sha256") != input_sha256
        or not isinstance(frames, list)
    ):
        raise ServiceError("invalid_pitch", "Pitch timeline does not match the vocals input")

    expected_count = math.ceil(duration_samples / HOP_SAMPLES)
    if len(frames) != expected_count:
        raise ServiceError("invalid_pitch", "Pitch timeline has an unexpected frame count")

    f0 = []
    voiced_probability = []
    f0_delta_cents = []
    f0_transition = []
    voiced_transition = []
    previous_f0 = None
    previous_voiced = None
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict) or frame.get("index") != index:
            raise ServiceError("invalid_pitch", "Pitch frames must be ordered and indexed")
        if frame.get("start_sample") != index * HOP_SAMPLES:
            raise ServiceError("invalid_pitch", "Pitch frame grid does not match the event grid")
        hz = frame.get("f0_hz")
        probability = frame.get("voiced_probability")
        if hz is not None and (
            isinstance(hz, bool)
            or not isinstance(hz, (int, float))
            or not math.isfinite(hz)
            or hz <= 0
        ):
            raise ServiceError("invalid_pitch", "Pitch F0 values must be positive finite numbers")
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(probability)
            or not 0.0 <= probability <= 1.0
        ):
            raise ServiceError("invalid_pitch", "Voiced probability must be finite and bounded")

        current_f0 = float(hz) if hz is not None else None
        current_voiced = float(probability)
        if current_f0 is not None and previous_f0 is not None:
            cents = abs(1200.0 * math.log2(current_f0 / previous_f0))
        else:
            cents = 0.0
        transition = min(1.0, max(0.0, (cents - 35.0) / 165.0))
        vuv = abs(current_voiced - previous_voiced) if previous_voiced is not None else 0.0
        if (current_f0 is None) != (previous_f0 is None) and index:
            vuv = max(vuv, 0.8)

        f0.append(current_f0)
        voiced_probability.append(current_voiced)
        f0_delta_cents.append(cents)
        f0_transition.append(transition)
        voiced_transition.append(min(1.0, vuv))
        previous_f0 = current_f0
        previous_voiced = current_voiced

    if not np.isfinite(np.asarray(voiced_probability)).all():
        raise ServiceError("invalid_pitch", "Pitch timeline contains non-finite values")
    return {
        "f0_hz": f0,
        "voiced_probability": voiced_probability,
        "f0_delta_cents": f0_delta_cents,
        "f0_transition": f0_transition,
        "voiced_transition": voiced_transition,
    }


def _timestamp_signal(events: list[dict], frame_count: int) -> list[float]:
    values = [0.0] * frame_count
    for event in events:
        sample = event["sample"]
        strength = event["strength"]
        index = min(frame_count - 1, max(0, int(round(sample / HOP_SAMPLES))))
        for offset, factor in ((-1, 0.35), (0, 1.0), (1, 0.35)):
            target = index + offset
            if 0 <= target < frame_count:
                values[target] = max(values[target], min(1.0, strength * factor))
    return values


def read_alignment_evidence(
    context: AnalysisContext,
    relative: str | None,
    *,
    duration_samples: int,
    frame_count: int,
) -> tuple[list[float], list[dict], bool]:
    if relative is None:
        return [0.0] * frame_count, [], False
    document = read_json_artifact(
        context, relative, max_bytes=64 * 1024 * 1024, error_code="invalid_alignment"
    )
    if document.get("format_version") != 1 or not isinstance(document.get("lines"), list):
        raise ServiceError("invalid_alignment", "Expected a version-1 alignment artifact")

    events: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for line in document["lines"]:
        if not isinstance(line, dict):
            continue
        collections = [
            ("character", line.get("characters", [])),
            ("unit", line.get("words", [])),
            ("phoneme", line.get("phonemes", [])),
            ("syllable", line.get("syllables", [])),
        ]
        for kind, items in collections:
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("status") not in {None, "aligned"}:
                    continue
                sample = item.get("start_sample")
                if isinstance(sample, bool) or not isinstance(sample, int):
                    continue
                if not 0 <= sample < duration_samples:
                    raise ServiceError("invalid_alignment", "Alignment timestamp exceeds audio")
                score = item.get("score")
                strength = (
                    float(score)
                    if isinstance(score, (int, float))
                    and not isinstance(score, bool)
                    and math.isfinite(score)
                    else 0.75
                )
                key = (sample, kind)
                if key not in seen:
                    seen.add(key)
                    events.append(
                        {
                            "sample": sample,
                            "strength": min(1.0, max(0.05, strength)),
                            "kind": kind,
                            "id": item.get("id"),
                        }
                    )
        line_start = line.get("start_sample")
        if isinstance(line_start, int) and not isinstance(line_start, bool) and line_start >= 0:
            events.append(
                {
                    "sample": line_start,
                    "strength": 0.9,
                    "kind": "line",
                    "id": line.get("id"),
                }
            )
    return _timestamp_signal(events, frame_count), events, True


def read_note_onsets(
    context: AnalysisContext,
    relative: str | None,
    *,
    duration_samples: int,
    frame_count: int,
) -> tuple[list[float], list[dict], bool]:
    if relative is None:
        return [0.0] * frame_count, [], False
    document = read_json_artifact(
        context, relative, max_bytes=32 * 1024 * 1024, error_code="invalid_notes"
    )
    notes = document.get("notes")
    if document.get("format_version") != 1 or not isinstance(notes, list):
        raise ServiceError("invalid_notes", "Expected a version-1 note candidate artifact")
    events = []
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            raise ServiceError("invalid_notes", "Note candidates must be objects")
        sample = note.get("start_sample")
        confidence = note.get("pitch_confidence")
        if (
            isinstance(sample, bool)
            or not isinstance(sample, int)
            or not 0 <= sample < duration_samples
        ):
            raise ServiceError("invalid_notes", "Note onset is outside the vocals input")
        strength = 0.8
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(confidence)
        ):
            strength = min(1.0, max(0.05, float(confidence)))
        events.append(
            {
                "sample": sample,
                "strength": strength,
                "kind": "note_onset",
                "id": note.get("id", f"note-{index + 1}"),
            }
        )
    return _timestamp_signal(events, frame_count), events, True


def derive_silence_breath(
    energy: list[float],
    spectral_flux: list[float],
    voiced_probability: list[float],
) -> tuple[list[float], list[float], list[float]]:
    silence = []
    breath = []
    boundary = []
    previous_silence = None
    for energy_value, flux, voiced in zip(energy, spectral_flux, voiced_probability, strict=True):
        current_silence = (1.0 - voiced) * (1.0 - energy_value)
        current_breath = (1.0 - voiced) * energy_value * min(1.0, 0.35 + flux)
        transition = (
            abs(current_silence - previous_silence) if previous_silence is not None else 0.0
        )
        silence.append(min(1.0, current_silence))
        breath.append(min(1.0, current_breath))
        boundary.append(min(1.0, max(transition, current_breath * 0.65)))
        previous_silence = current_silence
    return silence, breath, boundary
