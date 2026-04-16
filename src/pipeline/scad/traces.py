"""Trace channel fragment builders.

Produces ScadFragment cutouts from inflated trace polygons.  Each
InflatedTrace carries the exact Shapely Polygon footprint computed by
the Voronoi inflation step; this module simply converts those polygons
into SCAD fragments at the correct Z-layer.
"""

from __future__ import annotations

from shapely.geometry import MultiPolygon

from src.pipeline.config import FLOOR_MM, TRACE_HEIGHT_MM
from src.pipeline.router.models import RoutingResult

from .fragment import ScadFragment, PolygonGeometry


def build_trace_fragments(
    routing: RoutingResult,
    ceil_start: float,
) -> list[ScadFragment]:
    """Build trace channel fragments from inflated trace polygons."""
    channel_depth = TRACE_HEIGHT_MM
    frags: list[ScadFragment] = []

    for it in routing.inflated_traces:
        poly = it.polygon
        if poly is None or poly.is_empty:
            continue

        geoms = list(poly.geoms) if isinstance(poly, MultiPolygon) else [poly]
        for geom in geoms:
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
                depth=channel_depth,
                label=f"trace {it.net_id}",
            ))

    return frags
