"""Assemble labelled feature tables from recorded sessions.

Session layout on disk:

    data/sessions/<label>__<YYYYmmdd-HHMMSS>/
        capture.csv    per-frame RSSI rows
        meta.json      label, link info, capture stats, notes
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ..signal import features as ft
from ..signal import preprocess as pp

SESSIONS_DIR = Path("data/sessions")


@dataclass
class Session:
    path: Path
    label: str
    meta: dict

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def capture_csv(self) -> Path:
        return self.path / "capture.csv"


def discover(sessions_dir=SESSIONS_DIR) -> list[Session]:
    sessions = []
    for d in sorted(Path(sessions_dir).glob("*")):
        meta_path = d / "meta.json"
        if not (d.is_dir() and meta_path.exists() and (d / "capture.csv").exists()):
            continue
        meta = json.loads(meta_path.read_text())
        sessions.append(Session(path=d, label=meta.get("label", "unknown"), meta=meta))
    return sessions


def session_features(
    sess: Session,
    fs: float = 100.0,
    win_s: float = 2.0,
    hop_s: float = 0.5,
    include_level: bool = False,
    trim_s: float = 2.0,
) -> pd.DataFrame:
    """Window one session. `trim_s` drops the head and tail of the recording,
    where you were still walking to/from the laptop after hitting enter."""
    df = pp.load_capture(sess.capture_csv)
    if len(df) < 50:
        return pd.DataFrame()

    if trim_s > 0:
        t0, t1 = df["t"].iloc[0], df["t"].iloc[-1]
        df = df[(df["t"] >= t0 + trim_s) & (df["t"] <= t1 - trim_s)]
        if len(df) < 50:
            return pd.DataFrame()

    prepared = pp.prepare(df, fs=fs)
    feats = ft.sliding_windows(
        prepared, win_s=win_s, hop_s=hop_s, include_level=include_level
    )
    if feats.empty:
        return feats

    feats["label"] = sess.label
    feats["session"] = sess.name
    return feats


def build(
    sessions_dir=SESSIONS_DIR,
    fs: float = 100.0,
    win_s: float = 2.0,
    hop_s: float = 0.5,
    include_level: bool = False,
) -> tuple[pd.DataFrame, list[dict]]:
    """Build the full dataset. Returns (features, per-session summary)."""
    rows, summary = [], []
    for sess in discover(sessions_dir):
        f = session_features(
            sess, fs=fs, win_s=win_s, hop_s=hop_s, include_level=include_level
        )
        summary.append({
            "session": sess.name,
            "label": sess.label,
            "windows": len(f),
            "frames": sess.meta.get("frames_kept"),
            "rate_hz": sess.meta.get("rate_hz"),
            "duration_s": sess.meta.get("duration_s"),
        })
        if not f.empty:
            rows.append(f)

    data = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return data, summary
