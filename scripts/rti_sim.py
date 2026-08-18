#!/usr/bin/env python3
"""3D radio tomography: design study and accuracy simulation.

    .venv/bin/python scripts/rti_sim.py
    .venv/bin/python scripts/rti_sim.py --room 6 5 2.5 --per-wall 4

Answers, by simulation rather than assertion:
  1. how many nodes do you need, and how does accuracy degrade with link noise
  2. why node height diversity is mandatory for the z axis
  3. what a reconstruction actually looks like
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense.spatial import rti, simulate
from wifisense.spatial.geometry import VoxelGrid, perimeter_nodes

FADING_LEVELS = [0.5, 1.0, 1.5, 2.0, 3.0]


def node_sweep(room, grid, heights, per_walls, trials, seed) -> None:
    print("=" * 74)
    print("1. NODES vs LINK NOISE   (median XY localisation error, metres)")
    print("=" * 74)
    print("   noise_var is matched to the true fading -- see estimate_noise_var().")
    print()
    print(f"   {'nodes':>5} {'links':>6} | " +
          " ".join(f"{f:>6.1f}dB" for f in FADING_LEVELS))
    print("   " + "-" * 56)
    for pw in per_walls:
        nodes = perimeter_nodes(room[0], room[1], heights=heights, per_wall=pw)
        row = []
        for fad in FADING_LEVELS:
            r = rti.build(grid, nodes, noise_var=fad ** 2)
            res = simulate.sweep_positions(r, room, n_trials=trials, seed=seed,
                                           fading_db=fad)
            row.append(res["xy_median_m"])
        print(f"   {len(nodes):>5} {r.n_links:>6} | " +
              " ".join(f"{v:>8.2f}" for v in row))
    print("\n   Returns flatten past ~12 nodes: model error and node-placement")
    print("   error dominate, and neither is fixed by adding links.")


def height_study(room, grid, per_wall, trials, seed) -> None:
    print()
    print("=" * 74)
    print("2. THE COPLANAR TRAP   (why node heights must differ)")
    print("=" * 74)
    print("   Probe: a compact spherical target at random heights. A standing")
    print("   person spans every slab and would hide the question entirely.\n")
    layouts = {
        "all at 1.2 m (coplanar)": [1.2],
        "two heights 0.4 / 2.0 m": [0.4, 2.0],
        "three heights 0.4/1.2/2.0": [0.4, 1.2, 2.0],
    }
    print(f"   {'layout':<28}{'z_cover':>9}{'z_bias':>8}{'z_corr':>8}"
          f"{'z_MAE':>8}{'est_spread':>12}")
    print("   " + "-" * 73)
    spread = None
    for name, heights in layouts.items():
        nodes = perimeter_nodes(room[0], room[1], heights=heights, per_wall=per_wall)
        r = rti.build(grid, nodes, noise_var=1.5 ** 2)
        sp = r.sensitivity_profile()
        hz = simulate.sweep_heights(r, room, n_trials=trials, seed=seed + 1,
                                    fading_db=1.5)
        spread = hz["z_true_spread_m"]
        print(f"   {name:<28}{sp['z_coverage']:>9.2f}{sp['z_bias_m']:>8.2f}"
              f"{hz['z_corr']:>8.2f}{hz['z_mae_m']:>8.2f}{hz['z_est_spread_m']:>12.2f}")

    print(f"\n   True z spread across trials: {spread:.2f} m.")
    print("   Read est_spread, not z_MAE. Coplanar nodes return the SAME height")
    print("   every time (spread ~0), pinned to the node plane (z_bias). Their")
    print("   z_MAE only looks tolerable because the plane happens to sit near")
    print("   the middle of the room -- move the nodes and it collapses.")
    print("   z_corr is the honest number: near zero means the geometry carries")
    print("   no height information at all, and no solver can invent it.")


def render(room, grid, heights, per_wall, out_path, seed) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    nodes = perimeter_nodes(room[0], room[1], heights=heights, per_wall=per_wall)
    r = rti.build(grid, nodes, noise_var=1.5 ** 2)
    truth = np.array([room[0][0] + 0.62 * (room[0][1] - room[0][0]),
                      room[1][0] + 0.38 * (room[1][1] - room[1][0]), 0.875])
    rng = np.random.default_rng(seed)
    y = simulate.simulate_links(nodes, r.pairs, truth, room, rng=rng, fading_db=1.5)
    x = r.reconstruct(y)
    est = r.localize(x)
    cube = grid.as_cube(x)

    fig = plt.figure(figsize=(14, 4.6), constrained_layout=True)

    ax = fig.add_subplot(1, 3, 1, projection="3d")
    ax.scatter(*nodes.T, c="k", s=28, marker="^", label="nodes")
    for i, j in r.pairs[:: max(1, len(r.pairs) // 40)]:
        ax.plot(*zip(nodes[i], nodes[j]), color="0.75", lw=0.3)
    ax.scatter(*truth, c="tab:green", s=90, marker="o", label="true")
    ax.scatter(*est, c="tab:red", s=90, marker="x", label="estimate")
    ax.set(xlabel="x (m)", ylabel="y (m)", zlabel="z (m)",
           title=f"{len(nodes)} nodes / {r.n_links} links")
    ax.legend(fontsize=7, loc="upper left")

    ax = fig.add_subplot(1, 3, 2)
    im = ax.imshow(cube.max(axis=2).T, origin="lower", cmap="viridis",
                   extent=[*room[0], *room[1]], aspect="equal")
    ax.plot(*truth[:2], "o", mfc="none", mec="w", ms=14, mew=2)
    ax.plot(*est[:2], "x", color="r", ms=12, mew=2)
    ax.set(xlabel="x (m)", ylabel="y (m)", title="reconstruction, max over z")
    fig.colorbar(im, ax=ax, shrink=0.85, label="attenuation")

    ax = fig.add_subplot(1, 3, 3)
    ax.plot(cube.mean(axis=(0, 1)), grid.zs, "-o", ms=3)
    ax.axhline(truth[2], color="tab:green", ls="--", label="true z")
    ax.axhline(est[2], color="tab:red", ls=":", label="estimated z")
    ax.set(xlabel="mean attenuation", ylabel="z (m)", title="height profile")
    ax.legend(fontsize=8)

    err = np.linalg.norm(est[:2] - truth[:2])
    fig.suptitle(f"single-snapshot reconstruction   XY error {err:.2f} m   "
                 f"Z error {abs(est[2]-truth[2]):.2f} m", fontsize=11)
    fig.savefig(out_path, dpi=130)
    print(f"\nwrote {out_path}   (XY error {err:.2f} m, Z error "
          f"{abs(est[2]-truth[2]):.2f} m)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", nargs=3, type=float, default=[5.0, 4.0, 2.4],
                    metavar=("X", "Y", "Z"))
    ap.add_argument("--voxel", type=float, default=0.4)
    ap.add_argument("--per-wall", type=int, default=3)
    ap.add_argument("--trials", type=int, default=50)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--out", default="rti_reconstruction.png")
    args = ap.parse_args()

    room = ((0.0, args.room[0]), (0.0, args.room[1]), (0.0, args.room[2]))
    grid = VoxelGrid(*room, voxel_m=args.voxel)
    heights = [0.4, 1.2, min(2.0, args.room[2] - 0.3)]

    print(f"room {args.room[0]}x{args.room[1]}x{args.room[2]} m | "
          f"grid {grid.shape} = {grid.n_voxels} voxels @ {args.voxel} m\n")

    node_sweep(room, grid, heights, [2, 3, 4, 5, 6], args.trials, args.seed)
    height_study(room, grid, args.per_wall, args.trials, args.seed)
    render(room, grid, heights, args.per_wall, args.out, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
