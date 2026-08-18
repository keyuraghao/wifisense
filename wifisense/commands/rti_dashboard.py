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

import matplotlib
import matplotlib.colors as mcolors

from ..mesh.crypto import KeyStore
from ..mesh.protocol import DEFAULT_PORT
from ..mesh.server import MeshSession
from ..spatial.geometry import VoxelGrid
from ..ui import theme
from ..ui.theme import PALETTE as C


def main(argv=None) -> int:
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
    ap.add_argument("--key-file", default="config/mesh.key")
    ap.add_argument("--insecure", action="store_true",
                    help="accept unauthenticated frames (debugging only)")
    args = ap.parse_args(argv)

    keys = None
    if not args.insecure:
        try:
            keys = KeyStore.from_file(args.key_file)
        except FileNotFoundError:
            print(f"error: no mesh key at {args.key_file}\n"
                  f"run: .venv/bin/python scripts/gen_mesh_key.py\n"
                  f"(or --insecure to accept unauthenticated frames)",
                  file=sys.stderr)
            return 1
    else:
        print("WARNING: running INSECURE. Any host that can reach this port can "
              "inject fabricated measurements.", file=sys.stderr)

    matplotlib.use("Agg" if args.headless else args.backend)
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    room = ((0.0, args.room[0]), (0.0, args.room[1]), (0.0, args.room[2]))
    grid = VoxelGrid(*room, voxel_m=args.voxel)
    sess = MeshSession(grid, positions_path=args.config, port=args.port,
                       min_links=args.min_links, node_timeout=args.node_timeout,
                       keys=keys, allow_unauthenticated=args.insecure)
    sess.start()
    print(f"listening on UDP :{args.port}   room {args.room}   "
          f"grid {grid.shape} = {grid.n_voxels} voxels")
    print("transport: " + ("AES-128-GCM, replay protected"
                           if keys else "INSECURE - unauthenticated"))
    print(f"node positions from {args.config}")

    state = {"phase": "waiting", "cal_end": 0.0, "cal_result": None}

    theme.apply()
    fig = plt.figure(figsize=(15.5, 9.2))
    fig.canvas.manager.set_window_title("wifisense - mesh")

    # Explicit margins rather than constrained_layout: the status bar and the
    # tile strip are fixed furniture, and letting the solver move them makes the
    # header drift between frames, which reads as flicker on a live view.
    gs = fig.add_gridspec(
        3, 4, height_ratios=[1.35, 1.0, 0.32], width_ratios=[1.25, 1, 1, 1],
        left=0.035, right=0.985, top=0.872, bottom=0.055, hspace=0.44, wspace=0.26)

    ax3d   = fig.add_subplot(gs[0:2, 0], projection="3d")
    ax_map = fig.add_subplot(gs[0, 1])
    ax_mat = fig.add_subplot(gs[0, 2])
    ax_hist= fig.add_subplot(gs[0, 3])
    ax_rate= fig.add_subplot(gs[1, 1:3])
    ax_z   = fig.add_subplot(gs[1, 3])

    tiles = [fig.add_subplot(gs[2, i]) for i in range(4)]
    tile_gs = gs[2, :].subgridspec(1, 6, wspace=0.18)
    for a in tiles:
        a.remove()
    tiles = [fig.add_subplot(tile_gs[0, i]) for i in range(6)]

    status = theme.StatusBar(fig)

    ax3d.set_facecolor(C["bg"])
    ax3d.xaxis.set_pane_color((0, 0, 0, 0))
    ax3d.yaxis.set_pane_color((0, 0, 0, 0))
    ax3d.zaxis.set_pane_color((0, 0, 0, 0))

    def update(_):
        st_ = sess.step()
        snap, rec = st_["snapshot"], st_["recon"]
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
        placed = {i: st for i, st in nodes.items() if st.placed}
        ids = sorted(placed)
        active = set(snap["active_links"])
        cs, ad = st_["collector"], st_["adaptive"]

        # -- 3D room -------------------------------------------------------
        ax3d.clear()
        ax3d.set_facecolor(C["bg"])
        for a, b in active:
            pa, pb = nodes[a].position, nodes[b].position
            ax3d.plot(*zip(pa, pb), color=theme.fade(C["s2"], 0.30), lw=0.4)
        for nid, node in placed.items():
            up = node.alive(now, args.node_timeout)
            ax3d.scatter(*node.position, s=60 if up else 90,
                         c=C["ok"] if up else C["alert"],
                         marker="^" if up else "X", depthshade=False,
                         edgecolors=C["bg"], linewidths=0.8)
            ax3d.text(*node.position, f"  {node.label}", fontsize=6,
                      color=C["muted"] if up else C["alert"])
        if rec["ok"] and rec["position"] is not None:
            px, py, pz = rec["position"]
            ax3d.scatter(px, py, pz, s=190, c=C["active"], marker="o",
                         edgecolors=C["bg"], linewidths=1.4, depthshade=False)
            # Drop line to the floor: depth is unreadable in a static 3D render
            # without one, and this view is not interactive while animating.
            ax3d.plot([px, px], [py, py], [room[2][0], pz],
                      color=theme.fade(C["active"], 0.45), lw=1.0, ls=":")
        ax3d.set(xlim=room[0], ylim=room[1], zlim=room[2])
        span = [room[0][1] - room[0][0], room[1][1] - room[1][0],
                room[2][1] - room[2][0]]
        # Metres are metres: never distort the room. zoom fills the cell, which
        # a raw box_aspect leaves mostly empty for a wide, short room.
        ax3d.set_box_aspect(span, zoom=1.12)
        ax3d.set_xlabel("x", fontsize=7, color=C["dim"], labelpad=-4)
        ax3d.set_ylabel("y", fontsize=7, color=C["dim"], labelpad=-4)
        ax3d.set_zlabel("z", fontsize=7, color=C["dim"], labelpad=-4)
        ax3d.tick_params(labelsize=6, colors=C["dim"], pad=0)
        ax3d.grid(color=C["grid"])
        ax3d.set_title(f"room  ·  {len(active)} active links", loc="left",
                       fontsize=9.5, color=C["text"], weight="bold")

        # -- reconstruction ------------------------------------------------
        ax_map.clear()
        theme.image_panel(ax_map)
        if rec["ok"] and rec["field"] is not None:
            cube = grid.as_cube(rec["field"])
            ax_map.imshow(cube.max(axis=2).T, origin="lower", cmap=theme.FIELD,
                          extent=[*room[0], *room[1]], aspect="auto",
                          interpolation="bilinear")
            for nid in ids:
                q = placed[nid].position
                ax_map.plot(q[0], q[1], "^", ms=3.5, color=C["dim"])
            if rec["position"] is not None:
                ax_map.plot(*rec["position"][:2], "o", ms=11, mfc="none",
                            mec=C["active"], mew=1.8)
            theme.panel(ax_map, "reconstruction",
                        f"coverage {rec['coverage']:.2f}  ·  peak "
                        f"{rec['peak_attenuation']:.1f} dB")
        else:
            ax_map.set_xticks([]); ax_map.set_yticks([]); ax_map.grid(False)
            ax_map.text(0.5, 0.5, rec["reason"], ha="center", va="center",
                        fontsize=8, color=C["dim"], wrap=True,
                        transform=ax_map.transAxes)
            theme.panel(ax_map, "reconstruction", "unavailable")

        # -- link matrix ---------------------------------------------------
        ax_mat.clear()
        n = len(ids)
        if n:
            idx = {v: i for i, v in enumerate(ids)}
            M = np.full((n, n), np.nan)
            for (a, b), link in snap["links"].items():
                if a in idx and b in idx:
                    v = 1.0 if (a, b) in active else 0.0
                    M[idx[a], idx[b]] = M[idx[b], idx[a]] = v
            theme.image_panel(ax_mat)
            # Softened: a wall of saturated green is loud, and the information
            # is in the exceptions, so let the failures be the bright thing.
            cmap = mcolors.ListedColormap([C["alert"], theme.fade(C["ok"], 0.55)])
            ax_mat.imshow(M, cmap=cmap, vmin=0, vmax=1, interpolation="nearest")
            step = max(1, n // 12)
            ax_mat.set_xticks(range(0, n, step)); ax_mat.set_yticks(range(0, n, step))
            ax_mat.set_xticklabels([placed[ids[i]].label for i in range(0, n, step)],
                                   rotation=90, fontsize=5.5)
            ax_mat.set_yticklabels([placed[ids[i]].label for i in range(0, n, step)],
                                   fontsize=5.5)
        ax_mat.grid(False)
        theme.panel(ax_mat, "link matrix",
                    f"{len(active)} of {snap['n_possible_links']} pairs")

        # -- mesh health over time -----------------------------------------
        ax_hist.clear()
        h = sess.history[-400:]
        if h:
            t = np.array([x["t"] for x in h]) - now
            links = np.array([x["n_links"] for x in h], dtype=float)
            alive = np.array([x["n_alive"] for x in h], dtype=float)
            ax_hist.fill_between(t, 0, links, color=theme.fade(C["s2"], 0.30),
                                 linewidth=0)
            ax_hist.plot(t, links, color=C["s2"], lw=1.4, label="links")
            ax_hist.plot(t, alive, color=C["ok"], lw=1.2, label="nodes")
            ax_hist.legend(loc="upper left", ncol=2)
            ax_hist.set_xlim(t.min(), 0)
            ax_hist.set_ylim(0, max(4, links.max() * 1.25))
        ax_hist.set_xlabel("seconds ago", fontsize=7)
        theme.panel(ax_hist, "mesh health", "links and nodes over time")

        # -- per node loss --------------------------------------------------
        ax_rate.clear()
        if ids:
            loss = [placed[i].loss_rate * 100 for i in ids]
            cols = [C["ok"] if placed[i].alive(now, args.node_timeout) else C["alert"]
                    for i in ids]
            ax_rate.bar(range(len(ids)), loss, color=cols, width=0.62)
            ax_rate.set_xticks(range(len(ids)))
            ax_rate.set_xticklabels([placed[i].label for i in ids],
                                    rotation=90, fontsize=6)
            ax_rate.set_ylim(0, max(5, max(loss) * 1.3 if loss else 5))
            if loss and max(loss) < 0.05:
                ax_rate.text(0.5, 0.5, "no packet loss", ha="center", va="center",
                             transform=ax_rate.transAxes, fontsize=9,
                             color=C["dim"])
        ax_rate.set_ylabel("%", fontsize=7)
        theme.panel(ax_rate, "packet loss per node", "from sequence gaps")

        # -- height profile -------------------------------------------------
        ax_z.clear()
        if rec["ok"] and rec["field"] is not None:
            prof = grid.as_cube(rec["field"]).mean(axis=(0, 1))
            ax_z.plot(prof, grid.zs, color=C["s3"], lw=1.6)
            ax_z.fill_betweenx(grid.zs, 0, prof, color=theme.fade(C["s3"], 0.30),
                               linewidth=0)
            if rec["position"] is not None:
                ax_z.axhline(rec["position"][2], color=C["active"], ls="--", lw=1.1)
        ax_z.set_ylabel("z (m)", fontsize=7)
        theme.panel(ax_z, "height profile", "z resolution needs staggered nodes")

        # -- metric tiles ----------------------------------------------------
        health = snap["link_health"]
        theme.tile(tiles[0], "nodes", f"{snap['n_usable']}/{snap['n_placed']}",
                   C["ok"] if not snap["dead"] else C["alert"],
                   f"{len(snap['dead'])} dead  {len(snap['unplaced'])} unplaced")
        theme.tile(tiles[1], "links", f"{snap['n_active_links']}",
                   C["ok"] if health > 0.8 else C["warn"] if health > 0.4 else C["alert"],
                   f"{health:.0%} of {snap['n_possible_links']}")
        theme.tile(tiles[2], "coverage",
                   f"{rec.get('coverage', 0):.2f}" if rec["ok"] else "--",
                   C["active"] if rec["ok"] else C["dim"], "room observable")
        theme.tile(tiles[3], "packets", f"{cs.rate_hz():.0f}/s",
                   C["s2"], f"{cs.datagrams:,} total")
        theme.tile(tiles[4], "security",
                   "GCM" if st_["secure"] else "OFF",
                   C["ok"] if st_["secure"] else C["alert"],
                   f"{cs.auth_failures} rejected  {cs.replays} replays")
        theme.tile(tiles[5], "rebuilds", f"{ad['rebuilds']}", C["s3"],
                   f"{ad['last_rebuild_ms']:.1f} ms  {ad['hit_rate']:.0%} cached")

        # -- status ----------------------------------------------------------
        if state["phase"] == "waiting":
            status.set("WAITING FOR NODES",
                       f"{snap['n_usable']} usable, need 4  ·  listening on "
                       f"udp/{args.port}", "muted")
        elif state["phase"] == "calibrating":
            status.set("CALIBRATING",
                       f"{max(0, state['cal_end'] - now):.0f}s remaining  ·  "
                       f"keep the room EMPTY", "warn")
        elif rec["ok"]:
            px, py, pz = rec["position"]
            status.set("TRACKING",
                       f"x {px:5.2f}   y {py:5.2f}   z {pz:5.2f} m      "
                       f"{rec['n_nodes']} nodes / {rec['n_links']} links",
                       "active")
        else:
            status.set("DEGRADED", rec["reason"], "alert")

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
