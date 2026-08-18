#!/usr/bin/env python3
"""Virtual ESP32 mesh -- exercises the real UDP protocol with no hardware.

    # terminal 1
    .venv/bin/python scripts/rti_dashboard.py
    # terminal 2
    .venv/bin/python scripts/rti_fake_nodes.py --nodes 12 --fail-after 45 --fail-count 3

Sends genuine protocol datagrams to the server socket, so everything downstream
-- enrolment, liveness, baselines, topology rebuilds -- is exercised for real.
The only thing simulated is the radio.

Also writes config/nodes.json, which is how the server learns node positions.
That file is the "add a node" interface: see --write-config.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense.mesh.protocol import DEFAULT_PORT, Measurement, Report, pretty_id
from wifisense.spatial.geometry import link_pairs, perimeter_nodes
from wifisense.spatial.simulate import simulate_links


def fake_mac(i: int) -> str:
    return f"aabb{i:02x}{i:02x}{i:04x}"


def free_space_rssi(d: float, tx_dbm: float = 4.0, f_ghz: float = 2.437) -> float:
    """Log-distance path loss -- a plausible ESP32-to-ESP32 level indoors."""
    d = max(d, 0.3)
    fspl = 20 * np.log10(d) + 20 * np.log10(f_ghz) + 32.45
    return tx_dbm - fspl - 8.0            # 8 dB of indoor excess loss


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=12)
    ap.add_argument("--room", nargs=3, type=float, default=[5.0, 4.0, 2.4])
    ap.add_argument("--heights", nargs="*", type=float, default=[0.4, 1.2, 2.0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--rate", type=float, default=5.0, help="reports/s per node")
    ap.add_argument("--warmup", type=float, default=25.0,
                    help="seconds with an empty room, for the server to calibrate")
    ap.add_argument("--fail-after", type=float, default=0.0,
                    help="seconds before killing --fail-count nodes (0 = never)")
    ap.add_argument("--fail-count", type=int, default=3)
    ap.add_argument("--revive-after", type=float, default=0.0,
                    help="seconds after failure before the nodes come back")
    ap.add_argument("--fading", type=float, default=1.2, help="link noise, dB")
    ap.add_argument("--write-config", default="config/nodes.json")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = forever")
    args = ap.parse_args()

    room = ((0.0, args.room[0]), (0.0, args.room[1]), (0.0, args.room[2]))
    per_wall = max(1, args.nodes // 4)
    positions = perimeter_nodes(room[0], room[1], heights=args.heights,
                                per_wall=per_wall)[: args.nodes]
    ids = [fake_mac(i) for i in range(len(positions))]
    pairs = link_pairs(len(ids))

    cfg = Path(args.write_config)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"nodes": {
        pretty_id(ids[i]): {"name": f"n{i:02d}", "x": round(float(positions[i][0]), 3),
                            "y": round(float(positions[i][1]), 3),
                            "z": round(float(positions[i][2]), 3)}
        for i in range(len(ids))}}, indent=2))

    print(f"{len(ids)} virtual nodes -> {args.host}:{args.port} at {args.rate} Hz")
    print(f"positions written to {cfg}")
    print(f"room {args.room}  |  {len(pairs)} links")
    print(f"warmup (empty room, calibrate now): {args.warmup:.0f}s")
    if args.fail_after:
        print(f"will kill {args.fail_count} nodes at t={args.fail_after:.0f}s"
              + (f", revive at t={args.fail_after + args.revive_after:.0f}s"
                 if args.revive_after else " (permanently)"))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rng = np.random.default_rng(0)
    base = {tuple(p): free_space_rssi(np.linalg.norm(positions[p[0]] - positions[p[1]]))
            for p in map(tuple, pairs)}

    seq = 0
    t0 = time.time()
    dead: set[int] = set()
    # Explicit lifecycle flags. Inferring "have I killed yet?" from dead being
    # empty makes kill and revive retrigger each other every iteration.
    killed_once = False
    revived_once = False
    try:
        while True:
            now = time.time()
            el = now - t0
            if args.seconds and el > args.seconds:
                break

            if args.fail_after and not killed_once and el >= args.fail_after:
                dead = set(range(args.fail_count))
                killed_once = True
                print(f"\n[t={el:5.1f}s] KILLED nodes "
                      f"{[f'n{i:02d}' for i in sorted(dead)]}")
            if (args.revive_after and killed_once and not revived_once
                    and el >= args.fail_after + args.revive_after):
                print(f"\n[t={el:5.1f}s] REVIVED nodes "
                      f"{[f'n{i:02d}' for i in sorted(dead)]}")
                dead = set()
                revived_once = True

            # Person walks a slow ellipse once the warmup is over.
            if el < args.warmup:
                atten = np.zeros(len(pairs))
                where = "empty (calibration window)"
            else:
                th = 2 * np.pi * (el - args.warmup) / 40.0
                p = np.array([args.room[0] / 2 + 0.30 * args.room[0] * np.cos(th),
                              args.room[1] / 2 + 0.30 * args.room[1] * np.sin(th),
                              0.875])
                atten = simulate_links(positions, pairs, p, room, rng=rng,
                                       fading_db=0.0, quantise_db=0.0,
                                       node_jitter_m=0.0)
                where = f"person at ({p[0]:.1f}, {p[1]:.1f})"

            # Each node reports what it heard from every peer it can still hear.
            for i, nid in enumerate(ids):
                if i in dead:
                    continue
                ms = []
                for k, (a, b) in enumerate(pairs):
                    if i not in (a, b):
                        continue
                    j = b if a == i else a
                    if j in dead:
                        continue
                    rssi = base[(a, b)] - atten[k] + rng.normal(0, args.fading)
                    ms.append(Measurement(peer=ids[j], rssi_dbm=float(rssi),
                                          samples=int(args.rate)))
                sock.sendto(Report(node=nid, seq=seq, uptime_ms=int(el * 1000),
                                   measurements=ms).encode(), (args.host, args.port))

            seq += 1
            live = len(ids) - len(dead)
            print(f"  t={el:6.1f}s  {live:2d}/{len(ids)} nodes up  seq {seq:5d}  "
                  f"{where}          ", end="\r", flush=True)
            time.sleep(1.0 / args.rate)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
