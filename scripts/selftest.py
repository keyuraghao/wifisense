#!/usr/bin/env python3
"""Generate synthetic sessions and run the whole pipeline end to end.

    .venv/bin/python scripts/selftest.py

This validates capture-format -> preprocess -> features -> baseline -> model
without any radio. It is NOT a result: the synthetic channel is a toy. Its only
job is to fail loudly if you break the pipeline.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense.models.classify import grouped_cv_report
from wifisense.models.detector import EnergyDetector
from wifisense.pipeline.dataset import build

OUT = Path("data/sessions_synthetic")

# Each class is a mixture of narrowband components in the motion band, on top of
# a slow drift and quantisation to 1 dB (radiotap reports integer dBm).
CLASSES = {
    "empty":   dict(comps=[],                          noise=0.35),
    "sitting": dict(comps=[(0.30, 0.45), (0.9, 0.25)], noise=0.40),
    "walking": dict(comps=[(1.6, 1.8), (3.2, 1.1), (0.7, 1.4)], noise=0.55),
}
SESSIONS_PER_CLASS = 3
DURATION_S = 90.0
FRAME_RATE = 300.0


def synth_session(label: str, seed: int, path: Path) -> dict:
    rng = np.random.default_rng(seed)
    spec = CLASSES[label]

    # Poisson-ish arrivals: real 802.11 frames are not evenly spaced.
    gaps = rng.exponential(1.0 / FRAME_RATE, int(DURATION_S * FRAME_RATE))
    t = np.cumsum(gaps)
    t = t[t < DURATION_S]

    level = rng.uniform(-58, -38)                       # per-session RSSI offset
    drift = 1.2 * np.sin(2 * np.pi * 0.01 * t + rng.uniform(0, 6.28))
    x = level + drift

    for f0, amp in spec["comps"]:
        f = f0 * rng.uniform(0.85, 1.15)                # per-session variation
        a = amp * rng.uniform(0.8, 1.25)
        x = x + a * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))

    x = x + rng.normal(0, spec["noise"], t.size)
    x = np.round(x)                                     # 1 dB radiotap quantisation

    path.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "t": t + 1.7e9, "rssi_dbm": x.astype(int), "noise_dbm": -95,
        "freq_mhz": 5280, "rate_mbps": 6, "ftype": "data", "subtype": 8,
        "addr1": "aa:bb:cc:dd:ee:ff", "addr2": "20:37:f0:75:cb:d6",
        "addr3": "20:37:f0:75:cb:d6", "seq": np.arange(t.size) % 4096,
        "len": 128,
    }).to_csv(path / "capture.csv", index=False)

    meta = {"label": label, "synthetic": True, "frames_kept": int(t.size),
            "duration_s": float(t[-1] - t[0]),
            "rate_hz": float(t.size / (t[-1] - t[0])), "channel": 56}
    (path / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)

    print("generating synthetic sessions...")
    seed = 0
    for label in CLASSES:
        for i in range(SESSIONS_PER_CLASS):
            seed += 1
            m = synth_session(label, seed, OUT / f"{label}__synth{i}")
            print(f"  {label:<8} synth{i}  {m['frames_kept']:6d} frames "
                  f"@ {m['rate_hz']:.0f} Hz")

    print("\nbuilding features...")
    df, summary = build(OUT, fs=100.0, win_s=2.0, hop_s=0.5)
    print(pd.DataFrame(summary)[["session", "label", "windows"]].to_string(index=False))
    assert not df.empty, "FAIL: no windows produced"

    print("\nbaseline (leave-one-session-out, calibrated on 'empty'):")
    labels, fired = [], []
    for s in df["session"].unique():
        tr = df[(df["session"] != s) & (df["label"] == "empty")]
        te = df[df["session"] == s]
        if len(tr) < 20:
            continue
        det = EnergyDetector().calibrate(tr["std"].to_numpy())
        fired.append(det.predict(te["std"].to_numpy()))
        labels.append(te["label"].to_numpy())
    lab, fir = np.concatenate(labels), np.concatenate(fired)

    rates = {c: float(fir[lab == c].mean()) for c in sorted(set(lab))}
    for c, r in rates.items():
        kind = "false-alarm rate" if c == "empty" else "detection rate"
        print(f"  {c:<8} {kind}: {r:.3f}")

    far = rates.get("empty", 1.0)
    walk_recall = rates.get("walking", 0.0)
    sit_recall = rates.get("sitting", 0.0)
    print(f"\n  -> gross motion is trivially separable by energy alone;"
          f"\n     a seated, breathing body is not (recall {sit_recall:.2f})."
          f"\n     That gap is the entire argument for moving to CSI in Phase 2.")
    base_ok = walk_recall > 0.8 and far < 0.10

    print("\nsupervised (session-grouped CV):")
    res = grouped_cv_report(df)
    print(f"  {res['n_splits']}-fold over {res['n_sessions']} sessions")
    print(f"  accuracy: {res['accuracy']:.3f}")
    print(res["confusion"].to_string())

    chance = df["label"].value_counts(normalize=True).max()
    print(f"\nchance (majority class): {chance:.3f}")

    ok = res["accuracy"] > chance + 0.15 and base_ok
    print(f"\n{'PASS' if ok else 'FAIL'}: pipeline "
          f"{'works end to end' if ok else 'did not clear the sanity bar'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
