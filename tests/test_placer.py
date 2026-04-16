"""Tests for the component placer (Stage 3).

Uses the flashlight fixture as the primary test case:
  - 30×80mm rectangle outline
  - Button at (15, 45), LED at (15, 70) — UI-placed
  - Battery (25×48mm) and resistor (2.5×6.5mm) — auto-placed

Validates:
  - All components are placed
  - All placements are inside the outline
  - No overlaps (with keepout margins)
  - Net-connected components are reasonably close
  - Battery (bottom-mount) ends up near the bottom
  - Serialization round-trips correctly
"""

from __future__ import annotations

import json
import math
import unittest

from shapely.geometry import Polygon, box as shapely_box

from src.catalog.loader import load_catalog
from src.pipeline.design.models import (
    ComponentInstance, Net, OutlineVertex, Outline, UIPlacement, DesignSpec,
)
from src.pipeline.placer import (
    PlacedComponent,
    FullPlacement,
    PlacementError,
    place_components,
    placement_to_dict,
    parse_placement,
    parse_placed_components,
    footprint_halfdims,
    footprint_envelope_halfdims,
    pin_world_xy,
    aabb_gap,
    rect_inside_polygon,
)
from src.pipeline.placer.nets import build_net_graph, count_shared_nets, build_placement_groups, component_degree, net_fanout_map
from src.pipeline.placer.geometry import footprint_area
from src.pipeline.placer.models import ROUTING_CHANNEL_MM, MIN_PIN_CLEARANCE_MM
from tests.flashlight_fixture import make_flashlight_design


class TestPlacerGeometryHelpers(unittest.TestCase):
    """Unit tests for low-level geometry functions."""

    def testfootprint_halfdims_rect(self):
        """Rect body dimensions swap at 90°/270°."""
        from src.catalog.models import Component, Body, Mounting, Pin
        cat = Component(
            id="test", name="test", description="",
            ui_placement=False,
            body=Body(shape="rect", width_mm=6.0, length_mm=10.0, height_mm=3.0),
            mounting=Mounting(style="internal", allowed_styles=["internal"],
                              blocks_routing=False, keepout_margin_mm=1.0),
            pins=[],
        )
        self.assertEqual(footprint_halfdims(cat, 0), (3.0, 5.0))
        self.assertEqual(footprint_halfdims(cat, 90), (5.0, 3.0))
        self.assertEqual(footprint_halfdims(cat, 180), (3.0, 5.0))
        self.assertEqual(footprint_halfdims(cat, 270), (5.0, 3.0))

    def testfootprint_halfdims_circle(self):
        """Circle body is rotation-invariant."""
        from src.catalog.models import Component, Body, Mounting
        cat = Component(
            id="test", name="test", description="",
            ui_placement=True,
            body=Body(shape="circle", diameter_mm=8.0, height_mm=5.0),
            mounting=Mounting(style="top", allowed_styles=["top"],
                              blocks_routing=False, keepout_margin_mm=1.0),
            pins=[],
        )
        for rot in (0, 90, 180, 270):
            self.assertEqual(footprint_halfdims(cat, rot), (4.0, 4.0))

    def testpin_world_xy_no_rotation(self):
        wx, wy = pin_world_xy((3.0, 4.0), 10.0, 20.0, 0)
        self.assertAlmostEqual(wx, 13.0)
        self.assertAlmostEqual(wy, 24.0)

    def testpin_world_xy_90_rotation(self):
        wx, wy = pin_world_xy((3.0, 0.0), 10.0, 20.0, 90)
        self.assertAlmostEqual(wx, 10.0, places=5)
        self.assertAlmostEqual(wy, 23.0, places=5)

    def testaabb_gap_separated(self):
        # Two 2×2 boxes, 5mm apart horizontally
        gap = aabb_gap(0, 0, 1, 1, 6, 0, 1, 1)
        self.assertAlmostEqual(gap, 4.0)

    def testaabb_gap_touching(self):
        gap = aabb_gap(0, 0, 1, 1, 2, 0, 1, 1)
        self.assertAlmostEqual(gap, 0.0)

    def testaabb_gap_overlapping(self):
        gap = aabb_gap(0, 0, 2, 2, 1, 1, 2, 2)
        self.assertLess(gap, 0)

    def testrect_inside_polygon(self):
        poly = Polygon([(0, 0), (30, 0), (30, 80), (0, 80)])
        self.assertTrue(rect_inside_polygon(15, 40, 5, 5, poly))
        self.assertFalse(rect_inside_polygon(1, 1, 5, 5, poly))   # extends outside
        self.assertFalse(rect_inside_polygon(28, 40, 5, 5, poly))  # right edge out

    def test_footprint_envelope_larger_than_body(self):
        """Envelope includes pins that extend beyond the body."""
        from src.catalog.models import Component, Body, Mounting, Pin
        cat = Component(
            id="test_env", name="test", description="",
            ui_placement=False,
            body=Body(shape="rect", width_mm=6.5, length_mm=2.5, height_mm=2.5),
            mounting=Mounting(style="internal", allowed_styles=["internal"],
                              blocks_routing=False, keepout_margin_mm=1.0),
            pins=[
                Pin(id="1", label="L1", position_mm=(-5.0, 0),
                    direction="bidirectional", hole_diameter_mm=0.8,
                    description=""),
                Pin(id="2", label="L2", position_mm=(5.0, 0),
                    direction="bidirectional", hole_diameter_mm=0.8,
                    description=""),
            ],
        )
        # Body half-dims: (3.25, 1.25)
        body_hw, body_hh = footprint_halfdims(cat, 0)
        self.assertAlmostEqual(body_hw, 3.25)
        self.assertAlmostEqual(body_hh, 1.25)

        # Envelope must cover pins at ±5.0 + pad radius 0.4
        env_hw, env_hh = footprint_envelope_halfdims(cat, 0)
        self.assertAlmostEqual(env_hw, 5.4)   # 5.0 + 0.4
        self.assertGreaterEqual(env_hh, body_hh)

        # At 90° rotation the axes swap
        env_hw90, env_hh90 = footprint_envelope_halfdims(cat, 90)
        self.assertAlmostEqual(env_hh90, 5.4)
        self.assertGreaterEqual(env_hw90, body_hh)


class TestFlashlightPlacement(unittest.TestCase):
    """Integration test using the flashlight fixture."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.design = make_flashlight_design()
        cls.catalog_map = {c.id: c for c in cls.catalog.components}

    def test_placement_succeeds(self):
        """All 4 components should be placed without error."""
        result = place_components(self.design, self.catalog)
        self.assertIsInstance(result, FullPlacement)
        self.assertEqual(len(result.components), 4)

    def test_all_instance_ids_present(self):
        """Every component from the design appears in the placement."""
        result = place_components(self.design, self.catalog)
        placed_ids = {c.instance_id for c in result.components}
        design_ids = {c.instance_id for c in self.design.components}
        self.assertEqual(placed_ids, design_ids)

    def test_ui_components_at_specified_positions(self):
        """Button and LED should be at their UI-specified positions."""
        result = place_components(self.design, self.catalog)
        by_id = {c.instance_id: c for c in result.components}

        btn = by_id["btn_1"]
        self.assertAlmostEqual(btn.x_mm, 22.5)
        self.assertAlmostEqual(btn.y_mm, 70.0)

        led = by_id["led_1"]
        self.assertAlmostEqual(led.x_mm, 22.5)
        self.assertAlmostEqual(led.y_mm, 100.0)

    def test_all_inside_outline(self):
        """Every component envelope (body + pins) must lie inside the outline."""
        result = place_components(self.design, self.catalog)
        outline_poly = Polygon(self.design.outline.vertices)

        for pc in result.components:
            cat = self.catalog_map[pc.catalog_id]
            ehw, ehh = footprint_envelope_halfdims(cat, pc.rotation_deg)
            rect = shapely_box(
                pc.x_mm - ehw, pc.y_mm - ehh,
                pc.x_mm + ehw, pc.y_mm + ehh,
            )
            self.assertTrue(
                outline_poly.contains(rect),
                f"{pc.instance_id} at ({pc.x_mm}, {pc.y_mm}) envelope outside outline",
            )

    def test_no_overlaps(self):
        """No two component envelopes should overlap (respecting keepout)."""
        result = place_components(self.design, self.catalog)
        comps = result.components
        for i in range(len(comps)):
            ci = comps[i]
            cat_i = self.catalog_map[ci.catalog_id]
            ehw_i, ehh_i = footprint_envelope_halfdims(cat_i, ci.rotation_deg)
            ko_i = cat_i.mounting.keepout_margin_mm
            for j in range(i + 1, len(comps)):
                cj = comps[j]
                cat_j = self.catalog_map[cj.catalog_id]
                ehw_j, ehh_j = footprint_envelope_halfdims(cat_j, cj.rotation_deg)
                ko_j = cat_j.mounting.keepout_margin_mm
                gap = aabb_gap(
                    ci.x_mm, ci.y_mm, ehw_i, ehh_i,
                    cj.x_mm, cj.y_mm, ehw_j, ehh_j,
                )
                required = max(ko_i, ko_j)
                self.assertGreaterEqual(
                    gap, required - 0.01,
                    f"{ci.instance_id} and {cj.instance_id} envelopes overlap: "
                    f"gap={gap:.2f}mm < required={required:.2f}mm",
                )

    def test_battery_near_bottom(self):
        """Battery (bottom-mount) should be placed in the lower half."""
        result = place_components(self.design, self.catalog)
        bat = next(c for c in result.components if c.instance_id == "bat_1")
        # The outline is 0-120mm tall; battery should be in the lower third
        self.assertLess(
            bat.y_mm, 50.0,
            f"Battery at y={bat.y_mm:.1f}mm — expected near bottom (< 50mm)",
        )

    def test_valid_rotations(self):
        """All rotations must be 0, 90, 180, or 270."""
        result = place_components(self.design, self.catalog)
        for c in result.components:
            self.assertIn(c.rotation_deg, (0, 90, 180, 270),
                          f"{c.instance_id} has invalid rotation {c.rotation_deg}")

    def test_outline_and_nets_passed_through(self):
        """FullPlacement should pass through outline and nets unchanged."""
        result = place_components(self.design, self.catalog)
        self.assertEqual(result.outline, self.design.outline)
        self.assertEqual(result.nets, self.design.nets)

    def test_no_pin_collocation(self):
        """No pin from one component should be too close to another's pin."""
        result = place_components(self.design, self.catalog)
        comps = result.components
        for i in range(len(comps)):
            ci = comps[i]
            cat_i = self.catalog_map[ci.catalog_id]
            pins_i = [
                pin_world_xy(p.position_mm, ci.x_mm, ci.y_mm, ci.rotation_deg)
                for p in cat_i.pins
            ]
            for j in range(i + 1, len(comps)):
                cj = comps[j]
                cat_j = self.catalog_map[cj.catalog_id]
                pins_j = [
                    pin_world_xy(p.position_mm, cj.x_mm, cj.y_mm, cj.rotation_deg)
                    for p in cat_j.pins
                ]
                for pi_idx, (px, py) in enumerate(pins_i):
                    for pj_idx, (qx, qy) in enumerate(pins_j):
                        dist = math.hypot(px - qx, py - qy)
                        self.assertGreaterEqual(
                            dist, MIN_PIN_CLEARANCE_MM - 0.01,
                            f"{ci.instance_id}.{cat_i.pins[pi_idx].id} and "
                            f"{cj.instance_id}.{cat_j.pins[pj_idx].id} "
                            f"are only {dist:.2f}mm apart "
                            f"(min {MIN_PIN_CLEARANCE_MM}mm)",
                        )

    def test_routing_channel_gaps(self):
        """Connected components must have enough gap for trace channels."""
        result = place_components(self.design, self.catalog)
        net_graph = build_net_graph(self.design.nets)
        comps = result.components
        for i in range(len(comps)):
            ci = comps[i]
            cat_i = self.catalog_map[ci.catalog_id]
            ehw_i, ehh_i = footprint_envelope_halfdims(cat_i, ci.rotation_deg)
            for j in range(i + 1, len(comps)):
                cj = comps[j]
                cat_j = self.catalog_map[cj.catalog_id]
                ehw_j, ehh_j = footprint_envelope_halfdims(cat_j, cj.rotation_deg)
                n_ch = count_shared_nets(
                    ci.instance_id, cj.instance_id, net_graph,
                )
                if n_ch == 0:
                    continue
                gap = aabb_gap(
                    ci.x_mm, ci.y_mm, ehw_i, ehh_i,
                    cj.x_mm, cj.y_mm, ehw_j, ehh_j,
                )
                required = n_ch * ROUTING_CHANNEL_MM
                self.assertGreaterEqual(
                    gap, required - 0.01,
                    f"{ci.instance_id}-{cj.instance_id} need {n_ch} "
                    f"channel(s) ({required:.1f}mm) but gap={gap:.2f}mm",
                )

    def test_spread_uses_available_space(self):
        """Auto-placed components should spread out when ample space exists.

        The flashlight outline is 45×120mm = 5400mm².  Components use
        roughly 25% of that.  The minimum gap between any two auto-placed
        components should be well above the bare keepout minimum (2mm).
        """
        result = place_components(self.design, self.catalog)
        auto_ids = {"bat_1", "r_1"}
        auto_comps = [c for c in result.components if c.instance_id in auto_ids]
        self.assertEqual(len(auto_comps), 2)

        bat = next(c for c in auto_comps if c.instance_id == "bat_1")
        res = next(c for c in auto_comps if c.instance_id == "r_1")

        cat_bat = self.catalog_map[bat.catalog_id]
        cat_res = self.catalog_map[res.catalog_id]
        ehw_b, ehh_b = footprint_envelope_halfdims(cat_bat, bat.rotation_deg)
        ehw_r, ehh_r = footprint_envelope_halfdims(cat_res, res.rotation_deg)

        gap = aabb_gap(
            bat.x_mm, bat.y_mm, ehw_b, ehh_b,
            res.x_mm, res.y_mm, ehw_r, ehh_r,
        )
        # With spread preference they should not be crammed at the
        # bare minimum; expect at least 4mm gap (> keepout of 2mm).
        self.assertGreater(
            gap, 4.0,
            f"bat_1 and r_1 are too close (gap={gap:.1f}mm) — "
            f"spread preference should have used available space",
        )


class TestCountSharedNets(unittest.TestCase):
    """Unit tests for count_shared_nets."""

    def test_flashlight_nets(self):
        """In the flashlight, each adjacent pair shares exactly 1 net."""
        design = make_flashlight_design()
        net_graph = build_net_graph(design.nets)
        # bat_1 <-> btn_1 via VCC
        self.assertEqual(count_shared_nets("bat_1", "btn_1", net_graph), 1)
        # btn_1 <-> r_1 via BTN_GND
        self.assertEqual(count_shared_nets("btn_1", "r_1", net_graph), 1)
        # r_1 <-> led_1 via LED_DRIVE
        self.assertEqual(count_shared_nets("r_1", "led_1", net_graph), 1)
        # bat_1 <-> led_1 via GND
        self.assertEqual(count_shared_nets("bat_1", "led_1", net_graph), 1)
        # non-adjacent: btn_1 <-> led_1 — no direct net
        self.assertEqual(count_shared_nets("btn_1", "led_1", net_graph), 0)


class TestPlacementGroups(unittest.TestCase):
    """Tests for connectivity-based placement grouping."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.catalog_map = {c.id: c for c in cls.catalog.components}

    def test_flashlight_single_group(self):
        """All flashlight auto-placed components form one group."""
        design = make_flashlight_design()
        net_graph = build_net_graph(design.nets)
        ui_ids = {up.instance_id for up in design.ui_placements}
        auto_ids = [
            ci.instance_id for ci in design.components
            if ci.instance_id not in ui_ids
        ]
        area_map = {
            iid: footprint_area(self.catalog_map[
                next(ci.catalog_id for ci in design.components
                     if ci.instance_id == iid)
            ])
            for iid in auto_ids
        }
        groups = build_placement_groups(auto_ids, net_graph, area_map)
        # All auto-placed instances are in one connected component
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]), set(auto_ids))

    def test_disconnected_components_separate_groups(self):
        """Components with no shared nets form separate groups."""
        net_graph = build_net_graph([])  # No nets
        groups = build_placement_groups(
            ["a", "b", "c"], net_graph, {"a": 10, "b": 5, "c": 1}
        )
        # Three singletons, sorted by area descending
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0], ["a"])  # largest area first

    def test_component_degree(self):
        """Degree counts unique neighbours."""
        design = make_flashlight_design()
        net_graph = build_net_graph(design.nets)
        degrees = component_degree(net_graph)
        # bat_1 connects to btn_1 (VCC) and led_1 (GND)
        self.assertEqual(degrees["bat_1"], 2)
        # r_1 connects to btn_1 (BTN_GND) and led_1 (LED_DRIVE)
        self.assertEqual(degrees["r_1"], 2)

    def test_hub_placed_first_in_group(self):
        """Within a group, the highest-degree component comes first."""
        # Create a star topology: hub connects to a, b, c directly
        from src.pipeline.design.models import Net
        nets = [
            Net(id="n1", pins=["hub:p1", "a:p1"]),
            Net(id="n2", pins=["hub:p2", "b:p1"]),
            Net(id="n3", pins=["hub:p3", "c:p1"]),
        ]
        net_graph = build_net_graph(nets)
        area_map = {"hub": 100, "a": 10, "b": 10, "c": 10}
        groups = build_placement_groups(
            ["a", "b", "c", "hub"], net_graph, area_map,
        )
        self.assertEqual(len(groups), 1)
        # Hub (degree 3) should be first
        self.assertEqual(groups[0][0], "hub")


class TestBatteryNearEdge(unittest.TestCase):
    """Battery (large component) should be placed near an outline edge."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.catalog_map = {c.id: c for c in cls.catalog.components}
        cls.design = make_flashlight_design()
        cls.result = place_components(cls.design, cls.catalog)

    def test_battery_near_edge(self):
        """The battery's envelope should be within 6mm of some outline edge.

        Large components benefit from edge positions so traces don't
        have to route around them.
        """
        from src.pipeline.placer.geometry import rect_edge_clearance
        bat = next(c for c in self.result.components if c.instance_id == "bat_1")
        cat = self.catalog_map[bat.catalog_id]
        ehw, ehh = footprint_envelope_halfdims(cat, bat.rotation_deg)
        clearance = rect_edge_clearance(
            bat.x_mm, bat.y_mm, ehw, ehh,
            self.design.outline.vertices,
        )
        self.assertLessEqual(
            clearance, 6.0,
            f"Battery envelope is {clearance:.1f}mm from nearest edge — "
            f"large components should be placed near edges",
        )


class TestPinSideAwareness(unittest.TestCase):
    """Resistor should approach the button from the pin side."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.catalog_map = {c.id: c for c in cls.catalog.components}
        cls.design = make_flashlight_design()
        cls.result = place_components(cls.design, cls.catalog)

    def test_resistor_on_button_pin_side(self):
        """The resistor should be closer to the button's connecting pins
        than to the opposite side.

        btn_1's pin B connects to r_1.  The resistor should approach
        from the side of the button where pin B is.
        """
        btn = next(c for c in self.result.components if c.instance_id == "btn_1")
        res = next(c for c in self.result.components if c.instance_id == "r_1")
        btn_cat = self.catalog_map[btn.catalog_id]

        # Find btn_1's pin B world position
        pin_b = next(p for p in btn_cat.pins if p.id in ("3", "4"))  # B side
        pin_wx, pin_wy = pin_world_xy(
            pin_b.position_mm, btn.x_mm, btn.y_mm, btn.rotation_deg,
        )

        # Vector from button centre to its pin B
        pin_dx = pin_wx - btn.x_mm
        pin_dy = pin_wy - btn.y_mm

        # Vector from button centre to resistor
        res_dx = res.x_mm - btn.x_mm
        res_dy = res.y_mm - btn.y_mm

        # Dot product should be positive (same side)
        dot = pin_dx * res_dx + pin_dy * res_dy
        self.assertGreater(
            dot, 0,
            f"Resistor is on the wrong side of the button "
            f"(dot={dot:.1f}) — pin-side awareness should pull it "
            f"toward the connecting pin",
        )


class TestNetFanout(unittest.TestCase):
    """Tests for net fanout detection and high-fanout proximity boost."""

    def test_fanout_map_flashlight(self):
        """Flashlight nets all have fanout 2."""
        design = make_flashlight_design()
        fmap = net_fanout_map(design.nets)
        for net_id, fanout in fmap.items():
            self.assertEqual(fanout, 2, f"Net {net_id} has fanout {fanout}")

    def test_fanout_map_high_fanout_net(self):
        """A net spanning 4 instances should have fanout 4."""
        nets = [
            Net(id="GND", pins=["a:gnd", "b:gnd", "c:gnd", "d:gnd"]),
            Net(id="SIG", pins=["a:out", "b:in"]),
        ]
        fmap = net_fanout_map(nets)
        self.assertEqual(fmap["GND"], 4)
        self.assertEqual(fmap["SIG"], 2)

    def test_net_edge_carries_fanout(self):
        """NetEdge.fanout should reflect the net's instance count."""
        nets = [
            Net(id="GND", pins=["a:gnd", "b:gnd", "c:gnd"]),
        ]
        graph = build_net_graph(nets)
        # Every edge on the GND net should have fanout=3
        for iid in ("a", "b", "c"):
            for edge in graph[iid]:
                self.assertEqual(
                    edge.fanout, 3,
                    f"Edge {iid}->{edge.other_iid} has fanout "
                    f"{edge.fanout}, expected 3",
                )

    def test_high_fanout_components_cluster(self):
        """Components sharing a high-fanout net should be placed
        closer together than an equivalent set of 2-pin nets would.

        This test creates a 5-component circuit on a large board.
        Three components share a 3-pin GND net.  We verify that the
        average distance among those three is smaller than the
        average distance to the non-GND components.
        """
        design = DesignSpec(
            components=[
                ComponentInstance(catalog_id="resistor_axial", instance_id="r_1"),
                ComponentInstance(catalog_id="resistor_axial", instance_id="r_2"),
                ComponentInstance(catalog_id="resistor_axial", instance_id="r_3"),
                ComponentInstance(catalog_id="resistor_axial", instance_id="r_4"),
                ComponentInstance(catalog_id="resistor_axial", instance_id="r_5"),
            ],
            nets=[
                # 3-pin GND net
                Net(id="GND", pins=["r_1:1", "r_2:1", "r_3:1"]),
                # r_4 and r_5 have only pairwise connections
                Net(id="SIG_A", pins=["r_1:2", "r_4:1"]),
                Net(id="SIG_B", pins=["r_2:2", "r_5:1"]),
            ],
            outline=Outline(points=[
                OutlineVertex(x=0, y=0),
                OutlineVertex(x=80, y=0),
                OutlineVertex(x=80, y=80),
                OutlineVertex(x=0, y=80),
            ]),
            ui_placements=[],
        )
        from src.catalog.loader import load_catalog
        catalog = load_catalog()
        result = place_components(design, catalog)
        by_id = {c.instance_id: c for c in result.components}

        import math
        # Average distance among the GND cluster (r_1, r_2, r_3)
        gnd_ids = ["r_1", "r_2", "r_3"]
        gnd_dists = []
        for i in range(len(gnd_ids)):
            for j in range(i + 1, len(gnd_ids)):
                a, b = by_id[gnd_ids[i]], by_id[gnd_ids[j]]
                gnd_dists.append(math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm))
        avg_gnd = sum(gnd_dists) / len(gnd_dists)

        # Average distance from GND cluster to non-GND (r_4, r_5)
        other_ids = ["r_4", "r_5"]
        cross_dists = []
        for g in gnd_ids:
            for o in other_ids:
                a, b = by_id[g], by_id[o]
                cross_dists.append(math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm))
        avg_cross = sum(cross_dists) / len(cross_dists)

        self.assertLess(
            avg_gnd, avg_cross,
            f"GND cluster avg distance ({avg_gnd:.1f}mm) should be "
            f"less than cross-cluster ({avg_cross:.1f}mm) — "
            f"high-fanout nets should pull components together",
        )


class TestPlacementSerialization(unittest.TestCase):
    """Test placement serialization round-trip."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.design = make_flashlight_design()
        cls.placement = place_components(cls.design, cls.catalog)

    def test_to_dict(self):
        """placement_to_dict produces a valid JSON-serializable dict."""
        d = placement_to_dict(self.placement)
        json_str = json.dumps(d)
        self.assertIsInstance(json_str, str)
        self.assertIn("components", d)
        self.assertNotIn("outline", d)
        self.assertNotIn("nets", d)
        self.assertEqual(len(d["components"]), 4)

    def test_round_trip(self):
        """placement_to_dict -> parse_placed_components should preserve data."""
        d = placement_to_dict(self.placement)
        restored_comps = parse_placed_components(d)
        self.assertEqual(len(restored_comps), len(self.placement.components))
        for orig, rest in zip(self.placement.components, restored_comps):
            self.assertEqual(orig.instance_id, rest.instance_id)
            self.assertEqual(orig.catalog_id, rest.catalog_id)
            self.assertAlmostEqual(orig.x_mm, rest.x_mm, places=2)
            self.assertAlmostEqual(orig.y_mm, rest.y_mm, places=2)
            self.assertEqual(orig.rotation_deg, rest.rotation_deg)


class TestPlacementErrors(unittest.TestCase):
    """Test error handling for impossible placements."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()

    def test_component_too_large_for_outline(self):
        """A component bigger than the outline should raise PlacementError."""
        tiny_outline = DesignSpec(
            components=[
                ComponentInstance(
                    catalog_id="battery_holder_2xAAA",
                    instance_id="bat_1",
                ),
            ],
            nets=[],
            outline=Outline(points=[
                OutlineVertex(x=0, y=0),
                OutlineVertex(x=10, y=0),   # only 10mm wide
                OutlineVertex(x=10, y=10),  # only 10mm tall
                OutlineVertex(x=0, y=10),
            ]),
            ui_placements=[],
        )
        with self.assertRaises(PlacementError) as ctx:
            place_components(tiny_outline, self.catalog)
        self.assertIn("bat_1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
