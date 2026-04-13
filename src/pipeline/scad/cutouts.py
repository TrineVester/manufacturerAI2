"""cutouts.py — build Cutout objects from placement, routing and catalog.

A ``Cutout`` is a 2-D polygon that gets extruded and subtracted from the
shell body.  The z_base + depth pair says exactly where in the Z-stack the
subtraction happens.  All cutouts are collected into a list and passed to
emit.py, which writes them as a single ``difference()`` block.

Z-layer constants (all in mm, from shell bottom at z=0)
────────────────────────────────────────────────────────
  FLOOR_TOP    = 2.0   solid floor — nothing cut below this line
  CAVITY_TOP   = 3.0   pinholes extend from FLOOR_TOP up to here
  CEIL_THICK   = 2.0   solid ceiling this thick below base_height_mm
  CEIL_START   = base_h − CEIL_THICK

  Cavity zone  = CAVITY_TOP → CEIL_START   (component pockets + trace channels)
  Surface zone = CEIL_START → dome_apex    (LED / button holes pierce ceiling)

Pinhole geometry
────────────────
Each through-hole pin gets a two-step channel:
  1. Shaft  (FLOOR_TOP → taper_z)      tight pin_d square, press-fit
  2. Taper  (taper_z   → CAVITY_TOP)   wider square, guides insertion and
                                        lets conductive filament bridge in
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field

from src.catalog.models import Component, CatalogResult
from src.pipeline.design.models import Outline, Enclosure
from src.pipeline.design.height_field import blended_height
from src.pipeline.placer.models import PlacedComponent, FullPlacement
from src.pipeline.router.models import RoutingResult

log = logging.getLogger(__name__)

# ── Z layer constants ──────────────────────────────────────────────

FLOOR_TOP: float = 2.0       # solid floor up to here
CAVITY_TOP: float = 3.0      # cavity / trace zone starts here
CEIL_THICKNESS: float = 2.0  # solid ceiling below base_height
SURFACE_OVERSHOOT: float = 0.5  # extra depth so surface holes cleanly exit

# ── Pinhole geometry ───────────────────────────────────────────────

PINHOLE_CLEARANCE: float = 0.15   # added to catalog hole_diameter_mm
PINHOLE_TAPER_D: float = 1.4      # wide entry funnel side length (mm)
PINHOLE_TAPER_DEPTH: float = 0.5  # taper zone height
PINHOLE_FUNNEL_STEPS: int = 3     # graduated funnel layers (smooth insertion)

# ── Trace / component dimensions  ──────────────────────────────────

TRACE_WIDTH: float = 1.2           # conductive-filament channel width (mm)
COMPONENT_MARGIN: float = 1.5      # extra clearance around body footprint

# ── Button snap-fit geometry ───────────────────────────────────────

SNAP_WALL: float = 1.2             # socket wall thickness (mm)
SNAP_LIP: float = 0.5              # inward lip that retains the cap (mm)
SNAP_LIP_HEIGHT: float = 0.4       # lip thickness in Z (mm)
SNAP_GAP: float = 0.3              # clearance between cap and socket wall

# ── Support platform ──────────────────────────────────────────────

SUPPORT_PLATFORM_H: float = 1.0    # default platform height above CAVITY_TOP (mm)
SUPPORT_PAD: float = 1.0           # extra XY margin beyond body for platform


# ── Data type ─────────────────────────────────────────────────────


@dataclass
class Cutout:
    """A 2-D polygon extruded and subtracted from the shell body.

    If ``cylinder_r`` is set the polygon field is ignored and a
    ``cylinder(h=depth, r=cylinder_r)`` centred on (cylinder_cx, cylinder_cy)
    is emitted instead — far faster for CGAL than a polygon extrude.
    """

    polygon: list[list[float]]   # CCW vertices in mm (ignored when cylinder_r set)
    depth: float                 # extrusion height of the cut (mm)
    z_base: float = 0.0          # z where the cut starts (0 = shell bottom)
    label: str = ""              # comment written in the SCAD output
    cylinder_r: float | None = None   # if set → emit cylinder() instead of polygon
    cylinder_cx: float = 0.0          # cylinder centre x (mm)
    cylinder_cy: float = 0.0          # cylinder centre y (mm)


# ── Geometry helpers ───────────────────────────────────────────────


def _rect(cx: float, cy: float, w: float, h: float) -> list[list[float]]:
    """CCW rectangle centred on (cx, cy)."""
    hw, hh = w / 2, h / 2
    return [
        [cx - hw, cy - hh],
        [cx + hw, cy - hh],
        [cx + hw, cy + hh],
        [cx - hw, cy + hh],
    ]


def _circle(cx: float, cy: float, r: float, n: int = 24) -> list[list[float]]:
    """Approximate circle as n-gon CCW."""
    return [
        [cx + r * math.cos(2 * math.pi * i / n),
         cy + r * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]


def _rotate_pt(px: float, py: float, angle_deg: int) -> tuple[float, float]:
    """Rotate point (px, py) around the origin by angle_deg degrees."""
    if angle_deg == 0:
        return px, py
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return px * cos_a - py * sin_a, px * sin_a + py * cos_a


def _segment_rect(
    x1: float, y1: float,
    x2: float, y2: float,
    width: float,
) -> list[list[float]]:
    """CCW rectangle along segment (x1,y1)→(x2,y2) with the given width."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return _rect(x1, y1, width, width)
    px = -dy / length * width / 2
    py =  dx / length * width / 2
    return [
        [x1 - px, y1 - py],
        [x2 - px, y2 - py],
        [x2 + px, y2 + py],
        [x1 + px, y1 + py],
    ]


# ── Public API ─────────────────────────────────────────────────────


def build_cutouts(
    placement: FullPlacement,
    routing: RoutingResult,
    catalog: CatalogResult,
    outline: Outline,
    enclosure: Enclosure,
) -> list[Cutout]:
    """Build the complete cutout list from placement + routing + catalog.

    Returns a list of ``Cutout`` objects ready to be passed to
    ``emit.generate_scad()``.
    """
    base_h = enclosure.height_mm
    ceil_start = base_h - CEIL_THICKNESS
    cavity_depth = ceil_start - CAVITY_TOP   # mm of open cavity

    cuts: list[Cutout] = []

    # Fast index for catalog look-ups
    cat_index: dict[str, Component] = {c.id: c for c in catalog.components}

    # 1. Component pockets, surface holes, pinholes
    for comp in placement.components:
        cat = cat_index.get(comp.catalog_id)
        if cat is None:
            log.warning("Unknown catalog entry '%s' — skipping cutouts", comp.catalog_id)
            continue

        _component_cutouts(
            comp, cat, cuts,
            outline, enclosure,
            base_h, ceil_start, cavity_depth,
        )

    # 2. Trace channels
    _trace_channels(routing, cuts, ceil_start)

    log.debug("build_cutouts: %d total cutouts", len(cuts))
    return cuts


# ── Component dispatch ─────────────────────────────────────────────


def _component_cutouts(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    outline: Outline,
    enclosure: Enclosure,
    base_h: float,
    ceil_start: float,
    cavity_depth: float,
) -> None:
    style = cat.mounting.style
    if style == "top":
        _top_mount(comp, cat, cuts, outline, enclosure, ceil_start, cavity_depth)
    elif style == "bottom":
        _bottom_mount(comp, cat, cuts, base_h, ceil_start, cavity_depth)
    elif style == "side":
        _side_mount(comp, cat, cuts, outline, enclosure, ceil_start, cavity_depth)
    else:
        # "internal" and everything else
        _internal_mount(comp, cat, cuts, ceil_start, cavity_depth)

    # Sound holes (speaker grille etc.) — processed regardless of mount style
    if hasattr(cat.mounting, 'sound_holes') and cat.mounting.sound_holes:
        sh = cat.mounting.sound_holes
        if getattr(sh, 'enabled', False):
            _sound_holes(comp, cat, cuts, outline, enclosure, ceil_start)


# ── Top-mount components ───────────────────────────────────────────


def _top_mount(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    outline: Outline,
    enclosure: Enclosure,
    ceil_start: float,
    cavity_depth: float,
) -> None:
    """Cutouts for surface-facing components (LEDs, buttons, any top-mount)."""
    cx, cy = comp.x_mm, comp.y_mm
    cid = comp.instance_id
    body = cat.body
    mounting = cat.mounting

    # Height of the local dome surface above this component
    dome_z = blended_height(cx, cy, outline, enclosure)
    surface_depth = dome_z - ceil_start + SURFACE_OVERSHOOT

    # ── Button (has a cap) ─────────────────────────────────────────
    if mounting.cap is not None:
        cap = mounting.cap
        cap_r = (cap.diameter_mm + 2 * cap.hole_clearance_mm) / 2

        # Cap hole: circle drilled from ceiling through dome
        cuts.append(Cutout(
            polygon=[],
            depth=max(surface_depth, 1.0),
            z_base=ceil_start,
            label=f"cap hole  — {cid}",
            cylinder_r=cap_r,
            cylinder_cx=cx,
            cylinder_cy=cy,
        ))

        # Socket wall: annular ring around the cap hole inside the cavity.
        # The socket is a cylinder whose inner radius matches the cap hole
        # and whose outer radius adds SNAP_WALL thickness.  We approximate
        # it as a polygon difference (outer circle - inner circle) emitted
        # as two separate cutouts:
        #   1. Outer socket pocket (larger cylinder) at full cavity depth
        #   2. Inner socket bore (cap-sized cylinder) at cavity depth
        # Only the body pocket cutout is needed; the cap hole already
        # carves the bore.  The socket is realised by the body pocket
        # being wider than the cap hole.
        socket_r = cap_r + SNAP_WALL + SNAP_GAP
        bw = max((body.width_mm or 6.0) + 2 * COMPONENT_MARGIN, socket_r * 2)
        bh = max((body.length_mm or 6.0) + 2 * COMPONENT_MARGIN, socket_r * 2)
        poly = _rect(cx, cy, bw, bh)
        if comp.rotation_deg:
            poly = _rotated(poly, comp.rotation_deg, cx, cy)
        cuts.append(Cutout(
            polygon=poly,
            depth=cavity_depth,
            z_base=CAVITY_TOP,
            label=f"button body — {cid}",
        ))

        # Snap-fit lip: a shallow annular ring just below the ceiling
        # that is NARROWER than the cap hole, creating an inward lip
        # that retains the cap once pushed past it.
        lip_r = cap_r - SNAP_LIP
        if lip_r > 0 and SNAP_LIP_HEIGHT > 0:
            cuts.append(Cutout(
                polygon=[],
                depth=SNAP_LIP_HEIGHT,
                z_base=ceil_start - SNAP_LIP_HEIGHT,
                label=f"snap lip — {cid}",
                cylinder_r=lip_r,
                cylinder_cx=cx,
                cylinder_cy=cy,
            ))

        _pinholes(comp, cat, cuts)
        return

    # ── Circle body (LED 5mm, IR emitter, etc.) ────────────────────
    if body.shape == "circle":
        body_r = (body.diameter_mm or 5.0) / 2

        # Surface hole: slightly oversized for clearance
        hole_r = body_r + 0.3
        cuts.append(Cutout(
            polygon=[],
            depth=max(surface_depth, 1.0),
            z_base=ceil_start,
            label=f"LED hole  — {cid}",
            cylinder_r=hole_r,
            cylinder_cx=cx,
            cylinder_cy=cy,
        ))

        # Body pocket: holds the LED body / shoulder
        pocket_r = body_r + COMPONENT_MARGIN
        cuts.append(Cutout(
            polygon=[],
            depth=cavity_depth,
            z_base=CAVITY_TOP,
            label=f"LED body  — {cid}",
            cylinder_r=pocket_r,
            cylinder_cx=cx,
            cylinder_cy=cy,
        ))

        _pinholes(comp, cat, cuts)
        return

    # ── Rect body (generic top-mount) ─────────────────────────────
    bw = (body.width_mm  or 5.0) + COMPONENT_MARGIN
    bh = (body.length_mm or 5.0) + COMPONENT_MARGIN
    poly = _rect(cx, cy, bw, bh)
    if comp.rotation_deg:
        poly = _rotated(poly, comp.rotation_deg, cx, cy)

    cuts.append(Cutout(
        polygon=poly,
        depth=max(surface_depth, 1.0),
        z_base=ceil_start,
        label=f"top surface hole — {cid}",
    ))
    cuts.append(Cutout(
        polygon=_rect(cx, cy, bw + 2 * COMPONENT_MARGIN, bh + 2 * COMPONENT_MARGIN),
        depth=cavity_depth,
        z_base=CAVITY_TOP,
        label=f"top body pocket  — {cid}",
    ))

    _pinholes(comp, cat, cuts)


# ── Bottom-mount components ────────────────────────────────────────


def _bottom_mount(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    base_h: float,
    ceil_start: float,
    cavity_depth: float,
) -> None:
    """Cutouts for bottom-mounted components (e.g. battery holder with hatch)."""
    cx, cy = comp.x_mm, comp.y_mm
    cid = comp.instance_id
    body = cat.body
    mounting = cat.mounting

    margin = COMPONENT_MARGIN
    bw = (body.width_mm  or 25.0) + 2 * margin
    bh = (body.length_mm or 48.0) + 2 * margin
    body_h = body.height_mm

    # Body pocket inside the cavity (from CAVITY_TOP upward)
    pocket_depth = min(body_h + margin, ceil_start - CAVITY_TOP)
    cuts.append(Cutout(
        polygon=_rect(cx, cy, bw, bh),
        depth=pocket_depth,
        z_base=CAVITY_TOP,
        label=f"bottom-mount body — {cid}",
    ))

    # Battery hatch opening through the floor (batteries loaded from bottom)
    if mounting.hatch and mounting.hatch.enabled:
        hatch_clr = mounting.hatch.clearance_mm
        hw2 = (body.width_mm  or 25.0) / 2 - hatch_clr
        hh2 = (body.length_mm or 48.0) / 2 - hatch_clr
        # Extend 1 mm below shell bottom (z=-1) to guarantee a clean cut
        hatch_depth = FLOOR_TOP + 1.5
        cuts.append(Cutout(
            polygon=[
                [cx - hw2, cy - hh2],
                [cx + hw2, cy - hh2],
                [cx + hw2, cy + hh2],
                [cx - hw2, cy + hh2],
            ],
            depth=hatch_depth,
            z_base=-1.0,
            label=f"battery floor opening — {cid}",
        ))

        # Ledge recesses on the long sides (hatch panel rests here)
        hatch_thick = mounting.hatch.thickness_mm
        ledge_w = 2.5
        ledge_d = hatch_thick + 0.3
        half_bw = (body.width_mm or 25.0) / 2 - hatch_clr
        for side in (-1, 1):
            ledge_cx = cx + side * (half_bw - ledge_w / 2)
            cuts.append(Cutout(
                polygon=_rect(ledge_cx, cy, ledge_w, (body.length_mm or 48.0) - hatch_clr * 2),
                depth=ledge_d + 0.5,
                z_base=-0.5,
                label=f"battery ledge — {cid}",
            ))

    # Pin pinholes
    _pinholes(comp, cat, cuts)


# ── Internal components ────────────────────────────────────────────


def _internal_mount(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    ceil_start: float,
    cavity_depth: float,
) -> None:
    """Pocket + support platform + pinholes for internal components.

    Internal components (MCU, resistors, etc.) need:
      1. A body pocket that clears the component + margins.
      2. A support platform just below the pocket — a solid ledge that
         remains after the cavity is carved.  This keeps the component
         at the correct Z height for pin alignment.
    """
    cx, cy = comp.x_mm, comp.y_mm
    cid = comp.instance_id
    body = cat.body
    margin = COMPONENT_MARGIN

    # Determine the component sitting height: pin legs extend from
    # FLOOR_TOP to CAVITY_TOP (0.5-1mm).  The body itself rests at
    # CAVITY_TOP, so we carve the body pocket starting there.  A
    # support platform lip around the pocket holds the body in place.
    body_h = body.height_mm
    # Only create a pocket as deep as the body + margin (don't over-carve)
    pocket_depth = min(body_h + margin, cavity_depth)

    if body.shape == "rect":
        w = (body.width_mm  or 5.0) + 2 * margin
        h = (body.length_mm or 5.0) + 2 * margin
        poly = _rect(cx, cy, w, h)
        if comp.rotation_deg:
            poly = _rotated(poly, comp.rotation_deg, cx, cy)
    elif body.shape == "circle":
        r = (body.diameter_mm or 5.0) / 2 + margin
        cuts.append(Cutout(
            polygon=[],
            depth=pocket_depth,
            z_base=CAVITY_TOP,
            label=f"body pocket — {cid}",
            cylinder_r=r,
            cylinder_cx=cx,
            cylinder_cy=cy,
        ))
        # Support platform — wider pocket at a shallower depth above
        # the body pocket so a rim of solid material supports the
        # component from below.
        plat_r = r + SUPPORT_PAD
        plat_depth = SUPPORT_PLATFORM_H
        if pocket_depth + plat_depth <= cavity_depth:
            cuts.append(Cutout(
                polygon=[],
                depth=plat_depth,
                z_base=CAVITY_TOP + pocket_depth,
                label=f"support platform — {cid}",
                cylinder_r=plat_r,
                cylinder_cx=cx,
                cylinder_cy=cy,
            ))
        _pinholes(comp, cat, cuts)
        return
    else:
        poly = _rect(cx, cy, 8.0 + 2 * margin, 8.0 + 2 * margin)

    cuts.append(Cutout(
        polygon=poly,
        depth=pocket_depth,
        z_base=CAVITY_TOP,
        label=f"body pocket — {cid}",
    ))

    # Support platform — a wider, shallower pocket above the body
    # pocket.  The resulting ledge supports the component at the
    # correct Z height.
    if body.shape == "rect":
        plat_w = (body.width_mm  or 5.0) + 2 * margin + 2 * SUPPORT_PAD
        plat_h = (body.length_mm or 5.0) + 2 * margin + 2 * SUPPORT_PAD
        plat_poly = _rect(cx, cy, plat_w, plat_h)
        if comp.rotation_deg:
            plat_poly = _rotated(plat_poly, comp.rotation_deg, cx, cy)
    else:
        plat_poly = _rect(cx, cy,
                          8.0 + 2 * margin + 2 * SUPPORT_PAD,
                          8.0 + 2 * margin + 2 * SUPPORT_PAD)

    plat_depth = SUPPORT_PLATFORM_H
    if pocket_depth + plat_depth <= cavity_depth:
        cuts.append(Cutout(
            polygon=plat_poly,
            depth=plat_depth,
            z_base=CAVITY_TOP + pocket_depth,
            label=f"support platform — {cid}",
        ))

    _pinholes(comp, cat, cuts)


# ── Side-mount components ──────────────────────────────────────────

# Wall aperture clearance added around body dimensions.
SIDE_APERTURE_CLR: float = 0.4

def _side_mount(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    outline: Outline,
    enclosure: Enclosure,
    ceil_start: float,
    cavity_depth: float,
) -> None:
    """Cutouts for side-mounted components (USB port, slide switch, etc.).

    Creates:
      1. A wall aperture piercing through the shell wall so the component
         face is accessible from outside.
      2. An internal body pocket for the component to sit in.
      3. Pinholes for electrical connections.
    """
    cx, cy = comp.x_mm, comp.y_mm
    cid = comp.instance_id
    body = cat.body
    margin = COMPONENT_MARGIN

    body_w = body.width_mm or 5.0
    body_l = body.length_mm or 5.0
    body_h = body.height_mm

    # Body pocket in the cavity (same as internal mount)
    pocket_depth = min(body_h + margin, cavity_depth)
    bw = body_w + 2 * margin
    bh = body_l + 2 * margin
    poly = _rect(cx, cy, bw, bh)
    if comp.rotation_deg:
        poly = _rotated(poly, comp.rotation_deg, cx, cy)
    cuts.append(Cutout(
        polygon=poly,
        depth=pocket_depth,
        z_base=CAVITY_TOP,
        label=f"side-mount body — {cid}",
    ))

    # Wall aperture: a rectangular hole from outside through the shell wall.
    # We find the nearest outline edge and extend the aperture outward.
    if outline and outline.points:
        _wall_aperture(comp, cat, cuts, outline, enclosure, ceil_start)

    _pinholes(comp, cat, cuts)


def _wall_aperture(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    outline: Outline,
    enclosure: Enclosure,
    ceil_start: float,
) -> None:
    """Create a rectangular wall aperture for a side-mount component.

    Finds the nearest outline edge, then cuts a rectangular hole through
    the wall at the component's position.
    """
    cx, cy = comp.x_mm, comp.y_mm
    body = cat.body
    body_w = body.width_mm or 5.0
    body_h = body.height_mm

    pts = [(p.x, p.y) for p in outline.points]
    if len(pts) < 3:
        return

    # Find nearest edge and its direction
    best_dist = float('inf')
    best_mid = (cx, cy)
    best_normal = (0.0, 1.0)

    for i in range(len(pts)):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % len(pts)]
        # Project component position onto edge segment
        ex, ey = bx - ax, by - ay
        seg_len = math.hypot(ex, ey)
        if seg_len < 1e-6:
            continue
        ex /= seg_len
        ey /= seg_len
        t = max(0.0, min(seg_len, (cx - ax) * ex + (cy - ay) * ey))
        foot_x = ax + t * ex
        foot_y = ay + t * ey
        dist = math.hypot(cx - foot_x, cy - foot_y)
        if dist < best_dist:
            best_dist = dist
            best_mid = (foot_x, foot_y)
            # Outward normal (assuming CCW outline)
            best_normal = (ey, -ex)

    # Aperture dimensions: component width wide, body height tall
    aperture_w = body_w + 2 * SIDE_APERTURE_CLR
    aperture_h = body_h + 2 * SIDE_APERTURE_CLR

    # Aperture is centred on the nearest wall point, extending outward
    # through the full shell wall thickness (enough to pierce)
    wall_thick = 5.0  # generous wall pierce depth
    nx, ny = best_normal
    mid_x = best_mid[0] + nx * wall_thick / 2
    mid_y = best_mid[1] + ny * wall_thick / 2

    # Build the aperture rectangle aligned with the wall edge
    # Direction along the wall
    wall_dir_x, wall_dir_y = -ny, nx
    hw = aperture_w / 2
    hd = wall_thick / 2

    poly = [
        [mid_x - wall_dir_x * hw - nx * hd, mid_y - wall_dir_y * hw - ny * hd],
        [mid_x + wall_dir_x * hw - nx * hd, mid_y + wall_dir_y * hw - ny * hd],
        [mid_x + wall_dir_x * hw + nx * hd, mid_y + wall_dir_y * hw + ny * hd],
        [mid_x - wall_dir_x * hw + nx * hd, mid_y - wall_dir_y * hw + ny * hd],
    ]

    # Z position: component sits on the cavity floor, aperture is at body height
    z_base = CAVITY_TOP
    depth = min(aperture_h, ceil_start - CAVITY_TOP + SURFACE_OVERSHOOT)
    cuts.append(Cutout(
        polygon=poly,
        depth=depth,
        z_base=z_base,
        label=f"wall aperture — {comp.instance_id}",
    ))


# ── Sound holes ────────────────────────────────────────────────────


def _sound_holes(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
    outline: Outline,
    enclosure: Enclosure,
    ceil_start: float,
) -> None:
    """Grid of small holes in the ceiling above a speaker/buzzer."""
    sh = cat.mounting.sound_holes
    if not sh or not sh.enabled:
        return

    cx, cy = comp.x_mm, comp.y_mm
    body = cat.body
    hole_r = sh.hole_diameter_mm / 2
    spacing = sh.hole_spacing_mm

    # Determine the area to fill with holes
    if body.shape == "circle":
        cover_r = (body.diameter_mm or 10.0) / 2 - 1.0
    else:
        cover_r = min(body.width_mm or 10.0, body.length_mm or 10.0) / 2 - 1.0

    dome_z = blended_height(cx, cy, outline, enclosure)
    surface_depth = dome_z - ceil_start + SURFACE_OVERSHOOT

    # Fill a grid of holes within the component's footprint radius
    n = int(cover_r / spacing) + 1
    for ix in range(-n, n + 1):
        for iy in range(-n, n + 1):
            hx = cx + ix * spacing
            hy = cy + iy * spacing
            dist = math.hypot(hx - cx, hy - cy)
            if dist > cover_r:
                continue
            cuts.append(Cutout(
                polygon=[],
                depth=max(surface_depth, 1.0),
                z_base=ceil_start,
                label=f"sound hole — {comp.instance_id}",
                cylinder_r=hole_r,
                cylinder_cx=hx,
                cylinder_cy=hy,
            ))


# ── Shared: pinholes ──────────────────────────────────────────────


def _pinholes(
    comp: PlacedComponent,
    cat: Component,
    cuts: list[Cutout],
) -> None:
    """Add press-fit shaft + graduated funnel pinholes for every pin.

    The pinhole is a multi-layer column:
      • Shaft  (FLOOR_TOP → taper_z) — tight square for press-fit.
      • Funnel (taper_z  → CAVITY_TOP) — graduated steps from pin_d to
        PINHOLE_TAPER_D, each step widening linearly.  The funnel
        guides component insertion and lets conductive filament bridge in.
    """
    cx, cy = comp.x_mm, comp.y_mm
    rot = comp.rotation_deg
    cid = comp.instance_id

    shaft_h = (CAVITY_TOP - FLOOR_TOP) - PINHOLE_TAPER_DEPTH
    taper_z = FLOOR_TOP + shaft_h
    n_steps = max(1, PINHOLE_FUNNEL_STEPS)
    step_h = PINHOLE_TAPER_DEPTH / n_steps

    for pin in cat.pins:
        px_rel, py_rel = float(pin.position_mm[0]), float(pin.position_mm[1])
        if rot:
            px_rel, py_rel = _rotate_pt(px_rel, py_rel, rot)
        px = cx + px_rel
        py = cy + py_rel

        # Determine shaft dimensions based on pin shape
        pin_shape = getattr(pin, "shape", "round")
        clearance = PINHOLE_CLEARANCE

        if pin_shape == "rect" and pin.shape_width_mm and pin.shape_length_mm:
            shaft_w = pin.shape_width_mm + clearance
            shaft_l = pin.shape_length_mm + clearance
            # Apply rotation for rect/slot shapes
            if rot in (90, 270):
                shaft_w, shaft_l = shaft_l, shaft_w
        elif pin_shape == "slot" and pin.shape_width_mm and pin.shape_length_mm:
            shaft_w = pin.shape_width_mm + clearance
            shaft_l = pin.shape_length_mm + clearance
            if rot in (90, 270):
                shaft_w, shaft_l = shaft_l, shaft_w
        else:
            # Round or default — square hole
            pin_d = pin.hole_diameter_mm + clearance
            shaft_w = pin_d
            shaft_l = pin_d

        # Shaft — tight press-fit
        cuts.append(Cutout(
            polygon=_rect(px, py, shaft_w, shaft_l),
            depth=shaft_h,
            z_base=FLOOR_TOP,
            label=f"pin {cid}:{pin.id}",
        ))

        # Graduated funnel — N steps widening from shaft dims to taper dims
        taper_w = max(PINHOLE_TAPER_D, shaft_w + 0.4)
        taper_l = max(PINHOLE_TAPER_D, shaft_l + 0.4)
        for step in range(n_steps):
            t = (step + 1) / n_steps   # 0..1 for linear interpolation
            dw = shaft_w + (taper_w - shaft_w) * t
            dl = shaft_l + (taper_l - shaft_l) * t
            cuts.append(Cutout(
                polygon=_rect(px, py, dw, dl),
                depth=step_h,
                z_base=taper_z + step * step_h,
                label=f"pin funnel {cid}:{pin.id}",
            ))


# ── Shared: trace channels ─────────────────────────────────────────


def _trace_channels(
    routing: RoutingResult,
    cuts: list[Cutout],
    ceil_start: float,
) -> None:
    """Add a channel pocket for every segment in every routed trace.

    Channels span the full cavity height (CAVITY_TOP → ceil_start) so
    the conductive filament fill makes solid contact with vertical pin
    shafts at each pad.
    """
    channel_depth = ceil_start - CAVITY_TOP

    for trace in routing.traces:
        path = trace.path
        if len(path) < 2:
            continue

        for i in range(len(path) - 1):
            x1, y1 = float(path[i][0]),   float(path[i][1])
            x2, y2 = float(path[i + 1][0]), float(path[i + 1][1])

            if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                continue

            cuts.append(Cutout(
                polygon=_segment_rect(x1, y1, x2, y2, TRACE_WIDTH),
                depth=channel_depth,
                z_base=CAVITY_TOP,
                label=f"trace {trace.net_id}",
            ))


# ── Rotation helper ────────────────────────────────────────────────


def _rotated(
    pts: list[list[float]],
    angle_deg: int,
    cx: float,
    cy: float,
) -> list[list[float]]:
    """Rotate a polygon by angle_deg around (cx, cy)."""
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    result = []
    for px, py in pts:
        dx, dy = px - cx, py - cy
        result.append([
            cx + dx * cos_a - dy * sin_a,
            cy + dx * sin_a + dy * cos_a,
        ])
    return result
