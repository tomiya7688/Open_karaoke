"""Algorithmic F0 detector interface and commercial-safe baseline detectors."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np

YIN_ID = "yin"
NACF_ID = "nacf"
DETECTOR_VERSION = "1.0.0"


@dataclass(frozen=True)
class PitchObservation:
    detector: str
    hz: float | None
    confidence: float
    voiced_probability: float
    reliability: float
    rms: float
    evidence: dict[str, float | int | str | bool | None]

    def as_dict(self) -> dict:
        return {
            "detector": self.detector,
            "hz": self.hz,
            "confidence": self.confidence,
            "voiced_probability": self.voiced_probability,
            "reliability": self.reliability,
            "rms": self.rms,
            "evidence": self.evidence,
        }


class PitchDetector(Protocol):
    id: str
    version: str
    reliability: float

    def detect(
        self,
        frame: np.ndarray,
        sample_rate: int,
        min_hz: float,
        max_hz: float,
        energy_floor: float,
    ) -> PitchObservation: ...


def _finite_frame(frame: np.ndarray) -> np.ndarray:
    values = np.asarray(frame, dtype=np.float64)
    if values.ndim != 1 or values.size < 8:
        raise ValueError("Pitch detector frame must be one-dimensional and non-empty")
    if not np.isfinite(values).all():
        raise ValueError("Pitch detector frame contains NaN or infinity")
    return values


def _linear_autocorrelation(values: np.ndarray) -> np.ndarray:
    length = len(values)
    fft_length = 1 << (2 * length - 1).bit_length()
    spectrum = np.fft.rfft(values, fft_length)
    return np.fft.irfft(spectrum * np.conj(spectrum), fft_length)[:length]


def _lag_bounds(length: int, sample_rate: int, min_hz: float, max_hz: float) -> tuple[int, int]:
    minimum = max(2, int(math.floor(sample_rate / max_hz)))
    maximum = min(length - 2, int(math.ceil(sample_rate / min_hz)))
    if maximum <= minimum:
        raise ValueError("Pitch detector frame is too short for the requested frequency range")
    return minimum, maximum


def _parabolic_position(values: np.ndarray, index: int, origin: int = 0) -> float:
    position = float(index + origin)
    if not 0 < index < len(values) - 1:
        return position
    left, center, right = (float(item) for item in values[index - 1 : index + 2])
    denominator = left - 2.0 * center + right
    if abs(denominator) > 1e-12:
        position += 0.5 * (left - right) / denominator
    return position


def _energy_probability(rms: float, energy_floor: float) -> float:
    if energy_floor <= 0:
        return 1.0
    return float(np.clip(rms / (energy_floor * 4.0), 0.0, 1.0))


class YinDetector:
    """FFT-accelerated YIN-style cumulative mean normalized difference detector."""

    id = YIN_ID
    version = DETECTOR_VERSION
    reliability = 1.0
    threshold = 0.18

    def detect(
        self,
        frame: np.ndarray,
        sample_rate: int,
        min_hz: float,
        max_hz: float,
        energy_floor: float,
    ) -> PitchObservation:
        values = _finite_frame(frame)
        centered = values - float(np.mean(values))
        rms = float(np.sqrt(np.mean(centered * centered)))
        if rms < energy_floor:
            return PitchObservation(
                self.id,
                None,
                0.0,
                0.0,
                self.reliability,
                rms,
                {"reason": "below_energy_floor", "threshold": self.threshold},
            )

        minimum_lag, maximum_lag = _lag_bounds(
            len(centered), sample_rate, min_hz, max_hz
        )
        autocorrelation = _linear_autocorrelation(centered)
        squared = centered * centered
        prefix = np.concatenate(([0.0], np.cumsum(squared)))
        lags = np.arange(1, maximum_lag + 1)
        difference = (
            prefix[len(centered) - lags]
            + (prefix[len(centered)] - prefix[lags])
            - 2.0 * autocorrelation[lags]
        )
        difference = np.maximum(difference, 0.0)
        cumulative = np.cumsum(difference)
        cmnd = np.ones(maximum_lag + 1, dtype=np.float64)
        cmnd[1:] = difference * lags / np.maximum(cumulative, 1e-12)

        selected: int | None = None
        for lag in range(minimum_lag, maximum_lag):
            if (
                cmnd[lag] < self.threshold
                and cmnd[lag] <= cmnd[lag - 1]
                and cmnd[lag] <= cmnd[lag + 1]
            ):
                selected = lag
                break
        if selected is None:
            selected = minimum_lag + int(np.argmin(cmnd[minimum_lag : maximum_lag + 1]))

        refined_lag = _parabolic_position(cmnd, selected)
        if refined_lag <= 0:
            refined_lag = float(selected)
        hz = float(sample_rate / refined_lag)
        confidence = float(np.clip(1.0 - cmnd[selected], 0.0, 1.0))
        voiced_probability = confidence * _energy_probability(rms, energy_floor)
        return PitchObservation(
            self.id,
            hz,
            confidence,
            voiced_probability,
            self.reliability,
            rms,
            {
                "lag": refined_lag,
                "cmnd": float(cmnd[selected]),
                "threshold": self.threshold,
            },
        )


class NormalizedAutocorrelationDetector:
    """Normalized autocorrelation detector with a mild long-lag fundamental preference."""

    id = NACF_ID
    version = DETECTOR_VERSION
    reliability = 0.85

    def detect(
        self,
        frame: np.ndarray,
        sample_rate: int,
        min_hz: float,
        max_hz: float,
        energy_floor: float,
    ) -> PitchObservation:
        values = _finite_frame(frame)
        centered = values - float(np.mean(values))
        rms = float(np.sqrt(np.mean(centered * centered)))
        if rms < energy_floor:
            return PitchObservation(
                self.id,
                None,
                0.0,
                0.0,
                self.reliability,
                rms,
                {"reason": "below_energy_floor"},
            )

        windowed = centered * np.hanning(len(centered))
        minimum_lag, maximum_lag = _lag_bounds(
            len(windowed), sample_rate, min_hz, max_hz
        )
        autocorrelation = _linear_autocorrelation(windowed)
        squared = windowed * windowed
        prefix = np.concatenate(([0.0], np.cumsum(squared)))
        lags = np.arange(minimum_lag, maximum_lag + 1)
        left_energy = prefix[len(windowed) - lags]
        right_energy = prefix[len(windowed)] - prefix[lags]
        denominator = np.sqrt(np.maximum(left_energy * right_energy, 1e-12))
        correlation = np.clip(autocorrelation[lags] / denominator, -1.0, 1.0)

        peaks = np.flatnonzero(
            (correlation[1:-1] >= correlation[:-2])
            & (correlation[1:-1] >= correlation[2:])
        ) + 1
        if len(peaks) == 0:
            selected_index = int(np.argmax(correlation))
        else:
            peak_values = correlation[peaks]
            best = float(np.max(peak_values))
            eligible = peaks[peak_values >= max(0.2, best * 0.98)]
            selected_index = int(
                eligible[-1] if len(eligible) else peaks[int(np.argmax(peak_values))]
            )

        refined_lag = _parabolic_position(correlation, selected_index, minimum_lag)
        if refined_lag <= 0:
            refined_lag = float(lags[selected_index])
        hz = float(sample_rate / refined_lag)
        confidence = float(np.clip(correlation[selected_index], 0.0, 1.0))
        voiced_probability = confidence * _energy_probability(rms, energy_floor)
        return PitchObservation(
            self.id,
            hz,
            confidence,
            voiced_probability,
            self.reliability,
            rms,
            {
                "lag": refined_lag,
                "normalized_correlation": float(correlation[selected_index]),
                "peak_count": int(len(peaks)),
            },
        )


DETECTOR_FACTORIES = {
    YIN_ID: YinDetector,
    NACF_ID: NormalizedAutocorrelationDetector,
}
