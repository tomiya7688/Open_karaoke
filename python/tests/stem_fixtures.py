"""Deterministic generated test material; no third-party recordings or model outputs."""

import numpy as np

DATASET_VERSION = "generated-harmonics-v1"


def reference_stems(frames=576_013):
    t = np.arange(frames, dtype=np.float64) / 48_000
    # Harmonic, slowly modulated lead with breath-sized gaps; this is not a real singer.
    frequency = 220 * np.exp2(2 * np.sin(2 * np.pi * t / 6) / 12)
    frequency += 2 * np.sin(2 * np.pi * 5.3 * t)
    phase = 2 * np.pi * np.cumsum(frequency) / 48_000
    envelope = 0.5 - 0.5 * np.cos(2 * np.pi * np.minimum(t % 1.5, 1.2) / 1.2)
    lead = sum(np.sin(k * phase) / k for k in range(1, 9)) * envelope * 0.09
    # Deterministic bass + two panned instrumental tones/percussive envelopes.
    bass = 0.08 * np.sin(2 * np.pi * 82.4 * t)
    pluck = np.exp(-8 * (t % 0.5))
    left = bass + 0.07 * np.sin(2 * np.pi * 523.25 * t) * pluck
    right = bass + 0.07 * np.sin(2 * np.pi * 659.25 * t) * pluck
    vocals = np.stack((lead, lead * 0.95), axis=1).astype(np.float32)
    backing = np.stack((left, right), axis=1).astype(np.float32)
    return vocals, backing
