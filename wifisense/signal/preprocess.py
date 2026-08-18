"""Turn an irregular stream of per-frame RSSI samples into a clean uniform series.

Order matters here and the reasons are physical, not cosmetic:

  1. deduplicate/sort   - libpcap timestamps can tie or reorder across queues
  2. resample           - frames arrive at whatever rate the MAC felt like;
                          every spectral method downstream assumes a fixed fs
  3. hampel             - rate adaptation and retransmits produce genuine
                          outliers that are NOT motion; a median filter kills
                          them without smearing real transitions the way a
                          mean filter would
  4. detrend            - AGC drift and thermal drift are slow and huge
                          relative to motion; remove them or they dominate
                          every variance feature you compute
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal as sps

# Human motion occupies roughly this band as seen through RSSI fluctuation.
# Below ~0.2 Hz you are looking at drift; above ~40 Hz at a 100 Hz sample rate
# you are looking at noise and aliased MAC-layer artefacts.
MOTION_BAND_HZ = (0.3, 40.0)
BREATHING_BAND_HZ = (0.15, 0.6)   # 9-36 breaths/min


def load_capture(path) -> pd.DataFrame:
    """Read a capture CSV, drop unusable rows, return time-sorted frames."""
    df = pd.read_csv(path)
    df = df.dropna(subset=["t", "rssi_dbm"])
    # Drop non-physical levels. See RSSICapture.max_rssi_dbm: 0 dBm rows are our
    # own transmitted frames looped back, not measurements of the channel.
    df = df[df["rssi_dbm"].between(-100, -1)]
    df = df.sort_values("t", kind="stable").reset_index(drop=True)
    return df


def resample_uniform(
    t: np.ndarray,
    x: np.ndarray,
    fs: float = 100.0,
    max_gap_s: float = 0.25,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Interpolate (t, x) onto a uniform grid at `fs` Hz.

    Returns (t_grid, x_grid, valid_mask). Samples that fall inside a gap longer
    than `max_gap_s` are marked invalid rather than silently interpolated --
    a 2-second hole filled by a straight line looks like perfect stillness to a
    variance detector, which is exactly the failure mode that makes a presence
    detector report "empty room" during a dropout.
    """
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    if t.size < 2:
        return np.empty(0), np.empty(0), np.empty(0, dtype=bool)

    t0, t1 = t[0], t[-1]
    n = int(np.floor((t1 - t0) * fs)) + 1
    t_grid = t0 + np.arange(n) / fs
    x_grid = np.interp(t_grid, t, x)

    # Mark grid points whose bracketing samples are too far apart.
    idx = np.searchsorted(t, t_grid, side="right")
    idx = np.clip(idx, 1, len(t) - 1)
    gaps = t[idx] - t[idx - 1]
    valid = gaps <= max_gap_s

    return t_grid, x_grid, valid


def hampel(x: np.ndarray, window: int = 11, n_sigma: float = 3.0) -> np.ndarray:
    """Replace outliers with the local median (rolling MAD criterion).

    MAD is scaled by 1.4826 so that it estimates sigma for Gaussian data.
    """
    s = pd.Series(np.asarray(x, dtype=float))
    med = s.rolling(window, center=True, min_periods=1).median()
    mad = (s - med).abs().rolling(window, center=True, min_periods=1).median()
    sigma = 1.4826 * mad
    out = s.where((s - med).abs() <= n_sigma * sigma.replace(0, np.nan), med)
    return out.fillna(s).to_numpy()


def bandpass(x: np.ndarray, fs: float, band=MOTION_BAND_HZ, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth bandpass. filtfilt so peaks do not shift in time."""
    lo, hi = band
    nyq = fs / 2.0
    hi = min(hi, nyq * 0.99)
    if lo >= hi:
        raise ValueError(f"invalid band {band} for fs={fs}")
    sos = sps.butter(order, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
    padlen = 3 * (2 * order + 1)
    if len(x) <= padlen:
        return np.zeros_like(x)
    return sps.sosfiltfilt(sos, x)


def detrend_moving(x: np.ndarray, fs: float, seconds: float = 5.0) -> np.ndarray:
    """Subtract a moving average -- a cheap highpass that keeps sample count."""
    win = max(3, int(seconds * fs))
    baseline = pd.Series(x).rolling(win, center=True, min_periods=1).mean().to_numpy()
    return x - baseline


def prepare(
    df: pd.DataFrame,
    fs: float = 100.0,
    max_gap_s: float = 0.25,
    do_hampel: bool = True,
) -> dict:
    """Full RSSI conditioning chain. Returns a dict of aligned arrays."""
    t_grid, x_grid, valid = resample_uniform(
        df["t"].to_numpy(), df["rssi_dbm"].to_numpy(), fs=fs, max_gap_s=max_gap_s
    )
    if t_grid.size == 0:
        raise ValueError("capture too short to resample")

    x_clean = hampel(x_grid) if do_hampel else x_grid
    x_motion = bandpass(x_clean, fs, MOTION_BAND_HZ)

    return {
        "t": t_grid,
        "raw": x_grid,
        "clean": x_clean,
        "motion": x_motion,
        "valid": valid,
        "fs": fs,
        "coverage": float(valid.mean()),
    }
