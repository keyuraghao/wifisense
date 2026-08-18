"""ESP32 CSI reader (Phase 1 hardware path).

Why this exists: the MT7921 in this laptop exports RSSI only -- one scalar per
frame. CSI gives you a complex gain per OFDM subcarrier, i.e. 52-234 numbers
per frame instead of 1, which is what makes frequency-selective fading, path
separation, and breathing extraction possible. A ~$5 ESP32 running Espressif's
esp-csi firmware is the cheapest legitimate CSI source that exists.

Wire format emitted by the esp-csi examples over USB serial:

    CSI_DATA,<type>,<mac>,<rssi>,<rate>,<sig_mode>,<mcs>,<bandwidth>,
    <smoothing>,<not_sounding>,<aggregation>,<stbc>,<fec_coding>,<sgi>,
    <noise_floor>,<ampdu_cnt>,<channel>,<secondary_channel>,
    <local_timestamp>,<ant>,<sig_len>,<rx_state>,<len>,<first_word>,
    "[i0,q0,i1,q1,...]"

The trailing array is int8 pairs in (imaginary, real) order -- note the order,
it is the single most common source of wrong phase in reimplementations.
"""
from __future__ import annotations

import csv
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ARRAY_RE = re.compile(r"\[([-\d,\s]+)\]")

# Subcarrier layouts by payload length. ESP32 zero-pads guard bands, so the
# usable set is a subset -- using all of them feeds the model pure noise
# columns that dominate a variance-based feature.
LLTF_VALID = np.r_[np.arange(6, 32), np.arange(33, 59)]  # 52 usable of 64


def parse_csi_line(line: str) -> dict | None:
    """Parse one CSI_DATA line into a dict with a complex `csi` array."""
    if not line.startswith("CSI_DATA"):
        return None
    m = ARRAY_RE.search(line)
    if not m:
        return None

    head = line[: m.start()].rstrip(", ").split(",")
    try:
        raw = np.fromstring(m.group(1), sep=",", dtype=np.int8)
    except Exception:
        return None
    if raw.size < 2 or raw.size % 2:
        return None

    # (imag, real) interleaved -> complex
    csi = raw[1::2].astype(np.float32) + 1j * raw[0::2].astype(np.float32)

    def geti(i, default=0):
        try:
            return int(head[i])
        except (IndexError, ValueError):
            return default

    return {
        "mac": head[2] if len(head) > 2 else "",
        "rssi": geti(3),
        "rate": geti(4),
        "mcs": geti(6),
        "bandwidth": geti(7),
        "noise_floor": geti(14),
        "channel": geti(16),
        "local_timestamp": geti(18),
        "n_sub": csi.size,
        "csi": csi,
    }


def usable_subcarriers(n_sub: int) -> np.ndarray:
    """Indices carrying data for a given CSI payload width."""
    if n_sub == 64:
        return LLTF_VALID
    # HT-LTF and wider layouts: drop DC and the outermost guard bins.
    guard = max(1, n_sub // 16)
    idx = np.arange(guard, n_sub - guard)
    return idx[idx != n_sub // 2]


@dataclass
class ESP32CSICapture:
    """Read CSI lines from an ESP32 serial port and stream amplitudes to CSV."""

    port: str = "/dev/ttyUSB0"
    baud: int = 921600
    out_path: Path | str = "data/sessions/csi/capture.csv"
    peer: str | None = None          # filter by transmitter MAC
    n_frames: int = 0                # 0 = unlimited
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    count: int = field(default=0, init=False)

    def run(self, seconds: float | None = None, progress=None) -> int:
        import serial  # pyserial; imported lazily so RSSI-only users need no device

        out = Path(self.out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + seconds if seconds else None
        header_written = False

        with serial.Serial(self.port, self.baud, timeout=1) as ser, \
             out.open("w", newline="") as fh:
            w = csv.writer(fh)
            while not self._stop.is_set():
                if deadline and time.time() > deadline:
                    break
                if self.n_frames and self.count >= self.n_frames:
                    break

                raw = ser.readline()
                if not raw:
                    continue
                rec = parse_csi_line(raw.decode("utf-8", errors="ignore").strip())
                if rec is None:
                    continue
                if self.peer and rec["mac"].lower() != self.peer.lower():
                    continue

                idx = usable_subcarriers(rec["n_sub"])
                amp = np.abs(rec["csi"][idx])
                phase = np.angle(rec["csi"][idx])

                if not header_written:
                    w.writerow(["t", "rssi_dbm", "noise_dbm", "channel"]
                               + [f"amp_{i}" for i in idx]
                               + [f"ph_{i}" for i in idx])
                    header_written = True

                w.writerow([f"{time.time():.6f}", rec["rssi"], rec["noise_floor"],
                            rec["channel"], *np.round(amp, 3), *np.round(phase, 4)])
                self.count += 1
                if progress and self.count % 100 == 0:
                    progress(self.count)
        return self.count

    def stop(self) -> None:
        self._stop.set()


def csi_to_motion_series(amp_matrix: np.ndarray) -> np.ndarray:
    """Reduce an (n_packets, n_subcarriers) amplitude matrix to one series.

    Standard trick from the CSI literature: subcarriers observe the same
    physical motion through different frequency-selective fades, so the motion
    is the dominant *common* component. Taking the first principal component
    after per-subcarrier standardisation both denoises and hands you a scalar
    that drops straight into the existing RSSI feature pipeline unchanged.
    """
    X = np.asarray(amp_matrix, dtype=float)
    X = X - X.mean(axis=0, keepdims=True)
    sd = X.std(axis=0, keepdims=True)
    X = X / np.where(sd > 1e-9, sd, 1.0)

    # Economy SVD: first right-singular vector is PC1.
    _, _, Vt = np.linalg.svd(X, full_matrices=False)
    pc1 = X @ Vt[0]
    return pc1
