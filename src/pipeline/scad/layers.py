"""layers.py — generate OpenSCAD lines for the shell body.

Uses stacked ``polygon() + linear_extrude()`` layers — one for the straight
wall, plus thin layers for chamfer/fillet profiles on top and bottom edges.
Inset polygons are pre-computed in Python via Shapely so no SCAD ``offset()``
or ``hull()`` is needed; this keeps OpenSCAD's CGAL solver fast even with
hundreds of cutouts.

The ``flat_pts`` argument (from ``outline.tessellate_outline``) is the
Bézier-expanded 2-D footprint polygon — identical to the footprint used for
cutout placement, so the shell and cutouts are always perfectly aligned.
"""

from __future__ import annotations

import math
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.pipeline.design.models import Outline, Enclosure

from src.pipeline.design.models import Outline, Enclosure
from shapely.geometry import Polygon as _ShapelyPoly

log = logging.getLogger(__name__)

# Number of stacked layers per chamfer / fillet profile zone.
# More steps = smoother rounded edge; each adds one SCAD union child.
# 6 steps keeps chamfer quality acceptable for 3D printing while
# halving the number of shell layers (vs 12), which speeds up CGAL CSG.
_CURVE_STEPS = 6


# ── Shapely inset helper ───────────────────────────────────────────────────────


def _inset_polygon(
    pts: list[list[float]],
    inset: float,
) -> str | None:
    """Shrink *pts* inward by *inset* mm (Shapely mitre-buffer).

    Returns the formatted ``points`` string for an OpenSCAD ``polygon()``,
    or ``None`` if the inset collapses the polygon entirely.
    """
    if inset <= 0:
        return ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in pts)
    poly = _ShapelyPoly(pts)
    shrunk = poly.buffer(-inset, join_style="mitre", mitre_limit=5.0)
    if shrunk.is_empty:
        return None
    if shrunk.geom_type == "MultiPolygon":
        shrunk = max(shrunk.geoms, key=lambda g: g.area)
    coords = list(shrunk.exterior.coords)[:-1]
    return ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in coords)


# ── Public API ─────────────────────────────────────────────────────────────────


def shell_body_lines(
    outline:   Outline,
    enclosure: Enclosure,
    flat_pts:  list[list[float]],
    indent:    str = "",
) -> list[str]:
    """Return OpenSCAD lines for the shell body using stacked linear_extrude.

    The body is composed of three zones (all using the Bézier-expanded
    ``flat_pts`` polygon, with Shapely-pre-computed inset profiles):

    * **Bottom edge zone** — stacked thin layers with an inward-shrinking
      polygon, creating a chamfer or quarter-circle fillet on the bottom edge.
    * **Straight wall** — a single ``linear_extrude`` of the full-size polygon.
    * **Top edge zone** — stacked thin layers with an inward-shrinking polygon
      producing a chamfer or fillet on the top edge.

    All inset polygons are pre-computed in Python via Shapely; no OpenSCAD
    ``offset()`` or ``hull()`` is needed, which keeps CGAL compile time fast
    even with hundreds of cutouts.
    """
    h        = enclosure.height_mm
    edge_top = enclosure.edge_top
    edge_bot = enclosure.edge_bottom

    top_type = (edge_top.type    if edge_top else "none") or "none"
    top_size = (edge_top.size_mm if edge_top else 0.0)    or 0.0
    bot_type = (edge_bot.type    if edge_bot else "none") or "none"
    bot_size = (edge_bot.size_mm if edge_bot else 0.0)    or 0.0

    has_top = top_size > 0 and top_type in ("chamfer", "fillet")
    has_bot = bot_size > 0 and bot_type in ("chamfer", "fillet")

    # Full-size polygon string (used for straight wall and as base for insets)
    full_pts = ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in flat_pts)

    # ── Simple case: no edge profiles ─────────────────────────────────────────
    if not has_top and not has_bot:
        log.info("Shell body: plain extrude h=%.1f mm, %d verts", h, len(flat_pts))
        return [
            f"// Shell body — plain extrude, h={h:.1f} mm",
            f"linear_extrude(height = {h:.3f})",
            f"    polygon(points = [{full_pts}]);",
        ]

    # ── Stacked-layer helper ───────────────────────────────────────────────────
    def _zone_layers(
        z_base:       float,
        size:         float,
        profile_type: str,
        direction:    str,   # "bottom" or "top"
    ) -> list[str]:
        """Build stacked linear_extrude layers for one chamfer/fillet zone."""
        out: list[str] = []
        for i in range(_CURVE_STEPS):
            frac0 = i       / _CURVE_STEPS
            frac1 = (i + 1) / _CURVE_STEPS

            if direction == "bottom":
                # θ: π/2 → 0  (inset shrinks from size → 0 upward)
                theta0 = (1.0 - frac0) * (math.pi / 2)
                theta1 = (1.0 - frac1) * (math.pi / 2)
                if profile_type == "fillet":
                    inset0 = size * (1.0 - math.cos(theta0))
                    z0     = size * (1.0 - math.sin(theta0))
                    z1     = size * (1.0 - math.sin(theta1))
                else:  # chamfer — linear
                    inset0 = size * (1.0 - frac0)
                    z0     = size * frac0
                    z1     = size * frac1
            else:  # top
                # θ: 0 → π/2  (inset grows from 0 → size upward)
                theta0 = frac0 * (math.pi / 2)
                theta1 = frac1 * (math.pi / 2)
                if profile_type == "fillet":
                    inset0 = size * (1.0 - math.cos(theta0))
                    z0     = z_base + size * math.sin(theta0)
                    z1     = z_base + size * math.sin(theta1)
                else:  # chamfer
                    inset0 = size * frac0
                    z0     = z_base + size * frac0
                    z1     = z_base + size * frac1

            dz = z1 - z0
            if dz < 1e-6:
                continue

            p = _inset_polygon(flat_pts, inset0)
            if p is None:
                continue

            out += [
                f"    translate([0, 0, {z0:.4f}])",
                f"        linear_extrude(height = {dz:.4f})",
                f"            polygon(points = [{p}]);",
            ]
        return out

    # ── Assemble zones ─────────────────────────────────────────────────────────
    wall_z0 = bot_size if has_bot else 0.0
    wall_z1 = (h - top_size) if has_top else h
    wall_h  = wall_z1 - wall_z0

    lines: list[str] = [
        f"// Shell body — stacked-layer extrude, h={h:.1f} mm",
        f"// bottom={bot_type}({bot_size:.1f} mm)  top={top_type}({top_size:.1f} mm)",
        f"// {len(flat_pts)} footprint vertices, {_CURVE_STEPS} steps per profile",
        "union() {",
    ]

    if has_bot:
        lines.append(f"    // Bottom {bot_type} ({bot_size:.1f} mm)")
        lines += _zone_layers(0.0, bot_size, bot_type, "bottom")

    if wall_h > 0:
        lines += [
            f"    // Straight wall ({wall_z0:.3f} mm to {wall_z1:.3f} mm)",
            f"    translate([0, 0, {wall_z0:.4f}])",
            f"        linear_extrude(height = {wall_h:.4f})",
            f"            polygon(points = [{full_pts}]);",
        ]

    if has_top:
        lines.append(f"    // Top {top_type} ({top_size:.1f} mm)")
        lines += _zone_layers(wall_z1, top_size, top_type, "top")

    lines.append("}")

    n_layers = (
        (_CURVE_STEPS if has_bot else 0)
        + (1 if wall_h > 0 else 0)
        + (_CURVE_STEPS if has_top else 0)
    )
    log.info(
        "Shell body: %d layers, h=%.1f mm, %d footprint verts",
        n_layers, h, len(flat_pts),
    )
    return lines
