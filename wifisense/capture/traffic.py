"""Traffic generation.

Ambient WiFi is a terrible sensing illuminator: an idle AP transmits beacons at
~9.8 Hz (102.4 ms TBTT), which Nyquist-limits you to ~4.9 Hz of observable
motion. Human torso/limb Doppler lives up to ~50 Hz, so you must *create* the
packet rate you want to sense at.

This module floods a target with ICMP echo requests so the AP is forced to emit
a dense, steady stream of frames you can measure the channel from.
"""
from __future__ import annotations

import shutil
import subprocess
import threading


class PingFlood:
    """Background ICMP flood at a fixed interval.

    `interval` is seconds between packets; each echo request also elicits a
    reply, so the observed frame rate at the sniffer is roughly 2/interval.
    Sub-0.2s intervals require root (kernel restriction on ping).
    """

    def __init__(self, target: str, interval: float = 0.002, size: int = 64):
        self.target = target
        self.interval = interval
        self.size = size
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def expected_rate_hz(self) -> float:
        return 2.0 / self.interval

    def start(self) -> None:
        if shutil.which("ping") is None:
            raise RuntimeError("ping not found")
        with self._lock:
            if self._proc is not None:
                return
            self._proc = subprocess.Popen(
                ["ping", "-i", str(self.interval), "-s", str(self.size),
                 "-q", self.target],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )

    def stop(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "PingFlood":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def default_gateway() -> str | None:
    """IP of the default gateway, which for a home setup is the router."""
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    parts = out.split()
    return parts[2] if len(parts) > 2 and parts[0] == "default" else None
