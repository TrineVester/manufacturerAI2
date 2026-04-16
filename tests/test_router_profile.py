"""Router performance profiling test.

Instruments every major phase of the routing algorithm to measure:
  - Wall-clock time per phase
  - Call counts for hot functions
  - Time per call for frequently-invoked operations

Run with:  python -m pytest tests/test_router_profile.py -v -s
"""

from __future__ import annotations

import json
import time
import unittest

import pytest
import functools
from pathlib import Path
from collections import defaultdict
from contextlib import contextmanager

from shapely.geometry import Polygon

from src.catalog.loader import load_catalog
from src.pipeline.placer import place_components
from src.pipeline.router.grid import RoutingGrid
from src.pipeline.router.models import RouterConfig
from src.pipeline.router.pathfinder import find_path, find_path_to_tree
from src.pipeline.router.engine import (
    _block_components, _build_all_pin_cells, _build_pin_voronoi,
    _compute_pad_radius, _compute_pin_clearance_cells,
    _parse_net_refs, _resolve_all_pads, _priority_order,
)
from src.pipeline.router.solution import Solution, _PinRef
from src.pipeline.router.pins import build_pin_pools
from src.pipeline.router import route_traces
from tests.flashlight_fixture import make_flashlight_design


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class CallTracker:
    """Wraps a function to track call count and cumulative time."""

    def __init__(self, func, name: str | None = None):
        self.func = func
        self.name = name or func.__qualname__
        self.call_count = 0
        self.total_time = 0.0
        self.times: list[float] = []
        functools.update_wrapper(self, func)

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        t0 = time.perf_counter()
        result = self.func(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        self.total_time += elapsed
        self.times.append(elapsed)
        return result

    def report(self) -> str:
        avg = (self.total_time / self.call_count * 1000) if self.call_count else 0
        mx = max(self.times) * 1000 if self.times else 0
        return (
            f"  {self.name:<40s}  calls={self.call_count:>5d}  "
            f"total={self.total_time * 1000:>8.1f}ms  "
            f"avg={avg:>7.2f}ms  max={mx:>7.2f}ms"
        )


@contextmanager
def timed_phase(name: str, results: dict):
    t0 = time.perf_counter()
    yield
    elapsed = time.perf_counter() - t0
    results[name] = elapsed


@pytest.mark.slow
class TestRouterProfile(unittest.TestCase):
    """Profile the router on the flashlight fixture."""

    def test_full_route_timing(self):
        """Measure total route_traces wall-clock time."""
        catalog = load_catalog()
        design = make_flashlight_design()
        placement = place_components(design, catalog)

        t0 = time.perf_counter()
        result = route_traces(placement, catalog)
        total = time.perf_counter() - t0

        print(f"\n{'='*70}")
        print(f"  TOTAL route_traces: {total*1000:.1f} ms")
        print(f"  Nets: {len(result.traces)}, "
              f"Failed: {len(result.failed_nets)}")
        print(f"{'='*70}")
        self.assertTrue(result.ok)

    def test_phase_breakdown(self):
        """Break down route_traces into phases and measure each."""
        catalog = load_catalog()
        design = make_flashlight_design()
        placement = place_components(design, catalog)

        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)
        phases: dict[str, float] = {}

        # Phase 1: Grid construction
        with timed_phase("1_grid_construction", phases):
            grid = RoutingGrid(
                outline_poly,
                resolution=config.grid_resolution_mm,
                edge_clearance=config.edge_clearance_mm,
                trace_width_mm=config.trace_width_mm,
                trace_clearance_mm=config.trace_clearance_mm,
            )

        print(f"\n  Grid: {grid.width}x{grid.height} = {grid.width * grid.height} cells")

        # Phase 1b: Raised floor blocking
        with timed_phase("1b_raised_floor", phases):
            grid.block_raised_floor(placement.outline, placement.enclosure)

        # Phase 1c: Component blocking
        pad_radius = _compute_pad_radius(config)
        with timed_phase("1c_component_blocking", phases):
            _block_components(grid, placement, catalog_map, pad_radius)

        # Phase 2a: Pin cell map
        with timed_phase("2a_pin_cell_map", phases):
            all_pin_cells = _build_all_pin_cells(placement, catalog, grid)

        # Phase 2b: Voronoi map
        pin_clearance_cells = _compute_pin_clearance_cells(config)
        with timed_phase("2b_voronoi_map", phases):
            pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)

        print(f"  Voronoi entries: {len(pin_voronoi)}")

        # Phase 3: Net parsing
        with timed_phase("3_net_parsing", phases):
            net_pad_map = _parse_net_refs(placement, catalog, catalog_map)

        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]

        # Phase 4a: Pad resolution
        with timed_phase("4a_pad_resolution", phases):
            pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )

        # Phase 4b: Priority ordering
        with timed_phase("4b_priority_ordering", phases):
            ordering = _priority_order(
                net_ids, net_pad_map, pads_map,
            )

        # Phase 5: Solution construction
        with timed_phase("5_solution_init", phases):
            solution = Solution(
                grid, config, placement, catalog,
                net_pad_map, pin_voronoi, all_pin_cells,
            )
            solution.expected_nets = set(net_ids)
            solution.pin_assignments = pin_assignments

        # Phase 6: Initial routing
        with timed_phase("6_initial_routing", phases):
            solution.route_nets(ordering, pads_map)

        initial_score = solution.score()

        # Phase 7: Iterative improvement
        with timed_phase("7_iterative_improvement", phases):
            if not solution.is_perfect():
                best = solution.snapshot()
                best_score = solution.score()
                stall = 0
                for iteration in range(config.max_improve_iterations):
                    targets = solution.worst_nets(k=3)
                    if not targets:
                        break
                    neighborhood = solution.neighborhood(targets)
                    before = solution.score()
                    solution.rip_up(neighborhood)
                    from src.pipeline.router.engine import _perturb
                    new_order = _perturb(neighborhood, targets, iteration)
                    solution.route_nets(new_order, pads_map)
                    after = solution.score()
                    if after < before:
                        best = solution.snapshot()
                        best_score = after
                        stall = 0
                        if solution.is_perfect():
                            break
                    else:
                        solution.restore(best)
                        stall += 1
                        if stall >= config.stall_limit:
                            break
                solution.restore(best)

        # Phase 8: Output conversion
        with timed_phase("8_output_conversion", phases):
            result = solution.to_result()

        # Print report
        total = sum(phases.values())
        print(f"\n{'='*70}")
        print(f"  ROUTER PHASE BREAKDOWN (total={total*1000:.1f} ms)")
        print(f"{'='*70}")
        for name, elapsed in phases.items():
            pct = (elapsed / total * 100) if total > 0 else 0
            print(f"  {name:<30s}  {elapsed*1000:>8.1f} ms  ({pct:>5.1f}%)")
        print(f"{'='*70}")
        print(f"  Initial score: {initial_score}")
        print(f"  Final score:   {solution.score()}")
        print(f"  Result OK:     {result.ok}")
        print(f"{'='*70}")

        self.assertTrue(result.ok)

    def test_hot_function_profiling(self):
        """Track call counts and timing for hot inner functions."""
        import src.pipeline.router.pathfinder as pf_mod
        import src.pipeline.router.solution as sol_mod

        orig_find_path = pf_mod.find_path
        orig_find_path_to_tree = pf_mod.find_path_to_tree

        trackers: list[CallTracker] = []

        fp_tracker = CallTracker(orig_find_path, "pathfinder.find_path")
        fpt_tracker = CallTracker(orig_find_path_to_tree, "pathfinder.find_path_to_tree")

        trackers.extend([fp_tracker, fpt_tracker])

        # Monkey-patch module-level functions (not bound methods)
        pf_mod.find_path = fp_tracker
        pf_mod.find_path_to_tree = fpt_tracker

        # Also patch the imports in solution.py
        sol_orig_find_path = sol_mod.find_path
        sol_orig_find_path_to_tree = sol_mod.find_path_to_tree
        sol_mod.find_path = fp_tracker
        sol_mod.find_path_to_tree = fpt_tracker

        try:
            catalog = load_catalog()
            design = make_flashlight_design()
            placement = place_components(design, catalog)

            t0 = time.perf_counter()
            result = route_traces(placement, catalog)
            total = time.perf_counter() - t0

            print(f"\n{'='*70}")
            print(f"  HOT FUNCTION PROFILING (total={total*1000:.1f} ms)")
            print(f"{'='*70}")
            for tracker in trackers:
                print(tracker.report())
            print(f"{'='*70}")

            self.assertTrue(result.ok)
        finally:
            pf_mod.find_path = orig_find_path
            pf_mod.find_path_to_tree = orig_find_path_to_tree
            sol_mod.find_path = sol_orig_find_path
            sol_mod.find_path_to_tree = sol_orig_find_path_to_tree

    def test_voronoi_block_unblock_profiling(self):
        """Measure voronoi blocking overhead specifically."""
        catalog = load_catalog()
        design = make_flashlight_design()
        placement = place_components(design, catalog)

        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)

        grid = RoutingGrid(
            outline_poly,
            resolution=config.grid_resolution_mm,
            edge_clearance=config.edge_clearance_mm,
            trace_width_mm=config.trace_width_mm,
            trace_clearance_mm=config.trace_clearance_mm,
        )
        grid.block_raised_floor(placement.outline, placement.enclosure)
        pad_radius = _compute_pad_radius(config)
        _block_components(grid, placement, catalog_map, pad_radius)

        all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
        pin_clearance_cells = _compute_pin_clearance_cells(config)
        pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)
        net_pad_map = _parse_net_refs(placement, catalog, catalog_map)
        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]
        pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )

        solution = Solution(
            grid, config, placement, catalog,
            net_pad_map, pin_voronoi, all_pin_cells,
        )

        total_block = 0.0
        total_unblock = 0.0
        block_calls = 0
        total_blocked_cells = 0

        for nid in net_ids:
            pads = pads_map.get(nid)
            if pads is None or len(pads) < 2:
                continue
            t0 = time.perf_counter()
            blocked = solution._block_voronoi(pads)
            total_block += time.perf_counter() - t0
            block_calls += 1
            total_blocked_cells += len(blocked)

            t0 = time.perf_counter()
            solution._unblock_voronoi(blocked)
            total_unblock += time.perf_counter() - t0

        print(f"\n{'='*70}")
        print(f"  VORONOI BLOCKING PROFILING")
        print(f"{'='*70}")
        print(f"  Voronoi entries: {len(pin_voronoi)}")
        print(f"  block/unblock cycles: {block_calls}")
        print(f"  Avg cells blocked per call: {total_blocked_cells / block_calls:.0f}" if block_calls else "  No calls")
        print(f"  _block_voronoi total: {total_block*1000:.2f} ms "
              f"(avg {total_block/block_calls*1000:.2f} ms/call)" if block_calls else "")
        print(f"  _unblock_voronoi total: {total_unblock*1000:.2f} ms "
              f"(avg {total_unblock/block_calls*1000:.2f} ms/call)" if block_calls else "")
        print(f"{'='*70}")

    def test_snapshot_restore_profiling(self):
        """Measure snapshot/restore overhead."""
        catalog = load_catalog()
        design = make_flashlight_design()
        placement = place_components(design, catalog)

        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)

        grid = RoutingGrid(
            outline_poly,
            resolution=config.grid_resolution_mm,
            edge_clearance=config.edge_clearance_mm,
            trace_width_mm=config.trace_width_mm,
            trace_clearance_mm=config.trace_clearance_mm,
        )
        grid.block_raised_floor(placement.outline, placement.enclosure)
        pad_radius = _compute_pad_radius(config)
        _block_components(grid, placement, catalog_map, pad_radius)

        all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
        pin_clearance_cells = _compute_pin_clearance_cells(config)
        pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)
        net_pad_map = _parse_net_refs(placement, catalog, catalog_map)
        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]
        pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )
        ordering = _priority_order(
            net_ids, net_pad_map, pads_map, grid, config, pin_voronoi,
        )

        solution = Solution(
            grid, config, placement, catalog,
            net_pad_map, pin_voronoi, all_pin_cells,
        )
        solution.expected_nets = set(net_ids)
        solution.pin_assignments = pin_assignments
        solution.route_nets(ordering, pads_map)

        N = 50
        snap_times = []
        restore_times = []

        for _ in range(N):
            t0 = time.perf_counter()
            snap = solution.snapshot()
            snap_times.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            solution.restore(snap)
            restore_times.append(time.perf_counter() - t0)

        avg_snap = sum(snap_times) / N * 1000
        avg_restore = sum(restore_times) / N * 1000

        print(f"\n{'='*70}")
        print(f"  SNAPSHOT/RESTORE PROFILING ({N} iterations)")
        print(f"{'='*70}")
        print(f"  Grid cells: {grid.width * grid.height}")
        print(f"  Trace owners: {len(grid._trace_owner)}")
        print(f"  Clearance owners: {len(grid._clearance_owner)}")
        print(f"  Routes: {len(solution.routes)}")
        print(f"  snapshot() avg: {avg_snap:.3f} ms")
        print(f"  restore()  avg: {avg_restore:.3f} ms")
        print(f"  Combined per cycle: {avg_snap + avg_restore:.3f} ms")
        print(f"{'='*70}")


def _load_large_fixture():
    """Load the 27-component button matrix design from session data."""
    from src.pipeline.design.parsing import parse_physical_design, parse_circuit
    from src.pipeline.placer.serialization import assemble_full_placement

    design_data = json.loads((FIXTURE_DIR / "large_design.json").read_text(encoding="utf-8"))
    circuit_data = json.loads((FIXTURE_DIR / "large_circuit.json").read_text(encoding="utf-8"))
    placement_data = json.loads((FIXTURE_DIR / "large_placement.json").read_text(encoding="utf-8"))

    physical = parse_physical_design(design_data)
    circuit = parse_circuit(circuit_data)
    placement = assemble_full_placement(
        placement_data, physical.outline, circuit.nets, physical.enclosure,
    )
    return placement


@unittest.skipUnless(FIXTURE_DIR.exists(), "Large fixture data not available")
@pytest.mark.slow
class TestLargeDesignProfile(unittest.TestCase):
    """Profile the router on a 27-component, 27-net button matrix design."""

    def test_large_full_route_timing(self):
        """Measure total route_traces wall-clock time on large design."""
        catalog = load_catalog()
        placement = _load_large_fixture()

        print(f"\n  Design: {len(placement.components)} components, "
              f"{len(placement.nets)} nets")

        t0 = time.perf_counter()
        result = route_traces(placement, catalog)
        total = time.perf_counter() - t0

        routed = {t.net_id for t in result.traces}
        print(f"\n{'='*70}")
        print(f"  LARGE DESIGN route_traces: {total*1000:.1f} ms")
        print(f"  Nets routed: {len(routed)}/{len(placement.nets)}, "
              f"Failed: {len(result.failed_nets)}")
        if result.failed_nets:
            print(f"  Failed: {result.failed_nets}")
        print(f"{'='*70}")

    def test_large_phase_breakdown(self):
        """Break down route_traces into phases on large design."""
        catalog = load_catalog()
        placement = _load_large_fixture()

        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)
        phases: dict[str, float] = {}

        with timed_phase("1_grid_construction", phases):
            grid = RoutingGrid(
                outline_poly,
                resolution=config.grid_resolution_mm,
                edge_clearance=config.edge_clearance_mm,
                trace_width_mm=config.trace_width_mm,
                trace_clearance_mm=config.trace_clearance_mm,
            )

        print(f"\n  Grid: {grid.width}x{grid.height} = {grid.width * grid.height} cells")

        with timed_phase("1b_raised_floor", phases):
            grid.block_raised_floor(placement.outline, placement.enclosure)

        pad_radius = _compute_pad_radius(config)
        with timed_phase("1c_component_blocking", phases):
            _block_components(grid, placement, catalog_map, pad_radius)

        with timed_phase("2a_pin_cell_map", phases):
            all_pin_cells = _build_all_pin_cells(placement, catalog, grid)

        pin_clearance_cells = _compute_pin_clearance_cells(config)
        with timed_phase("2b_voronoi_map", phases):
            pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)

        print(f"  Voronoi entries: {len(pin_voronoi)}")
        print(f"  Pin cells: {len(all_pin_cells)}")

        with timed_phase("3_net_parsing", phases):
            net_pad_map = _parse_net_refs(placement, catalog, catalog_map)

        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]
        print(f"  Routable nets: {len(net_ids)}")
        for nid in net_ids:
            print(f"    {nid}: {len(net_pad_map[nid])} pins")

        with timed_phase("4a_pad_resolution", phases):
            pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )

        with timed_phase("4b_priority_ordering", phases):
            ordering = _priority_order(
                net_ids, net_pad_map, pads_map,
            )

        with timed_phase("5_solution_init", phases):
            solution = Solution(
                grid, config, placement, catalog,
                net_pad_map, pin_voronoi, all_pin_cells,
            )
            solution.expected_nets = set(net_ids)
            solution.pin_assignments = pin_assignments

        with timed_phase("6_initial_routing", phases):
            solution.route_nets(ordering, pads_map)

        initial_score = solution.score()

        with timed_phase("7_iterative_improvement", phases):
            if not solution.is_perfect():
                best = solution.snapshot()
                best_score = solution.score()
                stall = 0
                for iteration in range(config.max_improve_iterations):
                    targets = solution.worst_nets(k=3)
                    if not targets:
                        break
                    neighborhood = solution.neighborhood(targets)
                    before = solution.score()
                    solution.rip_up(neighborhood)
                    from src.pipeline.router.engine import _perturb
                    new_order = _perturb(neighborhood, targets, iteration)
                    solution.route_nets(new_order, pads_map)
                    after = solution.score()
                    if after < before:
                        best = solution.snapshot()
                        best_score = after
                        stall = 0
                        if solution.is_perfect():
                            break
                    else:
                        solution.restore(best)
                        stall += 1
                        if stall >= config.stall_limit:
                            break
                solution.restore(best)

        with timed_phase("8_output_conversion", phases):
            result = solution.to_result()

        total = sum(phases.values())
        print(f"\n{'='*70}")
        print(f"  LARGE DESIGN PHASE BREAKDOWN (total={total*1000:.1f} ms)")
        print(f"{'='*70}")
        for name, elapsed in phases.items():
            pct = (elapsed / total * 100) if total > 0 else 0
            print(f"  {name:<30s}  {elapsed*1000:>8.1f} ms  ({pct:>5.1f}%)")
        print(f"{'='*70}")
        print(f"  Initial score: {initial_score}")
        print(f"  Final score:   {solution.score()}")
        print(f"  Result OK:     {result.ok}")
        print(f"{'='*70}")

    def test_large_hot_functions(self):
        """Track call counts and timing for hot functions on large design."""
        import src.pipeline.router.pathfinder as pf_mod
        import src.pipeline.router.solution as sol_mod

        orig_find_path = pf_mod.find_path
        orig_find_path_to_tree = pf_mod.find_path_to_tree
        sol_orig_find_path = sol_mod.find_path
        sol_orig_find_path_to_tree = sol_mod.find_path_to_tree

        trackers: list[CallTracker] = []
        fp_tracker = CallTracker(orig_find_path, "pathfinder.find_path")
        fpt_tracker = CallTracker(orig_find_path_to_tree, "pathfinder.find_path_to_tree")
        trackers.extend([fp_tracker, fpt_tracker])

        pf_mod.find_path = fp_tracker
        pf_mod.find_path_to_tree = fpt_tracker
        sol_mod.find_path = fp_tracker
        sol_mod.find_path_to_tree = fpt_tracker

        try:
            catalog = load_catalog()
            placement = _load_large_fixture()

            t0 = time.perf_counter()
            result = route_traces(placement, catalog)
            total = time.perf_counter() - t0

            print(f"\n{'='*70}")
            print(f"  LARGE DESIGN HOT FUNCTIONS (total={total*1000:.1f} ms)")
            print(f"{'='*70}")
            for tracker in trackers:
                print(tracker.report())
            print(f"{'='*70}")
        finally:
            pf_mod.find_path = orig_find_path
            pf_mod.find_path_to_tree = orig_find_path_to_tree
            sol_mod.find_path = sol_orig_find_path
            sol_mod.find_path_to_tree = sol_orig_find_path_to_tree

    def test_large_granular_per_net(self):
        """Granular per-net profiling: instrument every sub-operation
        inside route_net to identify exactly where time is spent.

        Tracks per-net:
          - voronoi block/unblock time + cells blocked
          - A* find_path / find_path_to_tree calls, time, path lengths
          - block_trace / free_trace calls and time
          - has_foreign_cells / find_crossed_nets time
          - rip_reroute attempts and time
          - jumper placement attempts and time
          - tree relaxation time
          - total route_net time and strategy used
        """
        import src.pipeline.router.pathfinder as pf_mod
        import src.pipeline.router.solution as sol_mod

        catalog = load_catalog()
        placement = _load_large_fixture()
        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)

        grid = RoutingGrid(
            outline_poly,
            resolution=config.grid_resolution_mm,
            edge_clearance=config.edge_clearance_mm,
            trace_width_mm=config.trace_width_mm,
            trace_clearance_mm=config.trace_clearance_mm,
        )
        grid.block_raised_floor(placement.outline, placement.enclosure)
        pad_radius = _compute_pad_radius(config)
        _block_components(grid, placement, catalog_map, pad_radius)
        all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
        pin_clearance_cells = _compute_pin_clearance_cells(config)
        pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)
        net_pad_map = _parse_net_refs(placement, catalog, catalog_map)
        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]
        pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )
        ordering = _priority_order(
            net_ids, net_pad_map, pads_map,
        )

        # Save originals
        orig_fp = pf_mod.find_path
        orig_fpt = pf_mod.find_path_to_tree
        sol_orig_fp = sol_mod.find_path
        sol_orig_fpt = sol_mod.find_path_to_tree
        orig_block_voronoi = sol_mod.Solution._block_voronoi
        orig_unblock_voronoi = sol_mod.Solution._unblock_voronoi
        orig_has_foreign = sol_mod.Solution._has_foreign_cells
        orig_find_crossed = sol_mod.Solution._find_crossed_nets
        orig_try_rip = sol_mod.Solution._try_rip_reroute
        orig_commit = sol_mod.Solution._commit

        # Per-net accumulator
        net_stats: dict[str, dict] = {}
        current_net: list[str] = [""]

        def _new_stats() -> dict:
            return {
                "find_path_calls": 0, "find_path_time": 0.0,
                "find_path_lengths": [],
                "find_path_to_tree_calls": 0, "find_path_to_tree_time": 0.0,
                "find_path_to_tree_lengths": [],
                "voronoi_block_time": 0.0, "voronoi_unblock_time": 0.0,
                "voronoi_cells_blocked": 0,
                "has_foreign_calls": 0, "has_foreign_time": 0.0,
                "find_crossed_calls": 0, "find_crossed_time": 0.0,
                "rip_reroute_calls": 0, "rip_reroute_time": 0.0,
                "commit_calls": 0, "commit_time": 0.0,
                "strategy": "unknown",
            }

        def _get_stats() -> dict:
            nid = current_net[0]
            if nid not in net_stats:
                net_stats[nid] = _new_stats()
            return net_stats[nid]

        # Instrumented wrappers
        def tracked_find_path(*args, **kwargs):
            s = _get_stats()
            s["find_path_calls"] += 1
            t0 = time.perf_counter()
            result = orig_fp(*args, **kwargs)
            s["find_path_time"] += time.perf_counter() - t0
            if result is not None:
                s["find_path_lengths"].append(len(result))
            return result

        def tracked_find_path_to_tree(*args, **kwargs):
            s = _get_stats()
            s["find_path_to_tree_calls"] += 1
            t0 = time.perf_counter()
            result = orig_fpt(*args, **kwargs)
            s["find_path_to_tree_time"] += time.perf_counter() - t0
            if result is not None:
                s["find_path_to_tree_lengths"].append(len(result))
            return result

        def tracked_block_voronoi(self_obj, pads):
            s = _get_stats()
            t0 = time.perf_counter()
            result = orig_block_voronoi(self_obj, pads)
            s["voronoi_block_time"] += time.perf_counter() - t0
            s["voronoi_cells_blocked"] += len(result)
            return result

        def tracked_unblock_voronoi(self_obj, blocked):
            s = _get_stats()
            t0 = time.perf_counter()
            result = orig_unblock_voronoi(self_obj, blocked)
            s["voronoi_unblock_time"] += time.perf_counter() - t0
            return result

        def tracked_has_foreign(self_obj, paths, net_id):
            s = _get_stats()
            s["has_foreign_calls"] += 1
            t0 = time.perf_counter()
            result = orig_has_foreign(self_obj, paths, net_id)
            s["has_foreign_time"] += time.perf_counter() - t0
            return result

        def tracked_find_crossed(self_obj, paths, net_id):
            s = _get_stats()
            s["find_crossed_calls"] += 1
            t0 = time.perf_counter()
            result = orig_find_crossed(self_obj, paths, net_id)
            s["find_crossed_time"] += time.perf_counter() - t0
            return result

        def tracked_try_rip(self_obj, *args, **kwargs):
            s = _get_stats()
            s["rip_reroute_calls"] += 1
            t0 = time.perf_counter()
            result = orig_try_rip(self_obj, *args, **kwargs)
            s["rip_reroute_time"] += time.perf_counter() - t0
            return result

        def tracked_commit(self_obj, net_id, paths, pads):
            s = _get_stats()
            s["commit_calls"] += 1
            t0 = time.perf_counter()
            result = orig_commit(self_obj, net_id, paths, pads)
            s["commit_time"] += time.perf_counter() - t0
            return result

        # Monkey-patch everything
        pf_mod.find_path = tracked_find_path
        pf_mod.find_path_to_tree = tracked_find_path_to_tree
        sol_mod.find_path = tracked_find_path
        sol_mod.find_path_to_tree = tracked_find_path_to_tree
        sol_mod.Solution._block_voronoi = tracked_block_voronoi
        sol_mod.Solution._unblock_voronoi = tracked_unblock_voronoi
        sol_mod.Solution._has_foreign_cells = tracked_has_foreign
        sol_mod.Solution._find_crossed_nets = tracked_find_crossed
        sol_mod.Solution._try_rip_reroute = tracked_try_rip
        sol_mod.Solution._commit = tracked_commit

        try:
            solution = Solution(
                grid, config, placement, catalog,
                net_pad_map, pin_voronoi, all_pin_cells,
            )
            solution.expected_nets = set(net_ids)
            solution.pin_assignments = pin_assignments

            net_times: list[tuple[str, int, float]] = []
            total_t0 = time.perf_counter()

            for nid in ordering:
                pads = pads_map.get(nid)
                if pads is None or len(pads) < 2:
                    continue
                current_net[0] = nid
                net_stats[nid] = _new_stats()
                t0 = time.perf_counter()
                solution.route_net(nid, pads)
                elapsed = time.perf_counter() - t0
                net_times.append((nid, len(pads), elapsed))

                # Determine which strategy was used
                s = net_stats[nid]
                if s["rip_reroute_calls"] > 0:
                    s["strategy"] = "rip_reroute"
                elif s["find_path_calls"] > 0 or s["find_path_to_tree_calls"] > 0:
                    s["strategy"] = "clean"
                else:
                    s["strategy"] = "failed"

            total_initial = time.perf_counter() - total_t0

            # ── Print per-net summary ──
            print(f"\n{'='*78}")
            print(f"  GRANULAR PER-NET PROFILING — INITIAL ROUTING")
            print(f"  Grid: {grid.width}x{grid.height} = {grid.width*grid.height} cells")
            print(f"{'='*78}")

            print(f"\n  {'NET':<20s} {'PINS':>4s} {'TOTAL':>8s} {'STRATEGY':<15s}"
                  f" {'A*':>6s} {'A*ms':>7s} {'TREE':>5s} {'TREEms':>7s}"
                  f" {'VOR':>6s} {'BLK/UNB':>8s}")
            print(f"  {'-'*96}")

            for nid, npins, elapsed in sorted(net_times, key=lambda x: -x[2]):
                s = net_stats[nid]
                fp_c = s["find_path_calls"]
                fp_t = s["find_path_time"] * 1000
                fpt_c = s["find_path_to_tree_calls"]
                fpt_t = s["find_path_to_tree_time"] * 1000
                vor_cells = s["voronoi_cells_blocked"]
                vor_t = (s["voronoi_block_time"] + s["voronoi_unblock_time"]) * 1000
                print(f"  {nid:<20s} {npins:>4d} {elapsed*1000:>7.0f}ms"
                      f" {s['strategy']:<15s}"
                      f" {fp_c:>6d} {fp_t:>6.0f}ms"
                      f" {fpt_c:>5d} {fpt_t:>6.0f}ms"
                      f" {vor_cells:>6d} {vor_t:>6.0f}ms")

            # ── Print aggregate sub-operation breakdown ──
            totals = _new_stats()
            for s in net_stats.values():
                totals["find_path_calls"] += s["find_path_calls"]
                totals["find_path_time"] += s["find_path_time"]
                totals["find_path_lengths"].extend(s["find_path_lengths"])
                totals["find_path_to_tree_calls"] += s["find_path_to_tree_calls"]
                totals["find_path_to_tree_time"] += s["find_path_to_tree_time"]
                totals["find_path_to_tree_lengths"].extend(s["find_path_to_tree_lengths"])
                totals["voronoi_block_time"] += s["voronoi_block_time"]
                totals["voronoi_unblock_time"] += s["voronoi_unblock_time"]
                totals["voronoi_cells_blocked"] += s["voronoi_cells_blocked"]
                totals["has_foreign_calls"] += s["has_foreign_calls"]
                totals["has_foreign_time"] += s["has_foreign_time"]
                totals["find_crossed_calls"] += s["find_crossed_calls"]
                totals["find_crossed_time"] += s["find_crossed_time"]
                totals["rip_reroute_calls"] += s["rip_reroute_calls"]
                totals["rip_reroute_time"] += s["rip_reroute_time"]
                totals["commit_calls"] += s["commit_calls"]
                totals["commit_time"] += s["commit_time"]

            fp_lens = totals["find_path_lengths"]
            fpt_lens = totals["find_path_to_tree_lengths"]

            print(f"\n{'='*78}")
            print(f"  AGGREGATE SUB-OPERATION BREAKDOWN (initial routing = {total_initial*1000:.0f} ms)")
            print(f"{'='*78}")
            print(f"  {'OPERATION':<35s} {'CALLS':>6s} {'TOTAL':>9s} {'AVG':>8s} {'MAX':>8s}")
            print(f"  {'-'*70}")

            def _row(name, calls, total_t, times_list=None):
                avg = (total_t / calls * 1000) if calls else 0
                mx = max(times_list) * 1000 if times_list else 0
                print(f"  {name:<35s} {calls:>6d} {total_t*1000:>8.0f}ms"
                      f" {avg:>7.1f}ms {mx:>7.1f}ms")

            _row("find_path (A*)", totals["find_path_calls"],
                 totals["find_path_time"])
            _row("find_path_to_tree (multi-src A*)", totals["find_path_to_tree_calls"],
                 totals["find_path_to_tree_time"])
            _row("voronoi_block", len(net_stats),
                 totals["voronoi_block_time"])
            _row("voronoi_unblock", len(net_stats),
                 totals["voronoi_unblock_time"])
            _row("has_foreign_cells", totals["has_foreign_calls"],
                 totals["has_foreign_time"])
            _row("find_crossed_nets", totals["find_crossed_calls"],
                 totals["find_crossed_time"])
            _row("try_rip_reroute", totals["rip_reroute_calls"],
                 totals["rip_reroute_time"])
            _row("commit (block_trace)", totals["commit_calls"],
                 totals["commit_time"])

            if fp_lens:
                print(f"\n  find_path lengths: "
                      f"avg={sum(fp_lens)/len(fp_lens):.0f} "
                      f"min={min(fp_lens)} max={max(fp_lens)} "
                      f"median={sorted(fp_lens)[len(fp_lens)//2]}")
            if fpt_lens:
                print(f"  find_path_to_tree lengths: "
                      f"avg={sum(fpt_lens)/len(fpt_lens):.0f} "
                      f"min={min(fpt_lens)} max={max(fpt_lens)} "
                      f"median={sorted(fpt_lens)[len(fpt_lens)//2]}")

            # ── Strategy breakdown ──
            strat_counts: dict[str, int] = defaultdict(int)
            strat_times: dict[str, float] = defaultdict(float)
            for (nid, _np, elapsed), s in zip(
                sorted(net_times, key=lambda x: x[0]),
                [net_stats[nid] for (nid, _, _) in sorted(net_times, key=lambda x: x[0])]
            ):
                strat_counts[s["strategy"]] += 1
                strat_times[s["strategy"]] += elapsed

            print(f"\n  Strategy breakdown:")
            for strat in sorted(strat_counts.keys()):
                cnt = strat_counts[strat]
                t = strat_times[strat]
                print(f"    {strat:<20s}  nets={cnt:>3d}  total={t*1000:>8.0f}ms"
                      f"  avg={t/cnt*1000:>7.0f}ms")

            print(f"\n  Score: {solution.score()}")
            print(f"{'='*78}")

        finally:
            pf_mod.find_path = orig_fp
            pf_mod.find_path_to_tree = orig_fpt
            sol_mod.find_path = sol_orig_fp
            sol_mod.find_path_to_tree = sol_orig_fpt
            sol_mod.Solution._block_voronoi = orig_block_voronoi
            sol_mod.Solution._unblock_voronoi = orig_unblock_voronoi
            sol_mod.Solution._has_foreign_cells = orig_has_foreign
            sol_mod.Solution._find_crossed_nets = orig_find_crossed
            sol_mod.Solution._try_rip_reroute = orig_try_rip
            sol_mod.Solution._commit = orig_commit

    def test_large_improvement_loop_profiling(self):
        """Profile each iteration of the improvement loop: rip-up, re-route,
        snapshot, restore, with per-iteration timing and score tracking."""
        catalog = load_catalog()
        placement = _load_large_fixture()
        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)

        grid = RoutingGrid(
            outline_poly,
            resolution=config.grid_resolution_mm,
            edge_clearance=config.edge_clearance_mm,
            trace_width_mm=config.trace_width_mm,
            trace_clearance_mm=config.trace_clearance_mm,
        )
        grid.block_raised_floor(placement.outline, placement.enclosure)
        pad_radius = _compute_pad_radius(config)
        _block_components(grid, placement, catalog_map, pad_radius)
        all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
        pin_clearance_cells = _compute_pin_clearance_cells(config)
        pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)
        net_pad_map = _parse_net_refs(placement, catalog, catalog_map)
        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]
        pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )
        ordering = _priority_order(
            net_ids, net_pad_map, pads_map,
        )

        solution = Solution(
            grid, config, placement, catalog,
            net_pad_map, pin_voronoi, all_pin_cells,
        )
        solution.expected_nets = set(net_ids)
        solution.pin_assignments = pin_assignments
        solution.route_nets(ordering, pads_map)

        initial_score = solution.score()

        print(f"\n{'='*78}")
        print(f"  IMPROVEMENT LOOP PROFILING")
        print(f"  Initial score: {initial_score}")
        print(f"{'='*78}")
        print(f"  {'ITER':>4s} {'RESULT':<10s} {'TOTAL':>7s} "
              f"{'RIP-UP':>7s} {'ROUTE':>7s} {'SNAP':>6s} {'RESTORE':>7s}"
              f" {'#NETS':>5s} {'SCORE'}")
        print(f"  {'-'*85}")

        if not solution.is_perfect():
            best = solution.snapshot()
            best_score = solution.score()
            stall = 0

            for iteration in range(config.max_improve_iterations):
                iter_t0 = time.perf_counter()
                targets = solution.worst_nets(k=3)
                if not targets:
                    print(f"  {iteration+1:>4d} no-targets — stopping")
                    break

                neighborhood = solution.neighborhood(targets)
                before = solution.score()

                t_rip = time.perf_counter()
                solution.rip_up(neighborhood)
                rip_ms = (time.perf_counter() - t_rip) * 1000

                from src.pipeline.router.engine import _perturb
                new_order = _perturb(neighborhood, targets, iteration)

                t_route = time.perf_counter()
                solution.route_nets(new_order, pads_map)
                route_ms = (time.perf_counter() - t_route) * 1000

                after = solution.score()
                snap_ms = 0.0
                restore_ms = 0.0

                if after < before:
                    t_snap = time.perf_counter()
                    best = solution.snapshot()
                    snap_ms = (time.perf_counter() - t_snap) * 1000
                    best_score = after
                    stall = 0
                    result_str = "IMPROVED"
                    if solution.is_perfect():
                        total_ms = (time.perf_counter() - iter_t0) * 1000
                        print(f"  {iteration+1:>4d} {'PERFECT':<10s} {total_ms:>6.0f}ms"
                              f" {rip_ms:>6.0f}ms {route_ms:>6.0f}ms"
                              f" {snap_ms:>5.0f}ms {restore_ms:>6.0f}ms"
                              f" {len(neighborhood):>5d} {after}")
                        break
                else:
                    t_restore = time.perf_counter()
                    solution.restore(best)
                    restore_ms = (time.perf_counter() - t_restore) * 1000
                    stall += 1
                    result_str = f"stall({stall})"

                total_ms = (time.perf_counter() - iter_t0) * 1000
                print(f"  {iteration+1:>4d} {result_str:<10s} {total_ms:>6.0f}ms"
                      f" {rip_ms:>6.0f}ms {route_ms:>6.0f}ms"
                      f" {snap_ms:>5.0f}ms {restore_ms:>6.0f}ms"
                      f" {len(neighborhood):>5d} {after}")

                if stall >= config.stall_limit:
                    print(f"  Stalled at iteration {iteration+1}")
                    break

            solution.restore(best)

        print(f"\n  Final score: {solution.score()}")
        print(f"{'='*78}")


@unittest.skipUnless(FIXTURE_DIR.exists(), "Large fixture data not available")
@pytest.mark.slow
class TestAStarInternals(unittest.TestCase):
    """Profile the internal breakdown of A* pathfinding calls."""

    def test_astar_internals(self):
        """Instrument find_path_to_tree to count nodes expanded/pushed,
        heap ops, min_h time, g-list init time, etc."""
        import heapq as _heapq
        import src.pipeline.router.pathfinder as pf_mod
        from src.pipeline.router.grid import FREE, BLOCKED, TRACE_PATH, PERMANENTLY_BLOCKED

        catalog = load_catalog()
        placement = _load_large_fixture()
        config = RouterConfig()
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(placement.outline.vertices)

        grid = RoutingGrid(
            outline_poly,
            resolution=config.grid_resolution_mm,
            edge_clearance=config.edge_clearance_mm,
            trace_width_mm=config.trace_width_mm,
            trace_clearance_mm=config.trace_clearance_mm,
        )
        grid.block_raised_floor(placement.outline, placement.enclosure)
        pad_radius = _compute_pad_radius(config)
        _block_components(grid, placement, catalog_map, pad_radius)

        all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
        pin_clearance_cells = _compute_pin_clearance_cells(config)
        pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)
        net_pad_map = _parse_net_refs(placement, catalog, catalog_map)
        net_ids = [
            n.id for n in placement.nets
            if len(net_pad_map.get(n.id, [])) >= 2
        ]
        pads_map, pin_assignments = _resolve_all_pads(
            net_ids, net_pad_map, placement, catalog, grid,
            build_pin_pools(placement, catalog),
        )
        ordering = _priority_order(
            net_ids, net_pad_map, pads_map, grid, config, pin_voronoi,
        )

        import numpy as np
        TURN_PENALTY = config.turn_penalty
        DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))

        call_stats: list[dict] = []

        def instrumented_find_path_to_tree(
            grid, source, tree, *,
            turn_penalty=TURN_PENALTY, crossing_cost=0, cost_map=None,
        ):
            W = grid.width
            H = grid.height
            N = W * H
            cells = grid._cells
            INF = 0x7FFFFFFF

            if isinstance(source, set):
                sources = source
            else:
                sources = {source}

            overlap = sources & tree
            if overlap:
                cell = next(iter(overlap))
                call_stats.append({
                    "type": "overlap", "expanded": 0, "pushed": 0,
                    "minh_time": 0, "init_time": 0, "loop_time": 0,
                    "total_time": 0, "path_len": 1, "tree_size": len(tree),
                    "source_size": len(sources), "heap_pops": 0,
                    "skipped_closed": 0,
                })
                return [cell]

            t_total = time.perf_counter()

            t_init = time.perf_counter()
            tree_mask = bytearray(N)
            tree_list = list(tree)
            for tx, ty in tree_list:
                tree_mask[ty * W + tx] = 1

            from src.pipeline.router.pathfinder import _manhattan_dt
            h_map = _manhattan_dt(W, H, tree_list)

            g = [INF] * N
            parent = [-1] * N
            closed = bytearray(N)

            counter = 0
            heap = []
            for sx, sy in sources:
                if not (0 <= sx < W and 0 <= sy < H):
                    continue
                skey = sy * W + sx
                if cells[skey] != FREE and not tree_mask[skey]:
                    continue
                g[skey] = 0
                h0 = h_map[skey]
                _heapq.heappush(heap, (h0, counter, sx, sy, -1))
                counter += 1
            init_time = time.perf_counter() - t_init

            if not heap:
                call_stats.append({
                    "type": "no_path", "expanded": 0, "pushed": counter,
                    "init_time": init_time, "loop_time": 0,
                    "total_time": time.perf_counter() - t_total,
                    "path_len": 0, "tree_size": len(tree_list),
                    "source_size": len(sources), "heap_pops": 0,
                    "skipped_closed": 0,
                })
                return None

            expanded = 0
            pushed = counter
            heap_pops = 0
            skipped_closed = 0

            t_loop = time.perf_counter()
            result_path = None

            while heap:
                f, _cnt, cx, cy, direction = _heapq.heappop(heap)
                heap_pops += 1
                key = cy * W + cx

                if closed[key]:
                    skipped_closed += 1
                    continue
                closed[key] = 1
                expanded += 1

                if tree_mask[key]:
                    path = [(cx, cy)]
                    k = key
                    while True:
                        pk = parent[k]
                        if pk < 0:
                            break
                        path.append((pk % W, pk // W))
                        k = pk
                    path.reverse()
                    result_path = path
                    break

                cur_g = g[key]

                for d, (dx, dy) in enumerate(DIRS):
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < W and 0 <= ny < H):
                        continue
                    nkey = ny * W + nx
                    if closed[nkey]:
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

                    is_turn = direction != -1 and direction != d
                    cost = 1 + (turn_penalty if is_turn else 0) + cross_extra
                    if cost_map is not None:
                        cost += cost_map.get(nkey, 0)
                    tentative_g = cur_g + cost

                    if tentative_g < g[nkey]:
                        g[nkey] = tentative_g
                        parent[nkey] = key
                        h = h_map[nkey]
                        counter += 1
                        pushed += 1
                        _heapq.heappush(heap, (tentative_g + h, counter, nx, ny, d))

            loop_time = time.perf_counter() - t_loop
            total_time = time.perf_counter() - t_total

            call_stats.append({
                "type": "found" if result_path else "no_path",
                "expanded": expanded,
                "pushed": pushed,
                "heap_pops": heap_pops,
                "skipped_closed": skipped_closed,
                "init_time": init_time,
                "loop_time": loop_time,
                "total_time": total_time,
                "path_len": len(result_path) if result_path else 0,
                "tree_size": len(tree_list),
                "source_size": len(sources),
            })
            return result_path

        orig_fpt = pf_mod.find_path_to_tree
        import src.pipeline.router.solution as sol_mod
        sol_orig_fpt = sol_mod.find_path_to_tree

        pf_mod.find_path_to_tree = instrumented_find_path_to_tree
        sol_mod.find_path_to_tree = instrumented_find_path_to_tree

        try:
            solution = Solution(
                grid, config, placement, catalog,
                net_pad_map, pin_voronoi, all_pin_cells,
            )
            solution.expected_nets = set(net_ids)
            solution.pin_assignments = pin_assignments
            solution.route_nets(ordering, pads_map)

            print(f"\n{'='*90}")
            print(f"  A* INTERNALS PROFILING — find_path_to_tree")
            print(f"  {len(call_stats)} calls total")
            print(f"{'='*90}")

            real_calls = [s for s in call_stats if s["type"] != "overlap"]
            if real_calls:
                total_expanded = sum(s["expanded"] for s in real_calls)
                total_pushed = sum(s["pushed"] for s in real_calls)
                total_pops = sum(s["heap_pops"] for s in real_calls)
                total_skipped = sum(s["skipped_closed"] for s in real_calls)
                total_init = sum(s["init_time"] for s in real_calls)
                total_loop = sum(s["loop_time"] for s in real_calls)
                total_time = sum(s["total_time"] for s in real_calls)

                print(f"\n  AGGREGATE ({len(real_calls)} non-overlap calls):")
                print(f"    Total time:      {total_time*1000:>10.0f} ms")
                print(f"    Init time:       {total_init*1000:>10.0f} ms  ({total_init/total_time*100:>5.1f}%)")
                print(f"    Loop time:       {total_loop*1000:>10.0f} ms  ({total_loop/total_time*100:>5.1f}%)")
                print(f"    Nodes expanded:  {total_expanded:>10d}")
                print(f"    Nodes pushed:    {total_pushed:>10d}")
                print(f"    Heap pops:       {total_pops:>10d}")
                print(f"    Skipped closed:  {total_skipped:>10d}  ({total_skipped/(total_pops or 1)*100:.1f}%)")
                if total_expanded > 0:
                    print(f"    µs per expand:   {total_loop/total_expanded*1e6:>10.1f}")
                    print(f"    µs per push:     {total_loop/total_pushed*1e6:>10.1f}")

                print(f"\n  TOP 15 SLOWEST CALLS:")
                print(f"    {'#':>3s} {'TYPE':<8s} {'TOTAL':>7s} {'INIT':>6s} {'LOOP':>7s}"
                      f" {'EXPAND':>7s} {'PUSH':>7s}"
                      f" {'POPS':>7s} {'SKIP':>5s} {'TREE':>5s} {'SRC':>5s} {'PATH':>4s}")
                print(f"    {'-'*90}")
                for i, s in enumerate(sorted(real_calls, key=lambda x: -x["total_time"])[:15]):
                    print(f"    {i+1:>3d} {s['type']:<8s}"
                          f" {s['total_time']*1000:>6.0f}ms"
                          f" {s['init_time']*1000:>5.0f}ms"
                          f" {s['loop_time']*1000:>6.0f}ms"
                          f" {s['expanded']:>7d}"
                          f" {s['pushed']:>7d}"
                          f" {s['heap_pops']:>7d}"
                          f" {s['skipped_closed']:>5d}"
                          f" {s['tree_size']:>5d}"
                          f" {s['source_size']:>5d}"
                          f" {s['path_len']:>4d}")

                overlaps = [s for s in call_stats if s["type"] == "overlap"]
                print(f"\n  Overlap (instant) calls: {len(overlaps)}")

            print(f"{'='*90}")
        finally:
            pf_mod.find_path_to_tree = orig_fpt
            sol_mod.find_path_to_tree = sol_orig_fpt


if __name__ == "__main__":
    unittest.main()
