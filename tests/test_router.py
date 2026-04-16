"""Tests for the trace router (Stage 4).

Uses the flashlight fixture as the primary test case:
  - 45×120mm rectangle outline
  - Battery (bottom, auto-placed), resistor (internal, auto-placed)
  - Button at (22.5, 70), LED at (22.5, 100) — UI-placed
  - 4 two-pin nets: VCC, BTN_GND, LED_DRIVE, GND

Validates:
  - All nets are routed (no failures)
  - All traces are Manhattan (only horizontal/vertical segments)
  - All trace waypoints are inside the outline
  - No dynamic pin allocation needed (no MCU)
  - Serialization round-trips correctly
"""

from __future__ import annotations

import json
import math
import unittest

from shapely.geometry import Polygon, Point

from src.catalog.loader import load_catalog
from src.pipeline.placer import place_components, FullPlacement
from src.pipeline.router import (
    Trace, RoutingResult,
    route_traces,
    routing_to_dict, parse_routing,
)
from src.pipeline.router.grid import RoutingGrid
from src.pipeline.router.pathfinder import find_path, find_path_to_tree
from tests.flashlight_fixture import make_flashlight_design


class TestRoutingGrid(unittest.TestCase):
    """Unit tests for the routing grid."""

    def setUp(self):
        self.poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
        self.grid = RoutingGrid(self.poly, resolution=1.0, edge_clearance=1.0)

    def test_grid_dimensions(self):
        """Grid covers the bounding box."""
        self.assertGreater(self.grid.width, 0)
        self.assertGreater(self.grid.height, 0)

    def test_interior_cells_free(self):
        """Cells well inside the polygon are free."""
        gx, gy = self.grid.world_to_grid(10.0, 10.0)
        self.assertTrue(self.grid.is_free(gx, gy))

    def test_edge_cells_blocked(self):
        """Cells near the polygon edge are blocked (edge clearance)."""
        gx, gy = self.grid.world_to_grid(0.2, 0.2)
        self.assertTrue(self.grid.is_blocked(gx, gy))

    def test_outside_cells_blocked(self):
        """Cells outside the polygon are blocked."""
        gx, gy = self.grid.world_to_grid(-5.0, -5.0)
        self.assertTrue(self.grid.is_blocked(gx, gy))

    def test_block_and_free_cell(self):
        """Blocking and freeing cells works."""
        gx, gy = self.grid.world_to_grid(10.0, 10.0)
        self.assertTrue(self.grid.is_free(gx, gy))
        self.grid.block_cell(gx, gy)
        self.assertTrue(self.grid.is_blocked(gx, gy))
        self.grid.free_cell(gx, gy)
        self.assertTrue(self.grid.is_free(gx, gy))

    def test_coordinate_round_trip(self):
        """World -> grid -> world round-trips approximately."""
        wx, wy = 7.3, 12.8
        gx, gy = self.grid.world_to_grid(wx, wy)
        wx2, wy2 = self.grid.grid_to_world(gx, gy)
        self.assertAlmostEqual(wx, wx2, delta=1.0)
        self.assertAlmostEqual(wy, wy2, delta=1.0)


class TestPathfinder(unittest.TestCase):
    """Unit tests for A* pathfinding."""

    def setUp(self):
        self.poly = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
        self.grid = RoutingGrid(self.poly, resolution=1.0, edge_clearance=1.5)

    def test_straight_path(self):
        """Two points on the same row find a straight path."""
        src = self.grid.world_to_grid(5.0, 15.0)
        snk = self.grid.world_to_grid(25.0, 15.0)
        path = find_path(self.grid, src, snk)
        self.assertIsNotNone(path)
        self.assertEqual(path[0], src)
        self.assertEqual(path[-1], snk)
        # All y-coords should be the same (straight horizontal)
        ys = {p[1] for p in path}
        self.assertEqual(len(ys), 1)

    def test_l_shaped_path(self):
        """Path between offset points uses valid 8-directional routing."""
        src = self.grid.world_to_grid(5.0, 5.0)
        snk = self.grid.world_to_grid(25.0, 25.0)
        path = find_path(self.grid, src, snk)
        self.assertIsNotNone(path)
        for i in range(1, len(path)):
            dx = abs(path[i][0] - path[i - 1][0])
            dy = abs(path[i][1] - path[i - 1][1])
            self.assertLessEqual(dx, 1, f"Jump >1 in x at step {i}")
            self.assertLessEqual(dy, 1, f"Jump >1 in y at step {i}")
            self.assertGreater(dx + dy, 0, f"Zero-length step at {i}")

    def test_path_around_obstacle(self):
        """Path routes around a blocked rectangle."""
        # Block a wall in the middle
        for y in range(5, 25):
            gx, gy = self.grid.world_to_grid(15.0, float(y))
            self.grid.block_cell(gx, gy)

        src = self.grid.world_to_grid(10.0, 15.0)
        snk = self.grid.world_to_grid(20.0, 15.0)
        path = find_path(self.grid, src, snk)
        self.assertIsNotNone(path)
        self.assertEqual(path[0], src)
        self.assertEqual(path[-1], snk)

    def test_no_path(self):
        """Returns None when no path exists."""
        # Block a complete wall
        for y in range(self.grid.height):
            gx = self.grid.width // 2
            self.grid.block_cell(gx, y)
        src = self.grid.world_to_grid(5.0, 15.0)
        snk = self.grid.world_to_grid(25.0, 15.0)
        path = find_path(self.grid, src, snk)
        self.assertIsNone(path)

    def test_path_to_tree(self):
        """Point-to-tree routing finds a path to any tree cell."""
        tree = {
            self.grid.world_to_grid(20.0, 10.0),
            self.grid.world_to_grid(20.0, 15.0),
            self.grid.world_to_grid(20.0, 20.0),
        }
        src = self.grid.world_to_grid(5.0, 15.0)
        path = find_path_to_tree(self.grid, src, tree)
        self.assertIsNotNone(path)
        self.assertEqual(path[0], src)
        self.assertIn(path[-1], tree)


class TestFlashlightRouting(unittest.TestCase):
    """Integration test: route the flashlight fixture end-to-end."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.design = make_flashlight_design()
        cls.placement = place_components(cls.design, cls.catalog)
        cls.result = route_traces(cls.placement, cls.catalog)

    def test_all_nets_routed(self):
        """All 4 nets should be routed successfully."""
        self.assertEqual(
            len(self.result.failed_nets), 0,
            f"Failed nets: {self.result.failed_nets}",
        )

    def test_four_nets_have_traces(self):
        """Should produce traces for all 4 nets."""
        routed_nets = {t.net_id for t in self.result.traces}
        expected = {"VCC", "BTN_GND", "LED_DRIVE", "GND"}
        self.assertEqual(routed_nets, expected)

    def test_traces_are_manhattan(self):
        """All trace segments should be horizontal, vertical, or 45-degree diagonal."""
        for trace in self.result.traces:
            for i in range(1, len(trace.path)):
                x1, y1 = trace.path[i - 1]
                x2, y2 = trace.path[i]
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                is_horizontal = dy < 0.01
                is_vertical = dx < 0.01
                is_diagonal = abs(dx - dy) < 0.01
                self.assertTrue(
                    is_horizontal or is_vertical or is_diagonal,
                    f"Invalid segment in {trace.net_id}: "
                    f"({x1:.1f},{y1:.1f}) -> ({x2:.1f},{y2:.1f})",
                )

    def test_traces_inside_outline(self):
        """All trace waypoints should be inside the outline polygon."""
        outline_poly = Polygon(self.placement.outline.vertices)
        # Buffer slightly for grid quantization tolerance
        buffered = outline_poly.buffer(1.0)
        for trace in self.result.traces:
            for x, y in trace.path:
                self.assertTrue(
                    buffered.contains(Point(x, y)),
                    f"Trace point ({x:.1f},{y:.1f}) in {trace.net_id} "
                    f"is outside the outline",
                )

    def test_no_dynamic_pin_assignments(self):
        """Flashlight has no MCU, so no dynamic pin assignments."""
        # The flashlight uses buttons with pin groups (A, B) that ARE
        # allocatable, so we may see assignments for those.  But there
        # should be no MCU gpio assignments.
        for key in self.result.pin_assignments:
            self.assertNotIn("gpio", key,
                             f"Unexpected gpio assignment: {key}")

    def test_result_ok_property(self):
        """The ok property should be True when all nets routed."""
        self.assertTrue(self.result.ok)

    def test_no_trace_crossings(self):
        """No two traces from different nets may share the same grid cell."""
        # Reconstruct grid cells from world-coordinate traces
        from src.pipeline.router.models import RouterConfig
        from src.pipeline.router.grid import RoutingGrid
        cfg = RouterConfig()
        res = cfg.grid_resolution_mm

        # Build outline polygon to get grid origin
        outline_poly = Polygon(self.placement.outline.vertices)
        xmin, ymin, _, _ = outline_poly.bounds

        # Use the same world-to-grid conversion as the router
        def w2g(wx: float, wy: float) -> tuple[int, int]:
            gx = int(round((wx - xmin) / res - 0.5))
            gy = int(round((wy - ymin) / res - 0.5))
            return (gx, gy)

        # Map every trace waypoint + interpolated cells to net IDs
        cell_owner: dict[tuple[int, int], str] = {}
        crossings: list[str] = []

        for trace in self.result.traces:
            net = trace.net_id
            for i in range(len(trace.path) - 1):
                x1, y1 = trace.path[i]
                x2, y2 = trace.path[i + 1]
                # Interpolate Manhattan segment
                if abs(x2 - x1) > 0.001:  # horizontal
                    step = res if x2 > x1 else -res
                    x = x1
                    while (step > 0 and x <= x2 + 0.001) or (step < 0 and x >= x2 - 0.001):
                        cell = w2g(x, y1)
                        existing = cell_owner.get(cell)
                        if existing is not None and existing != net:
                            crossings.append(
                                f"{net} crosses {existing} at cell {cell}"
                            )
                        else:
                            cell_owner[cell] = net
                        x += step
                else:  # vertical
                    step = res if y2 > y1 else -res
                    y = y1
                    while (step > 0 and y <= y2 + 0.001) or (step < 0 and y >= y2 - 0.001):
                        cell = w2g(x1, y)
                        existing = cell_owner.get(cell)
                        if existing is not None and existing != net:
                            crossings.append(
                                f"{net} crosses {existing} at cell {cell}"
                            )
                        else:
                            cell_owner[cell] = net
                        y += step

        self.assertEqual(crossings, [], f"Found {len(crossings)} crossings: {crossings[:5]}")

    def test_trace_channel_walls(self):
        """Between any two traces of different nets there must be a wall of
        empty cells at least as wide as the trace clearance, forming a
        physical channel between them."""
        from src.pipeline.router.models import RouterConfig
        cfg = RouterConfig()
        res = cfg.grid_resolution_mm
        min_dist_mm = cfg.trace_width_mm / 2 + cfg.trace_clearance_mm

        outline_poly = Polygon(self.placement.outline.vertices)
        xmin, ymin, _, _ = outline_poly.bounds

        def w2g(wx: float, wy: float) -> tuple[int, int]:
            gx = int(round((wx - xmin) / res - 0.5))
            gy = int(round((wy - ymin) / res - 0.5))
            return (gx, gy)

        cell_owner: dict[tuple[int, int], str] = {}
        for trace in self.result.traces:
            net = trace.net_id
            for i in range(len(trace.path) - 1):
                x1, y1 = trace.path[i]
                x2, y2 = trace.path[i + 1]
                if abs(x2 - x1) > 0.001:
                    step = res if x2 > x1 else -res
                    x = x1
                    while (step > 0 and x <= x2 + 0.001) or (step < 0 and x >= x2 - 0.001):
                        cell_owner[w2g(x, y1)] = net
                        x += step
                else:
                    step = res if y2 > y1 else -res
                    y = y1
                    while (step > 0 and y <= y2 + 0.001) or (step < 0 and y >= y2 - 0.001):
                        cell_owner[w2g(x1, y)] = net
                        y += step

        search_cells = int(math.ceil(min_dist_mm / res))

        violations: list[str] = []
        for (gx, gy), net in cell_owner.items():
            for dy in range(-search_cells, search_cells + 1):
                for dx in range(-search_cells, search_cells + 1):
                    if dx == 0 and dy == 0:
                        continue
                    dist_mm = math.hypot(dx * res, dy * res)
                    if dist_mm > min_dist_mm:
                        continue
                    neighbour = (gx + dx, gy + dy)
                    other = cell_owner.get(neighbour)
                    if other is not None and other != net:
                        violations.append(
                            f"{net} at {(gx, gy)} too close to {other} "
                            f"at {neighbour} ({dist_mm:.2f}mm < {min_dist_mm:.2f}mm)"
                        )

        self.assertEqual(
            violations, [],
            f"Found {len(violations)} channel wall violations: "
            f"{violations[:5]}",
        )


class TestRoutingSerialization(unittest.TestCase):
    """Test JSON round-trip for routing results."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.design = make_flashlight_design()
        cls.placement = place_components(cls.design, cls.catalog)
        cls.result = route_traces(cls.placement, cls.catalog)

    def test_round_trip(self):
        """routing_to_dict -> parse_routing preserves data."""
        data = routing_to_dict(self.result)
        # Verify it's JSON-serializable
        json_str = json.dumps(data)
        data2 = json.loads(json_str)
        restored = parse_routing(data2)

        self.assertEqual(len(restored.traces), len(self.result.traces))
        self.assertEqual(restored.pin_assignments, self.result.pin_assignments)
        self.assertEqual(restored.failed_nets, self.result.failed_nets)

        for orig, rest in zip(self.result.traces, restored.traces):
            self.assertEqual(orig.net_id, rest.net_id)
            self.assertEqual(len(orig.path), len(rest.path))

    def test_dict_structure(self):
        """routing_to_dict produces expected keys."""
        data = routing_to_dict(self.result)
        self.assertIn("traces", data)
        self.assertIn("pin_assignments", data)
        self.assertIn("failed_nets", data)
        self.assertIsInstance(data["traces"], list)
        self.assertIsInstance(data["pin_assignments"], dict)
        self.assertIsInstance(data["failed_nets"], list)


if __name__ == "__main__":
    unittest.main()
