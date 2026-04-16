"""Shared trace geometry — single source of truth for trace shapes.

Every stage of the pipeline that needs to reason about trace geometry
(routing grid, SCAD cutouts, bitmap projection, ironing, postprocessor)
must use these functions so that all stages agree on the exact same shape.

A trace is a polyline of waypoints.  Each segment produces a **stadium**
(rectangle with semicircular endcaps) of radius ``trace_width / 2``
around the segment centreline.  Adjacent segments share endpoints, so
their endcap circles overlap to form smooth joints at bends.
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union


def point_seg_dist(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """Minimum Euclidean distance from point (px, py) to segment (ax, ay)-(bx, by)."""
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def trace_path_polygon(
    path: list[tuple[float, float]],
    trace_width: float,
    *,
    quad_segs: int = 8,
) -> Polygon | MultiPolygon | None:
    """Build the exact 2-D footprint of a trace path.

    The result is the Minkowski sum of the polyline with a circle of
    radius ``trace_width / 2`` — i.e. a series of overlapping stadiums
    merged into one polygon.

    Parameters
    ----------
    path : list of (x, y)
        Waypoints of the trace in mm.
    trace_width : float
        Full width of the trace in mm.
    quad_segs : int
        Number of line segments per quarter-circle in the endcap
        approximation — higher values give smoother curves.

    Returns
    -------
    Polygon | MultiPolygon | None
        The merged footprint, or None if the path has fewer than 2 points.
    """
    if len(path) < 2:
        return None
    line = LineString(path)
    poly = line.buffer(trace_width / 2, cap_style="round", join_style="round", quad_segs=quad_segs)
    if poly.is_empty:
        return None
    return poly
