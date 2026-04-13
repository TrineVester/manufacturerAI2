"""Trace bitmap rasterizer — convert routing traces to a 1-bit nozzle-pitch bitmap.

The Xaar 128 inkjet printhead has a nozzle pitch of 0.1371 mm (185 DPI).
Each trace centreline from the router is inflated to trace_width, then
rasterized into a grid of '1' (ink) and '0' (no ink) characters.

Coordinate system
─────────────────
  Row 0  → bed Y = 0   (front of bed)
  Row N  → bed Y = max  (back of bed)
  Col 0  → bed X = 0   (left)
  Col M  → bed X = max  (right)

  pixel_size = 0.1371 mm

Output format
─────────────
  trace_bitmap.txt — one row per line, each character '1' or '0'
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from src.session import Session
from src.pipeline.config import TRACE_RULES

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

PIXEL_SIZE_MM: float = 0.1371   # Xaar 128 nozzle pitch


@dataclass
class BitmapConfig:
    """Bitmap rasterization configuration."""

    pixel_size_mm: float = PIXEL_SIZE_MM
    cols: int = 0      # set from bed width
    rows: int = 0      # set from bed depth

    def bed_to_pixel(self, bed_x: float, bed_y: float) -> tuple[int, int]:
        """Convert bed coordinates (mm) to pixel grid (col, row)."""
        col = int(bed_x / self.pixel_size_mm)
        row = int(bed_y / self.pixel_size_mm)
        return col, row


@dataclass
class BitmapResult:
    """Result of trace rasterization."""

    success: bool
    message: str
    bitmap_path: Path | None = None
    cols: int = 0
    rows: int = 0
    pixel_size_mm: float = PIXEL_SIZE_MM
    trace_count: int = 0
    ink_pixels: int = 0


def _segment_cells(
    x0: float, y0: float,
    x1: float, y1: float,
    half_width: float,
    pixel_size: float,
) -> set[tuple[int, int]]:
    """Rasterize a single trace segment to pixel cells.

    Walks the centreline in steps of pixel_size * 0.5 and marks all
    pixels whose centre is within half_width of the line segment.
    """
    cells: set[tuple[int, int]] = set()

    dx = x1 - x0
    dy = y1 - y0
    seg_len = math.hypot(dx, dy)
    if seg_len < 1e-6:
        # Degenerate segment — just mark the single pixel
        col = int(x0 / pixel_size)
        row = int(y0 / pixel_size)
        cells.add((col, row))
        return cells

    # Step along centreline
    step = pixel_size * 0.5
    n_steps = max(1, int(seg_len / step))
    ux, uy = dx / seg_len, dy / seg_len

    # Also scan perpendicular to catch the full trace width
    perp_x, perp_y = -uy, ux
    n_perp = max(1, int(half_width / step))

    for i in range(n_steps + 1):
        t = i / n_steps
        cx = x0 + t * dx
        cy = y0 + t * dy

        for j in range(-n_perp, n_perp + 1):
            px = cx + j * step * perp_x
            py = cy + j * step * perp_y

            col = int(px / pixel_size)
            row = int(py / pixel_size)
            cells.add((col, row))

    return cells


def _trace_to_cells(
    path: list[tuple[float, float]],
    trace_width: float,
    pixel_size: float,
    offset_x: float,
    offset_y: float,
) -> set[tuple[int, int]]:
    """Rasterize a full trace (list of waypoints) to pixel cells.

    Applies bed offset and Y-mirror, then rasterizes each segment.
    """
    cells: set[tuple[int, int]] = set()
    half_w = trace_width / 2.0

    if len(path) < 2:
        return cells

    # Transform waypoints to bed coordinates
    bed_pts: list[tuple[float, float]] = []
    for x, y in path:
        bx = x + offset_x
        by = -y + offset_y  # Y-mirror: model Y-up → bed Y-front
        bed_pts.append((bx, by))

    for i in range(len(bed_pts) - 1):
        x0, y0 = bed_pts[i]
        x1, y1 = bed_pts[i + 1]
        cells |= _segment_cells(x0, y0, x1, y1, half_w, pixel_size)

    return cells


def _bed_center_offset(
    outline_verts: list[tuple[float, float]],
    bed_width: float,
    bed_height: float,
) -> tuple[float, float]:
    """Compute offset to center enclosure outline on the bitmap bed.

    Returns (offset_x, offset_y) in mm.
    """
    if not outline_verts:
        return bed_width / 2, bed_height / 2

    xs = [v[0] for v in outline_verts]
    ys = [v[1] for v in outline_verts]
    model_cx = (min(xs) + max(xs)) / 2
    model_cy = (min(ys) + max(ys)) / 2

    offset_x = bed_width / 2 - model_cx
    offset_y = bed_height / 2 + model_cy  # +cy because Y-mirror

    return offset_x, offset_y


def rasterize_traces(
    session: Session,
    bed_width_mm: float = 250.0,
    bed_depth_mm: float = 210.0,
) -> BitmapResult:
    """Rasterize routing traces to a 1-bit bitmap file.

    Parameters
    ----------
    session      : Session with routing.json
    bed_width_mm : Bed width in mm (default MK3S)
    bed_depth_mm : Bed depth in mm (default MK3S)

    Returns BitmapResult with path to trace_bitmap.txt.
    """
    # ── Load routing ──
    routing_raw = session.read_artifact("routing.json")
    if routing_raw is None:
        return BitmapResult(
            success=False,
            message="routing.json not found — run the router first.",
        )

    traces = routing_raw.get("traces", [])
    if not traces:
        return BitmapResult(
            success=False,
            message="No traces in routing.json.",
        )

    # ── Configure bitmap grid ──
    pixel_size = PIXEL_SIZE_MM
    cols = int(bed_width_mm / pixel_size)
    rows = int(bed_depth_mm / pixel_size)

    # ── Compute bed offset from outline ──
    outline_raw = routing_raw.get("outline", [])
    outline_verts: list[tuple[float, float]] = []
    for v in outline_raw:
        if isinstance(v, dict):
            outline_verts.append((v.get("x", 0), v.get("y", 0)))
        elif isinstance(v, (list, tuple)) and len(v) >= 2:
            outline_verts.append((float(v[0]), float(v[1])))

    offset_x, offset_y = _bed_center_offset(outline_verts, bed_width_mm, bed_depth_mm)

    # ── Rasterize all traces ──
    trace_width = TRACE_RULES.trace_width_mm
    all_cells: set[tuple[int, int]] = set()

    for trace in traces:
        path_raw = trace.get("path", [])
        path = [(p[0], p[1]) for p in path_raw if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(path) < 2:
            continue
        cells = _trace_to_cells(path, trace_width, pixel_size, offset_x, offset_y)
        all_cells |= cells

    # ── Build bitmap text ──
    # Filter out-of-bounds cells
    valid_cells = {(c, r) for c, r in all_cells if 0 <= c < cols and 0 <= r < rows}

    bitmap_lines: list[str] = []
    for r in range(rows):
        row_chars = ['0'] * cols
        for c, cr in ((c, cr) for c, cr in valid_cells if cr == r):
            row_chars[c] = '1'
        bitmap_lines.append("".join(row_chars))

    bitmap_text = "\n".join(bitmap_lines) + "\n"

    # ── Write output ──
    mfg_dir = session.path / "manufacturing"
    mfg_dir.mkdir(exist_ok=True)
    bitmap_path = mfg_dir / "trace_bitmap.txt"
    bitmap_path.write_text(bitmap_text, encoding="utf-8")

    ink_pixels = len(valid_cells)
    log.info(
        "Bitmap: %dx%d, %d traces, %d ink pixels (%.1f%% fill)",
        cols, rows, len(traces), ink_pixels,
        100.0 * ink_pixels / (cols * rows) if cols * rows else 0,
    )

    return BitmapResult(
        success=True,
        message=f"Rasterized {len(traces)} traces → {ink_pixels} ink pixels",
        bitmap_path=bitmap_path,
        cols=cols,
        rows=rows,
        pixel_size_mm=pixel_size,
        trace_count=len(traces),
        ink_pixels=ink_pixels,
    )
