"""Print-job manifest generator (optional).

Produces ``print_job.json`` — a convenience file that bundles the
physical parameters of a print job.  silver3dprinter does **not**
require this file; it only needs the trace bitmap and the embedded
``;silverink`` pause marker in the G-code.

The manifest is still generated when explicitly requested (e.g. by the
calibration debug endpoint) but is no longer part of the mandatory
manufacturing pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from src.pipeline.config import (
    PrinterDef,
    PIXEL_SIZE_MM,
    FLOOR_MM,
    TRACE_HEIGHT_MM,
    BedBitmap,
    get_printer,
)


@dataclass
class PrintJobManifest:
    """Physical parameters for a single print job."""

    # Bed
    bed_width_mm: float
    bed_depth_mm: float
    nominal_bed_width_mm: float
    nominal_bed_depth_mm: float

    # Keepout margins
    keepout_left_mm: float
    keepout_right_mm: float
    keepout_front_mm: float
    keepout_back_mm: float

    # Part bounding box on the bed (absolute bed coordinates)
    part_origin_x_mm: float
    part_origin_y_mm: float
    part_width_mm: float
    part_depth_mm: float

    # Bitmap
    bitmap_file: str
    bitmap_cols: int
    bitmap_rows: int
    pixel_size_x_mm: float
    pixel_size_y_mm: float

    # Ink layer
    ink_z_mm: float
    trace_height_mm: float

    # Gcode
    gcode_file: str
    ink_pause_marker: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_manifest(
    *,
    grid: BedBitmap,
    part_origin_x_mm: float,
    part_origin_y_mm: float,
    part_width_mm: float,
    part_depth_mm: float,
    gcode_file: str = "enclosure_staged.gcode",
    bitmap_file: str = "trace_bitmap.txt",
    printer: PrinterDef | None = None,
) -> PrintJobManifest:
    """Build a manifest from design geometry and hardware config."""
    pdef = printer or get_printer()

    return PrintJobManifest(
        bed_width_mm=pdef.bed_width,
        bed_depth_mm=pdef.bed_depth,
        nominal_bed_width_mm=pdef.nominal_bed_width,
        nominal_bed_depth_mm=pdef.nominal_bed_depth,
        keepout_left_mm=pdef.keepout_left,
        keepout_right_mm=pdef.keepout_right,
        keepout_front_mm=pdef.keepout_front,
        keepout_back_mm=pdef.keepout_back,
        part_origin_x_mm=round(part_origin_x_mm, 4),
        part_origin_y_mm=round(part_origin_y_mm, 4),
        part_width_mm=round(part_width_mm, 4),
        part_depth_mm=round(part_depth_mm, 4),
        bitmap_file=bitmap_file,
        bitmap_cols=grid.cols,
        bitmap_rows=grid.rows,
        pixel_size_x_mm=PIXEL_SIZE_MM,
        pixel_size_y_mm=PIXEL_SIZE_MM,
        ink_z_mm=FLOOR_MM + TRACE_HEIGHT_MM,
        trace_height_mm=TRACE_HEIGHT_MM,
        gcode_file=gcode_file,
        ink_pause_marker=";silverink",
    )


def write_manifest(manifest: PrintJobManifest, output_path: Path | str) -> Path:
    """Write the manifest as JSON."""
    output_path = Path(output_path)
    output_path.write_text(
        json.dumps(manifest.to_dict(), indent=2),
        encoding="utf-8",
    )
    return output_path
