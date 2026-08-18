"""Who is in the mesh, who is alive, and what each link is currently measuring.

Two ideas do the heavy lifting for fault tolerance:

  * Enrolment is automatic, placement is explicit. A node that has never been
    seen is registered the instant its first packet arrives, but it does NOT
    enter the reconstruction until someone tells the server where it physically
    is. Guessing a position would silently corrupt the geometry, and a wrong
    position is far worse than a missing node.

  * Baselines are per link and persistent. RTI measures the *change* from an
    empty room, so every link carries its own calibrated reference. Keeping
    those keyed by link rather than by reconstruction run means a node can
    drop out and rejoin without recalibrating the whole mesh -- which is the
    entire point of surviving node failure.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .protocol import Report, link_key, normalise_id, pretty_id

DEFAULT_NODE_TIMEOUT_S = 5.0
DEFAULT_LINK_TIMEOUT_S = 5.0


@dataclass
class NodeState:
    node_id: str
    first_seen: float
    last_seen: float
    reports: int = 0
    lost: int = 0                 # sequence gaps -> packets that never arrived
    last_seq: int = -1
    last_uptime_ms: int = 0
    reboots: int = 0
    name: str | None = None
    position: np.ndarray | None = None

    @property
    def placed(self) -> bool:
        return self.position is not None

    def age(self, now: float) -> float:
        return now - self.last_seen

    def alive(self, now: float, timeout: float = DEFAULT_NODE_TIMEOUT_S) -> bool:
        return self.age(now) <= timeout

    @property
    def loss_rate(self) -> float:
        total = self.reports + self.lost
        return self.lost / total if total else 0.0

    @property
    def label(self) -> str:
        return self.name or pretty_id(self.node_id)[-5:]


@dataclass
class LinkState:
    """One undirected link. Both directions are folded together."""

    a: str
    b: str
    last_seen: float = 0.0
    rssi_ewma: float = 0.0
    samples: int = 0
    baseline_db: float | None = None
    _baseline_acc: list = field(default_factory=list, repr=False)
    _var_acc: list = field(default_factory=list, repr=False)

    def update(self, rssi: float, now: float, alpha: float = 0.35) -> None:
        self.rssi_ewma = rssi if self.samples == 0 else \
            (1 - alpha) * self.rssi_ewma + alpha * rssi
        self.samples += 1
        self.last_seen = now

    def alive(self, now: float, timeout: float = DEFAULT_LINK_TIMEOUT_S) -> bool:
        return (now - self.last_seen) <= timeout

    @property
    def calibrated(self) -> bool:
        return self.baseline_db is not None

    @property
    def attenuation_db(self) -> float:
        """Added path loss vs the empty-room baseline. Positive = more blocked."""
        if self.baseline_db is None:
            return 0.0
        return self.baseline_db - self.rssi_ewma


class MeshRegistry:
    """Thread-safe view of the mesh. The UDP thread writes, the render loop reads."""

    def __init__(self, positions_path: str | Path = "config/nodes.json"):
        self.positions_path = Path(positions_path)
        self.nodes: dict[str, NodeState] = {}
        self.links: dict[tuple[str, str], LinkState] = {}
        self._lock = threading.RLock()
        self._calibrating = False
        self.load_positions()

    # -- placement ---------------------------------------------------------

    def load_positions(self) -> int:
        """Read MAC -> position map. Adding a node is editing this one file."""
        if not self.positions_path.exists():
            return 0
        data = json.loads(self.positions_path.read_text())
        n = 0
        with self._lock:
            for raw_id, spec in data.get("nodes", {}).items():
                try:
                    node_id = normalise_id(raw_id)
                except Exception:
                    continue
                st = self.nodes.get(node_id) or self._blank(node_id)
                st.position = np.array([float(spec["x"]), float(spec["y"]),
                                        float(spec["z"])])
                st.name = spec.get("name")
                self.nodes[node_id] = st
                n += 1
        return n

    def save_positions(self) -> None:
        self.positions_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            out = {"nodes": {
                pretty_id(nid): {
                    "name": st.name, "x": float(st.position[0]),
                    "y": float(st.position[1]), "z": float(st.position[2]),
                }
                for nid, st in sorted(self.nodes.items()) if st.placed
            }}
        self.positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.positions_path.write_text(json.dumps(out, indent=2))

    def place(self, node_id: str, x: float, y: float, z: float,
              name: str | None = None) -> None:
        node_id = normalise_id(node_id)
        with self._lock:
            st = self.nodes.get(node_id) or self._blank(node_id)
            st.position = np.array([x, y, z], dtype=float)
            if name:
                st.name = name
            self.nodes[node_id] = st

    def _blank(self, node_id: str) -> NodeState:
        now = time.time()
        return NodeState(node_id=node_id, first_seen=now, last_seen=0.0)

    # -- ingest ------------------------------------------------------------

    def ingest(self, report: Report, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            st = self.nodes.get(report.node)
            if st is None:                       # auto-enrol on first contact
                st = NodeState(node_id=report.node, first_seen=now, last_seen=now)
                self.nodes[report.node] = st

            # Uptime going backwards means the node rebooted; its sequence
            # counter restarted too, so do not score that as packet loss.
            if report.uptime_ms < st.last_uptime_ms:
                st.reboots += 1
                st.last_seq = -1
            st.last_uptime_ms = report.uptime_ms

            if st.last_seq >= 0 and report.seq > st.last_seq + 1:
                st.lost += report.seq - st.last_seq - 1
            st.last_seq = report.seq
            st.last_seen = now
            st.reports += 1

            for m in report.measurements:
                if m.peer not in self.nodes:     # heard-of-but-not-heard-from
                    self.nodes[m.peer] = NodeState(node_id=m.peer, first_seen=now,
                                                   last_seen=0.0)
                key = link_key(report.node, m.peer)
                link = self.links.get(key)
                if link is None:
                    link = LinkState(a=key[0], b=key[1])
                    self.links[key] = link
                link.update(m.rssi_dbm, now)
                if self._calibrating:
                    link._baseline_acc.append(m.rssi_dbm)

    # -- calibration -------------------------------------------------------

    def start_calibration(self) -> None:
        with self._lock:
            self._calibrating = True
            for link in self.links.values():
                link._baseline_acc.clear()

    def finish_calibration(self, min_samples: int = 10) -> dict:
        """Freeze empty-room baselines. Returns per-link noise variance too."""
        with self._lock:
            self._calibrating = False
            variances, done, skipped = [], 0, 0
            for link in self.links.values():
                acc = link._baseline_acc
                if len(acc) < min_samples:
                    skipped += 1
                    continue
                arr = np.asarray(acc, dtype=float)
                link.baseline_db = float(arr.mean())
                variances.append(float(arr.var()))
                link._baseline_acc = []
                done += 1
            return {
                "calibrated_links": done, "skipped_links": skipped,
                # This is exactly the quantity RTIReconstructor.noise_var needs.
                "noise_var": float(np.mean(variances)) if variances else 1.0,
            }

    @property
    def calibrating(self) -> bool:
        return self._calibrating

    # -- queries -----------------------------------------------------------

    def snapshot(self, now: float | None = None,
                 node_timeout: float = DEFAULT_NODE_TIMEOUT_S,
                 link_timeout: float = DEFAULT_LINK_TIMEOUT_S) -> dict:
        """Consistent picture of mesh health for the dashboard and reconstructor."""
        now = now if now is not None else time.time()
        with self._lock:
            nodes = dict(self.nodes)
            links = dict(self.links)

        alive = {i: s for i, s in nodes.items() if s.alive(now, node_timeout)}
        placed = {i: s for i, s in nodes.items() if s.placed}
        usable = {i: s for i, s in alive.items() if s.placed}

        active_links = [
            k for k, l in links.items()
            if l.alive(now, link_timeout) and l.calibrated
            and k[0] in usable and k[1] in usable
        ]
        n = len(usable)
        return {
            "now": now,
            "nodes": nodes,
            "links": links,
            "n_registered": len(nodes),
            "n_alive": len(alive),
            "n_placed": len(placed),
            "n_usable": n,
            "unplaced": sorted(i for i, s in alive.items() if not s.placed),
            "dead": sorted(i for i, s in placed.items() if not s.alive(now, node_timeout)),
            "active_links": active_links,
            "n_active_links": len(active_links),
            "n_possible_links": n * (n - 1) // 2,
            "link_health": (len(active_links) / (n * (n - 1) // 2)) if n > 1 else 0.0,
        }
