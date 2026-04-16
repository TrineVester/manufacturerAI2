"""Shared pin geometry — single source of truth for pin pad dimensions.

Every pipeline stage that needs the physical size of a pin pad or its
SCAD cutout shaft imports from here instead of computing locally.
"""

from __future__ import annotations

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon

from src.catalog.models import Pin
from src.pipeline.config import TRACE_RULES


def pin_pad_dimensions(pin: Pin) -> tuple[str, float, float]:
    """Return (shape_type, width_mm, height_mm) for a pin's physical pad.

    Uses the catalog shape directly — no clearance added.
    """
    if pin.shape and pin.shape.type in ("rect", "slot"):
        w = pin.shape.width_mm or pin.hole_diameter_mm
        h = pin.shape.length_mm or pin.hole_diameter_mm
        return (pin.shape.type, w, h)
    return ("circle", pin.hole_diameter_mm, pin.hole_diameter_mm)


def pin_shaft_dimensions(
    pin: Pin,
    clearance_mm: float = TRACE_RULES.pinhole_clearance_mm,
) -> tuple[float, float]:
    """Pin pad + physical FDM tolerance = shaft hole size for SCAD/gcode."""
    _, w, h = pin_pad_dimensions(pin)
    return (w + clearance_mm, h + clearance_mm)


def pin_shaft_poly(
    pin: Pin,
    world_x: float,
    world_y: float,
    rotation_deg: float = 0,
    clearance_mm: float = TRACE_RULES.pinhole_clearance_mm,
) -> Polygon:
    """Shapely polygon for the physical shaft hole cut into the enclosure.

    This is the pin pad enlarged by ``pinhole_clearance_mm`` — the actual
    void in the plastic.  Use this (not :func:`pin_pad_poly`) when building
    inflation keepout zones so the wall thickness is measured from the
    shaft edge, not the smaller catalog pad.
    """
    shaft_w, shaft_h = pin_shaft_dimensions(pin, clearance_mm)
    return _make_pin_poly("rect", shaft_w, shaft_h, world_x, world_y, rotation_deg)


def pin_pad_poly(
    pin: Pin,
    world_x: float,
    world_y: float,
    rotation_deg: float = 0,
) -> Polygon:
    """Create a Shapely polygon for a pin pad at its world position.

    The polygon represents the raw pad footprint (no clearance buffer).
    """
    stype, w, h = pin_pad_dimensions(pin)
    return _make_pin_poly(stype, w, h, world_x, world_y, rotation_deg)


def _make_pin_poly(
    stype: str,
    w: float,
    h: float,
    world_x: float,
    world_y: float,
    rotation_deg: float,
) -> Polygon:
    if stype == "rect":
        hw, hh = w / 2, h / 2
        box = Polygon([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)])
        if rotation_deg:
            box = affinity.rotate(box, rotation_deg, origin=(0, 0))
        return affinity.translate(box, world_x, world_y)

    if stype == "slot":
        half_major = max(w, h) / 2
        half_minor = min(w, h) / 2
        line = LineString([
            (-half_major + half_minor, 0),
            (half_major - half_minor, 0),
        ])
        slot = line.buffer(half_minor, quad_segs=8)
        if rotation_deg:
            slot = affinity.rotate(slot, rotation_deg, origin=(0, 0))
        return affinity.translate(slot, world_x, world_y)

    return Point(world_x, world_y).buffer(w / 2, quad_segs=8)
