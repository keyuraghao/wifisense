"""Per-frame RSSI capture from a monitor-mode interface.

This is the measurement primitive available on *any* WiFi chipset, including
ones with no CSI export (MT7921, most modern MediaTek/Realtek parts). Each
received frame yields one scalar: received power in dBm, quantised to 1 dB by
the radiotap header.

Signal model. RSSI is the squared magnitude of the sum over propagation paths:

    P(t) = | sum_k a_k(t) * exp(-j * 2*pi*f * tau_k(t)) |^2

A body moving through the room changes a_k and tau_k for the paths that hit it,
so P(t) fluctuates. That is the entire physical basis of RSSI sensing -- and
also its limit: one scalar per frame collapses all subcarriers and all paths,
so you can detect *that* something moved far more easily than *what* moved.
"""
from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from scapy.layers.dot11 import Dot11, RadioTap
from scapy.sendrecv import AsyncSniffer

FRAME_TYPES = {0: "mgmt", 1: "ctrl", 2: "data", 3: "ext"}

COLUMNS = [
    "t", "rssi_dbm", "noise_dbm", "freq_mhz", "rate_mbps",
    "ftype", "subtype", "addr1", "addr2", "addr3", "seq", "len",
]


@dataclass
class CaptureStats:
    frames: int = 0
    kept: int = 0
    rejected: int = 0
    first_t: float | None = None
    last_t: float | None = None
    rssi_sum: float = 0.0

    @property
    def duration_s(self) -> float:
        if self.first_t is None or self.last_t is None:
            return 0.0
        return self.last_t - self.first_t

    @property
    def rate_hz(self) -> float:
        return self.kept / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def mean_rssi(self) -> float:
        return self.rssi_sum / self.kept if self.kept else float("nan")


def _radiotap_field(pkt, name: str, default=None):
    """Radiotap fields are optional and driver-dependent; missing != zero."""
    try:
        value = pkt.getfieldval(name)
    except (AttributeError, KeyError, IndexError):
        return default
    return default if value is None else value


@dataclass
class RSSICapture:
    """Sniff frames on a monitor interface and stream them to CSV.

    peer: only keep frames where this MAC appears as transmitter or receiver.
          Normally the router BSSID -- it restricts you to one illuminator,
          which is what makes the RSSI series a coherent channel measurement
          rather than a mix of unrelated links.
    """

    iface: str
    out_path: Path
    peer: str | None = None
    keep_types: tuple[str, ...] = ("mgmt", "data", "ctrl")
    # Flush every N rows so another process can tail this file in real time.
    # 0 disables (fastest, but the file lags by one OS buffer).
    flush_every: int = 25
    # Frames the driver loops back from our OWN transmitter carry a TX radiotap
    # header with no received-signal field, which scapy surfaces as 0 dBm. A
    # real RX level is always negative, so anything >= max_rssi_dbm is our own
    # traffic, not a measurement. Observed at ~0.4% of frames under a ping
    # flood; each one is a ~56 dB outlier, easily enough to wreck a window.
    min_rssi_dbm: int = -100
    max_rssi_dbm: int = -1
    stats: CaptureStats = field(default_factory=CaptureStats)

    _sniffer: AsyncSniffer | None = field(default=None, init=False, repr=False)
    _fh: object = field(default=None, init=False, repr=False)
    _writer: object = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _handle(self, pkt) -> None:
        if not pkt.haslayer(Dot11):
            return
        self.stats.frames += 1

        dot11 = pkt[Dot11]
        ftype = FRAME_TYPES.get(dot11.type, "?")
        if ftype not in self.keep_types:
            return

        a1 = (dot11.addr1 or "").lower()
        a2 = (dot11.addr2 or "").lower()
        a3 = (dot11.addr3 or "").lower()
        if self.peer and self.peer not in (a1, a2, a3):
            return

        rssi = _radiotap_field(pkt, "dBm_AntSignal")
        if rssi is None:  # no signal field -> unusable for sensing
            return
        if not (self.min_rssi_dbm <= int(rssi) <= self.max_rssi_dbm):
            self.stats.rejected += 1
            return

        t = float(pkt.time)
        row = [
            f"{t:.6f}",
            int(rssi),
            _radiotap_field(pkt, "dBm_AntNoise", ""),
            _radiotap_field(pkt, "ChannelFrequency", ""),
            _radiotap_field(pkt, "Rate", ""),
            ftype,
            int(dot11.subtype),
            a1, a2, a3,
            (dot11.SC >> 4) if dot11.SC is not None else "",
            len(pkt),
        ]

        with self._lock:
            self._writer.writerow(row)
            self.stats.kept += 1
            if self.flush_every and self.stats.kept % self.flush_every == 0:
                self._fh.flush()
            self.stats.rssi_sum += float(rssi)
            if self.stats.first_t is None:
                self.stats.first_t = t
            self.stats.last_t = t

    def start(self) -> "RSSICapture":
        self.out_path = Path(self.out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.out_path.open("w", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(COLUMNS)

        self._sniffer = AsyncSniffer(iface=self.iface, prn=self._handle, store=False)
        self._sniffer.start()
        return self

    def stop(self) -> CaptureStats:
        if self._sniffer is not None:
            self._sniffer.stop()
            self._sniffer = None
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
                self._fh = None
        return self.stats

    def run_for(self, seconds: float, progress=None) -> CaptureStats:
        self.start()
        deadline = time.time() + seconds
        try:
            while time.time() < deadline:
                time.sleep(0.25)
                if progress is not None:
                    progress(self.stats, deadline - time.time())
        finally:
            return self.stop()

    def __enter__(self) -> "RSSICapture":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
