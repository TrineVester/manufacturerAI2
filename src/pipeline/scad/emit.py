"""emit.py — assemble shell-body lines and cutouts into a .scad file string.

The final SCAD structure is::

    // header
    $fn = 32;

    difference() {
        // ─── Shell body ───
        union() { ... }          // from layers.shell_body_lines()

        // ─── Cutouts ──────
        translate([0,0,z])       // one block per Cutout
            linear_extrude(h)
                polygon(...);
    }

When the cutout list is empty the ``difference()`` wrapper is omitted and the
shell body union is emitted directly (no unnecessary CSG overhead in OpenSCAD).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from shapely.geometry import MultiPolygon as _SMultiPoly
from shapely.geometry import Polygon as _SPoly
from shapely.ops import unary_union

from .cutouts import Cutout

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────


def _poly_str(pts: list) -> str:
    """Format [[x,y],...] as an OpenSCAD points literal."""
    return ", ".join(f"[{float(x):.3f}, {float(y):.3f}]" for x, y in pts)


def _indent(lines: list[str], prefix: str) -> list[str]:
    return [prefix + line for line in lines]


# ── Shapely cutout merging ─────────────────────────────────────────


def _merge_polygon_cutouts(
    cutouts: list[Cutout],
    outline_pts: list[list[float]] | None,
) -> list[tuple[str, list[str]]]:
    """Group polygon cutouts by (z_base, depth), merge with Shapely unary_union,
    simplify vertices, clip to the enclosure outline, and return as OpenSCAD
    multi-path polygon lines.

    Returns a list of (comment_line, scad_lines) tuples — one per z/depth group.
    This typically reduces 150-200 individual difference() children down to 4-5,
    which is the primary factor in OpenSCAD CGAL compile time.
    """
    # Build outline clip polygon once
    outline_poly: _SPoly | None = None
    if outline_pts and len(outline_pts) >= 3:
        try:
            p = _SPoly(outline_pts)
            outline_poly = p if p.is_valid else p.buffer(0)
        except Exception:
            pass

    # Group by (z_base, depth) — round to 3 dp to treat near-identical values as equal
    groups: dict[tuple[float, float], list[Cutout]] = defaultdict(list)
    for cut in cutouts:
        key = (round(cut.z_base, 3), round(cut.depth, 3))
        groups[key].append(cut)

    results: list[tuple[str, list[str]]] = []

    for (z_base, depth), members in sorted(groups.items()):
        # Collect label categories for the comment
        cats = sorted({m.label.split("\u2014")[0].split("—")[0].strip()
                       for m in members if m.label})
        label_str = ", ".join(cats[:4])
        if len(cats) > 4:
            label_str += f" (+{len(cats) - 4} more)"

        # Convert each cutout polygon to a Shapely Polygon
        shapely_polys: list[_SPoly] = []
        for m in members:
            if len(m.polygon) < 3:
                continue
            try:
                sp = _SPoly(m.polygon)
                if not sp.is_valid:
                    sp = sp.buffer(0)
                if sp.is_valid and not sp.is_empty:
                    shapely_polys.append(sp)
            except Exception:
                pass

        if not shapely_polys:
            continue

        merged = unary_union(shapely_polys)

        # ── Clip to enclosure outline ──────────────────────────────
        # Prevents component pockets / trace channels from bleeding
        # through the outer shell walls.
        if outline_poly is not None and not merged.is_empty:
            try:
                merged = merged.intersection(outline_poly)
            except Exception:
                pass

        if merged.is_empty:
            continue

        # ── Simplify vertex count ──────────────────────────────────
        # 0.05 mm tolerance is sub-layer on any FDM printer and
        # invisible in the final part; can halve the vertex count of
        # merged trace+pocket polygons.
        try:
            merged = merged.simplify(0.05, preserve_topology=True)
        except Exception:
            pass

        # Collect individual Polygon objects
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

        # ── Build OpenSCAD multi-path polygon ─────────────────────
        # polygon(points=[...], paths=[[exterior], [hole], ...])
        # Each disconnected region and each hole is a separate path.
        # This collapses N separate polygon() calls into one primitive.
        all_pts: list[tuple[float, float]] = []
        paths: list[list[int]] = []

        for poly in geoms:
            ext = list(poly.exterior.coords)[:-1]
            start = len(all_pts)
            all_pts.extend(ext)
            paths.append(list(range(start, start + len(ext))))
            for hole in poly.interiors:
                hc = list(hole.coords)[:-1]
                h_start = len(all_pts)
                all_pts.extend(hc)
                paths.append(list(range(h_start, h_start + len(hc))))

        pts_str   = ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in all_pts)
        paths_str = ", ".join(
            "[" + ", ".join(str(i) for i in p) + "]" for p in paths
        )

        comment = (
            f"  // z={z_base:.2f} d={depth:.2f}  "
            f"{len(members)} cutouts \u2192 {len(geoms)} polygon(s)  "
            f"{len(all_pts)} verts  [{label_str}]"
        )
        scad_lines = [
            f"  translate([0, 0, {z_base:.3f}])",
            f"    linear_extrude(height = {depth:.3f})",
            f"      polygon(points = [{pts_str}], paths = [{paths_str}]);",
        ]
        results.append((comment, scad_lines))

    return results


# ── Public API ─────────────────────────────────────────────────────


def generate_scad(
    shell_body_lines: list[str],
    cutouts: list[Cutout],
    session_id: str = "",
    metadata: dict | None = None,
    outline_pts: list[list[float]] | None = None,
) -> str:
    """Return a complete OpenSCAD source string.

    Parameters
    ----------
    shell_body_lines : list[str]
        Lines produced by ``layers.shell_body_lines()`` — forms the
        ``union()`` solid body block.
    cutouts : list[Cutout]
        Cutouts to subtract.  May be empty (solid shell, no booleans).
    session_id : str
        Written into the header comment.
    metadata : dict, optional
        Extra key-value pairs for the header comment (e.g. component count).
    outline_pts : list of [x, y], optional
        The 2-D enclosure footprint polygon.  When provided, polygon cutouts
        are clipped to it (prevents pockets bleeding through walls).
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

    # ── No cutouts — just emit the shell body directly ─────────────
    if not cutouts:
        return header + "\n".join(shell_body_lines) + "\n"

    # ── Split cutouts: cylinders are already fast, polygons need merging ──
    cylinder_cuts = [c for c in cutouts if c.cylinder_r is not None]
    polygon_cuts  = [c for c in cutouts if c.cylinder_r is     None]

    # Merge polygon cutouts: group by (z_base, depth), unary_union per group,
    # simplify vertices, clip to outline, emit as multi-path polygons.
    merged_groups = _merge_polygon_cutouts(polygon_cuts, outline_pts)

    log.info(
        "Cutout merging: %d polygon cuts → %d groups  |  %d cylinder cuts",
        len(polygon_cuts), len(merged_groups), len(cylinder_cuts),
    )

    # ── With cutouts — wrap in difference() ───────────────────────
    out_lines: list[str] = ["difference() {", ""]

    # Shell body (indented by 2 spaces) — wrapped in render() so OpenSCAD
    # pre-computes the CGAL mesh once before subtracting all cutouts.
    out_lines.append("  // --- Shell body -------------------------------------------------")
    out_lines.append("  render(convexity = 10)")
    out_lines += _indent(shell_body_lines, "  ")
    out_lines.append("")

    # ── Merged polygon groups ──────────────────────────────────────
    # Each group is one difference() child (vs. one per original cutout).
    out_lines.append("  // --- Polygon cutouts (merged by z-layer) -----------------------")
    for comment, scad_lines in merged_groups:
        out_lines.append("")
        out_lines.append(comment)
        out_lines += scad_lines

    # ── Cylinder cutouts ───────────────────────────────────────────
    # Wrap all cylinders in one union() so they count as a single
    # difference() child instead of N separate ones.
    if cylinder_cuts:
        out_lines.append("")
        out_lines.append(f"  // --- Cylindrical holes ({len(cylinder_cuts)}) ----------------------------")
        if len(cylinder_cuts) == 1:
            c = cylinder_cuts[0]
            out_lines.append(f"  // {c.label}")
            out_lines += [
                f"  translate([{c.cylinder_cx:.3f}, {c.cylinder_cy:.3f}, {c.z_base:.3f}])",
                f"    cylinder(h = {c.depth:.3f}, r = {c.cylinder_r:.3f});",
            ]
        else:
            out_lines.append("  union() {")
            for c in cylinder_cuts:
                if c.label:
                    out_lines.append(f"    // {c.label}")
                out_lines += [
                    f"    translate([{c.cylinder_cx:.3f}, {c.cylinder_cy:.3f}, {c.z_base:.3f}])",
                    f"      cylinder(h = {c.depth:.3f}, r = {c.cylinder_r:.3f});",
                ]
            out_lines.append("  }")

    out_lines += ["", "}"]

    return header + "\n".join(out_lines) + "\n"
