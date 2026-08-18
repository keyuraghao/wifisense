#!/usr/bin/env python3
"""Evaluate and fit the classifier, against the unsupervised baseline.

    .venv/bin/python scripts/train.py
    .venv/bin/python scripts/train.py --baseline-label empty
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from ..models.classify import (
    fit_final, feature_importance, grouped_cv_report, save_model,
)
from ..models.detector import EnergyDetector


def baseline_eval(df: pd.DataFrame, baseline_label: str, stat: str) -> None:
    """Calibrate the threshold detector on one class, score binary detection."""
    if baseline_label not in set(df["label"]):
        print(f"(skipping baseline: no '{baseline_label}' class)")
        return

    print(f"\n{'=' * 62}\nBASELINE  EnergyDetector on '{stat}' "
          f"(calibrated on '{baseline_label}')\n{'=' * 62}")

    sessions = df["session"].unique()
    y_true, y_pred = [], []
    for held_out in sessions:
        train = df[(df["session"] != held_out) & (df["label"] == baseline_label)]
        test = df[df["session"] == held_out]
        if len(train) < 20 or test.empty:
            continue
        det = EnergyDetector(statistic=stat).calibrate(train[stat].to_numpy())
        y_pred.append(det.predict(test[stat].to_numpy()))
        y_true.append((test["label"] != baseline_label).to_numpy())

    if not y_true:
        print("not enough sessions for leave-one-session-out baseline")
        return

    yt, yp = np.concatenate(y_true), np.concatenate(y_pred)
    tp = int((yt & yp).sum()); fp = int((~yt & yp).sum())
    fn = int((yt & ~yp).sum()); tn = int((~yt & ~yp).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    print(f"  accuracy {(tp + tn) / len(yt):.3f}   precision {prec:.3f}   "
          f"recall {rec:.3f}   F1 {f1:.3f}")
    print(f"  TP {tp}  FP {fp}  FN {fn}  TN {tn}")
    print(f"  false-alarm rate on '{baseline_label}': {fp / max(1, fp + tn):.4f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/datasets/features.parquet")
    ap.add_argument("--out", default="models/rf.joblib")
    ap.add_argument("--baseline-label", default="empty")
    ap.add_argument("--baseline-stat", default="std")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    path = Path(args.data)
    if not path.exists():
        print(f"error: {path} not found -- run scripts/build_dataset.py first")
        return 1
    df = pd.read_parquet(path)

    print(f"{len(df)} windows | {df['session'].nunique()} sessions | "
          f"{df['label'].nunique()} classes")
    print(df.groupby("label")["session"].nunique().rename("sessions").to_string())

    baseline_eval(df, args.baseline_label, args.baseline_stat)

    print(f"\n{'=' * 62}\nSUPERVISED  RandomForest, session-grouped CV\n{'=' * 62}")
    try:
        res = grouped_cv_report(df, seed=args.seed)
    except ValueError as e:
        print(f"error: {e}")
        return 1

    print(f"  {res['n_splits']}-fold GroupKFold over {res['n_sessions']} sessions")
    print(f"  accuracy {res['accuracy']:.3f}\n")
    print(res["report"])
    print(res["confusion"].to_string())

    model, feats = fit_final(df, seed=args.seed)
    print(f"\ntop features:\n{feature_importance(model, feats).to_string()}")

    save_model(model, feats, args.out)
    print(f"\nsaved -> {args.out}")

    if res["n_sessions"] < 4:
        print("\nCAUTION: fewer than 4 sessions. This accuracy is an anecdote, "
              "not a result. Record more, on different days and positions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
