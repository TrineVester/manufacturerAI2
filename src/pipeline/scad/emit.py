"""emit.py — assemble shell-body lines and ScadFragments into a .scad file.

The final SCAD structure::

    difference() {
        union() {
            // shell body
            // addition fragments
        }
        // cutout fragments (merged by z-layer)
    }
"""

from __future__ import annotations

import math
import logging
from collections import defaultdict
from datetime import datetime

from shapely.geometry import MultiPolygon as _SMultiPoly
from shapely.geometry import Point as _SPoint
from shapely.geometry import Polygon as _SPoly
from shapely.ops import unary_union

from .fragment import (
    ScadFragment, RectGeometry, CylinderGeometry,
    PolygonGeometry, SegmentGeometry, CapsuleGeometry,
)

log = logging.getLogger(__name__)


# ── Geometry → polygon conversion ─────────────────────────────────


def _cylinder_to_polygon(cg: CylinderGeometry, fn: int = 16) -> list[list[float]]:
    """Approximate a cylinder as a regular polygon with *fn* sides."""
    return [
        [cg.cx + cg.r * math.cos(2 * math.pi * i / fn),
         cg.cy + cg.r * math.sin(2 * math.pi * i / fn)]
        for i in range(fn)
    ]


def _capsule_to_polygon(cg: CapsuleGeometry, fn: int = 16) -> list[list[float]]:
    """Approximate hulled two-circle capsule as a polygon via Shapely."""
    c1 = _SPoint(cg.x1, cg.y1).buffer(cg.r1, resolution=fn // 4)
    c2 = _SPoint(cg.x2, cg.y2).buffer(cg.r2, resolution=fn // 4)
    hull = c1.union(c2).convex_hull
    coords = list(hull.exterior.coords)[:-1]
    return [[x, y] for x, y in coords]


def _fragment_to_polygon(frag: ScadFragment) -> list[list[float]] | None:
    """Convert a fragment's geometry to a polygon point list."""
    g = frag.geometry
    if isinstance(g, CylinderGeometry):
        return _cylinder_to_polygon(g)
    if isinstance(g, CapsuleGeometry):
        return _capsule_to_polygon(g)
    if isinstance(g, RectGeometry):
        return g.to_polygon()
    if isinstance(g, SegmentGeometry):
        return g.to_polygon()
    if isinstance(g, PolygonGeometry):
        return g.points
    return None


# ── Shapely merge (same z-layer optimization) ─────────────────────


def _merge_polygon_fragments(
    fragments: list[ScadFragment],
    outline_pts: list[list[float]] | None,
) -> list[tuple[str, list[str]]]:
    """Group polygon fragments by (z_base, depth), merge, clip, simplify."""
    outline_poly: _SPoly | None = None
    if outline_pts and len(outline_pts) >= 3:
        try:
            p = _SPoly(outline_pts)
            outline_poly = p if p.is_valid else p.buffer(0)
        except Exception:
            pass

    groups: dict[tuple[float, float], list[ScadFragment]] = defaultdict(list)
    for f in fragments:
        key = (round(f.z_base, 3), round(f.depth, 3))
        groups[key].append(f)

    results: list[tuple[str, list[str]]] = []

    for (z_base, depth), members in sorted(groups.items()):
        cats = sorted({m.label.split("\u2014")[0].split("—")[0].strip()
                       for m in members if m.label})
        label_str = ", ".join(cats[:4])
        if len(cats) > 4:
            label_str += f" (+{len(cats) - 4} more)"

        shapely_polys: list[_SPoly] = []
        for m in members:
            pts = _fragment_to_polygon(m)
            if pts is None or len(pts) < 3:
                continue
            # Carry over holes from PolygonGeometry if present
            holes = None
            if isinstance(m.geometry, PolygonGeometry) and m.geometry.holes:
                holes = [h for h in m.geometry.holes if len(h) >= 3]
            try:
                sp = _SPoly(pts, holes or [])
                if not sp.is_valid:
                    sp = sp.buffer(0)
                if sp.is_valid and not sp.is_empty:
                    shapely_polys.append(sp)
            except Exception:
                pass

        if not shapely_polys:
            continue

        merged = unary_union(shapely_polys)

        if outline_poly is not None and not merged.is_empty:
            try:
                clip = outline_poly.buffer(0.01, join_style="mitre", mitre_limit=5.0)
                merged = merged.intersection(clip)
            except Exception:
                pass

        if merged.is_empty:
            continue

        try:
            merged = merged.simplify(0.05, preserve_topology=True)
        except Exception:
            pass

        if not merged.is_valid:
            merged = merged.buffer(0)

        if isinstance(merged, _SPoly):
            geoms: list[_SPoly] = [merged]
        elif isinstance(merged, _SMultiPoly):
            geoms = list(merged.geoms)
        else:
            try:
                geoms = [g for g in merged.geoms if isinstance(g, _SPoly)]
            except Exception:
                continue

        if not geoms:
            continue

        # Round coordinates to output precision and re-validate.
        # Shapely may consider a polygon valid at float64 precision while
        # the 3dp-rounded version self-intersects.
        def _round_poly(p: _SPoly) -> _SPoly | None:
            """Round coords to 3dp and repair until valid or give up."""
            for _attempt in range(4):
                ext = [(round(x, 3), round(y, 3)) for x, y in p.exterior.coords]
                holes = [
                    [(round(x, 3), round(y, 3)) for x, y in h.coords]
                    for h in p.interiors
                ]
                try:
                    p = _SPoly(ext, holes)
                except Exception:
                    return None
                if p.is_valid:
                    return p
                repaired = p.buffer(0)
                if repaired.is_empty:
                    return None
                if isinstance(repaired, _SMultiPoly):
                    return repaired
                p = repaired
            return None

        rounded_geoms: list[_SPoly] = []
        for poly in geoms:
            result = _round_poly(poly)
            if result is None:
                continue
            if isinstance(result, _SMultiPoly):
                for part in result.geoms:
                    rp = _round_poly(part)
                    if rp is None or isinstance(rp, _SMultiPoly):
                        continue
                    if not rp.is_empty:
                        rounded_geoms.append(rp)
            elif not result.is_empty:
                rounded_geoms.append(result)

        if not rounded_geoms:
            continue

        all_pts: list[tuple[float, float]] = []
        paths: list[list[int]] = []
        for poly in rounded_geoms:
            ext = list(poly.exterior.coords)[:-1]
            start = len(all_pts)
            all_pts.extend(ext)
            paths.append(list(range(start, start + len(ext))))
            for hole in poly.interiors:
                hc = list(hole.coords)[:-1]
                h_start = len(all_pts)
                all_pts.extend(hc)
                paths.append(list(range(h_start, h_start + len(hc))))

        pts_str = ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in all_pts)
        paths_str = ", ".join(
            "[" + ", ".join(str(i) for i in p) + "]" for p in paths
        )

        comment = (
            f"  // z={z_base:.2f} d={depth:.2f}  "
            f"{len(members)} fragments \u2192 {len(geoms)} polygon(s)  "
            f"{len(all_pts)} verts  [{label_str}]"
        )
        _EPS = 0.001
        scad_lines = [
            f"  translate([0, 0, {z_base - _EPS:.3f}])",
            f"    linear_extrude(height = {depth + 2 * _EPS:.3f})",
            f"      polygon(points = [{pts_str}], paths = [{paths_str}]);",
        ]
        results.append((comment, scad_lines))

    return results


# ── Helpers ────────────────────────────────────────────────────────


def _indent(lines: list[str], prefix: str) -> list[str]:
    return [prefix + line for line in lines]


# ── Public API ─────────────────────────────────────────────────────


def generate_scad(
    shell_body_lines: list[str],
    fragments: list[ScadFragment],
    session_id: str = "",
    metadata: dict | None = None,
    outline_pts: list[list[float]] | None = None,
) -> str:
    """Return a complete OpenSCAD source string.

    Parameters
    ----------
    shell_body_lines : list[str]
        Lines produced by ``layers.shell_body_lines()``.
    fragments : list[ScadFragment]
        All geometry contributions (cutouts + additions).
    session_id : str
        Written into the header comment.
    metadata : dict, optional
        Extra key-value pairs for the header comment.
    outline_pts : list of [x, y], optional
        The 2-D enclosure footprint polygon for clipping.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    meta_str = ""
    if metadata:
        meta_str = "".join(f"//   {k}: {v}\n" for k, v in metadata.items())

    header = (
        "// ============================================================\n"
        "// manufacturerAI -- auto-generated enclosure\n"
        f"// Session  : {session_id}\n"
        f"// Generated: {now}\n"
        + meta_str
        + "// ============================================================\n"
        "\n"
        "$fn = 16;\n"
        "\n"
    )

    cutouts = [f for f in fragments if f.type == "cutout"]
    additions = [f for f in fragments if f.type == "addition"]

    # No cutouts and no additions — just emit the shell body
    if not cutouts and not additions:
        body = "\n".join(_indent(shell_body_lines, "  "))
        return header + f"mirror([0, 1, 0]) {{\n{body}\n}}\n"

    # Split cutouts: tilted go separate; everything else merges as polygons
    tilted_cuts: list[ScadFragment] = []
    polygon_cuts: list[ScadFragment] = []
    tapered_cuts: list[ScadFragment] = []
    for f in cutouts:
        if f.tilt_deg or f.rotate_3d:
            tilted_cuts.append(f)
        elif f.taper_scale:
            tapered_cuts.append(f)
        else:
            polygon_cuts.append(f)

    merged_groups = _merge_polygon_fragments(polygon_cuts, outline_pts)

    log.info(
        "Cutout merging: %d polygon → %d groups  |  %d tilted  |  %d additions",
        len(polygon_cuts), len(merged_groups), len(tilted_cuts), len(additions),
    )

    out_lines: list[str] = []

    has_cutouts = bool(cutouts)
    has_additions = bool(additions)

    if has_cutouts:
        out_lines.append("difference() {")
        out_lines.append("")

    # Shell body + additions wrapped in union()
    if has_additions:
        out_lines.append("  union() {")
        out_lines.append("    // --- Shell body ---")
        out_lines += _indent(shell_body_lines, "    ")
        out_lines.append("")
        out_lines.append("    // --- Additions ---")
        for a in additions:
            out_lines.append(f"    // {a.label}")
            out_lines += _indent(_fragment_scad_lines(a), "    ")
        out_lines.append("  }")
    else:
        prefix = "  " if has_cutouts else ""
        if has_cutouts:
            out_lines.append("  // --- Shell body ---")
        out_lines += _indent(shell_body_lines, prefix)

    if has_cutouts:
        out_lines.append("")
        out_lines.append("    // --- Polygon cutouts (merged by z-layer) ---")

        for comment, scad_lines in merged_groups:
            out_lines.append("")
            out_lines.append("    " + comment.lstrip())
            out_lines += _indent(scad_lines, "    ")

        if tapered_cuts:
            out_lines.append("")
            out_lines.append(f"    // --- Tapered cutouts ({len(tapered_cuts)}) ---")
            for c in tapered_cuts:
                if c.label:
                    out_lines.append(f"    // {c.label}")
                out_lines += _indent(_tapered_scad_lines(c), "    ")

        if tilted_cuts:
            out_lines.append("")
            out_lines.append(f"    // --- Tilted cutouts ({len(tilted_cuts)}) ---")
            for c in tilted_cuts:
                if c.label:
                    out_lines.append(f"    // {c.label}")
                out_lines += _indent(_tilted_scad_lines(c), "    ")

        out_lines += ["", "}"]     # close difference()

    body = "\n".join(_indent(out_lines, "  "))
    return header + f"mirror([0, 1, 0]) {{\n{body}\n}}\n"


def _tapered_scad_lines(frag: ScadFragment) -> list[str]:
    """Emit a smooth tapered extrusion (pinhole funnel).

    Uses ``linear_extrude(scale=...)`` so the shape at z_base is the
    narrow end and it widens to ``taper_scale`` at the top.
    """
    g = frag.geometry
    _EPS = 0.001
    scale = frag.taper_scale

    if isinstance(g, RectGeometry):
        return [
            f"translate([{g.cx:.3f}, {g.cy:.3f}, {frag.z_base - _EPS:.3f}])",
            f"  linear_extrude(height = {frag.depth + 2 * _EPS:.3f}, scale = [{scale:.4f}, {scale:.4f}])",
            f"    square([{g.width + 2 * _EPS:.3f}, {g.height + 2 * _EPS:.3f}], center = true);",
        ]
    elif isinstance(g, CylinderGeometry):
        r_top = g.r * scale
        return [
            f"translate([{g.cx:.3f}, {g.cy:.3f}, {frag.z_base - _EPS:.3f}])",
            f"  cylinder(h = {frag.depth + 2 * _EPS:.3f}, r1 = {g.r + _EPS:.3f}, r2 = {r_top + _EPS:.3f});",
        ]
    return [f"// unsupported tapered geometry: {frag.label}"]


def _tilted_scad_lines(frag: ScadFragment) -> list[str]:
    """Emit a fragment that is tilted (rotated in 3-D) around its centre."""
    g = frag.geometry
    _EPS = 0.001

    if frag.rotate_3d:
        rx, ry, rz = frag.rotate_3d
        z_center = frag.z_base
        length = frag.depth
        rot_str = f"rotate([{rx:.1f}, {ry:.1f}, {rz:.1f}])"
    else:
        z_center = frag.z_base + frag.depth / 2
        length = frag.tilt_length
        rot_str = f"rotate([0, {frag.tilt_deg:.1f}, {frag.rotation_deg:.1f}])"

    if isinstance(g, CylinderGeometry):
        base_lines = [
            f"translate([{g.cx:.3f}, {g.cy:.3f}, {z_center:.3f}])",
            f"  {rot_str}",
            f"    cylinder(h = {length + 2 * _EPS:.3f}, r = {g.r + _EPS:.3f}, center = true);",
        ]
    elif isinstance(g, RectGeometry):
        base_lines = [
            f"translate([{g.cx:.3f}, {g.cy:.3f}, {z_center:.3f}])",
            f"  {rot_str}",
            f"    linear_extrude(height = {length + 2 * _EPS:.3f}, center = true)",
            f"      square([{g.width + 2 * _EPS:.3f}, {g.height + 2 * _EPS:.3f}], center = true);",
        ]
    else:
        return [f"// unsupported tilted geometry: {frag.label}"]

    if frag.clip_half:
        big = max(length, g.r if isinstance(g, CylinderGeometry) else max(g.width, g.height)) + 10
        if frag.clip_half == "top":
            clip_z = z_center
        else:
            clip_z = z_center - big
        return [
            "intersection() {",
            *[f"  {l}" for l in base_lines],
            f"  translate([{g.cx - big:.3f}, {g.cy - big:.3f}, {clip_z:.3f}])",
            f"    cube([{2 * big:.3f}, {2 * big:.3f}, {big:.3f}]);",
            "}",
        ]

    return base_lines


def _fragment_scad_lines(frag: ScadFragment) -> list[str]:
    """Convert a single fragment to OpenSCAD lines (for additions or standalone use)."""
    if frag.tilt_deg or frag.rotate_3d:
        return _tilted_scad_lines(frag)
    g = frag.geometry
    if isinstance(g, CylinderGeometry):
        return [
            f"translate([{g.cx:.3f}, {g.cy:.3f}, {frag.z_base:.3f}])",
            f"  cylinder(h = {frag.depth:.3f}, r = {g.r:.3f});",
        ]
    pts = _fragment_to_polygon(frag)
    if pts and len(pts) >= 3:
        pts_str = ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in pts)
        return [
            f"translate([0, 0, {frag.z_base:.3f}])",
            f"  linear_extrude(height = {frag.depth:.3f})",
            f"    polygon(points = [{pts_str}]);",
        ]
    return [f"// empty fragment: {frag.label}"]
