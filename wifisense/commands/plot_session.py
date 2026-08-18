#!/usr/bin/env python3
"""Visualise one session: raw RSSI, motion signal, spectrogram, rate.

    .venv/bin/python scripts/plot_session.py data/sessions/walking__20260818-011500
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy import signal as sps

from ..signal import preprocess as pp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", help="session directory")
    ap.add_argument("--fs", type=float, default=100.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    sess = Path(args.session)
    df = pp.load_capture(sess / "capture.csv")
    meta = json.loads((sess / "meta.json").read_text()) if (sess / "meta.json").exists() else {}
    prep = pp.prepare(df, fs=args.fs)
    t = prep["t"] - prep["t"][0]

    fig, axes = plt.subplots(4, 1, figsize=(13, 11), constrained_layout=True)

    axes[0].plot(t, prep["raw"], lw=0.5, alpha=0.5, label="raw")
    axes[0].plot(t, prep["clean"], lw=0.8, label="hampel")
    axes[0].set_ylabel("RSSI (dBm)")
    axes[0].legend(loc="upper right", fontsize=8)
    rate = meta.get("rate_hz")
    rate_s = f"{rate:.0f}" if isinstance(rate, (int, float)) else "?"
    axes[0].set_title(f"{sess.name}   label={meta.get('label','?')}   "
                      f"{rate_s} Hz   ch{meta.get('channel','?')}")

    axes[1].plot(t, prep["motion"], lw=0.6, color="crimson")
    axes[1].set_ylabel(f"motion {pp.MOTION_BAND_HZ[0]}-{pp.MOTION_BAND_HZ[1]} Hz (dB)")
    axes[1].axhline(0, color="k", lw=0.4)

    nper = min(len(prep["motion"]), 256)
    f, tt, Sxx = sps.spectrogram(prep["motion"], fs=args.fs, nperseg=nper,
                                 noverlap=nper // 2)
    m = f <= 40
    axes[2].pcolormesh(tt, f[m], 10 * np.log10(Sxx[m] + 1e-12), shading="gouraud")
    axes[2].set_ylabel("Hz")
    axes[2].set_title("motion spectrogram (dB)", fontsize=9)

    # Instantaneous frame rate: a sanity check that traffic stayed steady.
    raw_t = df["t"].to_numpy()
    bins = np.arange(raw_t[0], raw_t[-1], 1.0)
    counts, edges = np.histogram(raw_t, bins=bins)
    axes[3].plot(edges[:-1] - raw_t[0], counts, lw=0.8, color="teal")
    axes[3].set_ylabel("frames/s")
    axes[3].set_xlabel("time (s)")
    axes[3].set_title(f"capture rate (coverage {prep['coverage']:.1%})", fontsize=9)

    out = Path(args.out) if args.out else sess / "overview.png"
    fig.savefig(out, dpi=130)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
