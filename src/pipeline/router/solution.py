"""Mutable routing solution — owns grid state and all per-net routes.

The engine creates one Solution, seeds it with an initial routing pass,
then iteratively improves it by ripping up problematic nets and
re-routing them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
from shapely import contains_xy as _contains_xy
from shapely.geometry import Point

from src.catalog.models import CatalogResult
from src.pipeline.placer.models import FullPlacement
from src.pipeline.pin_geometry import pin_shaft_dimensions

from .grid import RoutingGrid, FREE, BLOCKED, TRACE_PATH, PERMANENTLY_BLOCKED
from .models import Trace, RoutingResult, RouterConfig
from .pathfinder import find_path, find_path_to_tree
from .pins import pin_world_xy
from src.pipeline.trace_geometry import point_seg_dist

log = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────

@dataclass
class NetPad:
    instance_id: str
    pin_id: str
    group_id: str | None
    gx: int
    gy: int
    world_x: float
    world_y: float


@dataclass
class PadBlock:
    cx: int
    cy: int
    half_w_mm: float
    half_h_mm: float


@dataclass
class _PinRef:
    raw: str
    instance_id: str
    pin_or_group: str
    is_group: bool


@dataclass
class NetRoute:
    paths: list[list[tuple[int, int]]]
    pads: list[NetPad]
    pad_blocks: list[PadBlock] = field(default_factory=list)

    @property
    def trace_cells(self) -> int:
        return sum(len(p) for p in self.paths)


@dataclass
class Snapshot:
    routes: dict[str, NetRoute]
    cells: bytearray
    trace_owner: dict[int, str]
    clearance_owner: dict[int, set[str]]


# ── Solution ───────────────────────────────────────────────────────

class Solution:
    """A mutable routing state with snapshot/restore."""

    def __init__(
        self,
        grid: RoutingGrid,
        config: RouterConfig,
        placement: FullPlacement,
        catalog: CatalogResult,
        net_pad_map: dict[str, list[_PinRef]],
        pin_voronoi: dict[int, str] | None,
        all_pin_cells: dict[str, set[tuple[int, int]]],
    ) -> None:
        self.grid = grid
        self.config = config
        self.placement = placement
        self.catalog = catalog
        self.net_pad_map = net_pad_map
        self.pin_voronoi = pin_voronoi
        self.all_pin_cells = all_pin_cells

        self.routes: dict[str, NetRoute] = {}
        self.expected_nets: set[str] = set()
        self.pin_assignments: dict[str, str] = {}
        self._pad_radius = max(1, math.ceil(
            (config.trace_width_mm / 2 + config.trace_clearance_mm)
            / config.grid_resolution_mm
        ))

        self._voronoi_by_pin: dict[str, list[tuple[int, int]]] = {}
        self._voronoi_flat_by_pin: dict[str, np.ndarray] = {}
        if pin_voronoi is not None:
            W = grid.width
            groups: dict[str, list[int]] = {}
            for flat, pin_key in pin_voronoi.items():
                groups.setdefault(pin_key, []).append(flat)
            for pin_key, flats in groups.items():
                self._voronoi_flat_by_pin[pin_key] = np.array(flats, dtype=np.intp)
                self._voronoi_by_pin[pin_key] = [
                    (f % W, f // W) for f in flats
                ]

    # ── Scoring ────────────────────────────────────────────────

    def score(self) -> tuple[int, int]:
        """(missing_nets, total_trace_cells). Lower is better."""
        missing = len(self.expected_nets - set(self.routes)) if self.expected_nets else 0
        total_cells = sum(r.trace_cells for r in self.routes.values())
        return (missing, total_cells)

    def is_perfect(self) -> bool:
        return self.score()[0] == 0

    def trace_lengths_mm(self) -> dict[str, float]:
        """Per-net trace length in mm, computed from grid paths."""
        res = self.grid.resolution
        lengths: dict[str, float] = {}
        for net_id, route in self.routes.items():
            cells = sum(max(0, len(p) - 1) for p in route.paths)
            lengths[net_id] = round(cells * res, 2)
        return lengths

    # ── Snapshot / Restore ─────────────────────────────────────

    def snapshot(self) -> Snapshot:
        routes_copy: dict[str, NetRoute] = {}
        for nid, route in self.routes.items():
            routes_copy[nid] = NetRoute(
                paths=[list(p) for p in route.paths],
                pads=list(route.pads),
                pad_blocks=list(route.pad_blocks),
            )
        return Snapshot(
            routes=routes_copy,
            cells=bytearray(self.grid._cells),
            trace_owner=dict(self.grid._trace_owner),
            clearance_owner={k: set(v) for k, v in self.grid._clearance_owner.items()},
        )

    def restore(self, snap: Snapshot) -> None:
        self.routes = {
            nid: NetRoute(
                paths=[list(p) for p in route.paths],
                pads=list(route.pads),
                pad_blocks=list(route.pad_blocks),
            )
            for nid, route in snap.routes.items()
        }
        self.grid._cells[:] = snap.cells
        self.grid._trace_owner = dict(snap.trace_owner)
        self.grid._clearance_owner = {k: set(v) for k, v in snap.clearance_owner.items()}

    # ── Rip-up ─────────────────────────────────────────────────

    def rip_up(self, net_ids: list[str]) -> None:
        for nid in net_ids:
            route = self.routes.pop(nid, None)
            if route is None:
                continue
            for path in route.paths:
                self.grid.free_trace(path, net_id=nid)
            for pb in route.pad_blocks:
                self.grid.free_pad(pb.cx, pb.cy, pb.half_w_mm, pb.half_h_mm, net_id=nid)

    # ── Route nets ─────────────────────────────────────────────

    def route_net(
        self, net_id: str, pads: list[NetPad],
        cost_map: dict[int, float] | None = None,
    ) -> None:
        """Route one net. Tries clean route, then crossing rip-up."""
        if net_id in self.routes:
            self.rip_up([net_id])

        # 1. Try clean route
        paths, ok = self._find_paths(net_id, pads, cost_map=cost_map)
        if ok and paths and not self._has_foreign_cells(paths, net_id):
            pad_conflicts = self._find_pad_conflicts(pads, net_id)
            if not pad_conflicts:
                self._commit(net_id, paths, pads)
                return
            if self._try_rip_reroute(net_id, paths, pads, pad_conflicts):
                return

        # 2. Try crossing-cost route → surgical rip-up of crossed nets
        paths_cross, ok = self._find_paths(
            net_id, pads, crossing_cost=self.config.crossing_cost,
            cost_map=cost_map,
        )
        if ok and paths_cross:
            crossed = self._find_crossed_nets(paths_cross, net_id)
            pad_conflicts = self._find_pad_conflicts(pads, net_id)
            all_conflicts = crossed | pad_conflicts
            if not all_conflicts:
                self._commit(net_id, paths_cross, pads)
                return
            if self._try_rip_reroute(net_id, paths_cross, pads, all_conflicts):
                return

        log.info("  %-20s FAIL — no route", net_id)

    def route_nets(self, ordering: list[str], pads_map: dict[str, list[NetPad]]) -> None:
        for nid in ordering:
            pads = pads_map.get(nid)
            if pads is None or len(pads) < 2:
                continue
            self.route_net(nid, pads)

    # ── Identify worst nets and neighborhoods ──────────────────

    def worst_nets(self, k: int = 3) -> list[str]:
        """Return net IDs of the k longest-trace nets (candidates for rip-up)."""
        by_length = sorted(
            ((nid, r.trace_cells) for nid, r in self.routes.items()),
            key=lambda x: -x[1],
        )
        return [nid for nid, _ in by_length[:k]]

    def random_nets(self, k: int = 3) -> list[str]:
        """Return k random routed net IDs (for diversified refinement)."""
        import random as _rnd
        ids = list(self.routes.keys())
        if len(ids) <= k:
            return ids
        return _rnd.sample(ids, k)

    def find_blockers(
        self, missing: list[str], pads_map: dict[str, list[NetPad]],
    ) -> set[str]:
        """Identify routed nets that would be crossed when routing *missing* nets."""
        blockers: set[str] = set()
        for nid in missing:
            pads = pads_map.get(nid)
            if not pads or len(pads) < 2:
                continue
            paths, ok = self._find_paths(
                nid, pads, crossing_cost=self.config.crossing_cost,
            )
            if ok and paths:
                blockers.update(self._find_crossed_nets(paths, nid))
        return blockers

    def refine_single_net(
        self, net_id: str, pads: list[NetPad],
    ) -> bool:
        """Rip up one net and re-route it; keep only if trace is shorter."""
        route = self.routes.get(net_id)
        if route is None:
            return False
        old_cells = route.trace_cells
        snap = self.snapshot()

        self.rip_up([net_id])
        self.route_net(net_id, pads)

        new_route = self.routes.get(net_id)
        if new_route is None or new_route.trace_cells >= old_cells:
            self.restore(snap)
            return False
        return True

    def neighborhood(self, seeds: list[str]) -> list[str]:
        """Given seed net IDs, find all nets that share grid cells with
        them (adjacent or overlapping clearance zones)."""
        seed_cells: set[int] = set()
        W = self.grid.width
        for nid in seeds:
            route = self.routes.get(nid)
            if route is None:
                continue
            for path in route.paths:
                for gx, gy in path:
                    seed_cells.add(gy * W + gx)

        neighbors: set[str] = set(seeds)
        for flat in seed_cells:
            owner = self.grid._trace_owner.get(flat)
            if owner:
                neighbors.add(owner)
            cl_owners = self.grid._clearance_owner.get(flat)
            if cl_owners:
                neighbors.update(cl_owners)

        for nid in list(neighbors):
            route = self.routes.get(nid)
            if route is None:
                continue
            for path in route.paths:
                for gx, gy in path:
                    flat = gy * W + gx
                    owner = self.grid._trace_owner.get(flat)
                    if owner:
                        neighbors.add(owner)
                    cl_owners = self.grid._clearance_owner.get(flat)
                    if cl_owners:
                        neighbors.update(cl_owners)

        return [nid for nid in neighbors if nid in self.routes]

    # ── Output ─────────────────────────────────────────────────

    def to_result(self, *, include_debug: bool = True) -> RoutingResult:
        routed_paths = {nid: r.paths for nid, r in self.routes.items()}
        routed_pads = {nid: r.pads for nid, r in self.routes.items()}

        debug_grids: list[dict] = []
        if include_debug:
            from .debug import build_debug_grids
            debug_grids = build_debug_grids(
                self.placement, self.catalog, routed_paths, routed_pads,
                config=self.config, grid=self.grid,
            )

        traces = self._grid_paths_to_traces(routed_paths, routed_pads)
        failed_nets = sorted(self.expected_nets - set(self.routes)) if self.expected_nets else []

        return RoutingResult(
            traces=traces,
            pin_assignments=dict(self.pin_assignments),
            failed_nets=failed_nets,
            debug_grids=debug_grids,
        )

    # ── Internal: pathfinding ──────────────────────────────────

    def _find_paths(
        self,
        net_id: str,
        pads: list[NetPad],
        *,
        crossing_cost: int = 0,
        cost_map: dict[int, float] | None = None,
    ) -> tuple[list[list[tuple[int, int]]], bool]:
        if len(pads) < 2:
            return ([], False)

        blocked_v = self._block_voronoi(pads)

        if len(pads) == 2:
            src = (pads[0].gx, pads[0].gy)
            snk = (pads[1].gx, pads[1].gy)
            path = find_path(
                self.grid, src, snk,
                turn_penalty=self.config.turn_penalty,
                crossing_cost=crossing_cost,
                cost_map=cost_map,
            )
            self._unblock_voronoi(blocked_v)
            if path is None:
                return ([], False)
            return ([path], True)

        result = self._route_multi_pin(pads, crossing_cost, cost_map)
        self._unblock_voronoi(blocked_v)
        return result

    def _route_multi_pin(
        self,
        pads: list[NetPad],
        crossing_cost: int = 0,
        cost_map: dict[int, float] | None = None,
    ) -> tuple[list[list[tuple[int, int]]], bool]:
        mst_edges = _compute_mst(pads)
        all_paths: list[list[tuple[int, int]]] = []

        uf_parent = list(range(len(pads)))
        uf_rank = [0] * len(pads)

        def _find(x: int) -> int:
            while uf_parent[x] != x:
                uf_parent[x] = uf_parent[uf_parent[x]]
                x = uf_parent[x]
            return x

        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra == rb:
                return
            if uf_rank[ra] < uf_rank[rb]:
                ra, rb = rb, ra
            uf_parent[rb] = ra
            if uf_rank[ra] == uf_rank[rb]:
                uf_rank[ra] += 1

        comp_trees: dict[int, set[tuple[int, int]]] = {
            i: {(pads[i].gx, pads[i].gy)} for i in range(len(pads))
        }

        def _get_tree(idx: int) -> set[tuple[int, int]]:
            return comp_trees[_find(idx)]

        def _merge(a: int, b: int, path_cells: list[tuple[int, int]]) -> None:
            ra, rb = _find(a), _find(b)
            if ra == rb:
                comp_trees[ra].update(path_cells)
                return
            tree_a = comp_trees.pop(ra)
            tree_b = comp_trees.pop(rb)
            _union(a, b)
            new_root = _find(a)
            combined = tree_a | tree_b | set(path_cells)
            comp_trees[new_root] = combined

        for pa, pb in mst_edges:
            if _find(pa) == _find(pb):
                continue

            tree_a = _get_tree(pa)
            tree_b = _get_tree(pb)
            if len(tree_a) >= len(tree_b):
                src_tree, target_tree = tree_b, tree_a
            else:
                src_tree, target_tree = tree_a, tree_b

            path = find_path_to_tree(
                self.grid, src_tree, target_tree,
                turn_penalty=self.config.turn_penalty,
                crossing_cost=crossing_cost,
                cost_map=cost_map,
            )

            if path is not None:
                all_paths.append(path)
                _merge(pa, pb, path)
            else:
                return (all_paths, False)

        roots = {_find(i) for i in range(len(pads))}
        return (all_paths, len(roots) == 1)

    # ── Internal: commit / rip-up helpers ──────────────────────

    def _has_foreign_cells(
        self,
        paths: list[list[tuple[int, int]]],
        net_id: str,
    ) -> bool:
        W = self.grid.width
        for path in paths:
            for gx, gy in path:
                flat = gy * W + gx
                existing = self.grid._trace_owner.get(flat)
                if existing and existing != net_id:
                    return True
        return False

    def _commit(
        self,
        net_id: str,
        paths: list[list[tuple[int, int]]],
        pads: list[NetPad],
    ) -> None:
        for path in paths:
            self.grid.block_trace(path, net_id=net_id)
        pad_blocks = self._compute_pad_blocks(pads)
        for pb in pad_blocks:
            self.grid.block_pad(pb.cx, pb.cy, pb.half_w_mm, pb.half_h_mm, net_id=net_id)
        self.routes[net_id] = NetRoute(paths=paths, pads=pads, pad_blocks=pad_blocks)

    def _compute_pad_blocks(self, pads: list[NetPad]) -> list[PadBlock]:
        catalog_map = {c.id: c for c in self.catalog.components}
        placement_map = {pc.instance_id: pc for pc in self.placement.components}
        blocks: list[PadBlock] = []
        for pad in pads:
            pc = placement_map.get(pad.instance_id)
            if pc is None:
                continue
            cat = catalog_map.get(pc.catalog_id)
            if cat is None:
                continue
            pin_obj = next((p for p in cat.pins if p.id == pad.pin_id), None)
            if pin_obj is None:
                continue
            shaft_w, shaft_h = pin_shaft_dimensions(pin_obj)
            rot = pc.rotation_deg % 360
            if rot in (90, 270):
                shaft_w, shaft_h = shaft_h, shaft_w
            blocks.append(PadBlock(pad.gx, pad.gy, shaft_w / 2, shaft_h / 2))
        return blocks

    def _find_pad_conflicts(
        self, pads: list[NetPad], net_id: str,
    ) -> set[str]:
        pad_blocks = self._compute_pad_blocks(pads)
        if not pad_blocks:
            return set()
        conflicts: set[str] = set()
        W = self.grid.width
        H = self.grid.height
        res = self.grid.resolution
        cl_mm = self.grid.trace_clearance_mm + self.grid.trace_width_mm / 2
        for pb in pad_blocks:
            ext = math.ceil((max(pb.half_w_mm, pb.half_h_mm) + cl_mm) / res)
            for gy in range(max(0, pb.cy - ext), min(H, pb.cy + ext + 1)):
                for gx in range(max(0, pb.cx - ext), min(W, pb.cx + ext + 1)):
                    flat = gy * W + gx
                    owner = self.grid._trace_owner.get(flat)
                    if owner and owner != net_id:
                        conflicts.add(owner)
        return {cn for cn in conflicts if cn in self.routes}

    def _find_crossed_nets(
        self,
        paths: list[list[tuple[int, int]]],
        net_id: str,
    ) -> set[str]:
        crossed: set[str] = set()
        for path in paths:
            for gx, gy in path:
                owners = self.grid.cell_owner_at(gx, gy)
                crossed.update(owners - {net_id})
        return {cn for cn in crossed if cn in self.routes}

    def _try_rip_reroute(
        self,
        net_id: str,
        paths_cross: list[list[tuple[int, int]]],
        pads: list[NetPad],
        crossed: set[str],
        *,
        _depth: int = 0,
        _exempt: frozenset[str] | None = None,
    ) -> bool:
        _MAX_RIP_DEPTH = 2
        exempt = (_exempt or frozenset()) | frozenset(crossed) | frozenset({net_id})

        snap = self.snapshot()

        saved_pads: dict[str, list[NetPad]] = {}
        for cn in crossed:
            route = self.routes.get(cn)
            if route:
                saved_pads[cn] = route.pads

        for cn in crossed:
            route = self.routes.pop(cn, None)
            if route:
                for path in route.paths:
                    self.grid.free_trace(path, net_id=cn)
                for pb in route.pad_blocks:
                    self.grid.free_pad(pb.cx, pb.cy, pb.half_w_mm, pb.half_h_mm, net_id=cn)

        self._commit(net_id, paths_cross, pads)

        for cn in crossed:
            cn_pads = saved_pads.get(cn)
            if cn_pads is None:
                self.restore(snap)
                return False

            cn_paths, cn_ok = self._find_paths(cn, cn_pads)
            if cn_ok and cn_paths and not self._has_foreign_cells(cn_paths, cn):
                self._commit(cn, cn_paths, cn_pads)
                continue

            if _depth < _MAX_RIP_DEPTH:
                cn_cross_paths, cn_ok = self._find_paths(
                    cn, cn_pads, crossing_cost=self.config.crossing_cost,
                )
                if cn_ok and cn_cross_paths:
                    cn_crossed = self._find_crossed_nets(cn_cross_paths, cn)
                    if not cn_crossed:
                        self._commit(cn, cn_cross_paths, cn_pads)
                        continue
                    if not (cn_crossed & exempt) and cn_crossed:
                        if self._try_rip_reroute(
                            cn, cn_cross_paths, cn_pads, cn_crossed,
                            _depth=_depth + 1, _exempt=exempt,
                        ):
                            continue

            self.restore(snap)
            return False

        return True

    # ── Internal: Voronoi pin blocking ─────────────────────────

    def _block_voronoi(self, net_pads: list[NetPad]) -> np.ndarray:
        if not self._voronoi_flat_by_pin:
            return np.array([], dtype=np.intp)
        net_pin_keys = {f"{pad.instance_id}:{pad.pin_id}" for pad in net_pads}
        foreign_arrays = [
            arr for key, arr in self._voronoi_flat_by_pin.items()
            if key not in net_pin_keys
        ]
        if not foreign_arrays:
            return np.array([], dtype=np.intp)
        all_foreign = np.concatenate(foreign_arrays)
        cells_np = np.frombuffer(self.grid._cells, dtype=np.uint8)
        mask = cells_np[all_foreign] == FREE
        to_block = all_foreign[mask]
        cells_np[to_block] = BLOCKED
        return to_block

    def _unblock_voronoi(self, blocked: np.ndarray) -> None:
        if len(blocked) == 0:
            return
        cells_np = np.frombuffer(self.grid._cells, dtype=np.uint8)
        cells_np[blocked] = FREE

    # ── Internal: output conversion ────────────────────────────

    def _grid_paths_to_traces(
        self,
        routed_paths: dict[str, list[list[tuple[int, int]]]],
        routed_pads: dict[str, list[NetPad]],
    ) -> list[Trace]:
        outline = self.grid.outline_poly
        min_t2t = self.config.trace_clearance_mm + self.config.trace_width_mm
        min_t2p = self.config.pin_clearance_mm + self.config.trace_width_mm / 2

        all_pin_world = self._collect_all_pin_world()

        traces: list[Trace] = []
        snap_meta: list[_SnapMeta | None] = []

        for net_id, paths in routed_paths.items():
            pads = routed_pads.get(net_id, [])
            pad_by_grid: dict[tuple[int, int], NetPad] = {
                (p.gx, p.gy): p for p in pads
            }
            own_pins = {
                "{}:{}".format(p.instance_id, p.pin_id)
                for p in pads
            }
            foreign_pins = [
                pos for key, pos in all_pin_world.items()
                if key not in own_pins
            ]

            for grid_path in paths:
                if len(grid_path) < 2:
                    continue
                world_path = _simplify_path(grid_path, self.grid)

                start_pin = end_pin = None
                start_grid = end_grid = None
                start_grid_next = end_grid_prev = None

                start_pad = pad_by_grid.get(grid_path[0])
                if start_pad is not None:
                    sx, sy = start_pad.world_x, start_pad.world_y
                    gx0, gy0 = world_path[0]
                    if (sx, sy) != (gx0, gy0) and outline.contains(Point(sx, sy)):
                        bend = _best_snap_bend(
                            sx, sy, gx0, gy0, foreign_pins,
                        )
                        world_path[0:1] = bend
                        start_pin = (sx, sy)
                        start_grid = (gx0, gy0)
                        if len(world_path) > 3:
                            start_grid_next = world_path[3]

                end_pad = pad_by_grid.get(grid_path[-1])
                if end_pad is not None:
                    ex, ey = end_pad.world_x, end_pad.world_y
                    gxn, gyn = world_path[-1]
                    if (ex, ey) != (gxn, gyn) and outline.contains(Point(ex, ey)):
                        bend = _best_snap_bend(
                            ex, ey, gxn, gyn, foreign_pins,
                        )
                        world_path[-1:] = list(reversed(bend))
                        end_pin = (ex, ey)
                        end_grid = (gxn, gyn)
                        if len(world_path) > 3:
                            end_grid_prev = world_path[-4]

                clamped = _clamp_to_outline(world_path, outline)
                traces.append(Trace(net_id=net_id, path=clamped))

                meta = None
                if start_pin or end_pin:
                    meta = _SnapMeta(
                        start_pin=start_pin, start_grid=start_grid,
                        start_grid_next=start_grid_next,
                        end_pin=end_pin, end_grid=end_grid,
                        end_grid_prev=end_grid_prev,
                        own_foreign_pins=foreign_pins,
                    )
                snap_meta.append(meta)

        _optimize_snaps(traces, snap_meta, all_pin_world, min_t2t, min_t2p, outline)
        return traces

    def _collect_all_pin_world(self) -> dict[str, tuple[float, float]]:
        catalog_map = {c.id: c for c in self.catalog.components}
        positions: dict[str, tuple[float, float]] = {}
        for pc in self.placement.components:
            cat = catalog_map.get(pc.catalog_id)
            if cat is None:
                continue
            for pin in cat.pins:
                pos = pc.pin_positions.get(pin.id)
                if pos is not None:
                    wx, wy = pos
                else:
                    wx, wy = pin_world_xy(
                        pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg,
                    )
                positions["{}:{}".format(pc.instance_id, pin.id)] = (wx, wy)
        return positions


# ── Module-level helpers ───────────────────────────────────────────

def _compute_mst(pads: list[NetPad]) -> list[tuple[int, int]]:
    n = len(pads)
    if n < 2:
        return []

    edges: list[tuple[int, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            d = abs(pads[i].gx - pads[j].gx) + abs(pads[i].gy - pads[j].gy)
            edges.append((d, i, j))
    edges.sort()

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        return True

    result: list[tuple[int, int]] = []
    for d, i, j in edges:
        if union(i, j):
            result.append((i, j))
            if len(result) == n - 1:
                break

    return result


def _simplify_path(
    grid_path: list[tuple[int, int]],
    grid: RoutingGrid,
) -> list[tuple[float, float]]:
    if len(grid_path) <= 2:
        return [grid.grid_to_world(gx, gy) for gx, gy in grid_path]

    waypoints: list[tuple[int, int]] = [grid_path[0]]
    for i in range(1, len(grid_path) - 1):
        prev, curr, nxt = grid_path[i - 1], grid_path[i], grid_path[i + 1]
        d1 = (curr[0] - prev[0], curr[1] - prev[1])
        d2 = (nxt[0] - curr[0], nxt[1] - curr[1])
        if d1 != d2:
            waypoints.append(curr)
    waypoints.append(grid_path[-1])

    return [grid.grid_to_world(gx, gy) for gx, gy in waypoints]


def _snap_bend_clearance(
    segments: list[tuple[float, float]],
    foreign_pins: list[tuple[float, float]],
) -> float:
    best = float("inf")
    for px, py in foreign_pins:
        for i in range(len(segments) - 1):
            ax, ay = segments[i]
            bx, by = segments[i + 1]
            d = point_seg_dist(px, py, ax, ay, bx, by)
            if d < best:
                best = d
    return best


def _best_snap_bend(
    px: float, py: float,
    gx: float, gy: float,
    foreign_pins: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    option_a = [(px, py), (px, gy), (gx, gy)]
    option_b = [(px, py), (gx, py), (gx, gy)]

    if not foreign_pins:
        return option_a

    clearance_a = _snap_bend_clearance(option_a, foreign_pins)
    clearance_b = _snap_bend_clearance(option_b, foreign_pins)

    return option_a if clearance_a >= clearance_b else option_b


@dataclass
class _SnapMeta:
    start_pin: tuple[float, float] | None
    start_grid: tuple[float, float] | None
    start_grid_next: tuple[float, float] | None
    end_pin: tuple[float, float] | None
    end_grid: tuple[float, float] | None
    end_grid_prev: tuple[float, float] | None
    own_foreign_pins: list[tuple[float, float]]


def _clamp_to_outline(
    world_path: list[tuple[float, float]],
    outline: Polygon,
) -> list[tuple[float, float]]:
    xs = np.array([wx for wx, _ in world_path])
    ys = np.array([wy for _, wy in world_path])
    inside = _contains_xy(outline, xs, ys)
    clamped: list[tuple[float, float]] = []
    for i, (wx, wy) in enumerate(world_path):
        if inside[i]:
            clamped.append((wx, wy))
        else:
            nearest = outline.exterior.interpolate(
                outline.exterior.project(Point(wx, wy)),
            )
            clamped.append((nearest.x, nearest.y))
    return clamped


def _trace_pair_min_dist(
    path_a: list[tuple[float, float]],
    path_b: list[tuple[float, float]],
) -> float:
    best = float("inf")
    for i in range(len(path_a) - 1):
        ax1, ay1 = path_a[i]
        ax2, ay2 = path_a[i + 1]
        for j in range(len(path_b) - 1):
            bx1, by1 = path_b[j]
            bx2, by2 = path_b[j + 1]
            d = min(
                point_seg_dist(ax1, ay1, bx1, by1, bx2, by2),
                point_seg_dist(ax2, ay2, bx1, by1, bx2, by2),
                point_seg_dist(bx1, by1, ax1, ay1, ax2, ay2),
                point_seg_dist(bx2, by2, ax1, ay1, ax2, ay2),
            )
            if d < best:
                best = d
    return best


def _trace_pin_min_dist(
    path: list[tuple[float, float]],
    pins: list[tuple[float, float]],
) -> float:
    best = float("inf")
    for px, py in pins:
        for i in range(len(path) - 1):
            d = point_seg_dist(px, py, path[i][0], path[i][1],
                               path[i + 1][0], path[i + 1][1])
            if d < best:
                best = d
    return best


def _apply_snap(
    path: list[tuple[float, float]],
    meta: _SnapMeta,
    start_variant: int,
    end_variant: int,
    outline: Polygon,
) -> list[tuple[float, float]]:
    new_path = list(path)

    n_start = 3 if meta.start_pin is not None else 0
    n_end = 3 if meta.end_pin is not None else 0

    if n_start + n_end > len(new_path):
        return _clamp_to_outline(new_path, outline)

    if meta.start_pin is not None and meta.start_grid is not None:
        px, py = meta.start_pin
        use_next = start_variant >= 2 and meta.start_grid_next is not None
        gx, gy = meta.start_grid_next if use_next else meta.start_grid
        direction = start_variant % 2
        if direction == 0:
            bend = [(px, py), (px, gy), (gx, gy)]
        else:
            bend = [(px, py), (gx, py), (gx, gy)]

        if use_next and len(new_path) >= 4:
            new_path[0:4] = bend
        else:
            new_path[0:3] = bend

    if meta.end_pin is not None and meta.end_grid is not None:
        ex, ey = meta.end_pin
        use_prev = end_variant >= 2 and meta.end_grid_prev is not None
        gxn, gyn = meta.end_grid_prev if use_prev else meta.end_grid
        direction = end_variant % 2
        if direction == 0:
            bend = [(gxn, gyn), (ex, gyn), (ex, ey)]
        else:
            bend = [(gxn, gyn), (gxn, ey), (ex, ey)]

        if use_prev and len(new_path) >= 4:
            new_path[-4:] = bend
        else:
            new_path[-3:] = bend

    return _clamp_to_outline(new_path, outline)


def _optimize_snaps(
    traces: list[Trace],
    snap_meta: list[_SnapMeta | None],
    all_pin_world: dict[str, tuple[float, float]],
    min_t2t: float,
    min_t2p: float,
    outline: Polygon,
) -> None:
    threshold = min(min_t2t, min_t2p)
    margin = threshold + 1.0

    bboxes = []
    for t in traces:
        xs = [p[0] for p in t.path]
        ys = [p[1] for p in t.path]
        bboxes.append((min(xs), min(ys), max(xs), max(ys)))

    for _round in range(3):
        improved = False
        for idx in range(len(traces)):
            meta = snap_meta[idx]
            if meta is None or len(traces[idx].path) < 6:
                continue

            bi = bboxes[idx]
            net_id = traces[idx].net_id
            nearby = []
            for j in range(len(traces)):
                if j == idx or traces[j].net_id == net_id:
                    continue
                bj = bboxes[j]
                if bi[0] - margin > bj[2] or bi[2] + margin < bj[0]:
                    continue
                if bi[1] - margin > bj[3] or bi[3] + margin < bj[1]:
                    continue
                nearby.append(j)

            current_path = traces[idx].path
            own_pins = meta.own_foreign_pins
            current_worst = _snap_worst_clearance(
                current_path, [traces[j].path for j in nearby], own_pins,
            )
            if current_worst >= threshold:
                continue

            best_worst = current_worst
            best_path = None

            start_variants = [0, 1]
            if meta.start_pin and meta.start_grid_next:
                start_variants.extend([2, 3])
            end_variants = [0, 1]
            if meta.end_pin and meta.end_grid_prev:
                end_variants.extend([2, 3])

            for sv in start_variants:
                for ev in end_variants:
                    candidate = _apply_snap(
                        current_path, meta, sv, ev, outline,
                    )
                    worst = _snap_worst_clearance(
                        candidate, [traces[j].path for j in nearby], own_pins,
                    )
                    if worst > best_worst:
                        best_worst = worst
                        best_path = candidate

            if best_path is not None:
                traces[idx] = Trace(net_id=net_id, path=best_path)
                xs = [p[0] for p in best_path]
                ys = [p[1] for p in best_path]
                bboxes[idx] = (min(xs), min(ys), max(xs), max(ys))
                improved = True

        if not improved:
            break


def _snap_worst_clearance(
    path: list[tuple[float, float]],
    other_paths: list[list[tuple[float, float]]],
    foreign_pins: list[tuple[float, float]],
) -> float:
    worst = float("inf")
    n = len(path) - 1
    snap_segs = []
    for i in range(min(2, n)):
        snap_segs.append((path[i], path[i + 1]))
    for i in range(max(0, n - 2), n):
        if i >= 2:
            snap_segs.append((path[i], path[i + 1]))

    for (a1, a2) in snap_segs:
        for other in other_paths:
            for j in range(len(other) - 1):
                d = min(
                    point_seg_dist(a1[0], a1[1], other[j][0], other[j][1],
                                   other[j+1][0], other[j+1][1]),
                    point_seg_dist(a2[0], a2[1], other[j][0], other[j][1],
                                   other[j+1][0], other[j+1][1]),
                    point_seg_dist(other[j][0], other[j][1], a1[0], a1[1],
                                   a2[0], a2[1]),
                    point_seg_dist(other[j+1][0], other[j+1][1], a1[0], a1[1],
                                   a2[0], a2[1]),
                )
                if d < worst:
                    worst = d

    for px, py in foreign_pins:
        for (a1, a2) in snap_segs:
            d = point_seg_dist(px, py, a1[0], a1[1], a2[0], a2[1])
            if d < worst:
                worst = d

    return worst
