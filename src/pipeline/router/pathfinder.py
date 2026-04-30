"""A* pathfinder for Manhattan routing on the routing grid.

Supports:
  - Point-to-point routing (find_path)
  - Point-to-tree routing for multi-pin nets (find_path_to_tree)
  - Turn penalty to prefer straight runs
"""

from __future__ import annotations

import array as _array_mod
from functools import lru_cache
from heapq import heappush as _heappush, heappop as _heappop

import numpy as np
from scipy.ndimage import distance_transform_cdt

from .grid import RoutingGrid, FREE, BLOCKED, TRACE_PATH, PERMANENTLY_BLOCKED
from .models import TURN_PENALTY


# Manhattan and diagonal directions: (dx, dy)
DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))

_SQRT2 = 1.4142135623730951
_SQRT2_M1 = _SQRT2 - 1.0  # ≈ 0.4142

# Horizontal corner offset for each direction (0 for orthogonal dirs)
_DIAG_CORNER_H = (0, 0, 0, 0, 1, 1, -1, -1)
# Vertical corner offset multiplier for each direction (multiply by W)
_DIAG_CORNER_V = (0, 0, 0, 0, 1, -1, 1, -1)


def _build_angle_table():
    n = len(DIRS)
    tbl = [0] * (n * n)
    for i, (dx1, dy1) in enumerate(DIRS):
        for j, (dx2, dy2) in enumerate(DIRS):
            if i == j:
                continue
            dot = dx1 * dx2 + dy1 * dy2
            msq = (dx1 * dx1 + dy1 * dy1) * (dx2 * dx2 + dy2 * dy2)
            if msq == 1:
                tbl[i * n + j] = 2 if dot == 0 else (4 if dot < 0 else 0)
            elif msq == 4:
                tbl[i * n + j] = 2 if dot == 0 else (4 if dot < 0 else 0)
            else:
                tbl[i * n + j] = 1 if dot > 0 else 3
    return tuple(tbl)


_ANGLE_TABLE = _build_angle_table()
_TURN_FRAC = (0.0, 0.25, 1.0, 2.0, 3.0)


def _turn_cost(direction: int, d: int, turn_penalty: float) -> float:
    if direction == -1 or direction == d:
        return 0.0
    return turn_penalty * _TURN_FRAC[_ANGLE_TABLE[direction * 8 + d]]


def _octile_h(dx: int, dy: int) -> float:
    adx = abs(dx)
    ady = abs(dy)
    return max(adx, ady) + _SQRT2_M1 * min(adx, ady)


@lru_cache(maxsize=4)
def _build_cost_table(turn_penalty: float) -> tuple:
    """Precompute move_cost + turn_cost for all (prev_direction, d) pairs.

    Index: (direction + 1) * 8 + d
    direction -1 (no previous) maps to row 0; directions 0-7 map to rows 1-8.
    """
    tbl = [0.0] * (9 * 8)
    for direction in range(-1, 8):
        row = (direction + 1) * 8
        for d in range(8):
            move = 1.0 if d < 4 else _SQRT2
            tc = _turn_cost(direction, d, turn_penalty)
            tbl[row + d] = move + tc
    return tuple(tbl)


@lru_cache(maxsize=4)
def _build_neighbors(W: int, H: int) -> tuple:
    """Precompute valid (neighbor_key, direction) pairs for each cell."""
    N = W * H
    neighbors = [None] * N
    for y in range(H):
        for x in range(W):
            nbs = []
            for d, (dx, dy) in enumerate(DIRS):
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H:
                    nbs.append((ny * W + nx, d))
            neighbors[y * W + x] = tuple(nbs)
    return tuple(neighbors)


def find_path(
    grid: RoutingGrid,
    source: tuple[int, int],
    sink: tuple[int, int],
    *,
    turn_penalty: int = TURN_PENALTY,
    crossing_cost: int = 0,
    cost_map: dict[int, float] | None = None,
) -> list[tuple[int, int]] | None:
    """A* point-to-point Manhattan routing.

    Returns a list of (gx, gy) grid cells from source to sink,
    or None if no path exists.

    When crossing_cost > 0 the pathfinder is allowed to walk through
    TRACE_PATH and BLOCKED cells at the given extra cost per cell.
    """
    sx, sy = source
    tx, ty = sink

    W = grid.width
    H = grid.height
    cells = grid._cells

    if not (0 <= sx < W and 0 <= sy < H and 0 <= tx < W and 0 <= ty < H):
        return None
    protected = grid._protected
    if cells[sy * W + sx] == TRACE_PATH and (sx, sy) not in protected:
        return None
    if cells[ty * W + tx] == TRACE_PATH and (tx, ty) not in protected:
        return None
    if source == sink:
        return [source]

    N = W * H
    INF = float('inf')
    start_key = sy * W + sx
    sink_key = ty * W + tx

    neighbors = _build_neighbors(W, H)
    cost_tbl = _build_cost_table(turn_penalty)
    dcv = (0, 0, 0, 0, W, -W, W, -W)

    g = [INF] * N
    g[start_key] = 0
    parent = [-1] * N
    closed = bytearray(N)

    counter = 0
    heap: list[tuple[float, int, int, int]] = [(_octile_h(sx - tx, sy - ty), counter, start_key, -1)]

    while heap:
        f, _cnt, key, direction = _heappop(heap)

        if closed[key]:
            continue
        closed[key] = 1

        if key == sink_key:
            path = [(key % W, key // W)]
            k = key
            while True:
                pk = parent[k]
                if pk < 0:
                    break
                path.append((pk % W, pk // W))
                k = pk
            path.reverse()
            return path

        cur_g = g[key]
        dir_row = (direction + 1) * 8

        for nkey, d in neighbors[key]:
            if closed[nkey]:
                continue

            if d >= 4:
                c1 = key + _DIAG_CORNER_H[d]
                c2 = key + dcv[d]
                if cells[c1] == TRACE_PATH and cells[c2] == TRACE_PATH:
                    continue

            nval = cells[nkey]
            cross_extra = 0
            if nval != FREE:
                if nval == PERMANENTLY_BLOCKED:
                    continue
                if crossing_cost > 0 and (nval == TRACE_PATH or nval == BLOCKED):
                    cross_extra = crossing_cost
                else:
                    if nval == TRACE_PATH:
                        continue
                    if nkey != sink_key and nkey != start_key:
                        continue

            cost = cost_tbl[dir_row + d] + cross_extra
            if cost_map is not None:
                cost += cost_map.get(nkey, 0)
            tentative_g = cur_g + cost

            if tentative_g < g[nkey]:
                g[nkey] = tentative_g
                parent[nkey] = key
                counter += 1
                nx = nkey % W
                ny = nkey // W
                _heappush(heap, (tentative_g + _octile_h(nx - tx, ny - ty), counter, nkey, d))

    return None


def find_path_to_tree(
    grid: RoutingGrid,
    source: tuple[int, int] | set[tuple[int, int]],
    tree: set[tuple[int, int]],
    *,
    turn_penalty: int = TURN_PENALTY,
    crossing_cost: int = 0,
    cost_map: dict[int, float] | None = None,
) -> list[tuple[int, int]] | None:
    """A* from source point(s) to any cell in an existing routing tree.

    *source* may be a single ``(gx, gy)`` tuple **or** a set of
    candidate source cells (multi-source A*).

    Returns the path (grid cells) or None.
    """

    # Cache grid internals as locals
    W = grid.width
    H = grid.height
    N = W * H
    cells = grid._cells
    INF = float('inf')

    # ── Normalise source to a set ──────────────────────────────
    if isinstance(source, set):
        sources = source
    else:
        sources = {source}

    # Quick overlap check
    overlap = sources & tree
    if overlap:
        cell = next(iter(overlap))
        return [cell]

    # Build a flat bytearray mask for O(1) tree membership
    tree_mask = bytearray(N)
    tree_list = list(tree)
    for tx, ty in tree_list:
        tree_mask[ty * W + tx] = 1

    neighbors = _build_neighbors(W, H)
    h_map = _octile_dt(W, H, tree_list)
    cost_tbl = _build_cost_table(turn_penalty)
    dcv = (0, 0, 0, 0, W, -W, W, -W)

    # ── Pre-allocated containers ───────────────────────────────
    g = [INF] * N
    parent = [-1] * N
    closed = bytearray(N)

    # ── Seed heap with all valid source cells ──────────────────
    counter = 0
    heap: list[tuple[float, int, int, int]] = []

    protected = grid._protected
    for sx, sy in sources:
        if not (0 <= sx < W and 0 <= sy < H):
            continue
        skey = sy * W + sx
        if cells[skey] != FREE and not tree_mask[skey]:
            if (sx, sy) not in protected:
                continue
        g[skey] = 0
        _heappush(heap, (h_map[skey], counter, skey, -1))
        counter += 1

    if not heap:
        return None

    while heap:
        f, _cnt, key, direction = _heappop(heap)

        if closed[key]:
            continue
        closed[key] = 1

        if tree_mask[key]:
            path = [(key % W, key // W)]
            k = key
            while True:
                pk = parent[k]
                if pk < 0:
                    break
                path.append((pk % W, pk // W))
                k = pk
            path.reverse()
            return path

        cur_g = g[key]
        dir_row = (direction + 1) * 8

        for nkey, d in neighbors[key]:
            if closed[nkey]:
                continue

            if d >= 4:
                c1 = key + _DIAG_CORNER_H[d]
                c2 = key + dcv[d]
                if (cells[c1] == TRACE_PATH and not tree_mask[c1]
                        and cells[c2] == TRACE_PATH and not tree_mask[c2]):
                    continue

            nval = cells[nkey]
            cross_extra = 0
            if nval != FREE and not tree_mask[nkey]:
                if nval == PERMANENTLY_BLOCKED:
                    continue
                if crossing_cost > 0 and (nval == TRACE_PATH or nval == BLOCKED):
                    cross_extra = crossing_cost
                else:
                    continue

            cost = cost_tbl[dir_row + d] + cross_extra
            if cost_map is not None:
                cost += cost_map.get(nkey, 0)
            tentative_g = cur_g + cost

            if tentative_g < g[nkey]:
                g[nkey] = tentative_g
                parent[nkey] = key
                counter += 1
                _heappush(heap, (tentative_g + h_map[nkey], counter, nkey, d))

    return None


# ── Octile distance transform ─────────────────────────────────────

def _octile_dt(W: int, H: int, tree_cells: list[tuple[int, int]]) -> _array_mod.array:
    """Return flat array of octile distances to nearest tree cell.

    Combines Manhattan and Chebyshev distance transforms to produce an
    admissible octile heuristic for 8-directional movement.
    """
    mask = np.ones((H, W), dtype=bool)
    n = len(tree_cells)
    if n > 32:
        tc = np.array(tree_cells, dtype=np.intp)
        mask[tc[:, 1], tc[:, 0]] = False
    else:
        for tx, ty in tree_cells:
            mask[ty, tx] = False
    manhattan = distance_transform_cdt(mask, metric='taxicab').astype(np.float32)
    chebyshev = distance_transform_cdt(mask, metric='chessboard').astype(np.float32)
    octile = chebyshev + _SQRT2_M1 * (manhattan - chebyshev)
    return _array_mod.array('f', octile.tobytes())


# ── Fast L-shaped route ────────────────────────────────────────────

def _try_l_route(
    grid: RoutingGrid,
    source: tuple[int, int],
    sink: tuple[int, int],
) -> list[tuple[int, int]] | None:
    """Try a simple L-shaped (one-bend) route.  Returns path or None."""
    for h_first in (True, False):
        path = _l_route(grid, source, sink, h_first)
        if path is not None:
            return path
    return None


def _l_route(
    grid: RoutingGrid,
    source: tuple[int, int],
    sink: tuple[int, int],
    horizontal_first: bool,
) -> list[tuple[int, int]] | None:
    sx, sy = source
    tx, ty = sink

    cells = grid._cells
    W = grid.width
    H = grid.height

    def _ok(x: int, y: int) -> bool:
        if not (0 <= x < W and 0 <= y < H):
            return False
        val = cells[y * W + x]
        if val == FREE:
            return True
        if (x, y) == sink:
            return True
        return False

    path: list[tuple[int, int]] = [(sx, sy)]

    if horizontal_first:
        dx = 1 if tx > sx else -1
        x, y = sx, sy
        while x != tx:
            x += dx
            if not _ok(x, y):
                return None
            path.append((x, y))
        dy = 1 if ty > sy else -1
        while y != ty:
            y += dy
            if not _ok(x, y):
                return None
            path.append((x, y))
    else:
        dy = 1 if ty > sy else -1
        x, y = sx, sy
        while y != ty:
            y += dy
            if not _ok(x, y):
                return None
            path.append((x, y))
        dx = 1 if tx > sx else -1
        while x != tx:
            x += dx
            if not _ok(x, y):
                return None
            path.append((x, y))

    return path
