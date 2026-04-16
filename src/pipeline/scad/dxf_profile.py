"""dxf_profile.py — extract 2D profiles from DXF files.

Parses the ENTITIES section of an AutoCAD R14+ DXF file and returns
closed/open polylines, lines, points, circles, and arcs as typed
Python dataclasses.  Designed for loading click-lip cross-section
profiles but usable for any simple 2D DXF.

Only the geometry entities used by this project are supported.  Block
references, hatches, dimensions, and other complex entities are silently
skipped.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_INSUNITS_TO_MM: dict[int, float] = {
    0: 1.0,       # unitless — assume mm
    1: 25.4,      # inches
    2: 304.8,     # feet
    4: 1.0,       # millimeters
    5: 10.0,      # centimeters
    6: 1000.0,    # meters
    14: 0.001,    # micrometers
}

_BULGE_SEGMENTS = 8


@dataclass
class DxfPoint:
    x: float
    y: float
    layer: str = "0"


@dataclass
class DxfLine:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str = "0"


@dataclass
class DxfCircle:
    cx: float
    cy: float
    radius: float
    layer: str = "0"


@dataclass
class DxfArc:
    cx: float
    cy: float
    radius: float
    start_angle_deg: float
    end_angle_deg: float
    layer: str = "0"


@dataclass
class DxfPolyline:
    points: list[tuple[float, float]] = field(default_factory=list)
    is_closed: bool = False
    layer: str = "0"


@dataclass
class DxfEntities:
    polylines: list[DxfPolyline] = field(default_factory=list)
    lines: list[DxfLine] = field(default_factory=list)
    points: list[DxfPoint] = field(default_factory=list)
    circles: list[DxfCircle] = field(default_factory=list)
    arcs: list[DxfArc] = field(default_factory=list)
    units_scale: float = 1.0


def _tokenise(text: str) -> list[tuple[int, str]]:
    """Split DXF text into (group-code, value) pairs."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pairs: list[tuple[int, str]] = []
    i = 0
    while i + 1 < len(lines):
        code_str = lines[i].strip()
        val = lines[i + 1].strip()
        if not code_str:
            i += 1
            continue
        try:
            code = int(code_str)
        except ValueError:
            i += 1
            continue
        pairs.append((code, val))
        i += 2
    return pairs


def _bulge_to_arc_points(
    p1: tuple[float, float],
    p2: tuple[float, float],
    bulge: float,
    segments: int = _BULGE_SEGMENTS,
) -> list[tuple[float, float]]:
    """Subdivide a bulge arc between two vertices into line segments.

    A bulge of 0 means a straight line (returns empty list — no
    intermediate points needed).  Positive = CCW arc, negative = CW.
    """
    if abs(bulge) < 1e-9:
        return []

    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return []

    sagitta = abs(bulge) * chord / 2
    radius = (chord * chord / 4 + sagitta * sagitta) / (2 * sagitta)
    sweep = 4.0 * math.atan(abs(bulge))
    sign = 1.0 if bulge > 0 else -1.0

    mx = (p1[0] + p2[0]) / 2
    my = (p1[1] + p2[1]) / 2
    nx = -dy / chord
    ny = dx / chord
    d = radius - sagitta
    cx = mx + sign * d * nx
    cy = my + sign * d * ny

    a_start = math.atan2(p1[1] - cy, p1[0] - cx)

    arc_pts: list[tuple[float, float]] = []
    for i in range(1, segments):
        t = i / segments
        angle = a_start + sign * sweep * t
        arc_pts.append((cx + radius * math.cos(angle),
                        cy + radius * math.sin(angle)))
    return arc_pts


def _parse_header_units(pairs: list[tuple[int, str]]) -> float:
    """Extract $INSUNITS from header pairs and return mm scale factor."""
    for i, (code, val) in enumerate(pairs):
        if code == 9 and val == "$INSUNITS":
            for j in range(i + 1, min(i + 5, len(pairs))):
                if pairs[j][0] == 70:
                    unit_code = int(pairs[j][1])
                    return _INSUNITS_TO_MM.get(unit_code, 1.0)
    return 1.0


def _find_entities_range(
    pairs: list[tuple[int, str]],
) -> tuple[int, int]:
    """Return the (start, end) index range of the ENTITIES section."""
    start = end = -1
    for i, (code, val) in enumerate(pairs):
        if code == 0 and val == "SECTION":
            if i + 1 < len(pairs) and pairs[i + 1] == (2, "ENTITIES"):
                start = i + 2
        if start >= 0 and code == 0 and val == "ENDSEC":
            if i > start:
                end = i
                break
    return (start, end) if start >= 0 and end > start else (0, 0)


def _parse_lwpolyline(
    pairs: list[tuple[int, str]], start: int, end: int,
) -> DxfPolyline:
    """Parse an LWPOLYLINE entity from group-code pairs."""
    layer = "0"
    is_closed = False
    vertices: list[tuple[float, float]] = []
    bulges: list[float] = []
    cur_x: float | None = None

    for i in range(start, end):
        code, val = pairs[i]
        if code == 8:
            layer = val
        elif code == 70:
            is_closed = (int(val) & 1) == 1
        elif code == 10:
            if cur_x is not None:
                bulges.append(0.0)
            cur_x = float(val)
        elif code == 20:
            if cur_x is not None:
                vertices.append((cur_x, float(val)))
                cur_x = None
        elif code == 42:
            if bulges and len(bulges) == len(vertices):
                bulges[-1] = float(val)
            elif len(bulges) < len(vertices):
                bulges.append(float(val))

    if cur_x is not None:
        vertices.append((cur_x, 0.0))
        bulges.append(0.0)

    while len(bulges) < len(vertices):
        bulges.append(0.0)

    expanded: list[tuple[float, float]] = []
    n = len(vertices)
    for i in range(n):
        expanded.append(vertices[i])
        next_i = (i + 1) % n
        if next_i == 0 and not is_closed:
            continue
        if abs(bulges[i]) > 1e-9:
            arc_pts = _bulge_to_arc_points(vertices[i], vertices[next_i], bulges[i])
            expanded.extend(arc_pts)

    return DxfPolyline(points=expanded, is_closed=is_closed, layer=layer)


def _parse_line(
    pairs: list[tuple[int, str]], start: int, end: int,
) -> DxfLine:
    """Parse a LINE entity."""
    layer = "0"
    x1 = y1 = x2 = y2 = 0.0
    for i in range(start, end):
        code, val = pairs[i]
        if code == 8:
            layer = val
        elif code == 10:
            x1 = float(val)
        elif code == 20:
            y1 = float(val)
        elif code == 11:
            x2 = float(val)
        elif code == 21:
            y2 = float(val)
    return DxfLine(x1, y1, x2, y2, layer)


def _parse_point(
    pairs: list[tuple[int, str]], start: int, end: int,
) -> DxfPoint:
    """Parse a POINT entity."""
    layer = "0"
    x = y = 0.0
    for i in range(start, end):
        code, val = pairs[i]
        if code == 8:
            layer = val
        elif code == 10:
            x = float(val)
        elif code == 20:
            y = float(val)
    return DxfPoint(x, y, layer)


def _parse_circle(
    pairs: list[tuple[int, str]], start: int, end: int,
) -> DxfCircle:
    """Parse a CIRCLE entity."""
    layer = "0"
    cx = cy = 0.0
    r = 1.0
    for i in range(start, end):
        code, val = pairs[i]
        if code == 8:
            layer = val
        elif code == 10:
            cx = float(val)
        elif code == 20:
            cy = float(val)
        elif code == 40:
            r = float(val)
    return DxfCircle(cx, cy, r, layer)


def _parse_arc(
    pairs: list[tuple[int, str]], start: int, end: int,
) -> DxfArc:
    """Parse an ARC entity."""
    layer = "0"
    cx = cy = 0.0
    r = 1.0
    a_start = 0.0
    a_end = 360.0
    for i in range(start, end):
        code, val = pairs[i]
        if code == 8:
            layer = val
        elif code == 10:
            cx = float(val)
        elif code == 20:
            cy = float(val)
        elif code == 40:
            r = float(val)
        elif code == 50:
            a_start = float(val)
        elif code == 51:
            a_end = float(val)
    return DxfArc(cx, cy, r, a_start, a_end, layer)


def load_dxf(path: str | Path) -> DxfEntities:
    """Load a DXF file and return all recognised 2D entities.

    Supports LWPOLYLINE, LINE, POINT, CIRCLE, and ARC entities.
    Coordinates are scaled to millimetres based on $INSUNITS.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    pairs = _tokenise(text)
    units_scale = _parse_header_units(pairs)

    ent_start, ent_end = _find_entities_range(pairs)
    if ent_start >= ent_end:
        log.warning("DXF has no ENTITIES section: %s", path)
        return DxfEntities(units_scale=units_scale)

    entity_spans: list[tuple[str, int, int]] = []
    i = ent_start
    while i < ent_end:
        code, val = pairs[i]
        if code == 0:
            entity_type = val
            span_start = i + 1
            j = i + 1
            while j < ent_end and pairs[j][0] != 0:
                j += 1
            entity_spans.append((entity_type, span_start, j))
            i = j
        else:
            i += 1

    result = DxfEntities(units_scale=units_scale)

    for etype, s, e in entity_spans:
        if etype == "LWPOLYLINE":
            pl = _parse_lwpolyline(pairs, s, e)
            if units_scale != 1.0:
                pl.points = [(x * units_scale, y * units_scale) for x, y in pl.points]
            result.polylines.append(pl)
        elif etype == "LINE":
            ln = _parse_line(pairs, s, e)
            if units_scale != 1.0:
                ln.x1 *= units_scale
                ln.y1 *= units_scale
                ln.x2 *= units_scale
                ln.y2 *= units_scale
            result.lines.append(ln)
        elif etype == "POINT":
            pt = _parse_point(pairs, s, e)
            if units_scale != 1.0:
                pt.x *= units_scale
                pt.y *= units_scale
            result.points.append(pt)
        elif etype == "CIRCLE":
            ci = _parse_circle(pairs, s, e)
            if units_scale != 1.0:
                ci.cx *= units_scale
                ci.cy *= units_scale
                ci.radius *= units_scale
            result.circles.append(ci)
        elif etype == "ARC":
            ar = _parse_arc(pairs, s, e)
            if units_scale != 1.0:
                ar.cx *= units_scale
                ar.cy *= units_scale
                ar.radius *= units_scale
            result.arcs.append(ar)

    log.info(
        "DXF loaded: %d polylines, %d lines, %d points, %d circles, %d arcs (scale=%.3f)",
        len(result.polylines), len(result.lines), len(result.points),
        len(result.circles), len(result.arcs), units_scale,
    )
    return result
