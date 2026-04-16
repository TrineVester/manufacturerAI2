"""Tests for src/pipeline/scad/resolver.py — ComponentResolver fragment generation.

Coverage
--------
* Resistor (internal mount) — body pocket + pinholes + pin funnels.
* Tactile button (top mount) — cap hole through ceiling, body pocket,
  upper cavity, pinholes, and pin bridges for offset pins.
* Battery holder (bottom mount) — body pocket at cavity start, hatch
  floor opening and ledges (via scad features), pinholes.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.catalog.loader import _parse_component
from src.pipeline.config import CAVITY_START_MM, FLOOR_MM, CEILING_MM
from src.pipeline.design.models import Outline, OutlineVertex, Enclosure
from src.pipeline.placer.models import PlacedComponent
from src.pipeline.scad.fragment import (
    CylinderGeometry,
    PolygonGeometry,
    RectGeometry,
)
from src.pipeline.scad.resolver import ComponentResolver, ResolverContext

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"

ENCLOSURE_HEIGHT = 25.0
OUTLINE = Outline(points=[
    OutlineVertex(x=0,  y=0),
    OutlineVertex(x=60, y=0),
    OutlineVertex(x=60, y=120),
    OutlineVertex(x=0,  y=120),
])
ENCLOSURE = Enclosure(height_mm=ENCLOSURE_HEIGHT)
CEIL_START = ENCLOSURE_HEIGHT - CEILING_MM
CAVITY_DEPTH = CEIL_START - CAVITY_START_MM


def _constant_height(_x, _y, _outline, _enc):
    return ENCLOSURE_HEIGHT


def _load_catalog(name: str):
    path = CATALOG_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return _parse_component(data, source_file=str(path))


def _make_ctx(pause_z: float | None = None) -> ResolverContext:
    return ResolverContext(
        outline=OUTLINE,
        enclosure=ENCLOSURE,
        base_h=ENCLOSURE_HEIGHT,
        ceil_start=CEIL_START,
        cavity_depth=CAVITY_DEPTH,
        blended_height_fn=_constant_height,
        pause_z=pause_z,
    )


def _placed(catalog, *, instance_id: str, x: float, y: float,
            rotation: float = 0, style: str | None = None,
            pin_positions: dict | None = None,
            button_outline: list | None = None) -> PlacedComponent:
    return PlacedComponent(
        instance_id=instance_id,
        catalog_id=catalog.id,
        x_mm=x,
        y_mm=y,
        rotation_deg=rotation,
        mounting_style=style or catalog.mounting.style,
        pin_positions=pin_positions or {},
        button_outline=button_outline,
    )


def _labels(frags):
    return [f.label for f in frags]


# ── Resistor (internal mount) ────────────────────────────────────────


class TestResistorResolver(unittest.TestCase):
    """Resistor: internal mount, 2 pins, rect body, no cap/hatch."""

    @classmethod
    def setUpClass(cls):
        cls.cat = _load_catalog("resistor_axial")
        cls.placed = _placed(cls.cat, instance_id="r_1", x=30, y=60)
        cls.ctx = _make_ctx()
        cls.frags = ComponentResolver(cls.placed, cls.cat, cls.ctx).resolve()

    def test_has_body_pocket(self):
        pockets = [f for f in self.frags if "body pocket" in f.label]
        self.assertEqual(len(pockets), 1)

    def test_body_pocket_geometry_is_rect(self):
        pocket = next(f for f in self.frags if "body pocket" in f.label)
        self.assertIsInstance(pocket.geometry, RectGeometry)

    def test_body_pocket_dimensions(self):
        pocket = next(f for f in self.frags if "body pocket" in f.label)
        g = pocket.geometry
        self.assertAlmostEqual(g.width, self.cat.body.width_mm)
        self.assertAlmostEqual(g.height, self.cat.body.length_mm)

    def test_pinhole_count_matches_pins(self):
        shafts = [f for f in self.frags if f.label.startswith("pin r_1:")]
        self.assertEqual(len(shafts), len(self.cat.pins))

    def test_pin_funnel_count_matches_pins(self):
        funnels = [f for f in self.frags if "pin funnel" in f.label]
        self.assertEqual(len(funnels), len(self.cat.pins))

    def test_funnels_have_taper(self):
        for f in self.frags:
            if "pin funnel" in f.label:
                self.assertGreater(f.taper_scale, 1.0)

    def test_no_cap_or_hatch_fragments(self):
        labels = _labels(self.frags)
        self.assertFalse(any("cap" in l or "hatch" in l or "button" in l for l in labels))


# ── Tactile button (top mount) ──────────────────────────────────────


class TestButtonResolver(unittest.TestCase):
    """Tactile button: top mount with cap/actuator, 4 pins, pin groups."""

    @classmethod
    def setUpClass(cls):
        cls.cat = _load_catalog("tactile_button_6x6")
        cls.placed = _placed(
            cls.cat,
            instance_id="btn_1",
            x=30, y=70,
            pin_positions={
                "1": [26.5, 67.795],
                "2": [33.5, 67.795],
                "3": [26.5, 72.205],
                "4": [33.5, 72.205],
            },
        )
        cls.ctx = _make_ctx()
        cls.frags = ComponentResolver(cls.placed, cls.cat, cls.ctx).resolve()

    def test_has_button_hole(self):
        holes = [f for f in self.frags if "button hole" in f.label]
        self.assertEqual(len(holes), 1)

    def test_button_hole_is_cylinder(self):
        hole = next(f for f in self.frags if "button hole" in f.label)
        self.assertIsInstance(hole.geometry, CylinderGeometry)

    def test_button_hole_at_ceiling(self):
        hole = next(f for f in self.frags if "button hole" in f.label)
        self.assertAlmostEqual(hole.z_base, CEIL_START)

    def test_has_body_pocket(self):
        pockets = [f for f in self.frags if "body pocket" in f.label]
        self.assertEqual(len(pockets), 1)

    def test_has_upper_cavity(self):
        upper = [f for f in self.frags if "upper cavity" in f.label]
        self.assertEqual(len(upper), 1)

    def test_upper_cavity_connects_body_to_ceiling(self):
        pocket = next(f for f in self.frags if "body pocket" in f.label)
        upper = next(f for f in self.frags if "upper cavity" in f.label)
        body_top = pocket.z_base + pocket.depth
        self.assertAlmostEqual(upper.z_base, body_top, places=2)
        self.assertAlmostEqual(upper.z_base + upper.depth, CEIL_START, places=2)

    def test_pinhole_count(self):
        shafts = [f for f in self.frags if f.label.startswith("pin btn_1:")]
        self.assertEqual(len(shafts), 4)

    def test_pin_funnel_count(self):
        funnels = [f for f in self.frags if "pin funnel" in f.label]
        self.assertEqual(len(funnels), 4)

    def test_no_pin_bridges(self):
        bridges = [f for f in self.frags if "pin bridge" in f.label]
        self.assertEqual(len(bridges), 0)

    def test_no_hatch_fragments(self):
        labels = _labels(self.frags)
        self.assertFalse(any("hatch" in l for l in labels))


# ── Battery holder (bottom mount) ───────────────────────────────────


class TestBatteryResolver(unittest.TestCase):
    """Battery holder: bottom mount with hatch features, 2 large rectangular pins."""

    @classmethod
    def setUpClass(cls):
        cls.cat = _load_catalog("battery_holder_2xAAA")
        cls.placed = _placed(
            cls.cat,
            instance_id="bat_1",
            x=30, y=40,
            pin_positions={
                "V+":  [37.085, 66.0],
                "GND": [22.915, 66.0],
            },
        )
        cls.ctx = _make_ctx()
        cls.frags = ComponentResolver(cls.placed, cls.cat, cls.ctx).resolve()

    def test_has_channel_pocket(self):
        pockets = [f for f in self.frags if "channel pocket" in f.label]
        self.assertEqual(len(pockets), 1)

    def test_channel_pocket_depth_is_center_z(self):
        ch = self.cat.body.channels
        pocket = next(f for f in self.frags if "channel pocket" in f.label)
        self.assertAlmostEqual(pocket.depth, ch.center_z_mm)

    def test_has_cell_channels(self):
        channels = [f for f in self.frags if "cell channel" in f.label]
        self.assertEqual(len(channels), 2)

    def test_channels_are_half_cylinders(self):
        channel = next(f for f in self.frags if "cell channel 1" in f.label)
        self.assertEqual(channel.clip_half, "top")

    def test_channel_depth_is_cell_length(self):
        ch = self.cat.body.channels
        channel = next(f for f in self.frags if "cell channel 1" in f.label)
        self.assertAlmostEqual(channel.depth, ch.length_mm)

    def test_no_channel_access_opening(self):
        access = [f for f in self.frags if "channel access opening" in f.label]
        self.assertEqual(len(access), 0)

    def test_has_hatch_opening(self):
        openings = [f for f in self.frags if "floor opening" in f.label]
        self.assertEqual(len(openings), 1)

    def test_hatch_opening_is_rect(self):
        opening = next(f for f in self.frags if "floor opening" in f.label)
        self.assertIsInstance(opening.geometry, RectGeometry)

    def test_has_hatch_ledges(self):
        ledges = [f for f in self.frags if "hatch ledge" in f.label]
        self.assertEqual(len(ledges), 2)

    def test_pin_shafts_penetrate_below_trace(self):
        shafts = [f for f in self.frags if f.label.startswith("pin bat_1:")]
        self.assertEqual(len(shafts), len(self.cat.pins))
        for s in shafts:
            self.assertLess(s.z_base, FLOOR_MM)

    def test_pin_funnel_count(self):
        funnels = [f for f in self.frags if "pin funnel" in f.label]
        self.assertEqual(len(funnels), len(self.cat.pins))

    def test_no_cap_or_button_fragments(self):
        labels = _labels(self.frags)
        self.assertFalse(any("cap" in l or "button" in l for l in labels))

    def test_no_pin_bridges(self):
        bridges = [f for f in self.frags if "pin bridge" in f.label]
        self.assertEqual(len(bridges), 0)


# ── Cross-component sanity ──────────────────────────────────────────


class TestResolverSanity(unittest.TestCase):
    """Shared invariants that hold for all components."""

    @classmethod
    def setUpClass(cls):
        ctx = _make_ctx()
        cls.components = {}
        for name, iid, x, y, pins in [
            ("resistor_axial", "r_1", 30, 60, {}),
            ("tactile_button_6x6", "btn_1", 30, 70,
             {"1": [26.5, 67.795], "2": [33.5, 67.795],
              "3": [26.5, 72.205], "4": [33.5, 72.205]}),
            ("battery_holder_2xAAA", "bat_1", 30, 40,
             {"V+": [37.085, 66.0], "GND": [22.915, 66.0]}),
        ]:
            cat = _load_catalog(name)
            placed = _placed(cat, instance_id=iid, x=x, y=y, pin_positions=pins)
            cls.components[name] = ComponentResolver(placed, cat, ctx).resolve()

    def test_all_fragments_are_cutouts(self):
        for name, frags in self.components.items():
            for f in frags:
                self.assertEqual(f.type, "cutout", f"{name}: {f.label} is not cutout")

    def test_all_depths_positive(self):
        for name, frags in self.components.items():
            for f in frags:
                self.assertGreater(f.depth, 0, f"{name}: {f.label} has non-positive depth")

    def test_all_labels_non_empty(self):
        for name, frags in self.components.items():
            for f in frags:
                self.assertTrue(f.label, f"{name}: fragment has empty label")

    def test_funnel_taper_above_one(self):
        for name, frags in self.components.items():
            for f in frags:
                if "funnel" in f.label:
                    self.assertGreater(f.taper_scale, 1.0,
                                       f"{name}: {f.label} taper_scale not >1")

    def test_shaft_no_taper(self):
        for name, frags in self.components.items():
            for f in frags:
                if f.label.startswith("pin ") and "funnel" not in f.label and "bridge" not in f.label:
                    self.assertEqual(f.taper_scale, 0.0,
                                     f"{name}: {f.label} shaft should have no taper")


if __name__ == "__main__":
    unittest.main()
