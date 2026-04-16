"""Hierarchical routing profiler with interactive sunburst visualization.

Instruments every significant operation in the routing pipeline with nested
timers. Produces an interactive HTML sunburst chart where you can click into
any slice to see its sub-breakdown, and "unaccounted" time is explicitly shown.

Run:  python tests/profile_sunburst.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.profile_base import (
    HTimer, TimerNode, print_tree, build_sunburst_html, build_insights_html,
    _collect_leaf_times,
)


# ── Profiled routing pipeline ────────────────────────────────────


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def run_profiled_route():
    from src.pipeline.design.parsing import parse_physical_design, parse_circuit
    from src.pipeline.placer.serialization import assemble_full_placement
    from src.catalog.loader import load_catalog
    from src.pipeline.router.grid import RoutingGrid
    from src.pipeline.router.models import RouterConfig
    from src.pipeline.router.engine import (
        _block_components, _build_all_pin_cells, _build_pin_voronoi,
        _compute_pad_radius, _compute_pin_clearance_cells,
        _parse_net_refs, _resolve_all_pads, _priority_order,
        _perturb, _re_resolve_and_route,
        _copy_pin_pools, _restore_pin_pools,
    )
    from src.pipeline.router.pins import build_pin_pools
    from src.pipeline.router.solution import Solution
    from src.pipeline.router import pathfinder as pf_mod
    from src.pipeline.router.pathfinder import (
        _try_l_route, _octile_dt, _octile_h, _build_neighbors,
        _build_cost_table, _SQRT2,
        FREE, BLOCKED, TRACE_PATH, PERMANENTLY_BLOCKED,
    )
    from heapq import heappush as _heappush, heappop as _heappop
    import array as _array_mod
    import src.pipeline.router.solution as sol_mod
    from src.pipeline.router.grid import RoutingGrid as grid_cls
    from shapely.geometry import Polygon

    ht = HTimer()

    # ── Load fixture ──
    design_data = json.loads((FIXTURE_DIR / "large_design.json").read_text(encoding="utf-8"))
    circuit_data = json.loads((FIXTURE_DIR / "large_circuit.json").read_text(encoding="utf-8"))
    placement_data = json.loads((FIXTURE_DIR / "large_placement.json").read_text(encoding="utf-8"))

    physical = parse_physical_design(design_data)
    circuit = parse_circuit(circuit_data)
    catalog = load_catalog()
    placement = assemble_full_placement(
        placement_data, physical.outline, circuit.nets, physical.enclosure,
    )
    catalog_map = {c.id: c for c in catalog.components}
    config = RouterConfig()
    outline_poly = Polygon(placement.outline.vertices)

    # ── Instrument functions ──────────────────────────────────────

    # Wrap pathfinder functions
    orig_find_path = pf_mod.find_path
    orig_find_path_to_tree = pf_mod.find_path_to_tree

    from src.pipeline.router.models import TURN_PENALTY as _TURN_PENALTY

    def timed_find_path(grid, source, sink, *, turn_penalty=_TURN_PENALTY,
                        crossing_cost=0, cost_map=None):
        with ht.section("find_path"):
            if cost_map is None:
                with ht.section("l_route_attempt"):
                    l_path = _try_l_route(grid, source, sink)
                if l_path is not None:
                    return l_path
            with ht.section("astar"):
                sx, sy = source
                tx, ty = sink
                W = grid.width
                H = grid.height
                cells = grid._cells
                if not (0 <= sx < W and 0 <= sy < H and 0 <= tx < W and 0 <= ty < H):
                    return None
                if cells[sy * W + sx] == TRACE_PATH or cells[ty * W + tx] == TRACE_PATH:
                    return None
                if source == sink:
                    return [source]
                N = W * H
                INF = float('inf')
                start_key = sy * W + sx
                sink_key = ty * W + tx
                with ht.section("alloc"):
                    neighbors = _build_neighbors(W, H)
                    cost_tbl = _build_cost_table(turn_penalty)
                    g = [INF] * N
                    parent = [-1] * N
                    closed = bytearray(N)
                    g[start_key] = 0
                    counter = 0
                    heap = [(_octile_h(sx - tx, sy - ty), counter, start_key, -1)]
                with ht.section("search") as search_node:
                    _t_heappop = 0.0; _t_visit = 0.0
                    _t_closed = 0.0; _t_cell = 0.0; _t_cost = 0.0
                    _t_heuristic = 0.0; _t_heappush = 0.0
                    _n_expanded = 0; _n_pushed = 0; _n_skipped = 0
                    _n_closed = 0; _n_cell = 0; _n_evaluated = 0
                    _perf = time.perf_counter
                    found_key = -1
                    while heap:
                        _t0 = _perf()
                        f, _cnt, key, direction = _heappop(heap)
                        _t1 = _perf()
                        _t_heappop += _t1 - _t0
                        if closed[key]:
                            _t_visit += _perf() - _t1
                            _n_skipped += 1
                            continue
                        closed[key] = 1
                        _n_expanded += 1
                        if key == sink_key:
                            _t_visit += _perf() - _t1
                            found_key = key
                            break
                        cur_g = g[key]
                        _t_visit += _perf() - _t1
                        dir_row = (direction + 1) * 8
                        _te0 = _perf()
                        for nkey, d in neighbors[key]:
                            if closed[nkey]:
                                _n_closed += 1
                                _te0 = _perf()
                                _t_closed += _te0 - _te0
                                continue
                            _tbc = _perf()
                            _t_closed += _tbc - _te0
                            nval = cells[nkey]
                            cross_extra = 0
                            if nval != FREE:
                                if nval == PERMANENTLY_BLOCKED:
                                    _n_cell += 1
                                    _te0 = _perf()
                                    _t_cell += _te0 - _tbc
                                    continue
                                if crossing_cost > 0 and (nval == TRACE_PATH or nval == BLOCKED):
                                    cross_extra = crossing_cost
                                else:
                                    _n_cell += 1
                                    _te0 = _perf()
                                    _t_cell += _te0 - _tbc
                                    continue
                            _n_evaluated += 1
                            _tf1 = _perf()
                            _t_cell += _tf1 - _tbc
                            cost = cost_tbl[dir_row + d] + cross_extra
                            if cost_map is not None:
                                cost += cost_map.get(nkey, 0)
                            tentative_g = cur_g + cost
                            if tentative_g < g[nkey]:
                                g[nkey] = tentative_g
                                parent[nkey] = key
                                _tc1 = _perf()
                                _t_cost += _tc1 - _tf1
                                counter += 1
                                h_val = _octile_h(nkey % W - tx, nkey // W - ty)
                                _th1 = _perf()
                                _t_heuristic += _th1 - _tc1
                                _heappush(heap, (tentative_g + h_val, counter, nkey, d))
                                _n_pushed += 1
                                _te0 = _perf()
                                _t_heappush += _te0 - _th1
                                continue
                            _te0 = _perf()
                            _t_cost += _te0 - _tf1
                        _t_closed += _perf() - _te0

                    _pop_total = _t_heappop + _t_visit
                    _pop_node = TimerNode(name="heap_pop", elapsed=_pop_total, call_count=_n_expanded + _n_skipped)
                    _pop_node.add_child_node(TimerNode(name="heappop_call", elapsed=_t_heappop, call_count=_n_expanded + _n_skipped))
                    _pop_node.add_child_node(TimerNode(name="visit_check", elapsed=_t_visit, call_count=_n_expanded + _n_skipped))
                    search_node.add_child_node(_pop_node)

                    _filter_total = _t_closed + _t_cell
                    _eval_total = _filter_total + _t_cost
                    _eval_node = TimerNode(name="neighbor_eval", elapsed=_eval_total, call_count=_n_expanded)
                    _filter_node = TimerNode(name="neighbor_filter", elapsed=_filter_total, call_count=_n_closed + _n_cell + _n_evaluated)
                    _filter_node.add_child_node(TimerNode(name="closed_check", elapsed=_t_closed, call_count=_n_closed))
                    _filter_node.add_child_node(TimerNode(name="cell_filter", elapsed=_t_cell, call_count=_n_cell + _n_evaluated))
                    _eval_node.add_child_node(_filter_node)
                    _eval_node.add_child_node(TimerNode(name="cost_compute", elapsed=_t_cost, call_count=_n_evaluated))
                    search_node.add_child_node(_eval_node)

                    _push_total = _t_heuristic + _t_heappush
                    _push_node = TimerNode(name="heap_push", elapsed=_push_total, call_count=_n_pushed)
                    _push_node.add_child_node(TimerNode(name="heuristic", elapsed=_t_heuristic, call_count=_n_pushed))
                    _push_node.add_child_node(TimerNode(name="heappush_call", elapsed=_t_heappush, call_count=_n_pushed))
                    search_node.add_child_node(_push_node)
                if found_key < 0:
                    return None
                with ht.section("reconstruct"):
                    path = [(found_key % W, found_key // W)]
                    k = found_key
                    while True:
                        pk = parent[k]
                        if pk < 0:
                            break
                        path.append((pk % W, pk // W))
                        k = pk
                    path.reverse()
                    return path

    def timed_find_path_to_tree(grid, source, tree, *, turn_penalty=_TURN_PENALTY,
                                crossing_cost=0, cost_map=None):
        with ht.section("find_path_to_tree"):
            W, H = grid.width, grid.height
            N = W * H
            cells = grid._cells

            if isinstance(source, set):
                sources = source
            else:
                sources = {source}

            overlap = sources & tree
            if overlap:
                return [next(iter(overlap))]

            with ht.section("build_tree_mask"):
                tree_mask = bytearray(N)
                tree_list = list(tree)
                for tx, ty in tree_list:
                    tree_mask[ty * W + tx] = 1

            with ht.section("manhattan_dt"):
                h_map = _octile_dt(W, H, tree_list)

            with ht.section("astar"):
                INF = 0x7FFFFFFF
                with ht.section("alloc"):
                    neighbors = _build_neighbors(W, H)
                    cost_tbl = _build_cost_table(turn_penalty)
                    g = [INF] * N
                    parent = [-1] * N
                    closed = bytearray(N)
                with ht.section("seed"):
                    counter = 0
                    heap = []
                    for sx, sy in sources:
                        if not (0 <= sx < W and 0 <= sy < H):
                            continue
                        skey = sy * W + sx
                        if cells[skey] != FREE and not tree_mask[skey]:
                            continue
                        g[skey] = 0
                        _heappush(heap, (h_map[skey], counter, skey, -1))
                        counter += 1
                    if not heap:
                        return None
                with ht.section("search") as search_node2:
                    _t_heappop = 0.0; _t_visit = 0.0
                    _t_closed = 0.0; _t_cell = 0.0; _t_cost = 0.0
                    _t_heuristic = 0.0; _t_heappush = 0.0
                    _n_expanded = 0; _n_pushed = 0; _n_skipped = 0
                    _n_closed = 0; _n_cell = 0; _n_evaluated = 0
                    _perf = time.perf_counter
                    found_key = -1
                    while heap:
                        _t0 = _perf()
                        f, _cnt, key, direction = _heappop(heap)
                        _t1 = _perf()
                        _t_heappop += _t1 - _t0
                        if closed[key]:
                            _t_visit += _perf() - _t1
                            _n_skipped += 1
                            continue
                        closed[key] = 1
                        _n_expanded += 1
                        if tree_mask[key]:
                            _t_visit += _perf() - _t1
                            found_key = key
                            break
                        cur_g = g[key]
                        _t_visit += _perf() - _t1
                        dir_row = (direction + 1) * 8
                        _te0 = _perf()
                        for nkey, d in neighbors[key]:
                            if closed[nkey]:
                                _n_closed += 1
                                _te0 = _perf()
                                _t_closed += _te0 - _te0
                                continue
                            _tbc = _perf()
                            _t_closed += _tbc - _te0
                            nval = cells[nkey]
                            cross_extra = 0
                            if nval != FREE and not tree_mask[nkey]:
                                if nval == PERMANENTLY_BLOCKED:
                                    _n_cell += 1
                                    _te0 = _perf()
                                    _t_cell += _te0 - _tbc
                                    continue
                                if crossing_cost > 0 and (nval == TRACE_PATH or nval == BLOCKED):
                                    cross_extra = crossing_cost
                                else:
                                    _n_cell += 1
                                    _te0 = _perf()
                                    _t_cell += _te0 - _tbc
                                    continue
                            _n_evaluated += 1
                            _tf1 = _perf()
                            _t_cell += _tf1 - _tbc
                            cost = cost_tbl[dir_row + d] + cross_extra
                            if cost_map is not None:
                                cost += cost_map.get(nkey, 0)
                            tentative_g = cur_g + cost
                            if tentative_g < g[nkey]:
                                g[nkey] = tentative_g
                                parent[nkey] = key
                                _tc1 = _perf()
                                _t_cost += _tc1 - _tf1
                                counter += 1
                                h_val = h_map[nkey]
                                _th1 = _perf()
                                _t_heuristic += _th1 - _tc1
                                _heappush(heap, (tentative_g + h_val, counter, nkey, d))
                                _n_pushed += 1
                                _te0 = _perf()
                                _t_heappush += _te0 - _th1
                                continue
                            _te0 = _perf()
                            _t_cost += _te0 - _tf1
                        _t_closed += _perf() - _te0

                    _pop_total = _t_heappop + _t_visit
                    _pop_node = TimerNode(name="heap_pop", elapsed=_pop_total, call_count=_n_expanded + _n_skipped)
                    _pop_node.add_child_node(TimerNode(name="heappop_call", elapsed=_t_heappop, call_count=_n_expanded + _n_skipped))
                    _pop_node.add_child_node(TimerNode(name="visit_check", elapsed=_t_visit, call_count=_n_expanded + _n_skipped))
                    search_node2.add_child_node(_pop_node)

                    _filter_total = _t_closed + _t_cell
                    _eval_total = _filter_total + _t_cost
                    _eval_node = TimerNode(name="neighbor_eval", elapsed=_eval_total, call_count=_n_expanded)
                    _filter_node = TimerNode(name="neighbor_filter", elapsed=_filter_total, call_count=_n_closed + _n_cell + _n_evaluated)
                    _filter_node.add_child_node(TimerNode(name="closed_check", elapsed=_t_closed, call_count=_n_closed))
                    _filter_node.add_child_node(TimerNode(name="cell_filter", elapsed=_t_cell, call_count=_n_cell + _n_evaluated))
                    _eval_node.add_child_node(_filter_node)
                    _eval_node.add_child_node(TimerNode(name="cost_compute", elapsed=_t_cost, call_count=_n_evaluated))
                    search_node2.add_child_node(_eval_node)

                    _push_total = _t_heuristic + _t_heappush
                    _push_node = TimerNode(name="heap_push", elapsed=_push_total, call_count=_n_pushed)
                    _push_node.add_child_node(TimerNode(name="heuristic", elapsed=_t_heuristic, call_count=_n_pushed))
                    _push_node.add_child_node(TimerNode(name="heappush_call", elapsed=_t_heappush, call_count=_n_pushed))
                    search_node2.add_child_node(_push_node)
                if found_key < 0:
                    return None
                with ht.section("reconstruct"):
                    path = [(found_key % W, found_key // W)]
                    k = found_key
                    while True:
                        pk = parent[k]
                        if pk < 0:
                            break
                        path.append((pk % W, pk // W))
                        k = pk
                    path.reverse()
                    return path

    pf_mod.find_path = timed_find_path
    sol_mod.find_path = timed_find_path
    pf_mod.find_path_to_tree = timed_find_path_to_tree
    sol_mod.find_path_to_tree = timed_find_path_to_tree

    # Wrap grid operations
    orig_block_trace = grid_cls.block_trace
    orig_free_trace = grid_cls.free_trace

    def timed_block_trace(self, *a, **kw):
        with ht.section("block_trace"):
            return orig_block_trace(self, *a, **kw)

    def timed_free_trace(self, *a, **kw):
        with ht.section("free_trace"):
            return orig_free_trace(self, *a, **kw)

    grid_cls.block_trace = timed_block_trace
    grid_cls.free_trace = timed_free_trace

    # Wrap solution methods
    orig_block_voronoi = Solution._block_voronoi
    orig_unblock_voronoi = Solution._unblock_voronoi
    orig_find_crossed = Solution._find_crossed_nets
    orig_has_foreign = Solution._has_foreign_cells
    orig_commit = Solution._commit
    orig_try_rip_reroute = Solution._try_rip_reroute
    orig_snapshot = Solution.snapshot
    orig_restore = Solution.restore
    orig_rip_up = Solution.rip_up

    def timed_block_voronoi(self, *a, **kw):
        with ht.section("block_voronoi"):
            return orig_block_voronoi(self, *a, **kw)

    def timed_unblock_voronoi(self, *a, **kw):
        with ht.section("unblock_voronoi"):
            return orig_unblock_voronoi(self, *a, **kw)

    def timed_find_crossed(self, *a, **kw):
        with ht.section("find_crossed_nets"):
            return orig_find_crossed(self, *a, **kw)

    def timed_has_foreign(self, *a, **kw):
        with ht.section("has_foreign_cells"):
            return orig_has_foreign(self, *a, **kw)

    def timed_commit(self, *a, **kw):
        with ht.section("commit"):
            return orig_commit(self, *a, **kw)

    def timed_try_rip_reroute(self, *a, **kw):
        with ht.section("try_rip_reroute"):
            return orig_try_rip_reroute(self, *a, **kw)

    def timed_snapshot(self, *a, **kw):
        with ht.section("snapshot"):
            return orig_snapshot(self, *a, **kw)

    def timed_restore(self, *a, **kw):
        with ht.section("restore"):
            return orig_restore(self, *a, **kw)

    def timed_rip_up(self, *a, **kw):
        with ht.section("rip_up"):
            return orig_rip_up(self, *a, **kw)

    orig_grid_paths_to_traces = Solution._grid_paths_to_traces
    orig_to_result = Solution.to_result

    def timed_grid_paths_to_traces(self, *a, **kw):
        with ht.section("grid_paths_to_traces"):
            return orig_grid_paths_to_traces(self, *a, **kw)

    def timed_to_result(self, *a, **kw):
        with ht.section("to_result"):
            return orig_to_result(self, *a, **kw)

    Solution._block_voronoi = timed_block_voronoi
    Solution._unblock_voronoi = timed_unblock_voronoi
    Solution._find_crossed_nets = timed_find_crossed
    Solution._has_foreign_cells = timed_has_foreign
    Solution._commit = timed_commit
    Solution._try_rip_reroute = timed_try_rip_reroute
    Solution.snapshot = timed_snapshot
    Solution.restore = timed_restore
    Solution.rip_up = timed_rip_up
    Solution._grid_paths_to_traces = timed_grid_paths_to_traces
    Solution.to_result = timed_to_result

    try:
        with ht.section("total"):
            # ── Phase 1: Grid construction ──
            with ht.section("grid_construction"):
                with ht.section("grid_init"):
                    grid = RoutingGrid(
                        outline_poly,
                        resolution=config.grid_resolution_mm,
                        edge_clearance=config.edge_clearance_mm,
                        trace_width_mm=config.trace_width_mm,
                        trace_clearance_mm=config.trace_clearance_mm,
                    )
                with ht.section("block_raised_floor"):
                    grid.block_raised_floor(placement.outline, placement.enclosure)
                with ht.section("block_components"):
                    pad_radius = _compute_pad_radius(config)
                    _block_components(grid, placement, catalog_map, pad_radius)

            # ── Phase 2: Pin mapping & Voronoi ──
            with ht.section("pin_setup"):
                with ht.section("build_pin_cells"):
                    all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
                with ht.section("pin_clearance"):
                    pin_clearance_cells = _compute_pin_clearance_cells(config)
                with ht.section("build_voronoi"):
                    pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)

            # ── Phase 3: Net parsing & pad resolution ──
            with ht.section("net_resolution"):
                with ht.section("parse_net_refs"):
                    net_pad_map = _parse_net_refs(placement, catalog, catalog_map)
                with ht.section("filter_nets"):
                    net_ids = [
                        n.id for n in placement.nets
                        if len(net_pad_map.get(n.id, [])) >= 2
                    ]
                with ht.section("build_pin_pools"):
                    pin_pools = build_pin_pools(placement, catalog)
                with ht.section("resolve_pads"):
                    pads_map, pin_assignments = _resolve_all_pads(
                        net_ids, net_pad_map, placement, catalog, grid, pin_pools,
                    )
                with ht.section("priority_order"):
                    ordering = _priority_order(
                        net_ids, net_pad_map, pads_map,
                    )

            # ── Phase 4: Solution construction ──
            with ht.section("solution_init"):
                solution = Solution(
                    grid, config, placement, catalog,
                    net_pad_map, pin_voronoi, all_pin_cells,
                )
                solution.expected_nets = set(net_ids)
                solution.pin_assignments = pin_assignments

            # ── Phase 5: Initial routing ──
            with ht.section("initial_routing"):
                solution.route_nets(ordering, pads_map)

            score = solution.score()
            print(f"Initial score: {score}")

            if solution.is_perfect():
                print("Perfect solution on initial pass!")
            else:
                # ── Phase 6: Iterative improvement ──
                with ht.section("improvement_loop"):
                    best = solution.snapshot()
                    best_score = solution.score()
                    best_pads_map = dict(pads_map)
                    best_pin_assignments = dict(pin_assignments)
                    best_pin_pools = _copy_pin_pools(pin_pools)
                    stall = 0
                    iteration = 0
                    profiler_max_iterations = 200
                    t_loop_start = time.perf_counter()
                    profiler_time_limit = 60.0

                    while True:
                        all_routed = best_score[0] == 0
                        if all_routed and iteration >= config.max_improve_iterations:
                            break
                        if iteration >= profiler_max_iterations:
                            print(f"  Profiler cap reached ({profiler_max_iterations} iters)")
                            break
                        if time.perf_counter() - t_loop_start > profiler_time_limit:
                            print(f"  Profiler time limit reached ({profiler_time_limit}s)")
                            break

                        targets = solution.worst_nets(k=3)
                        if not targets:
                            break

                        neighborhood = solution.neighborhood(targets)
                        before = solution.score()

                        solution.rip_up(neighborhood)
                        new_order = _perturb(neighborhood, targets, iteration)

                        with ht.section("re_route"):
                            solution.route_nets(new_order, pads_map)

                        with ht.section("retry_unrouted"):
                            unrouted = [nid for nid in net_ids
                                        if nid not in solution.routes and nid in pads_map]
                            if unrouted:
                                solution.route_nets(unrouted, pads_map)

                        with ht.section("re_resolve_unrouted"):
                            unrouted_still = [nid for nid in net_ids
                                             if nid not in solution.routes]
                            if unrouted_still:
                                _re_resolve_and_route(
                                    unrouted_still, net_pad_map, placement, catalog,
                                    grid, pin_pools, pin_assignments, pads_map, solution,
                                )

                        after = solution.score()

                        if after < before:
                            best = solution.snapshot()
                            best_score = after
                            best_pads_map = dict(pads_map)
                            best_pin_assignments = dict(pin_assignments)
                            best_pin_pools = _copy_pin_pools(pin_pools)
                            stall = 0
                            print(f"  Iter {iteration+1}: improved {before} -> {after}")
                            if solution.is_perfect():
                                print("  Perfect solution found!")
                                break
                        else:
                            solution.restore(best)
                            pads_map.update(best_pads_map)
                            pin_assignments.update(best_pin_assignments)
                            _restore_pin_pools(pin_pools, best_pin_pools)
                            stall += 1
                            print(f"  Iter {iteration+1}: no improvement (stall {stall})")

                            if all_routed and stall >= config.stall_limit:
                                print(f"  Stalled for {stall} iterations, stopping")
                                break

                        iteration += 1

                    solution.restore(best)
                    pin_assignments.update(best_pin_assignments)
                    solution.pin_assignments = pin_assignments
                    final_score = solution.score()
                    print(f"Final score: {final_score}")

            # ── Phase 7: Output conversion ──
            with ht.section("output_conversion"):
                solution.to_result(include_debug=False)

    finally:
        # Restore all originals
        pf_mod.find_path = orig_find_path
        sol_mod.find_path = orig_find_path
        pf_mod.find_path_to_tree = orig_find_path_to_tree
        sol_mod.find_path_to_tree = orig_find_path_to_tree
        grid_cls.block_trace = orig_block_trace
        grid_cls.free_trace = orig_free_trace
        Solution._block_voronoi = orig_block_voronoi
        Solution._unblock_voronoi = orig_unblock_voronoi
        Solution._find_crossed_nets = orig_find_crossed
        Solution._has_foreign_cells = orig_has_foreign
        Solution._commit = orig_commit
        Solution._try_rip_reroute = orig_try_rip_reroute
        Solution.snapshot = orig_snapshot
        Solution.restore = orig_restore
        Solution.rip_up = orig_rip_up
        Solution._grid_paths_to_traces = orig_grid_paths_to_traces
        Solution.to_result = orig_to_result

    return ht


def main():
    if not FIXTURE_DIR.exists():
        print(f"Fixture data not found at: {FIXTURE_DIR}")
        return

    print("Running profiled route...")
    ht = run_profiled_route()

    # Print tree to console
    print("\n" + "=" * 90)
    print("  HIERARCHICAL TIMING BREAKDOWN")
    print("=" * 90)
    root = ht.root.children[0]  # the "total" section
    print_tree(root, parent_elapsed=0)
    print("=" * 90)

    # Build sunburst HTML
    out_path = Path(__file__).resolve().parent.parent / "outputs" / "profile_sunburst.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_sunburst_html(root, title="Router Hierarchical Profile")
    out_path.write_text(html, encoding="utf-8")
    print(f"\nInteractive sunburst chart saved to: {out_path}")

    insights_path = out_path.with_name("profile_insights.html")
    insights_html = build_insights_html(root, title="Router Optimization Insights")
    insights_path.write_text(insights_html, encoding="utf-8")
    print(f"Optimization insights saved to: {insights_path}")


if __name__ == "__main__":
    main()
