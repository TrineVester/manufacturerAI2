"""A* pathfinder for Manhattan routing on the routing grid.

Supports:
  - Point-to-point routing (findPath)
  - Point-to-tree routing for multi-pin nets (findPathToTree)
  - Turn penalty to prefer straight runs
  - Optional cell cost function for edge-hugging power traces
  - Crossing-aware mode for rip-up (heavy penalty for blocked cells)
"""

from __future__ import annotations

import heapq

from .grid import RoutingGrid, FREE, TRACE_PATH, PERMANENTLY_BLOCKED
from .models import TURN_PENALTY, CROSSING_PENALTY

# Per-cell extra cost treated as "strong avoidance" for component bodies.
# A detour of N cells beats going through N/12 body cells — enough to
# redirect short-to-medium routes around components without causing
# failures when no external path exists (e.g. resistor far from its pin).
BODY_EXTRA = 12


# Manhattan directions: (dx, dy)
DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def find_path(
    grid: RoutingGrid,
    source: tuple[int, int],
    sink: tuple[int, int],
    *,
    turn_penalty: int = TURN_PENALTY,
) -> list[tuple[int, int]] | None:
    """A* point-to-point Manhattan routing.

    Returns a list of (gx, gy) grid cells from source to sink,
    or None if no path exists.
    """
    sx, sy = source
    tx, ty = sink

    # Cache grid internals as locals — avoids repeated attribute
    # lookups and method-call overhead in the inner loop.
    W = grid.width
    H = grid.height
    cells = grid._cells

    if not (0 <= sx < W and 0 <= sy < H and 0 <= tx < W and 0 <= ty < H):
        return None
    # Reject if source or sink is occupied by another net's trace
    if cells[sy * W + sx] == TRACE_PATH:
        return None
    if cells[ty * W + tx] == TRACE_PATH:
        return None
    if source == sink:
        return [source]

    # Try L-shaped routes first (fast path)
    l_path = _try_l_route(grid, source, sink)
    if l_path is not None:
        return l_path

    # Full A*
    start_key = sy * W + sx
    sink_key = ty * W + tx
    h0 = abs(sx - tx) + abs(sy - ty)
    counter = 0
    heap: list[tuple[int, int, int, int, int, int]] = [(h0, counter, sx, sy, -1, -1)]
    g_scores: dict[int, int] = {start_key: 0}
    parents: dict[int, tuple[int, int]] = {}  # key -> (parent_key, direction)
    closed: set[int] = set()

    while heap:
        f, _cnt, cx, cy, direction, parent_key = heapq.heappop(heap)
        key = cy * W + cx

        if key in closed:
            continue
        closed.add(key)
        if key != start_key:
            parents[key] = (parent_key, direction)

        if key == sink_key:
            # Reconstruct path
            path = [(cx, cy)]
            k = key
            while k in parents:
                pk, _ = parents[k]
                if pk < 0:
                    break
                path.append((pk % W, pk // W))
                k = pk
            path.reverse()
            return path

        cur_g = g_scores[key]

        for d, (dx, dy) in enumerate(DIRS):
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < W and 0 <= ny < H):
                continue
            nkey = ny * W + nx
            if nkey in closed:
                continue
            # Allow stepping onto the source or sink even if blocked,
            # but NEVER if occupied by another net's trace (TRACE_PATH).
            nval = cells[nkey]
            if nval != FREE:
                if nval == TRACE_PATH:
                    continue
                if (nx, ny) != sink and (nx, ny) != source:
                    continue

            is_turn = direction != -1 and direction != d
            cost = 1 + (turn_penalty if is_turn else 0) + grid._cost_map.get(nkey, 0)
            tentative_g = cur_g + cost

            if nkey not in g_scores or tentative_g < g_scores[nkey]:
                g_scores[nkey] = tentative_g
                h = abs(nx - tx) + abs(ny - ty)
                counter += 1
                heapq.heappush(heap, (tentative_g + h, counter, nx, ny, d, key))

    return None


def find_path_to_tree(
    grid: RoutingGrid,
    source: tuple[int, int] | set[tuple[int, int]],
    tree: set[tuple[int, int]],
    *,
    turn_penalty: int = TURN_PENALTY,
    allow_crossings: bool = False,
) -> list[tuple[int, int]] | None:
    """A* from source point(s) to any cell in an existing routing tree.

    Used for multi-pin nets: connect each pad to the growing tree.

    *source* may be a single ``(gx, gy)`` tuple **or** a set of
    candidate source cells (multi-source A*).  Multi-source routing
    simultaneously searches from every source cell and returns the
    shortest path from any source to any target tree cell.  This
    prevents parallel duplicate traces when connecting two sub-trees.

    If allow_crossings=True, blocked (non-permanent) cells can be
    traversed with a heavy penalty.  This is used during rip-up to
    find minimum-crossing paths.

    Returns the path (grid cells) or None.
    """

    # Cache grid internals as locals
    W = grid.width
    H = grid.height
    cells = grid._cells

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

    # Precompute tree coordinates for fast heuristic
    tree_list = list(tree)
    tree_xs = tuple(t[0] for t in tree_list)
    tree_ys = tuple(t[1] for t in tree_list)
    n_tree = len(tree_list)

    def min_h(x: int, y: int) -> int:
        best = abs(x - tree_xs[0]) + abs(y - tree_ys[0])
        for i in range(1, n_tree):
            d = abs(x - tree_xs[i]) + abs(y - tree_ys[i])
            if d < best:
                best = d
                if d == 0:
                    return 0
        return best

    tree_keys = frozenset(t[1] * W + t[0] for t in tree_list)

    # ── Seed heap with all valid source cells ──────────────────
    counter = 0
    heap: list[tuple[int, int, int, int, int, int]] = []
    g_scores: dict[int, int] = {}
    source_keys: set[int] = set()

    for sx, sy in sources:
        if not (0 <= sx < W and 0 <= sy < H):
            continue
        skey = sy * W + sx
        sval = cells[skey]
        # Skip cells occupied by another net's trace or permanently blocked
        if sval == TRACE_PATH or sval == PERMANENTLY_BLOCKED:
            continue
        source_keys.add(skey)
        h0 = min_h(sx, sy)
        heapq.heappush(heap, (h0, counter, sx, sy, -1, -1))
        g_scores[skey] = 0
        counter += 1

    if not heap:
        return None

    parents: dict[int, tuple[int, int]] = {}
    closed: set[int] = set()

    while heap:
        f, _cnt, cx, cy, direction, parent_key = heapq.heappop(heap)
        key = cy * W + cx

        if key in closed:
            continue
        closed.add(key)
        if key not in source_keys:
            parents[key] = (parent_key, direction)

        if key in tree_keys:
            # Reconstruct path
            path = [(cx, cy)]
            k = key
            while k in parents:
                pk, _ = parents[k]
                if pk < 0:
                    break
                path.append((pk % W, pk // W))
                k = pk
            path.reverse()
            return path

        cur_g = g_scores[key]

        for d, (dx, dy) in enumerate(DIRS):
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < W and 0 <= ny < H):
                continue
            nkey = ny * W + nx
            if nkey in closed:
                continue

            is_tree_cell = nkey in tree_keys
            nval = cells[nkey]
            cell_free = nval == FREE

            # Never cross an existing trace, even in crossing-aware mode
            if not cell_free and not is_tree_cell:
                if nval == TRACE_PATH:
                    continue
                if not allow_crossings or nval == PERMANENTLY_BLOCKED:
                    continue

            is_turn = direction != -1 and direction != d
            cost = 1 + (turn_penalty if is_turn else 0) + grid._cost_map.get(nkey, 0)
            if not cell_free and not is_tree_cell:
                cost += CROSSING_PENALTY
            tentative_g = cur_g + cost

            if nkey not in g_scores or tentative_g < g_scores[nkey]:
                g_scores[nkey] = tentative_g
                h = min_h(nx, ny)
                counter += 1
                heapq.heappush(heap, (tentative_g + h, counter, nx, ny, d, key))

    return None


# ── Fast L-shaped route ────────────────────────────────────────────

def _try_l_route(
    grid: RoutingGrid,
    source: tuple[int, int],
    sink: tuple[int, int],
) -> list[tuple[int, int]] | None:
    """Try a simple L-shaped (one-bend) route.  Returns path or None."""
    # Try horizontal-first then vertical-first
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

    # Cache grid internals as locals
    cells = grid._cells
    cost_map = grid._cost_map
    W = grid.width
    H = grid.height

    path: list[tuple[int, int]] = [(sx, sy)]

    if horizontal_first:
        # Horizontal leg
        dx = 1 if tx > sx else -1
        x, y = sx, sy
        while x != tx:
            x += dx
            if not (0 <= x < W and 0 <= y < H):
                return None
            val = cells[y * W + x]
            if val == TRACE_PATH:
                return None
            if val != FREE and (x, y) != sink:
                return None
            if cost_map.get(y * W + x, 0):   # avoid soft-cost zones — let A* decide
                return None
            path.append((x, y))
        # Vertical leg
        dy = 1 if ty > sy else -1
        while y != ty:
            y += dy
            if not (0 <= x < W and 0 <= y < H):
                return None
            val = cells[y * W + x]
            if val == TRACE_PATH:
                return None
            if val != FREE and (x, y) != sink:
                return None
            if cost_map.get(y * W + x, 0):
                return None
            path.append((x, y))
    else:
        # Vertical leg
        dy = 1 if ty > sy else -1
        x, y = sx, sy
        while y != ty:
            y += dy
            if not (0 <= x < W and 0 <= y < H):
                return None
            val = cells[y * W + x]
            if val == TRACE_PATH:
                return None
            if val != FREE and (x, y) != sink:
                return None
            if cost_map.get(y * W + x, 0):
                return None
            path.append((x, y))
        # Horizontal leg
        dx = 1 if tx > sx else -1
        while x != tx:
            x += dx
            if not (0 <= x < W and 0 <= y < H):
                return None
            val = cells[y * W + x]
            if val == TRACE_PATH:
                return None
            if val != FREE and (x, y) != sink:
                return None
            if cost_map.get(y * W + x, 0):
                return None
            path.append((x, y))

    return path
