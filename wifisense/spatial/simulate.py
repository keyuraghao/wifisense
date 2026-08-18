"""Forward simulation of a multi-node link set, for validating reconstruction.

Deliberately NOT the same model the reconstructor inverts. Simulating with the
identical operator you invert is the "inverse crime": the reconstruction looks
excellent and the number tells you nothing about reality. So here the forward
model uses

  * a finer voxel grid than the reconstruction grid,
  * a soft Fresnel-style taper instead of the reconstructor's hard ellipse,
  * correlated multipath fading, which in a real room dominates every other
    error term,
  * and the 1 dB radiotap quantiser, which is the true measurement floor.

The numbers this produces are still optimistic -- no walls, no furniture, no
body orientation effects -- but they are not self-fulfilling.
"""
from __future__ import annotations

import numpy as np

from .geometry import VoxelGrid
from .rti import ellipse_weights


def person_field(grid: VoxelGrid, position, radius_m: float = 0.20,
                 height_m: float = 1.75, attenuation: float = 1.0) -> np.ndarray:
    """Ground-truth attenuation field for a standing person: a vertical cylinder."""
    px, py = position[0], position[1]
    c = grid.centers
    in_disc = (c[:, 0] - px) ** 2 + (c[:, 1] - py) ** 2 <= radius_m ** 2
    in_height = c[:, 2] <= height_m
    return np.where(in_disc & in_height, attenuation, 0.0)


def blob_field(grid: VoxelGrid, position, radius_m: float = 0.35,
               attenuation: float = 1.0) -> np.ndarray:
    """A compact spherical target -- unlike a full-height person, its height is
    a real unknown, so it is the right probe for testing z reconstruction."""
    d2 = ((grid.centers - np.asarray(position)) ** 2).sum(axis=1)
    return np.where(d2 <= radius_m ** 2, attenuation, 0.0)


def simulate_links(nodes: np.ndarray, pairs: np.ndarray, position,
                   room, voxel_m: float = 0.2, radius_m: float = 0.20,
                   height_m: float = 1.75, attenuation: float = 1.0,
                   fading_db: float = 1.5, quantise_db: float = 1.0,
                   node_jitter_m: float = 0.05,
                   rng: np.random.Generator | None = None) -> np.ndarray:
    """Return measured attenuation change per link, in dB.

    fading_db defaults to 1.5 rather than something flattering. A person moving
    in a room perturbs multipath on links they never block, and a pure shadowing
    model cannot represent that; lumping it into the noise term is crude but
    honest. Reported RTI accuracy is extremely sensitive to this number, which
    is why scripts/rti_sim.py sweeps it instead of quoting one figure.

    node_jitter_m models the fact that you place nodes with a tape measure. The
    reconstructor assumes nominal positions; the physics uses the real ones.
    """
    rng = rng or np.random.default_rng()
    fine = VoxelGrid(room[0], room[1], room[2], voxel_m=voxel_m)

    true_nodes = nodes + rng.normal(0.0, node_jitter_m, size=nodes.shape) \
        if node_jitter_m else nodes
    W = ellipse_weights(true_nodes, pairs, fine, soft=True, sigma_m=0.18)
    x = (blob_field(fine, position, radius_m, attenuation) if height_m is None
         else person_field(fine, position, radius_m, height_m, attenuation))
    y = W @ x

    # Multipath fading: the real killer. Not white -- it is roughly constant per
    # link over short intervals, which is why RTI differences against a
    # calibrated empty-room baseline rather than using absolute path loss.
    y = y + rng.normal(0.0, fading_db, size=y.shape)

    if quantise_db:
        y = np.round(y / quantise_db) * quantise_db
    return y


def sweep_positions(recon, room, n_trials: int = 60, margin_m: float = 0.6,
                    z_true: float = 0.875, seed: int = 0, **sim_kw) -> dict:
    """Localisation error over random target positions inside the room.

    z_true defaults to the cylinder's centre of mass (half of 1.75 m), which is
    the only z a shadowing model can meaningfully report for a standing person.
    """
    rng = np.random.default_rng(seed)
    (x0, x1), (y0, y1), _ = room

    truths, ests = [], []
    for _ in range(n_trials):
        p = np.array([rng.uniform(x0 + margin_m, x1 - margin_m),
                      rng.uniform(y0 + margin_m, y1 - margin_m),
                      z_true])
        y = simulate_links(recon.nodes, recon.pairs, p, room, rng=rng, **sim_kw)
        ests.append(recon.localize(recon.reconstruct(y)))
        truths.append(p)

    truths, ests = np.asarray(truths), np.asarray(ests)
    err = ests - truths
    err_xy = np.linalg.norm(err[:, :2], axis=1)
    err_z = np.abs(err[:, 2])

    return {
        "n_trials": n_trials,
        "n_links": recon.n_links,
        "n_nodes": len(recon.nodes),
        "xy_mean_m": float(err_xy.mean()),
        "xy_median_m": float(np.median(err_xy)),
        "xy_p90_m": float(np.percentile(err_xy, 90)),
        "z_mean_m": float(err_z.mean()),
        "z_median_m": float(np.median(err_z)),
        "truths": truths, "ests": ests,
    }


def sweep_heights(recon, room, n_trials: int = 60, margin_m: float = 0.8,
                  blob_radius_m: float = 0.35, seed: int = 0, **sim_kw) -> dict:
    """Can this geometry actually report the HEIGHT of a compact target?

    Uses a sphere rather than a standing person, because a full-height cylinder
    intersects every horizontal slab and so hides the question entirely.
    Correlation between true and estimated z is the answer: near 0 means the
    geometry is blind in z no matter how good the solver is.
    """
    rng = np.random.default_rng(seed)
    (x0, x1), (y0, y1), (z0, z1) = room

    truths, ests = [], []
    for _ in range(n_trials):
        p = np.array([rng.uniform(x0 + margin_m, x1 - margin_m),
                      rng.uniform(y0 + margin_m, y1 - margin_m),
                      rng.uniform(z0 + 0.5, z1 - 0.5)])
        y = simulate_links(recon.nodes, recon.pairs, p, room, rng=rng,
                           radius_m=blob_radius_m, height_m=None, **sim_kw)
        ests.append(recon.localize(recon.reconstruct(y)))
        truths.append(p)

    truths, ests = np.asarray(truths), np.asarray(ests)
    zt, ze = truths[:, 2], ests[:, 2]
    corr = float(np.corrcoef(zt, ze)[0, 1]) if zt.std() > 0 and ze.std() > 0 else 0.0

    return {
        "z_corr": corr,
        "z_mae_m": float(np.abs(ze - zt).mean()),
        "z_est_spread_m": float(ze.std()),
        "z_true_spread_m": float(zt.std()),
        "xy_median_m": float(np.median(np.linalg.norm(ests[:, :2] - truths[:, :2], axis=1))),
    }
