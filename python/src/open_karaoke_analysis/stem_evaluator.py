"""Initial reference-based stem evaluator; structural checks are not musical quality claims."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from .adapters import atomic_json


def si_sdr(reference, estimate) -> float | None:
    """Zero-mean, scale-invariant SDR. Silent reference is undefined, not infinity."""
    ref = np.asarray(reference, dtype=np.float64).reshape(-1)
    est = np.asarray(estimate, dtype=np.float64).reshape(-1)
    if (
        ref.shape != est.shape
        or not ref.size
        or not np.isfinite(ref).all()
        or not np.isfinite(est).all()
    ):
        raise ValueError("SI-SDR requires equal, finite, nonempty arrays")
    ref = ref - ref.mean()
    est = est - est.mean()
    energy = float(ref @ ref)
    if energy <= 1e-20:
        return None
    if float(est @ est) <= 1e-20:
        return -120.0
    target = ref * float(est @ ref) / energy
    noise = est - target
    floor = energy * 1e-12
    return float(
        10 * np.log10(max(float(target @ target), floor) / max(float(noise @ noise), floor))
    )


def evaluate_arrays(
    reference_vocals,
    reference_backing,
    vocals,
    backing,
    *,
    dataset_version="synthetic-v1",
    min_vocals_si_sdr=None,
) -> dict:
    arrays = [
        np.asarray(a, dtype=np.float64)
        for a in (reference_vocals, reference_backing, vocals, backing)
    ]
    if not arrays[0].size or any(
        a.shape != arrays[0].shape or not np.isfinite(a).all() for a in arrays
    ):
        raise ValueError("All stems must have the same nonempty shape and finite samples")
    rv, rb, v, b = arrays
    mix = rv + rb
    metrics = {}
    for name, ref, est in (("vocals", rv, v), ("accompaniment", rb, b)):
        score, baseline = si_sdr(ref, est), si_sdr(ref, mix)
        error_energy = float(np.sum((ref - est) ** 2))
        ref_energy = float(np.sum(ref**2))
        metrics[name] = {
            "si_sdr_db": score,
            "si_sdri_db": None if score is None or baseline is None else score - baseline,
            "waveform_sdr_db": None
            if ref_energy <= 1e-20
            else float(10 * np.log10(ref_energy / max(error_energy, ref_energy * 1e-12))),
            "error_rms": float(np.sqrt(np.mean((ref - est) ** 2))),
        }
    metrics["reconstruction_peak_error"] = float(np.max(np.abs(v + b - mix)))
    passed = True
    if min_vocals_si_sdr is not None:
        if not np.isfinite(min_vocals_si_sdr):
            raise ValueError("Quality threshold must be finite")
        score = metrics["vocals"]["si_sdr_db"]
        passed = score is not None and score >= min_vocals_si_sdr
    fingerprint = hashlib.sha256()
    for ref in (rv, rb):
        fingerprint.update(np.ascontiguousarray(ref, dtype="<f4").tobytes())
    return {
        "evaluator": "stem-separation",
        "version": 1,
        "dataset_version": dataset_version,
        "dataset_sha256": fingerprint.hexdigest(),
        "metrics": metrics,
        "quality_gate_applied": min_vocals_si_sdr is not None,
        "thresholds": {"min_vocals_si_sdr_db": min_vocals_si_sdr},
        "passed": passed,
    }


def evaluate_files(paths: list[Path], **kwargs) -> dict:
    stems = []
    for path in paths:
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if rate != 48_000 or audio.shape[1] != 2:
            raise ValueError("Evaluator requires 48 kHz stereo stems")
        stems.append(audio)
    if len(stems) != 4:
        raise ValueError("Four reference/estimated stems are required")
    return evaluate_arrays(*stems, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference-vocals", "reference-accompaniment", "vocals", "accompaniment"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--min-vocals-si-sdr", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = evaluate_files(
            [args.reference_vocals, args.reference_accompaniment, args.vocals, args.accompaniment],
            dataset_version=args.dataset_version,
            min_vocals_si_sdr=args.min_vocals_si_sdr,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"Evaluation failed: {error}\n")
    atomic_json(args.output, result)
    print(json.dumps(result, allow_nan=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
