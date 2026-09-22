"""Bounded-memory 48 kHz separation with overlap/add and exact-length outputs."""

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .contracts import ServiceError

SAMPLE_RATE = 48_000
MAX_FRAMES = SAMPLE_RATE * 60 * 30


def inspect_audio(path: Path):
    try:
        info = sf.info(path)
    except (RuntimeError, OSError) as error:
        raise ServiceError("invalid_audio", "Input is not a readable normalized WAV") from error
    if info.format not in {"WAV", "WAVEX"} or info.samplerate != SAMPLE_RATE or info.channels != 2:
        raise ServiceError("invalid_audio", "Input must be a 48 kHz stereo WAV; normalize it first")
    if not 0 < info.frames <= MAX_FRAMES:
        raise ServiceError("invalid_audio", "Input must be nonempty and at most 30 minutes")
    return info


def separate_audio(source: Path, directory: Path, backend, context, options: dict) -> dict:
    info = inspect_audio(source)
    chunk = options["chunk_seconds"] * SAMPLE_RATE
    fade = options["overlap_seconds"] * SAMPLE_RATE
    hop = chunk - fade
    # Accumulators live on disk rather than holding a whole song and all model stems in RAM.
    accum_path, norm_path = directory / ".accum", directory / ".norm"
    accum = np.memmap(accum_path, mode="w+", dtype="float32", shape=(info.frames, 2))
    norm = np.memmap(norm_path, mode="w+", dtype="float32", shape=(info.frames,))
    peaks = {"vocals": 0.0, "accompaniment": 0.0}
    reconstruction_peak = 0.0
    try:
        with sf.SoundFile(source) as audio:
            for start in range(0, info.frames, hop):
                context.checkpoint()
                audio.seek(start)
                mix = audio.read(min(chunk, info.frames - start), dtype="float32", always_2d=True)
                if not np.isfinite(mix).all():
                    raise ServiceError("invalid_audio", "Input contains non-finite samples")
                length = len(mix)
                if np.any(mix):
                    resampled = resample_poly(mix, 147, 160, axis=0).astype(np.float32)
                    estimated = np.asarray(backend.vocals(resampled), dtype=np.float32)
                    if estimated.shape != resampled.shape or not np.isfinite(estimated).all():
                        raise ServiceError(
                            "invalid_model_output", "Separator returned invalid samples", 500
                        )
                    vocal = resample_poly(estimated, 160, 147, axis=0)[:length].astype(np.float32)
                    if vocal.shape != mix.shape or not np.isfinite(vocal).all():
                        raise ServiceError(
                            "invalid_model_output", "Resampled stem has invalid shape", 500
                        )
                else:
                    vocal = np.zeros_like(mix)
                context.checkpoint()
                weights = np.ones(length, dtype=np.float32)
                edge = min(fade, length)
                if start > 0:
                    weights[:edge] *= np.arange(1, edge + 1, dtype=np.float32) / (edge + 1)
                if start + length < info.frames:
                    weights[-edge:] *= np.arange(edge, 0, -1, dtype=np.float32) / (edge + 1)
                accum[start : start + length] += vocal * weights[:, None]
                norm[start : start + length] += weights
                context.report(0.1 + 0.7 * min(1, (start + length) / info.frames), "separating")

            with (
                sf.SoundFile(
                    directory / "vocals.wav",
                    "w",
                    samplerate=SAMPLE_RATE,
                    channels=2,
                    subtype="FLOAT",
                    format="WAV",
                ) as vocals,
                sf.SoundFile(
                    directory / "accompaniment.wav",
                    "w",
                    samplerate=SAMPLE_RATE,
                    channels=2,
                    subtype="FLOAT",
                    format="WAV",
                ) as accompaniment,
            ):
                audio.seek(0)
                for start in range(0, info.frames, 65_536):
                    context.checkpoint()
                    mix = audio.read(
                        min(65_536, info.frames - start), dtype="float32", always_2d=True
                    )
                    end = start + len(mix)
                    scale = np.asarray(norm[start:end])
                    if np.any(scale <= 0):
                        raise ServiceError(
                            "invalid_model_output", "Overlap/add left uncovered samples", 500
                        )
                    vocal = np.asarray(accum[start:end]) / scale[:, None]
                    # Preserve the original mixture, including frequencies above the model bandwidth.
                    backing = mix - vocal
                    if not np.isfinite(vocal).all() or not np.isfinite(backing).all():
                        raise ServiceError(
                            "invalid_model_output", "Non-finite output after mixing", 500
                        )
                    vocals.write(vocal)
                    accompaniment.write(backing)
                    peaks["vocals"] = max(peaks["vocals"], float(np.max(np.abs(vocal))))
                    peaks["accompaniment"] = max(
                        peaks["accompaniment"], float(np.max(np.abs(backing)))
                    )
                    reconstruction_peak = max(
                        reconstruction_peak, float(np.max(np.abs(vocal + backing - mix)))
                    )
    finally:
        # Explicitly close mappings before unlinking, including on Windows.
        accum._mmap.close()
        norm._mmap.close()
        accum_path.unlink(missing_ok=True)
        norm_path.unlink(missing_ok=True)
    return {
        "sample_rate": SAMPLE_RATE,
        "channels": 2,
        "duration_samples": info.frames,
        "peak_amplitude": peaks,
        "exceeds_unity": any(p > 1 for p in peaks.values()),
        "reconstruction_peak_error": reconstruction_peak,
    }
