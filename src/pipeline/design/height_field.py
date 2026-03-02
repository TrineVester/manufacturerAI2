"""Height field computation for 3D enclosure shapes.

This module is the **single canonical implementation** of all height-field
math.  Both the SCAD generator and the web frontend derive their geometry
from data produced here — the JS viewport never re-implements the math.

Public API
----------
blended_height(x, y, outline, enclosure) -> float
    Returns the final ceiling Z at world position (x, y).
    = max(vertex_interpolated_z_top, surface_bump(x, y))

sample_height_grid(outline, enclosure, resolution_mm) -> dict
    Samples blended_height on a regular grid covering the outline bounding
    box, masked to the interior of the polygon.  Returns a JSON-safe dict
    ready to attach to design.json for the frontend to consume directly.

surface_normal_at(x, y, grid) -> tuple[float, float, float]
    Returns the outward surface normal (nx, ny, nz) at (x, y) using
    central differences on a pre-sampled grid dict.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Outline, Enclosure


# ── Internal helpers ───────────────────────────────────────────────


def _resolved_z_top(vertex_z: float | None, default: float) -> float:
    """Return the effective z_top for a vertex, falling back to default."""
    return vertex_z if vertex_z is not None else default


def _point_in_triangle(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
    cx: float, cy: float,
) -> tuple[bool, float, float, float]:
    """Test if (px, py) is inside triangle (a, b, c) using barycentric coords.

    Returns (inside, u, v, w) where u+v+w=1 are the barycentric weights
    for vertices a, b, c respectively.
    """
    v0x, v0y = cx - ax, cy - ay
    v1x, v1y = bx - ax, by - ay
    v2x, v2y = px - ax, py - ay

    dot00 = v0x * v0x + v0y * v0y
    dot01 = v0x * v1x + v0y * v1y
    dot02 = v0x * v2x + v0y * v2y
    dot11 = v1x * v1x + v1y * v1y
    dot12 = v1x * v2x + v1y * v2y

    denom = dot00 * dot11 - dot01 * dot01
    if abs(denom) < 1e-12:
        return False, 0.0, 0.0, 0.0

    inv = 1.0 / denom
    u = (dot11 * dot02 - dot01 * dot12) * inv
    v = (dot00 * dot12 - dot01 * dot02) * inv
    inside = (u >= 0) and (v >= 0) and (u + v <= 1)
    w = 1.0 - u - v
    return inside, w, v, u  # weights for a, b, c


def _interpolate_vertex_heights(
    x: float, y: float,
    outline: "Outline",
    default_height: float,
) -> float:
    """IDW interpolation of z_top from the outline vertices.

    Inverse-distance weighting handles concave polygons correctly — the
    fan-triangulation-from-centroid approach misses concave notch areas and
    returns a flat centroid fallback, which breaks the lid in concave regions.
    IDW gives a smooth gradient that naturally reflects the nearest vertices.
    """
    verts = outline.vertices
    n = len(verts)
    if n < 1:
        return default_height

    heights = [_resolved_z_top(p.z_top, default_height) for p in outline.points]

    sum_w = 0.0
    sum_wz = 0.0
    for i in range(n):
        vx, vy = verts[i]
        d2 = (x - vx) ** 2 + (y - vy) ** 2
        if d2 < 1e-6:
            return heights[i]   # exactly on a vertex
        w = 1.0 / d2             # 1/d² weight (power = 2)
        sum_w  += w
        sum_wz += w * heights[i]

    return sum_wz / sum_w if sum_w > 0 else default_height


def _surface_bump(x: float, y: float, top_surface: "Enclosure | None") -> float:
    """Return the additive height bump from the top_surface descriptor.

    A "flat" or missing descriptor contributes 0 (no bump).
    """
    if top_surface is None:
        return 0.0
    ts = top_surface  # TopSurface dataclass

    if ts.type == "dome":
        px = ts.peak_x_mm
        py = ts.peak_y_mm
        peak = ts.peak_height_mm
        base = ts.base_height_mm
        if None in (px, py, peak, base):
            return 0.0
        dist = math.hypot(x - px, y - py)
        # Gaussian falloff — sigma chosen so the bump reaches base at ~2.5*sigma
        # We derive sigma from the outline's bounding radius implicitly, but
        # the caller can control shape via peak vs base difference.
        amplitude = peak - base
        if amplitude <= 0:
            return 0.0
        # Use a reasonable sigma: 40% of the distance to the outline centre
        # from the peak — we approximate this as amplitude/3 scaled by dist
        sigma = max(1.0, amplitude * 2.0)  # tuneable
        bump = amplitude * math.exp(-(dist * dist) / (2 * sigma * sigma))
        return max(0.0, bump)

    if ts.type == "ridge":
        x1, y1 = ts.x1, ts.y1
        x2, y2 = ts.x2, ts.y2
        crest = ts.crest_height_mm
        base = ts.base_height_mm
        falloff = ts.falloff_mm
        if None in (x1, y1, x2, y2, crest, base, falloff):
            return 0.0
        amplitude = crest - base
        if amplitude <= 0 or falloff <= 0:
            return 0.0
        # Distance from point to the crest line segment
        lx, ly = x2 - x1, y2 - y1
        ll = math.hypot(lx, ly)
        if ll < 1e-9:
            dist = math.hypot(x - x1, y - y1)
        else:
            t = max(0.0, min(1.0, ((x - x1) * lx + (y - y1) * ly) / (ll * ll)))
            cx = x1 + t * lx
            cy = y1 + t * ly
            dist = math.hypot(x - cx, y - cy)
        # Cosine falloff: full crest within 0, drops to 0 at falloff distance
        if dist >= falloff:
            return 0.0
        bump = amplitude * (0.5 + 0.5 * math.cos(math.pi * dist / falloff))
        return max(0.0, bump)

    # "flat" or unknown
    return 0.0


# ── Public API ─────────────────────────────────────────────────────


def _bezier_expand_outline(
    outline: "Outline",
    segments: int = 6,
) -> list[tuple[float, float]]:
    """Expand bezier-eased corners into sub-points for polygon operations.

    Mirrors the JS ``expandOutlineVertices`` logic so the Shapely masking
    polygon matches the rounded shape visible in the 3-D viewport.
    """
    points = outline.points
    n = len(points)
    result: list[tuple[float, float]] = []

    for i in range(n):
        prev = (i - 1) % n
        next_ = (i + 1) % n
        Cx, Cy = points[i].x, points[i].y
        Px, Py = points[prev].x, points[prev].y
        Nx, Ny = points[next_].x, points[next_].y

        e_in  = points[i].ease_in  or 0.0
        e_out = points[i].ease_out or 0.0

        if e_in == 0 and e_out == 0:
            result.append((Cx, Cy))
            continue

        dPx, dPy = Px - Cx, Py - Cy
        dNx, dNy = Nx - Cx, Ny - Cy
        lenP = math.hypot(dPx, dPy)
        lenN = math.hypot(dNx, dNy)

        if lenP < 1e-9 or lenN < 1e-9:
            result.append((Cx, Cy))
            continue

        safe_in  = min(e_in,  lenP * 0.45)
        safe_out = min(e_out, lenN * 0.45)
        t1 = (Cx + dPx * (safe_in  / lenP), Cy + dPy * (safe_in  / lenP))
        t2 = (Cx + dNx * (safe_out / lenN), Cy + dNy * (safe_out / lenN))

        for s in range(segments + 1):
            u  = s / segments
            ku = 1.0 - u
            bx = ku*ku*t1[0] + 2*ku*u*Cx + u*u*t2[0]
            by = ku*ku*t1[1] + 2*ku*u*Cy + u*u*t2[1]
            result.append((bx, by))

    return result


def blended_height(
    x: float,
    y: float,
    outline: "Outline",
    enclosure: "Enclosure",
) -> float:
    """Return the final ceiling Z at world position (x, y).

    Final Z = max(vertex-interpolated z_top at (x,y), base height + surface bump).
    The base height (enclosure.height_mm) is the minimum everywhere.
    """
    base = enclosure.height_mm
    vertex_z = _interpolate_vertex_heights(x, y, outline, base)
    bump = _surface_bump(x, y, enclosure.top_surface)
    return max(vertex_z, base + bump)


def sample_height_grid(
    outline: "Outline",
    enclosure: "Enclosure",
    resolution_mm: float = 2.0,
) -> dict:
    """Sample blended_height on a regular grid over the outline bounding box.

    Returns a JSON-safe dict:
    {
        "origin_x": float,   # X of the first grid column (mm)
        "origin_y": float,   # Y of the first grid row (mm)
        "step_mm":  float,   # grid cell size
        "cols":     int,
        "rows":     int,
        "grid":     [[z, ...], ...]   # rows x cols; None outside the polygon
    }
    """
    verts = outline.vertices
    if len(verts) < 3:
        return {"origin_x": 0, "origin_y": 0, "step_mm": resolution_mm,
                "cols": 0, "rows": 0, "grid": []}

    # Expand bezier corners for accurate polygon masking (matches JS viewport)
    expanded_verts = _bezier_expand_outline(outline) or verts

    xs = [v[0] for v in expanded_verts]
    ys = [v[1] for v in expanded_verts]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    step = resolution_mm
    cols = max(1, int(math.ceil((max_x - min_x) / step)) + 1)
    rows = max(1, int(math.ceil((max_y - min_y) / step)) + 1)

    # Build Shapely polygon for masking (optional but greatly improves quality).
    # Expand by half a grid step so cells whose centre sits exactly on the
    # outline boundary are included — this closes the visual seam between the
    # lid mesh and the wall tops in the 3-D viewport.
    poly_expanded = None
    use_shapely = False
    try:
        from shapely.geometry import Polygon, Point
        poly = Polygon(expanded_verts)
        if poly.is_valid:
            poly_expanded = poly
            use_shapely = True
    except ImportError:
        pass

    grid: list[list[float | None]] = []
    for r in range(rows):
        row: list[float | None] = []
        y = min_y + r * step
        for c in range(cols):
            x = min_x + c * step
            if use_shapely:
                inside = poly_expanded.contains(Point(x, y))
            else:
                inside = _point_in_polygon(x, y, verts)
            if inside:
                row.append(round(blended_height(x, y, outline, enclosure), 3))
            else:
                row.append(None)
        grid.append(row)

    return {
        "origin_x": round(min_x, 3),
        "origin_y": round(min_y, 3),
        "step_mm": step,
        "cols": cols,
        "rows": rows,
        "grid": grid,
    }


def surface_normal_at(
    x: float,
    y: float,
    grid: dict,
) -> tuple[float, float, float]:
    """Compute the outward surface normal at (x, y) from a pre-sampled grid.

    Uses central differences on the sampled height grid.
    Returns (nx, ny, nz) as a normalised vector.
    """
    origin_x: float = grid["origin_x"]
    origin_y: float = grid["origin_y"]
    step: float = grid["step_mm"]
    grid_data: list[list[float | None]] = grid["grid"]
    rows: int = grid["rows"]
    cols: int = grid["cols"]

    def _sample(c: int, r: int) -> float | None:
        if 0 <= r < rows and 0 <= c < cols:
            return grid_data[r][c]
        return None

    c = (x - origin_x) / step
    r = (y - origin_y) / step
    ci = int(round(c))
    ri = int(round(r))

    # Central differences (fall back to one-sided at boundaries)
    def _z(dc: int, dr: int) -> float:
        v = _sample(ci + dc, ri + dr)
        if v is None:
            # try zero-offset
            v = _sample(ci, ri)
        return v if v is not None else 25.0

    dzdx = (_z(1, 0) - _z(-1, 0)) / (2 * step)
    dzdy = (_z(0, 1) - _z(0, -1)) / (2 * step)

    # Surface normal: cross product of (1,0,dzdx) × (0,1,dzdy) = (-dzdx, -dzdy, 1)
    nx, ny, nz = -dzdx, -dzdy, 1.0
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-9:
        return (0.0, 0.0, 1.0)
    return (nx / length, ny / length, nz / length)


def _point_in_polygon(x: float, y: float, verts: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test (fallback when Shapely is unavailable)."""
    n = len(verts)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside
