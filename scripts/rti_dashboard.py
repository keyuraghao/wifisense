#!/usr/bin/env python3
"""Live mesh dashboard: who is talking to whom, and the 3D reconstruction.

    .venv/bin/python scripts/rti_dashboard.py
    .venv/bin/python scripts/rti_dashboard.py --room 6 5 2.5 --calibrate 20

Test the whole stack with no hardware by running the virtual mesh alongside it:
    .venv/bin/python scripts/rti_fake_nodes.py --nodes 12 --fail-after 45

Six panels:
  1. 3D room: node positions coloured by health, active links, target estimate
  2. link matrix: which node pairs are currently exchanging packets
  3. mesh health over time: nodes alive and links active
  4. reconstruction, top-down
  5. per node: report rate and packet loss
  6. status text: counts, unplaced nodes, degradation reason
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib

from wifisense.mesh.protocol import DEFAULT_PORT
from wifisense.mesh.server import MeshSession
from wifisense.spatial.geometry import VoxelGrid

ALIVE, DEAD, UNPLACED = "#2e7d32", "#c62828", "#f9a825"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", nargs=3, type=float, default=[5.0, 4.0, 2.4])
    ap.add_argument("--voxel", type=float, default=0.4)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--config", default="config/nodes.json")
    ap.add_argument("--calibrate", type=float, default=20.0,
                    help="seconds of empty-room calibration once nodes appear")
    ap.add_argument("--min-links", type=int, default=10)
    ap.add_argument("--node-timeout", type=float, default=5.0)
    ap.add_argument("--hop", type=float, default=0.5)
    ap.add_argument("--backend", default="TkAgg")
    ap.add_argument("--headless", type=float, default=0.0)
    ap.add_argument("--save", default=None)
    args = ap.parse_args()

    matplotlib.use("Agg" if args.headless else args.backend)
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    room = ((0.0, args.room[0]), (0.0, args.room[1]), (0.0, args.room[2]))
    grid = VoxelGrid(*room, voxel_m=args.voxel)
    sess = MeshSession(grid, positions_path=args.config, port=args.port,
                       min_links=args.min_links, node_timeout=args.node_timeout)
    sess.start()
    print(f"listening on UDP :{args.port}   room {args.room}   "
          f"grid {grid.shape} = {grid.n_voxels} voxels")
    print(f"node positions from {args.config}")

    state = {"phase": "waiting", "cal_end": 0.0, "cal_result": None}

    fig = plt.figure(figsize=(15, 9), constrained_layout=True)
    gs = fig.add_gridspec(3, 3, height_ratios=[3, 3, 1])
    ax3d = fig.add_subplot(gs[0:2, 0], projection="3d")
    ax_mat = fig.add_subplot(gs[0, 1])
    ax_hist = fig.add_subplot(gs[0, 2])
    ax_map = fig.add_subplot(gs[1, 1])
    ax_rate = fig.add_subplot(gs[1, 2])
    ax_txt = fig.add_subplot(gs[2, :]); ax_txt.axis("off")
    banner = fig.suptitle("", fontsize=13, fontweight="bold")

    def update(_):
        st = sess.step()
        snap, rec = st["snapshot"], st["recon"]
        now = snap["now"]

        # Calibration is automatic: once enough placed nodes are alive and
        # reporting, take the baseline. Everything before it is uncalibrated.
        if state["phase"] == "waiting" and snap["n_usable"] >= 4:
            sess.registry.start_calibration()
            state["phase"] = "calibrating"
            state["cal_end"] = now + args.calibrate
        elif state["phase"] == "calibrating" and now >= state["cal_end"]:
            state["cal_result"] = sess.registry.finish_calibration()
            sess.noise_var = max(state["cal_result"]["noise_var"], 0.05)
            sess.calibrated = state["cal_result"]["calibrated_links"] > 0
            state["phase"] = "running"

        nodes = snap["nodes"]
        placed = {i: s for i, s in nodes.items() if s.placed}
        ids = sorted(placed)
        active = set(snap["active_links"])

        # -- 1. 3D room ---------------------------------------------------
        ax3d.clear()
        for nid, s in placed.items():
            alive = s.alive(now, args.node_timeout)
            ax3d.scatter(*s.position, c=ALIVE if alive else DEAD,
                         s=70 if alive else 110,
                         marker="^" if alive else "X", depthshade=False)
            ax3d.text(*s.position, f" {s.label}", fontsize=6)
        for a, b in active:
            pa, pb = nodes[a].position, nodes[b].position
            ax3d.plot(*zip(pa, pb), color="0.7", lw=0.35)
        if rec["ok"] and rec["position"] is not None:
            ax3d.scatter(*rec["position"], c="tab:blue", s=160, marker="o",
                         edgecolors="k", depthshade=False)
        ax3d.set(xlim=room[0], ylim=room[1], zlim=room[2],
                 xlabel="x", ylabel="y", zlabel="z")
        ax3d.set_title(f"{len(active)} active links", fontsize=9)

        # -- 2. link matrix ------------------------------------------------
        ax_mat.clear()
        n = len(ids)
        if n:
            idx = {v: i for i, v in enumerate(ids)}
            M = np.full((n, n), np.nan)
            for (a, b), link in snap["links"].items():
                if a in idx and b in idx:
                    v = 1.0 if (a, b) in active else 0.0
                    M[idx[a], idx[b]] = M[idx[b], idx[a]] = v
            ax_mat.imshow(M, cmap="RdYlGn", vmin=0, vmax=1, interpolation="nearest")
            ax_mat.set_xticks(range(n)); ax_mat.set_yticks(range(n))
            ax_mat.set_xticklabels([placed[i].label for i in ids], rotation=90, fontsize=5)
            ax_mat.set_yticklabels([placed[i].label for i in ids], fontsize=5)
        ax_mat.set_title(f"link matrix  {len(active)}/{snap['n_possible_links']}",
                         fontsize=9)

        # -- 3. mesh health over time --------------------------------------
        ax_hist.clear()
        h = sess.history[-400:]
        if h:
            t = np.array([x["t"] for x in h]) - now
            ax_hist.plot(t, [x["n_alive"] for x in h], color=ALIVE, lw=1.4,
                         label="nodes alive")
            ax_hist.plot(t, [x["n_links"] for x in h], color="tab:blue", lw=1.0,
                         label="links active")
            ax_hist.legend(fontsize=6, loc="lower left")
        ax_hist.set_title("mesh health", fontsize=9)
        ax_hist.set_xlabel("seconds ago", fontsize=7)

        # -- 4. reconstruction ---------------------------------------------
        ax_map.clear()
        if rec["ok"] and rec["field"] is not None:
            cube = grid.as_cube(rec["field"])
            ax_map.imshow(cube.max(axis=2).T, origin="lower", cmap="viridis",
                          extent=[*room[0], *room[1]], aspect="equal")
            if rec["position"] is not None:
                ax_map.plot(*rec["position"][:2], "x", color="r", ms=12, mew=2)
            ax_map.set_title(f"reconstruction  coverage {rec['coverage']:.2f}",
                             fontsize=9)
        else:
            ax_map.text(0.5, 0.5, rec["reason"], ha="center", va="center",
                        fontsize=8, wrap=True, transform=ax_map.transAxes)
            ax_map.set_title("reconstruction unavailable", fontsize=9)

        # -- 5. per node rate / loss ---------------------------------------
        ax_rate.clear()
        if ids:
            labels = [placed[i].label for i in ids]
            loss = [placed[i].loss_rate * 100 for i in ids]
            colors = [ALIVE if placed[i].alive(now, args.node_timeout) else DEAD
                      for i in ids]
            ax_rate.bar(range(len(ids)), loss, color=colors)
            ax_rate.set_xticks(range(len(ids)))
            ax_rate.set_xticklabels(labels, rotation=90, fontsize=5)
            ax_rate.set_ylabel("packet loss %", fontsize=7)
        ax_rate.set_title("per node loss", fontsize=9)

        # -- 6. status -----------------------------------------------------
        ax_txt.clear(); ax_txt.axis("off")
        cs, ad = st["collector"], st["adaptive"]
        unplaced = snap["unplaced"]
        lines = [
            f"registered {snap['n_registered']}   alive {snap['n_alive']}   "
            f"placed {snap['n_placed']}   usable {snap['n_usable']}   "
            f"dead {len(snap['dead'])}   unplaced {len(unplaced)}",
            f"links {snap['n_active_links']}/{snap['n_possible_links']} "
            f"({snap['link_health']:.0%})   "
            f"datagrams {cs.datagrams} @ {cs.rate_hz():.0f}/s   "
            f"errors {cs.errors}   noise_var {st['noise_var']:.2f} dB^2",
            f"topology rebuilds {ad['rebuilds']}  cache hits {ad['cache_hits']} "
            f"({ad['hit_rate']:.0%})  last rebuild {ad['last_rebuild_ms']:.1f} ms",
        ]
        if unplaced:
            from wifisense.mesh.protocol import pretty_id
            lines.append(f"UNPLACED (add to {args.config} to include them): "
                         + ", ".join(pretty_id(u) for u in unplaced[:6]))
        if snap["dead"]:
            lines.append("DEAD: " + ", ".join(nodes[d].label for d in snap["dead"]))
        ax_txt.text(0.01, 0.95, "\n".join(lines), va="top", fontsize=9,
                    family="monospace", transform=ax_txt.transAxes)

        if state["phase"] == "waiting":
            banner.set_text(f"WAITING for nodes  ({snap['n_usable']} usable, need 4)")
            banner.set_color("#616161")
        elif state["phase"] == "calibrating":
            banner.set_text(f"CALIBRATING {max(0, state['cal_end']-now):4.1f}s "
                            f"- keep the room EMPTY")
            banner.set_color("#ef6c00")
        elif rec["ok"]:
            p = rec["position"]
            banner.set_text(f"TRACKING  ({p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f}) m   "
                            f"{rec['n_nodes']} nodes / {rec['n_links']} links   "
                            f"coverage {rec['coverage']:.2f}")
            banner.set_color(ALIVE)
        else:
            banner.set_text(f"DEGRADED  {rec['reason']}")
            banner.set_color(DEAD)

    try:
        if args.headless:
            end = time.time() + args.headless
            t0 = time.time()
            while time.time() < end:
                update(None)
                sn = sess.registry.snapshot()
                rc = sess.history[-1] if sess.history else {}
                pos = rc.get("position")
                print(f"  t={time.time()-t0:5.1f}s  {state['phase']:<11s} "
                      f"alive {sn['n_alive']:2d}/{sn['n_placed']:2d}  "
                      f"links {sn['n_active_links']:3d}  "
                      f"cov {rc.get('coverage', 0):.2f}  "
                      + (f"pos ({pos[0]:.2f},{pos[1]:.2f},{pos[2]:.2f})"
                         if pos else "no fix"))
                time.sleep(args.hop)
            snap = sess.registry.snapshot()
            print(f"\nheadless: {snap['n_alive']} alive, "
                  f"{snap['n_active_links']} links, phase={state['phase']}")
        else:
            anim = FuncAnimation(fig, update, interval=int(args.hop * 1000),
                                 cache_frame_data=False)
            fig._keep_anim = anim
            plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        sess.stop()

    if args.save:
        fig.savefig(args.save, dpi=120)
        print(f"wrote {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
