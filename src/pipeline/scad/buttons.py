"""buttons.py — generate printable custom button SCAD alongside the enclosure.

Each button is a separate OpenSCAD module placed next to the enclosure on the
build plate.  The button geometry snaps onto the switch actuator cylinder and
has a flat cap on top.

Button anatomy (bottom to top as printed):
    1. Socket   — ring that grips around the switch actuator cylinder
    2. Stem     — shaft extending up through the enclosure cavity & ceiling
    3. Cap      — flat visible top surface

The matching ceiling cutout is generated separately by the resolver using the
button outline (with clearance) so the button slides freely up and down.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass

from src.catalog.models import Body, Component, SwitchActuator
from src.pipeline.config import CAVITY_START_MM, CEILING_MM
from src.pipeline.design.models import Outline, Enclosure
from src.pipeline.placer.models import PlacedComponent

log = logging.getLogger(__name__)

# ── Tolerances ────────────────────────────────────────────────────

SOCKET_CLEARANCE_MM: float = 0.15   # radial gap between socket inner wall and cylinder
SOCKET_WALL_MM: float = 1.0         # wall thickness of the snap-on socket
BUTTON_CLEARANCE_MM: float = 0.3    # gap around button stem in the ceiling hole
LIP_WIDTH_MM: float = 1.0           # how far the cap extends beyond the hole
MIN_CAP_THICKNESS_MM: float = 1.5   # minimum cap thickness at thinnest edge
CIRCLE_FACETS: int = 32             # polygon resolution for circular outlines


@dataclass
class ButtonConfig:
    """All parameters needed to generate one custom button."""
    instance_id: str
    # World position of the button on the enclosure
    cx: float
    cy: float
    rotation_deg: float
    # Button cap outline (relative to button centre, in mm)
    outline: list[list[float]]
    # Stem outline (cap shrunk by lip width — passes through the ceiling hole)
    stem_outline: list[list[float]]
    # Switch actuator dimensions
    actuator: SwitchActuator
    # Enclosure geometry
    ceil_start: float               # Z where ceiling starts (enclosure.height_mm - CEILING_MM)


def _circle_polygon(radius: float, n: int = CIRCLE_FACETS) -> list[list[float]]:
    """Generate a regular polygon approximating a circle centred at origin."""
    return [
        [radius * math.cos(2 * math.pi * i / n),
         radius * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]


def _offset_polygon(pts: list[list[float]], offset: float) -> list[list[float]]:
    """Inset (negative offset) or outset (positive) a polygon using Shapely."""
    from shapely.geometry import Polygon as SPoly
    poly = SPoly(pts)
    buffered = poly.buffer(offset, join_style="mitre", mitre_limit=5.0)
    if buffered.is_empty:
        return pts  # fallback to original if offset collapses it
    if buffered.geom_type == "MultiPolygon":
        buffered = max(buffered.geoms, key=lambda g: g.area)
    return [[x, y] for x, y in list(buffered.exterior.coords)[:-1]]


def _body_polygon(body: Body) -> list[list[float]]:
    """Return the 2-D footprint of a component body as a point list."""
    if body.shape == "circle" and body.diameter_mm is not None:
        return _circle_polygon(body.diameter_mm / 2)
    w = body.width_mm or 0
    l = body.length_mm or 0
    hw, hl = w / 2, l / 2
    return [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]]


def _clamp_to_body(stem_pts: list[list[float]], body: Body) -> list[list[float]]:
    """Intersect *stem_pts* with the component body footprint.

    If the stem already fits inside the body, it is returned unchanged.
    Otherwise the intersection is returned so the stem never exceeds
    the body opening.
    """
    from shapely.geometry import Polygon as SPoly
    stem_poly = SPoly(stem_pts)
    body_poly = SPoly(_body_polygon(body))
    if body_poly.contains(stem_poly):
        return stem_pts
    clipped = stem_poly.intersection(body_poly)
    if clipped.is_empty:
        return _offset_polygon(_body_polygon(body), -BUTTON_CLEARANCE_MM)
    if clipped.geom_type == "MultiPolygon":
        clipped = max(clipped.geoms, key=lambda g: g.area)
    if hasattr(clipped, "exterior"):
        return [[x, y] for x, y in list(clipped.exterior.coords)[:-1]]
    return _offset_polygon(_body_polygon(body), -BUTTON_CLEARANCE_MM)


def build_button_configs(
    components: list[PlacedComponent],
    catalog_index: dict[str, Component],
    outline: Outline,
    enclosure: Enclosure,
    ceil_start: float,
) -> list[ButtonConfig]:
    """Build ButtonConfig objects for every placed component that has a switch
    actuator defined in its catalog cap."""
    configs: list[ButtonConfig] = []

    for comp in components:
        cat = catalog_index.get(comp.catalog_id)
        if cat is None:
            continue
        cap = cat.mounting.cap
        if cap is None or cap.actuator is None:
            continue

        # Determine button outline
        if comp.button_outline is not None:
            cap_outline = [list(p) for p in comp.button_outline]
        else:
            # Default: circle from cap diameter
            cap_outline = _circle_polygon(cap.diameter_mm / 2)

        # Stem must fit through the switch body opening
        stem_outline = _offset_polygon(_body_polygon(cat.body), -BUTTON_CLEARANCE_MM)

        configs.append(ButtonConfig(
            instance_id=comp.instance_id,
            cx=comp.x_mm,
            cy=comp.y_mm,
            rotation_deg=comp.rotation_deg,
            outline=cap_outline,
            stem_outline=stem_outline,
            actuator=cap.actuator,
            ceil_start=ceil_start,
        ))

    return configs


def _fmt_pts(pts: list[list[float]]) -> str:
    """Format a point list for OpenSCAD polygon()."""
    return ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in pts)


def generate_button_scad(config: ButtonConfig) -> list[str]:
    """Generate OpenSCAD lines for one printable custom button.

    The button is generated in local coordinates (centred at origin, base on
    z=0) and will be translated to its print position by the caller.
    """
    act = config.actuator
    lines: list[str] = []
    cid = config.instance_id

    # ── Dimensions ─────────────────────────────────────────────────
    socket_inner_r = act.cylinder_diameter_mm / 2 + SOCKET_CLEARANCE_MM
    socket_outer_r = socket_inner_r + SOCKET_WALL_MM
    socket_h = act.cylinder_height_mm

    # Distance from switch actuator top to ceiling bottom
    switch_top_z = CAVITY_START_MM + act.total_height_mm
    gap_to_ceiling = config.ceil_start - switch_top_z
    if gap_to_ceiling < 0:
        gap_to_ceiling = 0

    stem_height = gap_to_ceiling + CEILING_MM  # traverses from switch top through ceiling

    outline = config.outline
    cap_base_height = MIN_CAP_THICKNESS_MM

    # ── Socket ─────────────────────────────────────────────────────
    lines.append(f"// Custom button: {cid}")
    lines.append(f"// Socket (snaps onto switch cylinder Ø{act.cylinder_diameter_mm:.2f}mm)")
    lines.append(f"difference() {{")
    lines.append(f"  cylinder(h = {socket_h:.3f}, r = {socket_outer_r:.3f}, $fn = {CIRCLE_FACETS});")
    lines.append(f"  cylinder(h = {socket_h:.3f}, r = {socket_inner_r:.3f}, $fn = {CIRCLE_FACETS});")
    lines.append(f"}}")

    # ── Stem ───────────────────────────────────────────────────────
    stem_outline = config.stem_outline
    stem_pts_str = _fmt_pts(stem_outline)

    lines.append(f"// Stem (passes through cavity + ceiling)")
    lines.append(f"translate([0, 0, {socket_h:.3f}])")
    lines.append(f"  linear_extrude(height = {stem_height:.3f})")
    lines.append(f"    polygon(points = [{stem_pts_str}]);")

    # ── Cap (flat top) ─────────────────────────────────────────────
    cap_z_start = socket_h + stem_height
    cap_outline_str = _fmt_pts(outline)

    lines.append(f"// Cap")
    lines.append(f"translate([0, 0, {cap_z_start:.3f}])")
    lines.append(f"  linear_extrude(height = {cap_base_height:.3f})")
    lines.append(f"    polygon(points = [{cap_outline_str}]);")

    return lines


def generate_all_buttons_scad(
    configs: list[ButtonConfig],
    enclosure_max_x: float,
    enclosure_min_y: float,
) -> str:
    """Generate complete SCAD source for all custom buttons.

    Buttons are placed in a row to the right of the enclosure with 5mm spacing.
    Each button is centred at x = enclosure_max_x + spacing + button_radius.
    """
    if not configs:
        return ""

    spacing = 5.0  # gap between enclosure edge and first button
    button_gap = 3.0  # gap between buttons

    parts: list[str] = []
    parts.append("")
    parts.append("// ============================================================")
    parts.append("// Custom buttons — printed next to the enclosure")
    parts.append("// ============================================================")
    parts.append("")

    current_x = enclosure_max_x + spacing

    for cfg in configs:
        # Compute button footprint radius for placement
        outline = cfg.outline
        if outline:
            btn_radius = max(math.hypot(p[0], p[1]) for p in outline)
        else:
            btn_radius = 5.0

        place_x = current_x + btn_radius
        place_y = enclosure_min_y

        btn_lines = generate_button_scad(cfg)

        parts.append(f"// Button for {cfg.instance_id}")
        parts.append(f"translate([{place_x:.3f}, {place_y:.3f}, 0]) {{")
        for line in btn_lines:
            parts.append(f"  {line}")
        parts.append("}")
        parts.append("")

        current_x = place_x + btn_radius + button_gap

    return "\n".join(parts)


def button_cutout_outline(config: ButtonConfig) -> list[list[float]]:
    """Return the 2D outline for the ceiling cutout hole (stem + clearance).

    The cutout is the stem outline offset outward by BUTTON_CLEARANCE_MM,
    positioned in world coordinates (translated to the button's cx, cy).
    """
    stem = config.stem_outline
    clearance_outline = _offset_polygon(stem, BUTTON_CLEARANCE_MM)

    # Translate to world position
    cx, cy = config.cx, config.cy
    return [[p[0] + cx, p[1] + cy] for p in clearance_outline]
