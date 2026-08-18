#!/usr/bin/env python3
"""Turn recorded sessions into a windowed feature table.

    .venv/bin/python scripts/build_dataset.py
    .venv/bin/python scripts/build_dataset.py --win 3.0 --hop 0.25
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense.pipeline.dataset import build


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default="data/sessions")
    ap.add_argument("--out", default="data/datasets/features.parquet")
    ap.add_argument("--fs", type=float, default=100.0, help="resample rate (Hz)")
    ap.add_argument("--win", type=float, default=2.0, help="window length (s)")
    ap.add_argument("--hop", type=float, default=0.5, help="window hop (s)")
    ap.add_argument("--include-level", action="store_true",
                    help="add absolute RSSI level features (see features.py)")
    args = ap.parse_args()

    data, summary = build(args.sessions, fs=args.fs, win_s=args.win,
                          hop_s=args.hop, include_level=args.include_level)

    print(pd.DataFrame(summary).to_string(index=False) if summary
          else f"no sessions found in {args.sessions}")
    if data.empty:
        print("\nno windows produced -- record sessions first with scripts/collect.py")
        return 1

    print("\nclass balance:")
    print(data["label"].value_counts().to_string())

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data.to_parquet(out, index=False)
    print(f"\nwrote {len(data)} windows x {data.shape[1]} cols -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
