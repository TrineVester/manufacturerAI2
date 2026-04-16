"""Trace channel fragment builders.

Produces ScadFragment cutouts from inflated trace polygons.  When
inflated polygons are unavailable (no Voronoi inflation step), falls
back to buffering raw trace centre-line paths at the configured trace
width to produce fixed-width channel cutouts.
"""

from __future__ import annotations

import logging

from shapely.geometry import LineString, MultiPolygon

from src.pipeline.config import FLOOR_MM, TRACE_HEIGHT_MM, TRACE_RULES
from src.pipeline.router.models import RoutingResult

from .fragment import ScadFragment, PolygonGeometry

log = logging.getLogger(__name__)


def _polygon_to_fragment(poly, label: str, depth: float) -> list[ScadFragment]:
    """Convert a Shapely polygon (or MultiPolygon) into ScadFragment cutouts."""
    geoms = list(poly.geoms) if isinstance(poly, MultiPolygon) else [poly]
    frags: list[ScadFragment] = []
    for geom in geoms:
        if geom.is_empty:
            continue
        coords = list(geom.exterior.coords)[:-1]
        pts = [[x, y] for x, y in coords]
        holes = None
        if geom.interiors:
            holes = [
                [[x, y] for x, y in ring.coords[:-1]]
                for ring in geom.interiors
            ]
        frags.append(ScadFragment(
            type="cutout",
            geometry=PolygonGeometry(pts, holes),
            z_base=FLOOR_MM,
            depth=depth,
            label=label,
        ))
    return frags


def build_trace_fragments(
    routing: RoutingResult,
    ceil_start: float,
) -> list[ScadFragment]:
    """Build trace channel fragments from inflated trace polygons.

    Falls back to fixed-width buffered centre-lines when inflated
    polygons are not available.
    """
    channel_depth = TRACE_HEIGHT_MM
    frags: list[ScadFragment] = []

    if routing.inflated_traces:
        for it in routing.inflated_traces:
            poly = it.polygon
            if poly is None or poly.is_empty:
                continue
            frags.extend(_polygon_to_fragment(poly, f"trace {it.net_id}", channel_depth))
    else:
        # Fallback: buffer raw trace centre-line paths at trace width
        half_w = TRACE_RULES.trace_width_mm / 2
        log.info("No inflated traces — generating channels from %d raw trace paths (width=%.2f mm)",
                 len(routing.traces), half_w * 2)
        for trace in routing.traces:
            if len(trace.path) < 2:
                continue
            line = LineString(trace.path)
            poly = line.buffer(half_w, cap_style="round", join_style="round")
            if poly.is_empty:
                continue
            frags.extend(_polygon_to_fragment(poly, f"trace {trace.net_id}", channel_depth))

    return frags
