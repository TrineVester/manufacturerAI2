"""Negotiated congestion routing loop (PathFinder algorithm).

TODO: This module is currently unused. It can be integrated later.

Routes all nets simultaneously, iteratively escalating costs on
congested cells until every cell has at most one net.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .grid import RoutingGrid
from .models import RouterConfig
from .pathfinder import find_path, find_path_to_tree

log = logging.getLogger(__name__)


@dataclass
class NetState:
    """Mutable routing state for a single net."""

    net_id: str
    pins: list[tuple[int, int]]
    path: list[list[tuple[int, int]]] = field(default_factory=list)
    path_cells: set[int] = field(default_factory=set)

    def path_cells_as_coords(self, width: int) -> list[tuple[int, int]]:
        return [(flat % width, flat // width) for flat in self.path_cells]


def negotiate(
    grid: RoutingGrid,
    net_states: dict[str, NetState],
    cfg: RouterConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Run the negotiated congestion loop.

    Returns the final (occupancy, history) arrays.
    """
    occupancy = np.zeros((grid.height, grid.width), dtype=np.int16)
    history = np.zeros((grid.height, grid.width), dtype=np.float32)
    penalty_factor = 1.0
    clearance_cells = max(1, int(round(cfg.trace_clearance_mm / cfg.grid_resolution_mm)))

    for ns in net_states.values():
        _route_net(grid, ns, occupancy, history, 0.0,
                   cfg.turn_penalty, cfg.proximity_penalty, clearance_cells)
        _add_to_occupancy(occupancy, ns, grid)

    for iteration in range(cfg.max_negotiate_iterations):
        order = sorted(
            net_states.values(),
            key=lambda n: _count_conflicts(n, occupancy, grid),
            reverse=True,
        )

        rerouted = 0
        for ns in order:
            conflicts = _count_conflicts(ns, occupancy, grid)
            if conflicts == 0:
                continue
            _remove_from_occupancy(occupancy, ns, grid)
            _route_net(grid, ns, occupancy, history, penalty_factor,
                       cfg.turn_penalty, cfg.proximity_penalty, clearance_cells)
            _add_to_occupancy(occupancy, ns, grid)
            rerouted += 1

        conflict_mask = occupancy > 1
        total_conflicts = int(np.sum(conflict_mask))
        if total_conflicts == 0:
            log.info("Negotiation converged in %d iterations", iteration + 1)
            break

        history[conflict_mask] += 1.0
        penalty_factor *= cfg.penalty_growth_factor

    else:
        remaining = int(np.sum(occupancy > 1))
        if remaining > 0:
            log.warning(
                "Negotiation did not fully converge: %d conflict cells remain",
                remaining,
            )

    return occupancy, history


def build_net_states(
    nets,
    grid: RoutingGrid,
    comp_map: dict,
) -> dict[str, NetState]:
    """Build NetState entries from resolved nets and placed components."""
    states: dict[str, NetState] = {}
    for net in nets:
        pins: list[tuple[int, int]] = []
        for ref in net.pins:
            if ":" not in ref:
                continue
            iid, pid = ref.split(":", 1)
            pc = comp_map.get(iid)
            if pc is None:
                continue
            pos = pc.pin_positions.get(pid)
            if pos is None:
                continue
            gc, gr = grid.world_to_grid(pos[0], pos[1])
            if grid.in_bounds((gc, gr)):
                pins.append((gc, gr))
        if len(pins) >= 2:
            states[net.id] = NetState(net_id=net.id, pins=pins)
    return states


# ── Internal helpers ───────────────────────────────────────────────


def _route_net(
    grid: RoutingGrid,
    ns: NetState,
    occupancy: np.ndarray,
    history: np.ndarray,
    penalty_factor: float,
    turn_penalty: int,
    proximity_penalty: float,
    clearance_cells: int,
) -> None:
    ns.path.clear()
    ns.path_cells.clear()

    if len(ns.pins) < 2:
        return

    if len(ns.pins) == 2:
        path = find_path(
            grid, ns.pins[0], ns.pins[1],
            occupancy, history, ns.net_id, penalty_factor,
            turn_penalty, proximity_penalty, clearance_cells,
        )
        if path:
            ns.path = [path]
            ns.path_cells = {grid.flat(c, r) for c, r in path}
        return

    mst = _kruskal_mst(ns.pins)
    connected: set[int] = set()
    tree_cells: set[tuple[int, int]] = set()

    for pa_idx, pb_idx in mst:
        pa = ns.pins[pa_idx]
        pb = ns.pins[pb_idx]

        if not connected:
            source = pa
        elif pa_idx in connected and pb_idx not in connected:
            source = pa
        elif pb_idx in connected and pa_idx not in connected:
            source = pb
            pb = ns.pins[pa_idx]
            pa_idx, pb_idx = pb_idx, pa_idx
        else:
            source = pa

        if tree_cells and pa_idx in connected:
            path = find_path_to_tree(
                grid, pb, tree_cells,
                occupancy, history, ns.net_id, penalty_factor,
                turn_penalty, proximity_penalty, clearance_cells,
            )
        else:
            path = find_path(
                grid, source, pb,
                occupancy, history, ns.net_id, penalty_factor,
                turn_penalty, proximity_penalty, clearance_cells,
            )

        if path:
            ns.path.append(path)
            tree_cells.update(path)
        connected.add(pa_idx)
        connected.add(pb_idx)

    ns.path_cells = {grid.flat(c, r) for c, r in tree_cells}


def _kruskal_mst(pins: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Kruskal's MST on Manhattan distances. Returns (idx_a, idx_b) pairs."""
    n = len(pins)
    edges: list[tuple[int, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            d = abs(pins[i][0] - pins[j][0]) + abs(pins[i][1] - pins[j][1])
            edges.append((d, i, j))
    edges.sort()

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    result: list[tuple[int, int]] = []
    for _, i, j in edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
            result.append((i, j))
            if len(result) == n - 1:
                break
    return result


def _add_to_occupancy(
    occupancy: np.ndarray, ns: NetState, grid: RoutingGrid,
) -> None:
    for flat in ns.path_cells:
        r, c = divmod(flat, grid.width)
        c, r = flat % grid.width, flat // grid.width
        occupancy[r, c] += 1


def _remove_from_occupancy(
    occupancy: np.ndarray, ns: NetState, grid: RoutingGrid,
) -> None:
    for flat in ns.path_cells:
        c, r = flat % grid.width, flat // grid.width
        occupancy[r, c] = max(0, occupancy[r, c] - 1)


def _count_conflicts(
    ns: NetState, occupancy: np.ndarray, grid: RoutingGrid,
) -> int:
    count = 0
    for flat in ns.path_cells:
        c, r = flat % grid.width, flat // grid.width
        if occupancy[r, c] > 1:
            count += 1
    return count
