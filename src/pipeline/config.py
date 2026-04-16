"""Shared physical constants for the manufacturing pipeline.

These values describe the physical properties of conductive-ink traces,
pin holes, and board edges.  Both the **placer** (which reserves routing
channels between components) and the **router** (which lays down actual
traces) derive their clearance parameters from this single source of truth.

Change a value here and both stages will stay in sync automatically.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ── Pixel resolution ───────────────────────────────────────────────
#
# The bitmap pixel size equals the Xaar 128 nozzle pitch.  This is the
# only printhead parameter manufacturerAI needs — everything else
# (nozzle count, firing, lanes, timing) is handled by the printer.

PIXEL_SIZE_MM: float = 0.1371

FDM_NOZZLE_D: float = 0.4
FDM_EXTRUSION_W: float = FDM_NOZZLE_D * 1.125  # 0.45 mm
TWO_WALLS_MM: float = 2 * FDM_EXTRUSION_W       # 0.9 mm


@dataclass(frozen=True)
class TraceRules:
    """Physical design rules for conductive-ink traces.

    All distances are in millimetres.
    """

    trace_width_mm: float = 1.0
    """Width of a single conductive-ink trace."""

    trace_clearance_mm: float = TWO_WALLS_MM
    """Minimum edge-to-edge gap between two traces (or a trace and
    another net's clearance zone).  Equals two FDM perimeter walls."""

    pin_clearance_mm: float = TWO_WALLS_MM
    """Minimum gap from a trace edge to a foreign pin centre.
    Equals two FDM perimeter walls."""

    edge_clearance_mm: float = 1.5
    """Minimum distance from a trace to the outline edge."""

    grid_resolution_mm: float = 0.5
    """Routing-grid cell size (mm).  Independent of the bitmap resolution;
    the bitmap is rendered from world-mm trace coordinates after routing."""

    max_trace_width_mm: float = 10.0
    """Maximum width a trace can expand to during Voronoi inflation."""

    pinhole_clearance_mm: float = 1.0
    """Extra mm added to each pin dimension for the physical 3D cutout
    (shaft hole in plastic).  Accounts for FDM print tolerance."""

    pinhole_taper_extra_mm: float = 1.0
    """Extra width on each side of the funnel mouth above the pin shaft."""

    pinhole_taper_depth_mm: float = 1.5
    """Height of the graduated funnel zone above the pin shaft."""

    max_common_hole_diameter_mm: float = 1.2
    """Largest common pin hole diameter across the catalog, used to
    derive minimum pin-to-pin centre distance."""

    # ── Derived helpers ────────────────────────────────────────────

    @property
    def routing_channel_mm(self) -> float:
        """Width needed per trace channel between components.

        One channel = trace_width + trace_clearance (the gap the router
        enforces on each side is already half the clearance, so one full
        clearance between two traces is correct).
        """
        return self.trace_width_mm + self.trace_clearance_mm

    @property
    def min_pin_clearance_mm(self) -> float:
        """Minimum centre-to-centre distance between pin holes of
        different components.

        Ensures a trace (with its clearance envelope) can pass between
        two pins without violating pin_clearance on either side.
        """
        return self.max_common_hole_diameter_mm + 2 * self.pin_clearance_mm

    @property
    def min_edge_clearance_mm(self) -> float:
        """Hard minimum body-to-outline distance for the placer.

        Matches the router edge clearance so traces at the body perimeter
        can still reach the outline-inset boundary.
        """
        return self.edge_clearance_mm


# Module-level singleton — importable everywhere.
TRACE_RULES = TraceRules()


# ── Bed bitmap ────────────────────────────────────────────────────
#
# The bitmap covers the entire nominal build plate with pixels at
# nozzle-pitch resolution.  No offset calculations happen here — the
# printer is responsible for applying its own calibrated FDM-to-inkjet
# offset when interpreting the bitmap during sweeps.


@dataclass(frozen=True)
class BedBitmap:
    """Full-bed bitmap geometry for a specific printer + printhead.

    Each pixel is one nozzle pitch wide/tall.  Column 0 = bed X = 0,
    row 0 = bed Y = 0.  The bitmap is a direct 1:1 projection of the
    build plate — no offsets, padding, or sweep alignment are applied.
    """

    cols: int
    rows: int
    pixel_size_mm: float

    def bed_to_pixel(self, bed_x: float, bed_y: float) -> tuple[float, float]:
        """Convert absolute bed coordinates (mm) to pixel coordinates."""
        return bed_x / self.pixel_size_mm, bed_y / self.pixel_size_mm


def bed_bitmap(pdef: "PrinterDef") -> BedBitmap:
    """Compute the bed bitmap geometry for a printer.

    Dimensions cover the full nominal bed at nozzle-pitch resolution.
    """
    cols = math.ceil(pdef.nominal_bed_width / PIXEL_SIZE_MM)
    rows = math.ceil(pdef.nominal_bed_depth / PIXEL_SIZE_MM)
    return BedBitmap(cols=cols, rows=rows, pixel_size_mm=PIXEL_SIZE_MM)



# ── Enclosure Z-layer constants (mm) ──────────────────────────────
#
# The enclosure is a vertical stack of zones.  Every stage that
# needs a Z-height (scad cutouts, pause-point computation, design
# validation) must reference these constants so they stay in sync.
#
#   0 ─── build plate
#   │  FLOOR_MM (1.6)        solid printed floor (ironed top surface)
#   │  FLOOR_MM              trace zone begins (conductive ink on ironed surface)
#   │  FLOOR_MM + TRACE_H    trace zone top (shallow channels, 0.3 mm)
#   │  CAVITY_START_MM (2.2) component zone begins (= FLOOR_MM + COMP_OFFSET)
#   │  ... component pockets / pin shafts ...
#   │  CEIL_START            = total_height - CEILING_MM
#   │  CEILING_MM (2)        solid printed ceiling
#   └── total_height

FLOOR_MM: float = 1.6
TRACE_HEIGHT_MM: float = 1.0
COMPONENT_OFFSET_MM: float = 0.6
CAVITY_START_MM: float = FLOOR_MM + COMPONENT_OFFSET_MM
CEILING_MM: float = 2.0
PIN_FLOOR_PENETRATION: float = 0.6


# ── Two-part enclosure: snap-fit constants (mm) ───────────────────

SPLIT_OVERLAP_MM: float = 2.0       # Lap joint overlap between top and bottom
SNAP_POST_WIDTH: float = 3.0        # Snap tab width along wall
SNAP_POST_HEIGHT: float = 4.0       # Snap tab protrusion above split plane
SNAP_POST_THICKNESS: float = 1.2    # Snap tab depth (wall-normal direction)
SNAP_BARB_MM: float = 0.3           # Barb overhang for click engagement
SNAP_CLEARANCE_MM: float = 0.3      # Clearance in female slot (per side)
SNAP_SPACING_MM: float = 25.0       # Max perimeter distance between snap posts
MIN_SNAP_POSTS: int = 4             # Minimum number of snap posts


def component_z_range(
    mounting_style: str,
    body_height_mm: float,
    pin_length_mm: float | None,
    ceil_start: float,
) -> tuple[float, float]:
    """Compute (body_floor_z, body_top_z) for a component.

    Top-mounted components (buttons, LEDs) are placed as high as
    possible so that the body top is flush with the ceiling.  Internal
    components sit at FLOOR_MM + pin_length, letting pins extend
    downward into deeper holes.  Bottom and side mounts stay at
    CAVITY_START_MM.

    Pin length is treated as a maximum (pins can be cut shorter).
    If pin_length_mm is None the body sits at CAVITY_START_MM.
    """
    if mounting_style in ("bottom", "side"):
        body_top = min(CAVITY_START_MM + body_height_mm, ceil_start)
        return CAVITY_START_MM, body_top

    effective_pin = pin_length_mm if pin_length_mm is not None else (CAVITY_START_MM - FLOOR_MM)

    if mounting_style == "top":
        body_floor = ceil_start - body_height_mm
        body_floor = max(body_floor, CAVITY_START_MM)
        needed_pin = body_floor - FLOOR_MM
        if needed_pin > effective_pin:
            body_floor = FLOOR_MM + effective_pin
            body_floor = max(body_floor, CAVITY_START_MM)
    else:
        body_floor = FLOOR_MM + effective_pin
        body_floor = max(body_floor, CAVITY_START_MM)
        if body_floor + body_height_mm > ceil_start:
            body_floor = ceil_start - body_height_mm
            body_floor = max(body_floor, CAVITY_START_MM)

    body_top = min(body_floor + body_height_mm, ceil_start)
    return body_floor, body_top


# ── Printer definitions ────────────────────────────────────────────

@dataclass(frozen=True)
class PrinterDef:
    """Static definition of a supported 3D printer.

    ``nominal_bed_width/depth`` are the physical bed dimensions matching
    PrusaSlicer's ``bed_shape``.

    ``keepout_*`` margins define the area the inkjet cannot reach.
    These are derived from the printer's calibrated FDM-to-inkjet offset
    and communicated by the printer.  The usable area for placing parts
    is the nominal bed minus these margins.

    The bitmap is a direct projection of the full nominal bed — no
    offset calculations are applied here.  The printer applies its own
    calibrated offset when interpreting the bitmap during sweeps.
    """
    id: str
    label: str
    nominal_bed_width: float   # mm — full bed (matches PrusaSlicer bed_shape)
    nominal_bed_depth: float   # mm
    keepout_left: float = 0.0    # mm — inkjet cannot reach this far from left edge
    keepout_right: float = 50.0  # mm — inkjet cannot reach this far from right edge
    keepout_front: float = 0.0   # mm — inkjet cannot reach this far from front edge
    keepout_back: float = 35.0   # mm — inkjet cannot reach this far from back edge
    max_z_mm: float = 210.0    # mm — maximum build height
    profile_filename: str = ""
    native_printer: str | None = None
    native_print: str | None = None
    native_material: str | None = None
    thumbnails: str | None = None

    @property
    def usable_width(self) -> float:
        """Usable print area width (nominal minus keepout margins)."""
        return self.nominal_bed_width - self.keepout_left - self.keepout_right

    @property
    def usable_depth(self) -> float:
        """Usable print area depth (nominal minus keepout margins)."""
        return self.nominal_bed_depth - self.keepout_front - self.keepout_back

    @property
    def bed_width(self) -> float:
        """Usable print area width (alias for backward compatibility)."""
        return self.usable_width

    @property
    def bed_depth(self) -> float:
        """Usable print area depth (alias for backward compatibility)."""
        return self.usable_depth

    @property
    def usable_center(self) -> tuple[float, float]:
        """Absolute bed coordinate of the usable-area centre."""
        return (
            self.keepout_left + self.usable_width / 2,
            self.keepout_front + self.usable_depth / 2,
        )


PRINTERS: dict[str, PrinterDef] = {
    "mk3s": PrinterDef(
        id="mk3s",
        label="Prusa MK3S",
        nominal_bed_width=250.0,
        nominal_bed_depth=210.0,
        keepout_left=0.0,
        keepout_right=64.67,
        keepout_front=0.0,
        keepout_back=34.84,
        max_z_mm=210.0,
        profile_filename="slicer_profile_mk3s.ini",
    ),
    "mk3s_plus": PrinterDef(
        id="mk3s_plus",
        label="Prusa i3 MK3S+",
        nominal_bed_width=250.0,
        nominal_bed_depth=210.0,
        keepout_left=0.0,
        keepout_right=64.67,
        keepout_front=0.0,
        keepout_back=34.84,
        max_z_mm=210.0,
        profile_filename="slicer_profile_mk3s_plus.ini",
    ),
    "coreone": PrinterDef(
        id="coreone",
        label="Prusa Core One+",
        nominal_bed_width=250.0,
        nominal_bed_depth=250.0,
        keepout_left=14.0,
        keepout_right=32.0,
        keepout_front=0.0,
        keepout_back=32.0,
        max_z_mm=220.0,
        profile_filename="slicer_profile_coreone.ini",
        native_printer="Prusa CORE One HF0.4 nozzle",
        native_print="0.20mm BALANCED @COREONE HF0.4",
        native_material="Prusament PLA @COREONE HF0.4",
        thumbnails="16x16/PNG,220x124/PNG",
    ),
}

DEFAULT_PRINTER = "coreone"


def get_printer(printer_id: str | None = None) -> PrinterDef:
    """Return the *PrinterDef* for *printer_id* (falls back to default)."""
    pid = (printer_id or DEFAULT_PRINTER).lower().strip()
    if pid not in PRINTERS:
        log.warning("Unknown printer '%s' — falling back to %s", pid, DEFAULT_PRINTER)
        pid = DEFAULT_PRINTER
    return PRINTERS[pid]
