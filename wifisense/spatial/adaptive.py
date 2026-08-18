"""Reconstruction that survives nodes joining, failing, and coming back.

The inverse operator Pi = C W^T (W C W^T + sigma^2 I)^-1 is built for one
specific set of links. When a node dies, its links vanish from the measurement
vector, so Pi is no longer the right operator -- feeding it a short vector is a
shape error, and feeding it zeros is worse, because zero attenuation on a dead
link is a positive claim that nothing is there.

So the operator is rebuilt whenever the topology changes. That is affordable
because the expensive part is the N x N spatial prior, which depends only on the
voxel grid and is computed once at startup. A rebuild then costs one N x M
product and one M x M inverse -- microseconds at mesh scale. Recent operators
are cached, so a node that flaps between up and down does not trigger a rebuild
each time it changes state.
"""
from __future__ import annotations

import time
from collections import OrderedDict

import numpy as np

from .geometry import VoxelGrid
from .rti import RTIReconstructor, spatial_prior


class AdaptiveReconstructor:
    """Maintains a valid inverse for whatever subset of the mesh is currently up."""

    def __init__(self, grid: VoxelGrid, corr_len_m: float = 0.5,
                 prior_var: float = 1.0, ellipse_m: float = 0.3,
                 min_links: int = 10, cache_size: int = 16):
        self.grid = grid
        self.corr_len_m = corr_len_m
        self.ellipse_m = ellipse_m
        self.min_links = min_links
        self.cache_size = cache_size

        # Computed once. This is what makes live topology changes cheap.
        self.prior_cov = spatial_prior(grid, corr_len_m, prior_var)

        self._cache: OrderedDict = OrderedDict()
        self.rebuilds = 0
        self.cache_hits = 0
        self.last_rebuild_ms = 0.0
        self._current_key = None

    @staticmethod
    def _key(links, node_ids, noise_var) -> tuple:
        # noise_var is bucketed so ordinary drift does not force a rebuild.
        return (tuple(sorted(links)), tuple(sorted(node_ids)), round(float(noise_var), 1))

    def get(self, node_positions: dict, active_links: list, noise_var: float
            ) -> RTIReconstructor | None:
        """Operator for this topology, or None if too little of the mesh is up."""
        usable = [l for l in active_links
                  if l[0] in node_positions and l[1] in node_positions]
        if len(usable) < self.min_links:
            self._current_key = None
            return None

        ids = sorted({n for link in usable for n in link})
        key = self._key(usable, ids, noise_var)
        self._current_key = key

        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            return hit

        t0 = time.perf_counter()
        index = {nid: i for i, nid in enumerate(ids)}
        nodes = np.array([node_positions[nid] for nid in ids], dtype=float)
        pairs = np.array([[index[a], index[b]] for a, b in usable], dtype=int)

        recon = RTIReconstructor(
            grid=self.grid, nodes=nodes, pairs=pairs, ellipse_m=self.ellipse_m,
            corr_len_m=self.corr_len_m, noise_var=max(noise_var, 1e-3),
            prior_cov=self.prior_cov,
        )
        recon.node_ids = ids                 # so callers can map y back to links
        recon.link_order = list(usable)

        self.last_rebuild_ms = (time.perf_counter() - t0) * 1e3
        self.rebuilds += 1

        self._cache[key] = recon
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return recon

    def reconstruct(self, node_positions: dict, links_to_attenuation: dict,
                    noise_var: float) -> dict:
        """One reconstruction from whatever links currently report.

        links_to_attenuation maps (a, b) -> added path loss in dB. Only links
        present in that mapping are used, so a failed node simply contributes
        nothing rather than contributing a false zero.
        """
        active = sorted(links_to_attenuation)
        recon = self.get(node_positions, active, noise_var)
        if recon is None:
            return {
                "ok": False,
                "reason": f"only {len(active)} usable links, need {self.min_links}",
                "n_links": len(active), "field": None, "position": None,
            }

        y = np.array([links_to_attenuation[l] for l in recon.link_order], dtype=float)
        field = recon.reconstruct(y)
        pos = recon.localize(field)
        res = recon.resolution_matrix_diag()

        return {
            "ok": True, "reason": "", "field": field, "position": pos,
            "n_links": len(recon.link_order), "n_nodes": len(recon.node_ids),
            "node_ids": recon.node_ids,
            # How much of the room this surviving subset can actually see. Watch
            # this rather than the node count: losing one corner node hurts far
            # more than losing one of two nodes on the same wall.
            "mean_resolution": float(res.mean()),
            "coverage": float((res > 0.05 * res.max()).mean()) if res.max() > 0 else 0.0,
            "peak_attenuation": float(np.max(field)),
            "rebuild_ms": self.last_rebuild_ms,
        }

    @property
    def stats(self) -> dict:
        total = self.rebuilds + self.cache_hits
        return {
            "rebuilds": self.rebuilds, "cache_hits": self.cache_hits,
            "cached_topologies": len(self._cache),
            "hit_rate": self.cache_hits / total if total else 0.0,
            "last_rebuild_ms": self.last_rebuild_ms,
        }
