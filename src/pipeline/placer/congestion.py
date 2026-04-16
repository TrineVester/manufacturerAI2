"""Coarse congestion grid for routing-aware placement.

Overlays a coarse tile grid on the board outline.  Each tile has a
*capacity* (how many traces can physically pass through it) and a
*demand* (how many net routes currently cross it).  The placer uses
this to penalise candidate positions that would create routing
bottlenecks — without running the real router.

Algorithm (global routing, standard EDA technique):
  1. Divide the outline bounding-box into tiles (~3–5 mm each).
  2. Mark tiles outside the outline (or under component bodies) as
     having zero or reduced capacity.
  3. For each net whose endpoints are both placed, run a BFS on the
     coarse tile grid to find the cheapest path, and increment
     demand along that path.
  4. During placement scoring, temporarily block the candidate's
     tiles and check if the net routes through it cause congestion.
"""

from __future__ import annotations

import math
from collections import deque

from shapely.geometry import Polygon

from src.pipeline.config import TRACE_RULES


# ── Constants ──────────────────────────────────────────────────────

DEFAULT_TILE_SIZE_MM = 3.0
"""Tile size in mm.  3 mm gives ~200 tiles on a 60×40 board.  Coarse
enough for speed, fine enough to detect bottlenecks."""

TRACE_PITCH_MM = TRACE_RULES.routing_channel_mm
"""Width consumed per trace (trace width + clearance between traces)."""


# ── Fast point-in-polygon (ray casting) ───────────────────────────

def _point_in_poly(
    px: float, py: float,
    verts: list[tuple[float, float]], n: int,
) -> bool:
    """Ray-casting point-in-polygon test (no Shapely overhead)."""
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# ── CongestionGrid ────────────────────────────────────────────────


class CongestionGrid:
    """Coarse tile grid with capacity tracking for congestion estimation.

    Tiles are indexed as (tx, ty).  Each tile stores:
      - capacity: max traces that fit through (0 for blocked tiles)
      - demand:   current number of net routes passing through
    """

    def __init__(
        self,
        outline_poly: Polygon,
        tile_size: float = DEFAULT_TILE_SIZE_MM,
        trace_pitch: float = TRACE_PITCH_MM,
    ) -> None:
        self.tile_size = tile_size
        self.trace_pitch = trace_pitch
        self.outline_poly = outline_poly

        xmin, ymin, xmax, ymax = outline_poly.bounds
        self.origin_x = xmin
        self.origin_y = ymin
        self.cols = max(1, int(math.ceil((xmax - xmin) / tile_size)))
        self.rows = max(1, int(math.ceil((ymax - ymin) / tile_size)))

        # Capacity per tile — how many traces fit side-by-side
        base_cap = max(1, int(tile_size / trace_pitch))
        self._capacity: list[int] = [0] * (self.cols * self.rows)
        self._demand: list[int] = [0] * (self.cols * self.rows)

        # Mark tiles inside the outline as having full capacity.
        # Use ray-casting point-in-polygon (avoids Shapely Point overhead).
        verts = list(outline_poly.exterior.coords)
        n_verts = len(verts)
        hole_rings = [
            (list(ring.coords), len(list(ring.coords)))
            for ring in outline_poly.interiors
        ]
        for ty in range(self.rows):
            cy = ymin + (ty + 0.5) * tile_size
            for tx in range(self.cols):
                cx = xmin + (tx + 0.5) * tile_size
                if _point_in_poly(cx, cy, verts, n_verts):
                    in_hole = any(
                        _point_in_poly(cx, cy, hv, hn)
                        for hv, hn in hole_rings
                    )
                    if not in_hole:
                        self._capacity[ty * self.cols + tx] = base_cap

        # Component body blocks (stored for undo)
        self._body_blocks: dict[str, list[int]] = {}

        # Committed net routes: net_id -> list of flat tile indices
        self._net_routes: dict[str, list[int]] = {}

    # ── Coordinate conversion ──────────────────────────────────────

    def world_to_tile(self, wx: float, wy: float) -> tuple[int, int]:
        tx = int((wx - self.origin_x) / self.tile_size)
        ty = int((wy - self.origin_y) / self.tile_size)
        tx = max(0, min(self.cols - 1, tx))
        ty = max(0, min(self.rows - 1, ty))
        return (tx, ty)

    def _flat(self, tx: int, ty: int) -> int:
        return ty * self.cols + tx

    def _in_bounds(self, tx: int, ty: int) -> bool:
        return 0 <= tx < self.cols and 0 <= ty < self.rows

    # ── Component body blocking ────────────────────────────────────

    def block_component(
        self, instance_id: str,
        cx: float, cy: float, hw: float, hh: float,
    ) -> None:
        """Reduce capacity in tiles covered by a component body."""
        left = cx - hw
        right = cx + hw
        bottom = cy - hh
        top = cy + hh

        tx_min = max(0, int((left - self.origin_x) / self.tile_size))
        tx_max = min(self.cols - 1, int((right - self.origin_x) / self.tile_size))
        ty_min = max(0, int((bottom - self.origin_y) / self.tile_size))
        ty_max = min(self.rows - 1, int((top - self.origin_y) / self.tile_size))

        blocked: list[int] = []
        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                flat = self._flat(tx, ty)
                if self._capacity[flat] > 0:
                    self._capacity[flat] = max(0, self._capacity[flat] - 1)
                    blocked.append(flat)

        self._body_blocks[instance_id] = blocked

    def unblock_component(self, instance_id: str) -> None:
        """Restore capacity lost from a prior block_component call."""
        blocked = self._body_blocks.pop(instance_id, [])
        base_cap = max(1, int(self.tile_size / self.trace_pitch))
        for flat in blocked:
            self._capacity[flat] = min(self._capacity[flat] + 1, base_cap)

    # ── Coarse net routing (BFS) ───────────────────────────────────

    def route_coarse(
        self, wx1: float, wy1: float, wx2: float, wy2: float,
    ) -> list[int] | None:
        """BFS shortest path between two world-coordinate points.

        Returns a list of flat tile indices, or None if no path.
        The path includes the source and sink tiles.
        """
        t1 = self.world_to_tile(wx1, wy1)
        t2 = self.world_to_tile(wx2, wy2)
        if t1 == t2:
            return [self._flat(*t1)]

        start = self._flat(*t1)
        goal = self._flat(*t2)

        # BFS — tiles with zero capacity are impassable
        visited: dict[int, int] = {start: -1}  # flat -> parent flat
        queue: deque[int] = deque([start])

        while queue:
            cur = queue.popleft()
            if cur == goal:
                # Reconstruct path
                path: list[int] = []
                c = cur
                while c != -1:
                    path.append(c)
                    c = visited[c]
                path.reverse()
                return path

            tx = cur % self.cols
            ty = cur // self.cols
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = tx + dx, ty + dy
                if not self._in_bounds(nx, ny):
                    continue
                nf = self._flat(nx, ny)
                if nf in visited:
                    continue
                # Allow traversal if tile has any capacity at all
                if self._capacity[nf] <= 0:
                    continue
                visited[nf] = cur
                queue.append(nf)

        return None  # no path (disconnected by blocked tiles)

    # ── Demand management ──────────────────────────────────────────

    def commit_net(self, net_id: str, path: list[int]) -> None:
        """Record that a net's coarse route uses these tiles."""
        # Remove previous route for this net if re-routing
        self.remove_net(net_id)
        self._net_routes[net_id] = path
        for flat in path:
            self._demand[flat] += 1

    def remove_net(self, net_id: str) -> None:
        """Remove a previously committed net route."""
        path = self._net_routes.pop(net_id, None)
        if path:
            for flat in path:
                self._demand[flat] = max(0, self._demand[flat] - 1)

    # ── Congestion queries ─────────────────────────────────────────

    def congestion_along(self, path: list[int]) -> float:
        """Sum of over-capacity violations along a tile path.

        Returns 0.0 if every tile on the path is within capacity.
        Higher values mean worse congestion.
        """
        total = 0.0
        for flat in path:
            excess = self._demand[flat] - self._capacity[flat]
            if excess > 0:
                total += excess
        return total

    def max_congestion_along(self, path: list[int]) -> float:
        """Worst single-tile over-capacity along a path."""
        worst = 0.0
        for flat in path:
            excess = self._demand[flat] - self._capacity[flat]
            if excess > worst:
                worst = excess
        return worst

    # ── Fast congestion check (no BFS) ─────────────────────────────

    def congestion_manhattan(
        self, wx1: float, wy1: float, wx2: float, wy2: float,
    ) -> float:
        """Check congestion along a Manhattan L-path between two points.

        Walks tiles in an L-shape (horizontal then vertical) from
        source to sink.  No BFS, no heap — just arithmetic.  Returns
        the sum of max(0, demand - capacity) along the path.

        If the straight L-path hits a zero-capacity tile, tries the
        other L (vertical-first).  If both are blocked, returns a
        fixed penalty.
        """
        t1x, t1y = self.world_to_tile(wx1, wy1)
        t2x, t2y = self.world_to_tile(wx2, wy2)

        for h_first in (True, False):
            cong = self._walk_l(t1x, t1y, t2x, t2y, h_first)
            if cong is not None:
                return cong
        return 5.0   # both L-paths blocked

    def _walk_l(
        self, x1: int, y1: int, x2: int, y2: int, h_first: bool,
    ) -> float | None:
        """Walk an L-shaped path, return congestion or None if blocked."""
        total = 0.0
        if h_first:
            # Horizontal leg
            dx = 1 if x2 >= x1 else -1
            x = x1
            while x != x2:
                flat = y1 * self.cols + x
                if self._capacity[flat] <= 0:
                    return None
                excess = self._demand[flat] - self._capacity[flat]
                if excess > 0:
                    total += excess
                x += dx
            # Vertical leg
            dy = 1 if y2 >= y1 else -1
            y = y1
            while True:
                flat = y * self.cols + x2
                if self._capacity[flat] <= 0:
                    return None
                excess = self._demand[flat] - self._capacity[flat]
                if excess > 0:
                    total += excess
                if y == y2:
                    break
                y += dy
        else:
            # Vertical leg
            dy = 1 if y2 >= y1 else -1
            y = y1
            while y != y2:
                flat = y * self.cols + x1
                if self._capacity[flat] <= 0:
                    return None
                excess = self._demand[flat] - self._capacity[flat]
                if excess > 0:
                    total += excess
                y += dy
            # Horizontal leg
            dx = 1 if x2 >= x1 else -1
            x = x1
            while True:
                flat = y2 * self.cols + x
                if self._capacity[flat] <= 0:
                    return None
                excess = self._demand[flat] - self._capacity[flat]
                if excess > 0:
                    total += excess
                if x == x2:
                    break
                x += dx
        return total
