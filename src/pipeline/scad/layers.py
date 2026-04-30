"""layers.py — generate OpenSCAD lines for the shell body.

Emits a single ``polyhedron()`` primitive built from vertex rings.  Each ring
corresponds to a profile step (bottom edge, top edge) or to the top/bottom
of the straight wall section.  Because every vertex in a ring can sit at a
different Z, the ceiling and floor can follow per-vertex heights defined in
the design outline.

Outline holes are built directly into the polyhedron: the caps are
triangulated as a polygon-with-holes (via earcut bridging), and each hole
boundary gets its own inner wall surface.  This avoids fragile CSG
``difference()`` operations for through-cuts.

Edge profiles (chamfer / fillet) are applied via miter-normal insets computed
in pure Python — preserving the exact vertex count across all rings so the
face index table stays consistent.

The ``flat_pts`` argument is the Bézier-expanded 2-D footprint polygon from
``outline.tessellate_outline`` — identical to the polygon used for cutout
placement so the shell and cutouts are always aligned.
"""

from __future__ import annotations

import math
import logging

from shapely.geometry import Polygon as ShapelyPolygon, Point as ShapelyPoint
from shapely.ops import nearest_points as shapely_nearest_points

from src.pipeline.design.models import Outline, Enclosure
from src.pipeline.design.height_field import (
    blended_height as _blended_height,
    blended_bottom_height as _blended_bottom_height,
)
from src.pipeline.scad.outline import tessellate_outline

log = logging.getLogger(__name__)

# Number of profile steps per chamfer / fillet zone.
# 6 gives adequate fillet quality for 3D printing (~15° per step).
_CURVE_STEPS = 6

# Minimum vertical gap between the last bottom-profile ring and the first
# top-profile ring.  Prevents coincident vertices at the chamfer/fillet
# junction that create mixed-topology meshes triggering CGAL 4.x assertions.
_MIN_WALL_GAP = 0.2


# ── Per-vertex miter inset ─────────────────────────────────────────────────────


def _polygon_signed_area(pts: list[list[float]]) -> float:
    """Signed shoelace area.  Positive = CCW in standard math coords (Y up)."""
    n = len(pts)
    return 0.5 * sum(
        pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
        for i in range(n)
    )


def _inset_polygon_pts(
    pts: list[list[float]],
    inset: float,
    _area: float | None = None,
) -> list[list[float]]:
    """Inset each vertex along its miter normal by ``inset`` mm.

    Always returns exactly ``len(pts)`` vertices — a hard requirement for
    building consistent polyhedron ring tables.  A miter limit of 5×
    prevents very acute corners from producing extreme spikes.

    Parameters
    ----------
    pts    : 2-D polygon vertices [[x, y], ...].
    inset  : Inward offset in mm.  ≤ 0 returns a copy of the original vertices.
    _area  : Pre-computed signed area (optional, avoids recomputing in a loop).
    """
    if inset < 1e-9:
        return [[x, y] for x, y in pts]

    n = len(pts)
    area = _area if _area is not None else _polygon_signed_area(pts)
    # +1 → CCW math convention (interior is to the left of each directed edge).
    # −1 → CW math / CCW screen convention (interior to the right).
    sign = 1.0 if area >= 0 else -1.0

    result: list[list[float]] = []
    for i in range(n):
        x0, y0 = pts[(i - 1) % n]
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]

        # Normalised tangent of the incoming edge (prev → current)
        dx_in = x1 - x0
        dy_in = y1 - y0
        len_in = math.hypot(dx_in, dy_in)
        dx_in /= max(len_in, 1e-12)
        dy_in /= max(len_in, 1e-12)

        # Normalised tangent of the outgoing edge (current → next)
        dx_out = x2 - x1
        dy_out = y2 - y1
        len_out = math.hypot(dx_out, dy_out)
        dx_out /= max(len_out, 1e-12)
        dy_out /= max(len_out, 1e-12)

        # Inward normals: left-perpendicular for CCW, right for CW
        nx_in  = sign * (-dy_in);  ny_in  = sign * dx_in
        nx_out = sign * (-dy_out); ny_out = sign * dx_out

        # Miter bisector (average of the two inward normals, normalised)
        bx = nx_in + nx_out
        by = ny_in + ny_out
        b_len = math.hypot(bx, by)
        if b_len < 1e-9:
            bx, by = nx_in, ny_in
        else:
            bx /= b_len
            by /= b_len

        # Scale: how far along the bisector to travel to get the requested
        # perpendicular inset distance.  Clamped so miter ≤ 5× inset.
        cos_a = nx_in * bx + ny_in * by
        miter = inset / max(cos_a, 0.2)

        result.append([x1 + bx * miter, y1 + by * miter])

    return result


def _safe_inset_polygon_pts(
    pts: list[list[float]],
    inset: float,
    _area: float | None = None,
) -> list[list[float]]:
    """Inset polygon with self-intersection guard.

    First tries fast miter inset.  If that produces a self-intersecting
    polygon (common for non-convex shapes with thin features), falls back to
    Shapely buffer + nearest-point projection, which always gives the correct
    perpendicular offset regardless of polygon complexity.  Each original
    vertex is projected to the nearest point on the shrunk polygon boundary,
    preserving the required N-vertex ring structure.
    """
    if inset < 1e-9:
        return [[x, y] for x, y in pts]

    result = _inset_polygon_pts(pts, inset, _area=_area)
    if ShapelyPolygon(result).is_valid:
        return result

    # Miter inset self-intersects — use Shapely buffer for a geometrically
    # correct offset, then project each original vertex back to N points.
    shrunk = ShapelyPolygon(pts).buffer(-inset)
    if not shrunk.is_empty and shrunk.is_valid:
        boundary = shrunk.boundary
        projected: list[list[float]] = []
        for x, y in pts:
            _, near = shapely_nearest_points(ShapelyPoint(x, y), boundary)
            projected.append([near.x, near.y])
        return projected

    # Polygon collapses entirely at this inset — return original vertices.
    return [[x, y] for x, y in pts]


# ── Ear-clipping polygon triangulation ─────────────────────────────────────────


def _point_in_triangle(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
    cx: float, cy: float,
) -> bool:
    """Return True if (px,py) is strictly inside triangle (a,b,c)."""
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _earclip(pts_2d: list[list[float]]) -> list[tuple[int, int, int]]:
    """Triangulate a simple polygon via ear clipping.

    Returns a list of index triples into *pts_2d*.  Works for any simple
    (non-self-intersecting) polygon regardless of convexity or winding.
    The output triangles inherit the winding orientation of the input.
    """
    n = len(pts_2d)
    if n < 3:
        return []
    if n == 3:
        return [(0, 1, 2)]

    area = _polygon_signed_area(pts_2d)
    ccw = area > 0

    remaining = list(range(n))
    tris: list[tuple[int, int, int]] = []
    max_iter = n * n

    for _ in range(max_iter):
        m = len(remaining)
        if m < 3:
            break
        if m == 3:
            tris.append((remaining[0], remaining[1], remaining[2]))
            break

        ear_found = False
        for i in range(m):
            p = remaining[(i - 1) % m]
            c = remaining[i]
            nx = remaining[(i + 1) % m]

            ax, ay = pts_2d[p]
            bx, by = pts_2d[c]
            cx, cy = pts_2d[nx]

            cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if ccw and cross <= 1e-12:
                continue
            if not ccw and cross >= -1e-12:
                continue

            ear_ok = True
            for j in range(m):
                if j == (i - 1) % m or j == i or j == (i + 1) % m:
                    continue
                qx, qy = pts_2d[remaining[j]]
                if _point_in_triangle(qx, qy, ax, ay, bx, by, cx, cy):
                    ear_ok = False
                    break

            if ear_ok:
                tris.append((p, c, nx))
                remaining.pop(i)
                ear_found = True
                break

        if not ear_found:
            for i in range(1, m - 1):
                tris.append((remaining[0], remaining[i], remaining[i + 1]))
            break

    return tris


def _earclip_with_holes(
    outer_pts: list[list[float]],
    hole_pts_list: list[list[list[float]]],
) -> list[tuple[int, int, int]]:
    """Triangulate a polygon with holes.

    Uses the mapbox earcut algorithm, which handles any simple polygon with
    any number of holes.

    Returns index triples into the combined vertex array:
    ``[outer_0 .. outer_N-1, hole0_0 .. hole0_H0-1, hole1_0 .. ...]``.

    The output triangles inherit the winding of the outer polygon, matching
    ``_earclip``'s behaviour.
    """
    if not hole_pts_list:
        return _earclip(outer_pts)

    import numpy as np
    import mapbox_earcut

    combined = list(outer_pts)
    ring_ends = [len(outer_pts)]
    for hpts in hole_pts_list:
        combined.extend(hpts)
        ring_ends.append(ring_ends[-1] + len(hpts))

    coords = np.array(combined, dtype=np.float64)
    rings = np.array(ring_ends, dtype=np.uint32)
    indices = mapbox_earcut.triangulate_float64(coords, rings)
    n = len(indices) // 3
    return [(int(indices[i * 3]), int(indices[i * 3 + 1]),
             int(indices[i * 3 + 2])) for i in range(n)]


# ── Polyhedron ring builder ───────────────────────────────────────────────────


def _smooth_top_zs(
    top_zs: list[float],
    sigma: float = 4.0,
    passes: int = 3,
) -> list[float]:
    """Smooth per-vertex ceiling heights along the perimeter ring.

    Applies a Gaussian blur (σ in vertex-index units) along the circular
    array of ceiling heights.  After every pass the result is clamped from
    below by the *original* values so component clearances are never reduced.
    """
    N    = len(top_zs)
    orig = list(top_zs)
    arr  = list(top_zs)
    half = int(math.ceil(3.0 * sigma))

    for _ in range(passes):
        new_arr: list[float] = []
        for i in range(N):
            wsum = 0.0
            vsum = 0.0
            for di in range(-half, half + 1):
                j = (i + di) % N
                w = math.exp(-0.5 * (di / sigma) ** 2)
                vsum += arr[j] * w
                wsum += w
            new_arr.append(vsum / wsum)
        # Clamp from below to preserve minimum component clearances
        arr = [max(orig[i], new_arr[i]) for i in range(N)]

    return arr


def _build_rings(
    flat_pts: list[list[float]],
    top_zs: list[float],
    enclosure: Enclosure,
    bottom_zs: list[float] | None = None,
    *,
    skip_edge_top: bool = False,
    skip_edge_bottom: bool = False,
) -> list[list[list[float]]]:
    """Build an ordered list of vertex rings from bottom to top.

    Each ring is a list of N ``[x, y, z]`` points (N = len(flat_pts)).
    Rings are stacked bottom → top:

    * Bottom edge zone: ``_CURVE_STEPS + 1`` rings (last ring =
      z=bottom_zs[i]+bot_size, full-width polygon = start of the straight wall).
    * OR single bottom ring at z=bottom_zs[i] (no bottom profile).
    * Top edge zone: ``_CURVE_STEPS + 1`` rings (first ring = per-vertex
      ``top_zs[i] − top_size``, full-width = end of the straight wall).
    * OR single per-vertex top ring at z=top_zs[i] (no top profile).

    The straight wall section is implicitly encoded as the quad between the
    last bottom ring and the first top ring.

    Parameters
    ----------
    bottom_zs : Per-vertex floor heights.  ``None`` or all-zeros gives a flat
                bottom at z=0.
    """
    N = len(flat_pts)
    if bottom_zs is None:
        bottom_zs = [0.0] * N

    edge_top = enclosure.edge_top
    edge_bot = enclosure.edge_bottom

    top_type = (edge_top.type    if edge_top else "none") or "none"
    top_size = (edge_top.size_mm if edge_top else 0.0)    or 0.0
    bot_type = (edge_bot.type    if edge_bot else "none") or "none"
    bot_size = (edge_bot.size_mm if edge_bot else 0.0)    or 0.0

    has_top = top_size > 0 and top_type in ("chamfer", "fillet") and not skip_edge_top
    has_bot = bot_size > 0 and bot_type in ("chamfer", "fillet") and not skip_edge_bottom

    area = _polygon_signed_area(flat_pts)
    rings: list[list[list[float]]] = []

    # ── Bottom edge rings ──────────────────────────────────────────────────────
    if has_bot:
        for step in range(_CURVE_STEPS + 1):
            frac = step / _CURVE_STEPS
            if bot_type == "fillet":
                theta = (1.0 - frac) * (math.pi / 2)
                inset_frac = 1.0 - math.cos(theta)
                z_frac     = 1.0 - math.sin(theta)
            else:  # chamfer
                inset_frac = 1.0 - frac
                z_frac     = frac
            inset = bot_size * inset_frac
            ipts = _safe_inset_polygon_pts(flat_pts, inset, _area=area)
            ring = []
            for i in range(N):
                # Clamp bot_size so the bottom profile doesn't exceed available wall
                avail = max(top_zs[i] - bottom_zs[i], _MIN_WALL_GAP)
                eff_bs = min(bot_size, avail * 0.45)
                eff_bs = max(eff_bs, 0.01)
                z = bottom_zs[i] + eff_bs * z_frac
                ring.append([ipts[i][0], ipts[i][1], z])
            rings.append(ring)
    else:
        rings.append([[flat_pts[i][0], flat_pts[i][1], bottom_zs[i]] for i in range(N)])

    # ── Top edge rings (also encodes the straight wall top in ring[0]) ─────────
    if has_top:
        for step in range(_CURVE_STEPS + 1):
            frac = step / _CURVE_STEPS
            if top_type == "fillet":
                theta    = frac * (math.pi / 2)
                inset    = top_size * (1.0 - math.cos(theta))
                z_offset = top_size * math.sin(theta)
            else:  # chamfer
                inset    = top_size * frac
                z_offset = top_size * frac
            ipts = _safe_inset_polygon_pts(flat_pts, inset, _area=area)
            ring = []
            for i in range(N):
                last_bot_z = (bottom_zs[i] + bot_size) if has_bot else bottom_zs[i]
                avail = max(top_zs[i] - last_bot_z, _MIN_WALL_GAP)
                eff_ts = min(top_size, avail - _MIN_WALL_GAP)
                eff_ts = max(eff_ts, 0.01)
                z = (top_zs[i] - eff_ts) + z_offset * (eff_ts / top_size)
                ring.append([ipts[i][0], ipts[i][1], z])
            rings.append(ring)
    else:
        rings.append([[flat_pts[i][0], flat_pts[i][1], top_zs[i]] for i in range(N)])

    return rings


def _polyhedron_shell(
    flat_pts: list[list[float]],
    top_zs: list[float],
    enclosure: Enclosure,
    outline: Outline | None = None,
    bottom_zs: list[float] | None = None,
    hole_pts_list: list[list[list[float]]] | None = None,
    hole_top_zs_list: list[list[float]] | None = None,
    hole_bot_zs_list: list[list[float]] | None = None,
    *,
    skip_edge_top: bool = False,
    skip_edge_bottom: bool = False,
    open_top: bool = False,
    open_bottom: bool = False,
) -> list[str]:
    """Emit an OpenSCAD ``polyhedron()`` for the shell body.

    Outline holes are integrated directly: their boundaries appear as inner
    wall surfaces in the polyhedron, and the caps are triangulated as a
    polygon-with-holes.  This produces a single watertight mesh without
    relying on CSG ``difference()`` for through-cuts.

    Face winding follows OpenSCAD's left-hand / CW-from-outside convention.
    """
    N = len(flat_pts)
    if bottom_zs is None:
        bottom_zs = [0.0] * N

    # Normalise to CCW so all downstream winding logic can assume positive area.
    if _polygon_signed_area(flat_pts) < 0:
        flat_pts = list(reversed(flat_pts))
        top_zs   = list(reversed(top_zs))
        bottom_zs = list(reversed(bottom_zs))
    holes = hole_pts_list or []
    hole_sizes = [len(h) for h in holes]
    N_total = N + sum(hole_sizes)

    # ── Build outer rings ──────────────────────────────────────────────────────
    outer_rings = _build_rings(
        flat_pts, top_zs, enclosure, bottom_zs=bottom_zs,
        skip_edge_top=skip_edge_top, skip_edge_bottom=skip_edge_bottom,
    )
    R = len(outer_rings)

    # ── Build hole rings ───────────────────────────────────────────────────────
    # Hole vertices keep constant XY (no edge profile) with Z linearly
    # distributed from floor to ceiling at each vertex position.
    hole_ring_data: list[list[list[list[float]]]] = []
    for hi, hpts in enumerate(holes):
        H = len(hpts)
        h_top = hole_top_zs_list[hi] if hole_top_zs_list else [max(top_zs)] * H
        h_bot = hole_bot_zs_list[hi] if hole_bot_zs_list else [0.0] * H
        h_rings: list[list[list[float]]] = []
        for ri in range(R):
            frac = ri / max(R - 1, 1)
            ring = [[hpts[vi][0], hpts[vi][1],
                      h_bot[vi] + frac * (h_top[vi] - h_bot[vi])]
                     for vi in range(H)]
            h_rings.append(ring)
        hole_ring_data.append(h_rings)

    # ── Assemble combined point array (ring-major) ─────────────────────────────
    all_pts: list[list[float]] = []
    for ri in range(R):
        all_pts.extend(outer_rings[ri])
        for hi in range(len(holes)):
            all_pts.extend(hole_ring_data[hi][ri])

    # ── Winding (always CCW after normalisation above) ──────────────────────
    # area = _polygon_signed_area(flat_pts)  →  guaranteed ≥ 0

    # ── Cap triangulation (polygon with holes) ─────────────────────────────────
    if holes:
        cap_tris = _earclip_with_holes(flat_pts, holes)
    else:
        cap_tris = _earclip(flat_pts)

    faces: list[list[int]] = []

    def idx(ri: int, vi: int) -> int:
        """Map (ring, combined-vertex-index) to flat point index."""
        return ri * N_total + vi

    # ── Bottom cap ─────────────────────────────────────────────────────────────
    if not open_bottom:
        for a, b, c in cap_tris:
            faces.append([a, b, c])

    # ── Top cap ──────────────────────────────────────────────────────────────────────
    if not open_top:
        top_base = (R - 1) * N_total
        for a, b, c in cap_tris:
            faces.append([top_base + a, top_base + c, top_base + b])

    # ── Outer side faces ───────────────────────────────────────────────────────
    for ri in range(R - 1):
        for vi in range(N):
            a = idx(ri,     vi)
            b = idx(ri,     (vi + 1) % N)
            c = idx(ri + 1, (vi + 1) % N)
            d = idx(ri + 1, vi)
            faces.append([a, d, c])
            faces.append([a, c, b])

    # ── Inner hole wall faces ─────────────────────────────────────────────────
    base_in_ring = N
    for hi in range(len(holes)):
        H = hole_sizes[hi]
        for ri in range(R - 1):
            for vi in range(H):
                a = idx(ri,     base_in_ring + vi)
                b = idx(ri,     base_in_ring + (vi + 1) % H)
                c = idx(ri + 1, base_in_ring + (vi + 1) % H)
                d = idx(ri + 1, base_in_ring + vi)
                faces.append([a, b, c])
                faces.append([a, c, d])
        base_in_ring += H

    # ── Format as OpenSCAD source ──────────────────────────────────────────────
    min_z = min(top_zs)
    max_z = max(top_zs)
    edge_top = enclosure.edge_top
    edge_bot = enclosure.edge_bottom
    top_type = (edge_top.type    if edge_top else "none") or "none"
    top_size = (edge_top.size_mm if edge_top else 0.0)    or 0.0
    bot_type = (edge_bot.type    if edge_bot else "none") or "none"
    bot_size = (edge_bot.size_mm if edge_bot else 0.0)    or 0.0

    n_holes = len(holes)
    pts_str   = ", ".join(
        f"[{x:.4f}, {y:.4f}, {z:.4f}]" for x, y, z in all_pts
    )
    faces_str = ", ".join(
        "[" + ", ".join(str(i) for i in face) + "]" for face in faces
    )

    log.info(
        "Shell body (polyhedron): %d rings, %d outer + %d hole verts/ring, "
        "%d pts, %d faces, %d holes, z=%.1f..%.1f mm",
        R, N, sum(hole_sizes), len(all_pts), len(faces), n_holes,
        min(bottom_zs), max_z,
    )

    return [
        f"// Shell body — polyhedron",
        f"// ceiling z: {min_z:.1f}..{max_z:.1f} mm  floor z: {min(bottom_zs):.1f}..{max(bottom_zs):.1f} mm"
        f"  bottom={bot_type}({bot_size:.1f}mm)  top={top_type}({top_size:.1f}mm)",
        f"// {N} outer + {sum(hole_sizes)} hole verts, {R} rings, "
        f"{len(all_pts)} pts, {len(faces)} faces, {n_holes} holes",
        f"polyhedron(",
        f"  points   = [{pts_str}],",
        f"  faces    = [{faces_str}],",
        f"  convexity = 10",
        f");",
    ]


# ── Public API ─────────────────────────────────────────────────────────────────


def shell_body_lines(
    outline:   Outline,
    enclosure: Enclosure,
    flat_pts:  list[list[float]],
    top_zs:    list[float] | None = None,
    bottom_zs: list[float] | None = None,
    *,
    skip_edge_top: bool = False,
    skip_edge_bottom: bool = False,
    open_top: bool = False,
    open_bottom: bool = False,
) -> list[str]:
    """Return OpenSCAD lines for the shell body as a single ``polyhedron()``.

    If the outline contains holes, they are tessellated and built directly
    into the polyhedron as inner wall surfaces with triangulated caps.

    Two-part enclosure parameters
    -----------------------------
    skip_edge_top    : suppress top edge profile (bottom part has flat cut)
    skip_edge_bottom : suppress bottom edge profile (top part has flat cut)
    open_top         : omit the top cap face (bottom part is open)
    open_bottom      : omit the bottom cap face (top part is open)
    """
    N = len(flat_pts)

    eff_top = top_zs if (top_zs is not None and len(top_zs) == N) else [enclosure.height_mm] * N
    eff_bot = bottom_zs if (bottom_zs is not None and len(bottom_zs) == N) else [0.0] * N

    hole_pts_list: list[list[list[float]]] | None = None
    hole_top_zs_list: list[list[float]] | None = None
    hole_bot_zs_list: list[list[float]] | None = None

    if outline and outline.holes:
        hole_pts_list = []
        hole_top_zs_list = []
        hole_bot_zs_list = []
        for h in outline.holes:
            hpts = tessellate_outline(Outline(points=h))
            hole_pts_list.append(hpts)
            hole_top_zs_list.append([
                _blended_height(x, y, outline, enclosure) for x, y in hpts
            ])
            hole_bot_zs_list.append([
                _blended_bottom_height(x, y, outline, enclosure) for x, y in hpts
            ])

    return _polyhedron_shell(
        flat_pts, eff_top, enclosure, outline=outline, bottom_zs=eff_bot,
        hole_pts_list=hole_pts_list,
        hole_top_zs_list=hole_top_zs_list,
        hole_bot_zs_list=hole_bot_zs_list,
        skip_edge_top=skip_edge_top,
        skip_edge_bottom=skip_edge_bottom,
        open_top=open_top,
        open_bottom=open_bottom,
    )
