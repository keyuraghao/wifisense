"""Rooms, voxel grids, and node layouts for spatial reconstruction."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class VoxelGrid:
    """Uniform 3D grid over a room. Voxel centres are what everything indexes."""

    x_range: tuple[float, float]
    y_range: tuple[float, float]
    z_range: tuple[float, float]
    voxel_m: float = 0.4

    def __post_init__(self) -> None:
        def axis(r):
            n = max(1, int(round((r[1] - r[0]) / self.voxel_m)))
            edges = np.linspace(r[0], r[1], n + 1)
            return 0.5 * (edges[:-1] + edges[1:])

        self.xs, self.ys, self.zs = axis(self.x_range), axis(self.y_range), axis(self.z_range)
        X, Y, Z = np.meshgrid(self.xs, self.ys, self.zs, indexing="ij")
        self.centers = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])

    @property
    def shape(self) -> tuple[int, int, int]:
        return len(self.xs), len(self.ys), len(self.zs)

    @property
    def n_voxels(self) -> int:
        return self.centers.shape[0]

    def as_cube(self, x: np.ndarray) -> np.ndarray:
        """Reshape a flat voxel vector back to (nx, ny, nz)."""
        return np.asarray(x).reshape(self.shape)

    def nearest_index(self, point) -> int:
        return int(np.argmin(np.linalg.norm(self.centers - np.asarray(point), axis=1)))


def perimeter_nodes(x_range, y_range, heights, per_wall: int = 2) -> np.ndarray:
    """Nodes spaced around the room walls, repeated at each height in `heights`.

    Height diversity is not decoration. With every node at one height the
    geometry is degenerate in z: all link paths lie in one plane, so nothing in
    the measurement distinguishes a target at 0.5 m from one at 1.5 m. See
    `rti.axis_resolvability`, which measures exactly this.
    """
    x0, x1 = x_range
    y0, y1 = y_range
    t = (np.arange(per_wall) + 0.5) / per_wall

    ring = np.vstack([
        np.column_stack([x0 + t * (x1 - x0), np.full(per_wall, y0)]),  # bottom
        np.column_stack([np.full(per_wall, x1), y0 + t * (y1 - y0)]),  # right
        np.column_stack([x1 - t * (x1 - x0), np.full(per_wall, y1)]),  # top
        np.column_stack([np.full(per_wall, x0), y1 - t * (y1 - y0)]),  # left
    ])

    nodes = []
    for i, (px, py) in enumerate(ring):
        # Stagger heights around the ring so adjacent nodes differ vertically.
        z = heights[i % len(heights)]
        nodes.append([px, py, z])
    return np.asarray(nodes, dtype=float)


def link_pairs(n_nodes: int) -> np.ndarray:
    """All unordered node pairs -- one link each. M = N(N-1)/2."""
    i, j = np.triu_indices(n_nodes, k=1)
    return np.column_stack([i, j])
