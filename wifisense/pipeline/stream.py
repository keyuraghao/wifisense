"""Live streaming helpers: follow a growing capture, hold a rolling window.

Capture needs root; plotting does not. Rather than run a GUI as root, the
capture process writes a tailable CSV and the viewer follows it. That split
also means you can watch a session live *while* it is being recorded, and
replay it afterwards through exactly the same display code.
"""
from __future__ import annotations

import collections
import csv
import io
import time
from pathlib import Path

import numpy as np
import pandas as pd


class CSVTailer:
    """Follow a CSV that another process is appending to.

    Survives the file not existing yet, and detects truncation/rotation (a new
    capture starting) by watching for the size going backwards.
    """

    def __init__(self, path, t_col: str = "t", v_col: str = "rssi_dbm"):
        self.path = Path(path)
        self.t_col, self.v_col = t_col, v_col
        self._fh: io.TextIOBase | None = None
        self._header: list[str] | None = None
        self._pending = ""
        self._pos = 0

    def _open(self) -> bool:
        if self._fh is not None:
            return True
        if not self.path.exists():
            return False
        self._fh = self.path.open("r")
        self._header, self._pending, self._pos = None, "", 0
        return True

    def _reset(self) -> None:
        if self._fh:
            self._fh.close()
        self._fh = None

    def poll(self) -> list[tuple[float, float]]:
        """Return (t, value) pairs appended since the last call."""
        if not self._open():
            return []

        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            self._reset()
            return []
        if size < self._pos:          # truncated -> a new capture started
            self._reset()
            return []

        chunk = self._fh.read()
        if not chunk:
            return []
        self._pos = self._fh.tell()

        data = self._pending + chunk
        lines = data.split("\n")
        self._pending = lines.pop()   # last element is a partial line

        out = []
        for line in lines:
            if not line:
                continue
            fields = next(csv.reader([line]), None)
            if not fields:
                continue
            if self._header is None:
                if self.t_col in fields:
                    self._header = fields
                continue
            try:
                row = dict(zip(self._header, fields))
                out.append((float(row[self.t_col]), float(row[self.v_col])))
            except (KeyError, ValueError):
                continue
        return out


class RingBuffer:
    """Fixed-duration ring of (t, value) samples, trimmed by timestamp.

    Duration is enforced by `trim()`, not by the deque's maxlen. maxlen is only
    a memory backstop for the case where trim() is never called -- sizing it
    from an assumed rate silently drops history the moment the real capture
    rate exceeds the assumption, which shows up as a plot that will not fill.
    """

    def __init__(self, seconds: float, max_hz: float = 5000.0):
        self.seconds = seconds
        self.buf: collections.deque = collections.deque(
            maxlen=max(1024, int(seconds * max_hz))
        )

    def add(self, t: float, v: float) -> None:
        self.buf.append((t, v))

    def add_many(self, pairs) -> None:
        for t, v in pairs:
            self.buf.append((t, v))

    def trim(self, now: float | None = None) -> None:
        if not self.buf:
            return
        cutoff = (now if now is not None else self.buf[-1][0]) - self.seconds
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def __len__(self) -> int:
        return len(self.buf)

    @property
    def span_s(self) -> float:
        return self.buf[-1][0] - self.buf[0][0] if len(self.buf) > 1 else 0.0

    @property
    def rate_hz(self) -> float:
        return len(self.buf) / self.span_s if self.span_s > 0 else 0.0

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(list(self.buf), columns=["t", "rssi_dbm"])

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.buf:
            return np.empty(0), np.empty(0)
        a = np.asarray(self.buf, dtype=float)
        return a[:, 0], a[:, 1]


class SessionReplayer:
    """Emit a recorded session at wall-clock speed, as if it were live.

    Lets you develop and demo the real-time display with no radio and no root.
    """

    def __init__(self, capture_csv, speed: float = 1.0):
        df = pd.read_csv(capture_csv, usecols=["t", "rssi_dbm"])
        df = df.sort_values("t").reset_index(drop=True)
        self.t = df["t"].to_numpy(dtype=float)
        self.v = df["rssi_dbm"].to_numpy(dtype=float)
        self.t0 = self.t[0] if len(self.t) else 0.0
        self.speed = speed
        self.i = 0
        self.start_wall = time.time()

    @property
    def exhausted(self) -> bool:
        return self.i >= len(self.t)

    def poll(self) -> list[tuple[float, float]]:
        elapsed = (time.time() - self.start_wall) * self.speed
        j = int(np.searchsorted(self.t, self.t0 + elapsed, side="right"))
        if j <= self.i:
            return []
        # Re-base timestamps onto wall clock so downstream code sees "now".
        out = [(self.start_wall + (self.t[k] - self.t0) / self.speed, self.v[k])
               for k in range(self.i, j)]
        self.i = j
        return out
