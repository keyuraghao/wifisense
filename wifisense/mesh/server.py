"""UDP collector and the live reconstruction session.

The collector thread does as little as possible: parse, hand to the registry,
return to the socket. Reconstruction happens on the caller's loop, so a slow
render can never cause the receive buffer to overflow and drop node reports.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from ..spatial.adaptive import AdaptiveReconstructor
from ..spatial.geometry import VoxelGrid
from .protocol import DEFAULT_PORT, MAX_DATAGRAM, ProtocolError, decode
from .registry import MeshRegistry


@dataclass
class CollectorStats:
    datagrams: int = 0
    bytes_rx: int = 0
    errors: int = 0
    last_error: str = ""
    started: float = field(default_factory=time.time)
    _recent: list = field(default_factory=list, repr=False)

    def note(self, now: float) -> None:
        self._recent.append(now)
        if len(self._recent) > 512:
            del self._recent[:256]

    def rate_hz(self, window_s: float = 3.0) -> float:
        if not self._recent:
            return 0.0
        now = self._recent[-1]
        recent = [t for t in self._recent if now - t <= window_s]
        return len(recent) / window_s if len(recent) > 1 else 0.0


class MeshCollector:
    """Receives node reports on a UDP port and files them in the registry."""

    def __init__(self, registry: MeshRegistry, host: str = "0.0.0.0",
                 port: int = DEFAULT_PORT):
        self.registry = registry
        self.host, self.port = host, port
        self.stats = CollectorStats()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> "MeshCollector":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A generous receive buffer: a 24-node mesh at 5 Hz is nothing, but a
        # burst after a network hiccup should not be silently discarded.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.5)

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mesh-collector")
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload, _addr = self._sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break

            now = time.time()
            self.stats.datagrams += 1
            self.stats.bytes_rx += len(payload)
            self.stats.note(now)
            try:
                self.registry.ingest(decode(payload), now=now)
            except ProtocolError as e:
                self.stats.errors += 1
                self.stats.last_error = str(e)
            except Exception as e:                      # a bad node must not
                self.stats.errors += 1                  # take down the mesh
                self.stats.last_error = f"{type(e).__name__}: {e}"

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self) -> "MeshCollector":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class MeshSession:
    """Collector + registry + adaptive reconstruction, driven by step()."""

    def __init__(self, grid: VoxelGrid, positions_path="config/nodes.json",
                 port: int = DEFAULT_PORT, min_links: int = 10,
                 node_timeout: float = 5.0, link_timeout: float = 5.0):
        self.grid = grid
        self.registry = MeshRegistry(positions_path)
        self.collector = MeshCollector(self.registry, port=port)
        self.adaptive = AdaptiveReconstructor(grid, min_links=min_links)
        self.node_timeout = node_timeout
        self.link_timeout = link_timeout
        self.noise_var = 2.25
        self.calibrated = False
        self.history: list = []

    def start(self) -> "MeshSession":
        self.collector.start()
        return self

    def stop(self) -> None:
        self.collector.stop()

    # -- calibration -------------------------------------------------------

    def calibrate(self, seconds: float = 20.0, progress=None) -> dict:
        """Record the empty room and freeze per-link baselines."""
        self.registry.start_calibration()
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(0.25)
            if progress:
                progress(deadline - time.time(), self.registry.snapshot())
        result = self.registry.finish_calibration()
        self.noise_var = max(result["noise_var"], 0.05)
        self.calibrated = result["calibrated_links"] > 0
        return result

    # -- main loop ---------------------------------------------------------

    def step(self) -> dict:
        """One pass: read mesh state, reconstruct from whatever is alive."""
        snap = self.registry.snapshot(node_timeout=self.node_timeout,
                                      link_timeout=self.link_timeout)

        positions = {nid: st.position for nid, st in snap["nodes"].items()
                     if st.placed}
        atten = {k: snap["links"][k].attenuation_db for k in snap["active_links"]}

        if not self.calibrated:
            recon = {"ok": False, "reason": "not calibrated -- run calibration "
                                            "with the room empty",
                     "n_links": len(atten), "field": None, "position": None}
        else:
            recon = self.adaptive.reconstruct(positions, atten, self.noise_var)

        state = {"snapshot": snap, "recon": recon,
                 "collector": self.collector.stats,
                 "adaptive": self.adaptive.stats,
                 "noise_var": self.noise_var, "calibrated": self.calibrated}

        self.history.append({
            "t": snap["now"], "n_alive": snap["n_alive"],
            "n_links": snap["n_active_links"], "ok": recon["ok"],
            "coverage": recon.get("coverage", 0.0),
            "position": None if recon["position"] is None
            else np.asarray(recon["position"]).tolist(),
        })
        if len(self.history) > 3000:
            del self.history[:1000]
        return state
