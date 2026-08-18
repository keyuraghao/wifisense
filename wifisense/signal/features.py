"""Windowed feature extraction for RSSI (and, unchanged, CSI amplitude) series.

Design rule: every feature must be invariant to the absolute RSSI level.
Absolute level encodes distance-to-router and antenna orientation, not
occupancy -- a model that learns it will score beautifully offline and collapse
the moment you move the laptop. So the features here are all computed on the
bandpassed motion signal, with the two level features kept separate and clearly
labelled so you can ablate them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal as sps
from scipy import stats

# Sub-bands of the motion spectrum, chosen from what the body actually does.
# NOTE: resolving a band requires win_s >= ~2/(band width). The breathing band
# is 0.6 Hz wide, so meaningful respiration features need win_s >= 8 s, not the
# 2 s default. The features are computed at any window length; they are only
# *informative* above that.
SUB_BANDS = {
    "quasi_static": (0.2, 0.8),   # breathing, postural sway
    "slow": (0.8, 3.0),           # torso translation, walking cadence
    "mid": (3.0, 10.0),           # limb swing, gestures
    "fast": (10.0, 30.0),         # rapid limb tips, fidget
}


def _spectral(x: np.ndarray, fs: float) -> dict:
    n = len(x)
    nperseg = min(n, max(64, int(fs * 2)))
    freqs, psd = sps.welch(x, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)

    # Rectangular integration (sum * bin width), NOT trapezoid. Trapezoid needs
    # two samples, so any band narrower than the FFT resolution silently
    # returned exactly 0.0 -- which is what the 0.2-0.8 Hz breathing band does
    # at the default 2 s window (0.5 Hz bins). Rectangular is also the correct
    # way to integrate a PSD over uniform bins.
    df_bin = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 1.0
    total = float(psd.sum() * df_bin) + 1e-12
    out = {}
    for name, (lo, hi) in SUB_BANDS.items():
        m = (freqs >= lo) & (freqs < hi)
        bp = float(psd[m].sum() * df_bin)
        out[f"bp_{name}"] = bp
        out[f"bpr_{name}"] = bp / total          # ratio -> gain invariant

    p = psd / psd.sum() if psd.sum() > 0 else psd
    out["spec_centroid"] = float((freqs * p).sum())
    out["spec_spread"] = float(np.sqrt(((freqs - out["spec_centroid"]) ** 2 * p).sum()))
    out["spec_entropy"] = float(-(p[p > 0] * np.log(p[p > 0])).sum() / np.log(len(p)))
    out["peak_freq"] = float(freqs[np.argmax(psd)])
    out["peak_power"] = float(psd.max())
    out["total_power"] = total
    return out


def _time_domain(x: np.ndarray) -> dict:
    ax = np.abs(x)
    dx = np.diff(x)
    return {
        "std": float(x.std()),
        "var": float(x.var()),
        "mad": float(np.median(np.abs(x - np.median(x)))),
        "iqr": float(np.subtract(*np.percentile(x, [75, 25]))),
        "range": float(x.max() - x.min()),
        "p95_abs": float(np.percentile(ax, 95)),
        "mean_abs": float(ax.mean()),
        "skew": float(stats.skew(x)) if x.std() > 0 else 0.0,
        "kurtosis": float(stats.kurtosis(x)) if x.std() > 0 else 0.0,
        "zcr": float((np.diff(np.sign(x)) != 0).mean()) if len(x) > 1 else 0.0,
        # First difference: sensitive to fast transitions, cheap motion proxy.
        "diff_std": float(dx.std()) if len(dx) else 0.0,
        "diff_mean_abs": float(np.abs(dx).mean()) if len(dx) else 0.0,
        # Autocorrelation at short lag -> smoothness vs. noise-likeness.
        "acf_lag1": float(np.corrcoef(x[:-1], x[1:])[0, 1]) if len(x) > 2 and x.std() > 0 else 0.0,
    }


def window_features(
    motion: np.ndarray,
    clean: np.ndarray,
    fs: float,
    include_level: bool = False,
) -> dict:
    """Features for one window. `motion` is bandpassed, `clean` is the raw level."""
    feats = _time_domain(motion)
    feats.update(_spectral(motion, fs))
    if include_level:
        # Deliberately opt-in: useful for localisation, poisonous for
        # position-invariant presence detection. See module docstring.
        feats["level_mean"] = float(clean.mean())
        feats["level_std"] = float(clean.std())
    return feats


def sliding_windows(
    prepared: dict,
    win_s: float = 2.0,
    hop_s: float = 0.5,
    min_coverage: float = 0.8,
    include_level: bool = False,
) -> pd.DataFrame:
    """Slide a window over a prepared capture and emit one feature row each.

    Windows whose valid-sample coverage falls below `min_coverage` are dropped:
    a window that is mostly interpolated across a dropout carries no channel
    information, and keeping it teaches the model that dropouts mean stillness.
    """
    fs = prepared["fs"]
    motion, clean, valid, t = (
        prepared["motion"], prepared["clean"], prepared["valid"], prepared["t"]
    )
    w = int(win_s * fs)
    h = max(1, int(hop_s * fs))
    if len(motion) < w:
        return pd.DataFrame()

    rows = []
    for start in range(0, len(motion) - w + 1, h):
        sl = slice(start, start + w)
        cov = float(valid[sl].mean())
        if cov < min_coverage:
            continue
        f = window_features(motion[sl], clean[sl], fs, include_level=include_level)
        f["t_start"] = float(t[start])
        f["t_center"] = float(t[start + w // 2])
        f["coverage"] = cov
        rows.append(f)

    return pd.DataFrame(rows)


FEATURE_PREFIXES_EXCLUDED = ("t_start", "t_center", "coverage", "label", "session")


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in FEATURE_PREFIXES_EXCLUDED]
