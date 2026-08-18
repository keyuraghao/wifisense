"""Unsupervised motion/presence detector.

This is the honest baseline every supervised result must be compared against.
It has no training labels: you calibrate on a stretch of empty room, then
declare motion when the windowed energy exceeds what the empty room ever did.

If a random forest cannot beat this, the forest has learned nothing about
motion -- it has learned your recording schedule.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np


@dataclass
class EnergyDetector:
    """Threshold on windowed motion energy, with hysteresis and N-of-M voting.

    threshold_hi : enter MOTION when the statistic exceeds this
    threshold_lo : leave MOTION only when it drops below this (hysteresis
                   stops the output chattering at the boundary)
    n_of_m       : require n positives out of the last m windows; trades
                   detection latency for false-alarm rate
    """

    threshold_hi: float = 0.0
    threshold_lo: float = 0.0
    n_of_m: tuple[int, int] = (2, 3)
    statistic: str = "std"

    def calibrate(self, baseline_values: np.ndarray, far_quantile: float = 0.999,
                  margin: float = 1.5) -> "EnergyDetector":
        """Set thresholds from empty-room windows.

        far_quantile picks the operating point: 0.999 means roughly 1 false
        alarm per 1000 baseline windows before the N-of-M vote, which at a
        0.5 s hop is about one per 8 minutes -- then the vote suppresses most
        of those, since isolated spikes cannot win 2-of-3.
        """
        v = np.asarray(baseline_values, dtype=float)
        v = v[np.isfinite(v)]
        if v.size < 20:
            raise ValueError("need >=20 baseline windows to calibrate")
        self.threshold_hi = float(np.quantile(v, far_quantile) * margin)
        self.threshold_lo = float(np.quantile(v, far_quantile))
        return self

    def score(self, values: np.ndarray) -> np.ndarray:
        """Raw per-window decision before smoothing."""
        return np.asarray(values, dtype=float) > self.threshold_hi

    def predict(self, values: np.ndarray) -> np.ndarray:
        """Hysteresis + N-of-M smoothed decisions."""
        v = np.asarray(values, dtype=float)
        n, m = self.n_of_m
        raw = np.zeros(len(v), dtype=bool)

        state = False
        for i, x in enumerate(v):
            if state:
                state = x > self.threshold_lo
            else:
                state = x > self.threshold_hi
            raw[i] = state

        if m <= 1:
            return raw
        out = np.zeros_like(raw)
        for i in range(len(raw)):
            window = raw[max(0, i - m + 1): i + 1]
            out[i] = window.sum() >= min(n, len(window))
        return out

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path) -> "EnergyDetector":
        d = json.loads(Path(path).read_text())
        d["n_of_m"] = tuple(d["n_of_m"])
        return cls(**d)
