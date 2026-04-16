"""ScadFragment — the universal geometry contribution type.

Every component resolver, trace builder, and pinhole builder produces a
list of ScadFragment objects.  The emitter collects them all and assembles
the final OpenSCAD source.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ── Geometry descriptors ───────────────────────────────────────────


@dataclass
class RectGeometry:
    """Axis-aligned rectangle centred on (cx, cy)."""
    cx: float
    cy: float
    width: float
    height: float

    def to_polygon(self) -> list[list[float]]:
        hw, hh = self.width / 2, self.height / 2
        return [
            [self.cx - hw, self.cy - hh],
            [self.cx + hw, self.cy - hh],
            [self.cx + hw, self.cy + hh],
            [self.cx - hw, self.cy + hh],
        ]


@dataclass
class CylinderGeometry:
    """Circle centred on (cx, cy) with radius r."""
    cx: float
    cy: float
    r: float


@dataclass
class PolygonGeometry:
    """Arbitrary 2-D polygon as CCW vertex list, with optional holes."""
    points: list[list[float]]
    holes: list[list[list[float]]] | None = None


@dataclass
class SegmentGeometry:
    """Rectangle along a line segment with a given width."""
    x1: float
    y1: float
    x2: float
    y2: float
    width: float

    def to_polygon(self) -> list[list[float]]:
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        length = math.hypot(dx, dy)
        if length < 1e-9:
            hw = self.width / 2
            return [
                [self.x1 - hw, self.y1 - hw],
                [self.x1 + hw, self.y1 - hw],
                [self.x1 + hw, self.y1 + hw],
                [self.x1 - hw, self.y1 + hw],
            ]
        px = -dy / length * self.width / 2
        py = dx / length * self.width / 2
        return [
            [self.x1 - px, self.y1 - py],
            [self.x2 - px, self.y2 - py],
            [self.x2 + px, self.y2 + py],
            [self.x1 + px, self.y1 + py],
        ]


@dataclass
class CapsuleGeometry:
    """Convex hull of two circles — a stadium/capsule shape.

    Used for jumper endpoints that are offset from a component pin.
    The SCAD emitter renders this as ``hull() { circle A; circle B; }``.
    """
    x1: float
    y1: float
    r1: float
    x2: float
    y2: float
    r2: float


Geometry = RectGeometry | CylinderGeometry | PolygonGeometry | SegmentGeometry | CapsuleGeometry


# ── Fragment ───────────────────────────────────────────────────────


@dataclass
class ScadFragment:
    """A single geometry contribution to the enclosure SCAD.

    type:
      "cutout"   — subtracted from the shell body (difference()).
      "addition" — unioned onto the shell body (e.g. snap-fit tabs).

    The geometry field holds a typed descriptor.  The emitter converts
    each descriptor to the appropriate OpenSCAD primitive.
    """

    type: str                           # "cutout" | "addition"
    geometry: Geometry
    z_base: float                       # Z where the extrusion starts (mm)
    depth: float                        # extrusion height (mm)
    label: str = ""
    rotation_deg: float = 0.0           # rotation around geometry centre
    rotate_cx: float = 0.0             # rotation pivot X (world coords)
    rotate_cy: float = 0.0             # rotation pivot Y (world coords)
    tilt_deg: float = 0.0              # 3-D tilt (0 = upright, 90 = horizontal)
    tilt_length: float = 0.0           # pre-tilt extrusion length (body height)
    taper_scale: float = 0.0           # >0: linear_extrude scale at top (smooth funnel)
    rotate_3d: tuple[float, float, float] | None = None  # full [rx, ry, rz] rotation
    clip_half: str = ""                  # "top" or "bottom" — keep only one Z half


# ── Helpers ────────────────────────────────────────────────────────


def rotated_polygon(
    pts: list[list[float]],
    angle_deg: float,
    cx: float,
    cy: float,
) -> list[list[float]]:
    """Rotate a polygon by angle_deg around (cx, cy)."""
    if angle_deg == 0:
        return pts
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return [
        [cx + (px - cx) * cos_a - (py - cy) * sin_a,
         cy + (px - cx) * sin_a + (py - cy) * cos_a]
        for px, py in pts
    ]


def rotate_point(
    px: float, py: float, angle_deg: float,
) -> tuple[float, float]:
    """Rotate (px, py) around the origin."""
    if angle_deg == 0:
        return px, py
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return px * cos_a - py * sin_a, px * sin_a + py * cos_a
