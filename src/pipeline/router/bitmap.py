"""Bitmap generation — renders routed traces to a full-bed bitmap.

The bitmap covers the entire nominal build plate at nozzle-pitch
resolution, with each pixel being one nozzle pitch wide/tall.
Column 0 = bed X = 0, row 0 = bed Y = 0.

No offset calculations happen here.  The printer applies its own
calibrated FDM-to-inkjet offset when interpreting the bitmap during
sweeps.

  - text rows  → Y positions (low→high, so row 0 in the file
    corresponds to bed Y = 0)
  - text cols  → X positions (low→high)

A '1' means "deposit conductive ink here", a '0' means "no ink".
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from shapely.affinity import translate, scale
from shapely.geometry import Polygon
from shapely.prepared import prep

from src.pipeline.config import BedBitmap
from src.pipeline.trace_geometry import point_seg_dist as _point_seg_dist
from .models import RoutingResult

log = logging.getLogger(__name__)


def _segment_cells(
    x0: float, y0: float, x1: float, y1: float,
    half_w: float,
    pixel_size: float,
    cols: int,
    rows: int,
) -> set[tuple[int, int]]:
    """Rasterize one line segment with thickness into bitmap cells.

    Uses pixel-center-to-segment distance for all orientations so that
    trace width is identical whether the segment is vertical, horizontal,
    or diagonal.  Axis-aligned segments use a tight bounding-box scan;
    diagonals use a walk along the centreline for efficiency.
    """
    cells: set[tuple[int, int]] = set()
    dx = x1 - x0
    dy = y1 - y0

    col_min = max(0, int(math.floor((min(x0, x1) - half_w) / pixel_size)))
    col_max = min(cols - 1, int(math.floor((max(x0, x1) + half_w) / pixel_size)))
    row_min = max(0, int(math.floor((min(y0, y1) - half_w) / pixel_size)))
    row_max = min(rows - 1, int(math.floor((max(y0, y1) + half_w) / pixel_size)))

    if abs(dx) < 1e-9 or abs(dy) < 1e-9:
        for r in range(row_min, row_max + 1):
            py = (r + 0.5) * pixel_size
            for c in range(col_min, col_max + 1):
                px = (c + 0.5) * pixel_size
                if _point_seg_dist(px, py, x0, y0, x1, y1) <= half_w:
                    cells.add((r, c))
        return cells

    seg_len = math.hypot(dx, dy)
    step = pixel_size * 0.5
    n_steps = max(1, int(math.ceil(seg_len / step)))
    hw_px = int(math.ceil(half_w / pixel_size))

    for s in range(n_steps + 1):
        t = s / n_steps
        cx = x0 + dx * t
        cy = y0 + dy * t
        cc = int(math.floor(cx / pixel_size))
        cr = int(math.floor(cy / pixel_size))
        for dr in range(-hw_px, hw_px + 1):
            for dc in range(-hw_px, hw_px + 1):
                r = cr + dr
                c = cc + dc
                if 0 <= r < rows and 0 <= c < cols:
                    px = (c + 0.5) * pixel_size
                    py = (r + 0.5) * pixel_size
                    if _point_seg_dist(px, py, x0, y0, x1, y1) <= half_w:
                        cells.add((r, c))

    return cells


def _trace_cells(
    path: list[tuple[float, float]],
    trace_width_mm: float,
    pixel_size: float,
    cols: int,
    rows: int,
) -> set[tuple[int, int]]:
    """Rasterize a trace path (any angle) into bitmap cell coordinates."""
    half_w = trace_width_mm / 2.0
    cells: set[tuple[int, int]] = set()
    for i in range(len(path) - 1):
        x0, y0 = path[i]
        x1, y1 = path[i + 1]
        cells |= _segment_cells(x0, y0, x1, y1, half_w, pixel_size, cols, rows)
    return cells


def _transform_polygon_to_bed(
    poly: Polygon,
    dx: float,
    dy: float,
) -> Polygon:
    """Apply the model-to-bed transform (x+dx, -y+dy) to a polygon."""
    mirrored = scale(poly, xfact=1, yfact=-1, origin=(0, 0))
    return translate(mirrored, xoff=dx, yoff=dy)


def _polygon_cells(
    poly: Polygon,
    pixel_size: float,
    cols: int,
    rows: int,
) -> set[tuple[int, int]]:
    """Rasterize a Shapely Polygon into bitmap cells via scanline containment."""
    cells: set[tuple[int, int]] = set()
    if poly.is_empty:
        return cells

    minx, miny, maxx, maxy = poly.bounds
    col_min = max(0, int(math.floor(minx / pixel_size)))
    col_max = min(cols - 1, int(math.floor(maxx / pixel_size)))
    row_min = max(0, int(math.floor(miny / pixel_size)))
    row_max = min(rows - 1, int(math.floor(maxy / pixel_size)))

    prepared = prep(poly)

    for r in range(row_min, row_max + 1):
        py = (r + 0.5) * pixel_size
        for c in range(col_min, col_max + 1):
            px = (c + 0.5) * pixel_size
            from shapely.geometry import Point
            if prepared.contains(Point(px, py)):
                cells.add((r, c))

    return cells


def generate_trace_bitmap(
    result: RoutingResult,
    trace_width_mm: float,
    *,
    grid: BedBitmap,
    model_to_bed: tuple[float, float] = (0.0, 0.0),
) -> list[str]:
    """Render inflated trace polygons into a full-bed bitmap.

    Each inflated trace carries the exact Shapely Polygon computed by
    the Voronoi inflation step.  This function rasterizes those polygons
    at nozzle-pitch resolution.

    Parameters
    ----------
    result : RoutingResult
        Completed routing result with inflated trace polygons.
    trace_width_mm : float
        Unused (kept for API compatibility with debug callers).
    grid : BedBitmap
        Bed bitmap geometry (from ``bed_bitmap(printer_def)``).
    model_to_bed : (float, float)
        Translation from model-local coordinates to absolute bed
        coordinates: ``bed_pos = model_pos + model_to_bed``.

    Returns
    -------
    list[str]
        Each text line corresponds to one Y position,
        emitted from lowest Y (row 0) to highest Y.
    """
    pixel_size = grid.pixel_size_mm
    cols = grid.cols
    rows = grid.rows
    dx, dy = model_to_bed

    ink_cells: set[tuple[int, int]] = set()

    if result.inflated_traces:
        for it in result.inflated_traces:
            bed_poly = _transform_polygon_to_bed(it.polygon, dx, dy)
            new_cells = _polygon_cells(bed_poly, pixel_size, cols, rows)
            if not new_cells:
                log.warning(
                    "Trace net=%s clipped to zero pixels — may be outside bed",
                    it.net_id,
                )
            ink_cells |= new_cells
    else:
        # Fallback: inflated polygons not available (e.g. loaded from JSON).
        # Rasterize raw centreline paths at the given trace width.
        log.info("No inflated traces — falling back to fixed-width rasterization")
        for trace in result.traces:
            bed_path = [(x + dx, -y + dy) for x, y in trace.path]
            ink_cells |= _trace_cells(bed_path, trace_width_mm, pixel_size, cols, rows)

    lines: list[str] = []
    for r in range(rows):
        line_chars = []
        for c in range(cols):
            line_chars.append('1' if (r, c) in ink_cells else '0')
        lines.append(''.join(line_chars))

    return lines


def write_trace_bitmap(
    result: RoutingResult,
    trace_width_mm: float,
    output_path: Path | str,
    *,
    grid: BedBitmap,
    model_to_bed: tuple[float, float] = (0.0, 0.0),
) -> Path:
    """Generate the trace bitmap and write it to a text file."""
    output_path = Path(output_path)
    lines = generate_trace_bitmap(
        result, trace_width_mm,
        grid=grid,
        model_to_bed=model_to_bed,
    )
    output_path.write_text('\n'.join(lines), encoding='utf-8')
    return output_path


def generate_fixed_width_bitmap(
    result: RoutingResult,
    trace_width_mm: float,
    *,
    grid: BedBitmap,
    model_to_bed: tuple[float, float] = (0.0, 0.0),
) -> list[str]:
    """Render traces at a fixed width — used by debug/test routes only.

    This bypasses inflated polygons and rasterizes the raw centreline
    paths at a uniform ``trace_width_mm``.
    """
    pixel_size = grid.pixel_size_mm
    cols = grid.cols
    rows = grid.rows
    dx, dy = model_to_bed

    ink_cells: set[tuple[int, int]] = set()
    for trace in result.traces:
        bed_path = [(x + dx, -y + dy) for x, y in trace.path]
        ink_cells |= _trace_cells(bed_path, trace_width_mm, pixel_size, cols, rows)

    lines: list[str] = []
    for r in range(rows):
        line_chars = []
        for c in range(cols):
            line_chars.append('1' if (r, c) in ink_cells else '0')
        lines.append(''.join(line_chars))
    return lines
