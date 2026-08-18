"""Radio Tomographic Imaging: many links -> a 3D attenuation field.

One link gives one number. You cannot locate anything in 3D with one number,
at any transmit power or carrier frequency. What you can do is surround the
space with cheap nodes and invert the resulting set of shadowing measurements
into a voxel field -- this is Wilson & Patwari's RTI (IEEE TMC 2010), and it is
the only route to 3D that works with commodity WiFi radios.

Forward model:   y = W x + n
    y   (M,)   change in path loss on each link, in dB
    x   (N,)   attenuation per voxel
    W   (M,N)  how much voxel j shadows link i

Inversion is severely underdetermined -- with 12 nodes you get 66 measurements
for hundreds of voxels -- so the prior does most of the work. The dual form
below inverts an M x M matrix rather than N x N, which is both faster and
numerically better behaved when M << N.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geometry import VoxelGrid, link_pairs


def ellipse_weights(nodes: np.ndarray, pairs: np.ndarray, grid: VoxelGrid,
                    ellipse_m: float = 0.3, soft: bool = False,
                    sigma_m: float = 0.15) -> np.ndarray:
    """Build W. Voxel j affects link i if it lies near the line of sight.

    The classic model weights a voxel by 1/sqrt(link length) inside an ellipse
    whose foci are the two nodes, i.e. where d(a,v) + d(v,b) < d(a,b) + ellipse_m.
    `soft=True` replaces the hard cutoff with a Gaussian taper on the excess
    path length -- physically closer to a Fresnel zone, and useful as a
    *different* forward model when simulating (see simulate.py).
    """
    a = nodes[pairs[:, 0]]                        # (M,3)
    b = nodes[pairs[:, 1]]
    d_link = np.linalg.norm(a - b, axis=1)        # (M,)
    c = grid.centers                              # (N,3)

    # (M,N) distances via broadcasting.
    da = np.linalg.norm(c[None, :, :] - a[:, None, :], axis=2)
    db = np.linalg.norm(c[None, :, :] - b[:, None, :], axis=2)
    excess = da + db - d_link[:, None]            # >= 0, 0 on the direct path

    if soft:
        w = np.exp(-0.5 * (excess / sigma_m) ** 2)
    else:
        w = (excess < ellipse_m).astype(float)

    return w / np.sqrt(d_link)[:, None]


def spatial_prior(grid: VoxelGrid, corr_len_m: float = 0.5,
                  variance: float = 1.0) -> np.ndarray:
    """Exponential covariance C_jk = var * exp(-||c_j - c_k|| / corr_len).

    This encodes "attenuation is spatially smooth", which is what stops the
    reconstruction from putting a spike in an arbitrary voxel that happens to
    fit the data.
    """
    c = grid.centers
    d = np.linalg.norm(c[:, None, :] - c[None, :, :], axis=2)
    return variance * np.exp(-d / corr_len_m)


@dataclass
class RTIReconstructor:
    """Precomputed linear inverse. Reconstruction is then one matrix-vector product.

    That is what makes RTI real-time: the geometry is fixed, so the (N,M)
    inverse operator is built once and every new measurement vector costs a
    single matvec.
    """

    grid: VoxelGrid
    nodes: np.ndarray
    pairs: np.ndarray
    ellipse_m: float = 0.3
    corr_len_m: float = 0.5
    # dB^2. MUST match the real per-link noise, which for RTI is dominated by
    # multipath fading, not thermal noise. Getting this wrong is not a cosmetic
    # tuning issue: an under-estimated noise_var makes the inverse over-trust
    # the data, and accuracy then gets *worse* as you add nodes. Measure it with
    # estimate_noise_var() on empty-room data rather than guessing.
    noise_var: float = 2.25
    prior_var: float = 1.0

    W: np.ndarray = field(init=False, repr=False)
    Pi: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.W = ellipse_weights(self.nodes, self.pairs, self.grid,
                                 ellipse_m=self.ellipse_m)
        C = spatial_prior(self.grid, self.corr_len_m, self.prior_var)

        # Dual (kernel) form: Pi = C W^T (W C W^T + sigma^2 I)^-1, an M x M solve.
        CWt = C @ self.W.T                                   # (N,M)
        S = self.W @ CWt + self.noise_var * np.eye(len(self.pairs))
        self.Pi = CWt @ np.linalg.inv(S)                     # (N,M)

    @property
    def n_links(self) -> int:
        return len(self.pairs)

    def reconstruct(self, y: np.ndarray) -> np.ndarray:
        """Link attenuation changes (dB) -> voxel attenuation field."""
        return self.Pi @ np.asarray(y, dtype=float)

    def localize(self, x: np.ndarray, top_frac: float = 0.02) -> np.ndarray:
        """Estimate a single target position as the centroid of the strongest voxels.

        Taking a centroid of the top few percent rather than the single argmax
        is markedly more stable: the peak voxel jitters between neighbours under
        noise, while the centroid does not.
        """
        x = np.asarray(x)
        k = max(1, int(top_frac * x.size))
        idx = np.argpartition(x, -k)[-k:]
        w = np.clip(x[idx], 0, None)
        if w.sum() <= 0:
            return self.grid.centers[int(np.argmax(x))]
        return (self.grid.centers[idx] * w[:, None]).sum(0) / w.sum()

    # -- diagnostics -------------------------------------------------------

    def resolution_matrix_diag(self) -> np.ndarray:
        """diag(Pi W): how much of each voxel's own value it recovers.

        1.0 = perfectly resolved, 0.0 = invisible to this geometry.
        """
        return np.einsum("ij,ji->i", self.Pi, self.W)

    def sensitivity_profile(self) -> dict:
        """Where in the room this node geometry can see anything at all.

        diag(Pi*W) is per-voxel recovered fraction. Averaged over x and y it
        gives a vertical sensitivity profile, and that profile is what exposes
        the coplanar-nodes failure -- but not in the way you might guess.

        Coplanar nodes do NOT produce a flat, information-free z profile. They
        produce a sharply PEAKED one: every link lies in the node plane, so the
        geometry only illuminates a thin horizontal slab there. Voxels above and
        below are simply unobserved. A target is then always reported at the
        node height regardless of where it actually is, which reads as a small
        z error whenever the target happens to sit near that plane, and is pure
        artefact. `z_coverage` and `z_bias_m` measure this directly; a z error
        alone will lie to you.
        """
        r = self.resolution_matrix_diag()
        cube = self.grid.as_cube(r)
        z_prof = cube.mean(axis=(0, 1))
        z_prof = z_prof / z_prof.max() if z_prof.max() > 0 else z_prof

        return {
            "mean": float(r.mean()),
            "z_profile": z_prof,
            # fraction of heights meaningfully illuminated at all
            "z_coverage": float((z_prof > 0.1).mean()),
            # height the geometry is centred on -- where it pulls every estimate
            "z_bias_m": float((z_prof * self.grid.zs).sum() / z_prof.sum()),
        }


def build(grid: VoxelGrid, nodes: np.ndarray, **kw) -> RTIReconstructor:
    return RTIReconstructor(grid=grid, nodes=nodes,
                            pairs=link_pairs(len(nodes)), **kw)


def estimate_noise_var(baseline_db: np.ndarray) -> float:
    """Per-link noise variance (dB^2) from repeated empty-room measurements.

    baseline_db: (T, M) array of link attenuation readings with nothing moving.
    The variance of each link over time is exactly the quantity `noise_var`
    needs, so this is a calibration you run once per deployment -- record a few
    minutes of empty room, call this, pass the result to the reconstructor.
    """
    b = np.asarray(baseline_db, dtype=float)
    if b.ndim != 2 or b.shape[0] < 2:
        raise ValueError("need a (T, M) array with T >= 2 baseline samples")
    return float(np.mean(np.var(b, axis=0)))
