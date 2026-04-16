"""extras.py — generate extra printable parts placed on the build plate.

Components can declare extra parts (via mounting.extras) that are printed
alongside the enclosure on the same build plate.  Each extra is a separate
SCAD body translated to a free area next to the enclosure.

Parts are described by shape + dimensions and rendered generically.
The only special case is shape="button", which delegates to the complex
button generator (socket + stem + cap with surface curvature).
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass

from src.catalog.models import BodyChannels, Component, ExtraPart
from src.pipeline.config import CAVITY_START_MM
from src.pipeline.design.models import Outline, Enclosure
from src.pipeline.placer.models import PlacedComponent

from .buttons import (
    build_button_configs,
    generate_button_scad,
    ButtonConfig,
)

log = logging.getLogger(__name__)

PART_GAP: float = 3.0


@dataclass
class PlacedExtra:
    """An extra part ready to emit, with its SCAD lines and footprint size."""
    label: str
    scad_lines: list[str]
    footprint_x: float
    footprint_y: float
    preamble: list[str] | None = None


def _resolve_dimensions(extra: ExtraPart, cat: Component) -> ExtraPart:
    """Fill in any missing dimensions from the component's body."""
    w = extra.width_mm or cat.body.width_mm
    l = extra.length_mm or cat.body.length_mm
    d = extra.diameter_mm or cat.body.diameter_mm
    t = extra.thickness_mm
    return ExtraPart(
        label=extra.label,
        shape=extra.shape,
        width_mm=w,
        length_mm=l,
        thickness_mm=t or 1.5,
        diameter_mm=d,
    )


def _generate_shape_scad(extra: ExtraPart) -> PlacedExtra:
    """Generate SCAD for a simple extruded shape (rect or circle)."""
    t = extra.thickness_mm or 1.5
    lines: list[str] = []

    if extra.shape == "circle":
        d = extra.diameter_mm or 10.0
        lines.append(f"linear_extrude(height = {t:.3f})")
        lines.append(f"  circle(d = {d:.3f}, $fn = 64);")
        return PlacedExtra(
            label=extra.label,
            scad_lines=lines,
            footprint_x=d,
            footprint_y=d,
        )

    w = extra.width_mm or 10.0
    l = extra.length_mm or 10.0
    lines.append(f"linear_extrude(height = {t:.3f})")
    lines.append(f"  square([{w:.3f}, {l:.3f}], center = true);")
    return PlacedExtra(
        label=extra.label,
        scad_lines=lines,
        footprint_x=w,
        footprint_y=l,
    )


TAB_WIDTH: float = 4.0
TAB_DEPTH: float = 0.8
TAB_HEIGHT: float = 1.5
LOOP_WIDTH: float = 4.0
LOOP_HEIGHT: float = 3.0
LOOP_THICKNESS: float = 1.0


def _generate_hatch_scad(extra: ExtraPart, channels: BodyChannels | None = None, body_height: float = 0) -> PlacedExtra:
    """Generate SCAD for a hatch panel with spring latch, ledge tab, and optional battery mold."""
    w = extra.width_mm or 24.4
    l = extra.length_mm or 45.9
    t = extra.thickness_mm or 1.5

    slit_w = LOOP_WIDTH + 1.0
    arm_gap = LOOP_THICKNESS * 2
    bend_r = arm_gap / 2 + LOOP_THICKNESS / 2
    slit_depth = LOOP_THICKNESS + arm_gap

    preamble = [
        f"hatch_w = {w:.3f};",
        f"hatch_l = {l:.3f};",
        f"hatch_t = {t:.3f};",
        f"loop_w = {LOOP_WIDTH:.3f};",
        f"loop_h = {LOOP_HEIGHT:.3f};",
        f"loop_t = {LOOP_THICKNESS:.3f};",
        f"tab_d = {TAB_DEPTH:.3f};",
        f"tab_h = {TAB_HEIGHT:.3f};",
        f"slit_w = {slit_w:.3f};",
        f"arm_gap = {arm_gap:.3f};",
        f"bend_r = {bend_r:.3f};",
        "",
        "module spring_latch() {",
        "  cube([loop_w, loop_t, loop_h]);",
        "  translate([0, -tab_d, hatch_t])",
        "    cube([loop_w, tab_d + loop_t, tab_h]);",
        "  translate([loop_w/2, loop_t + arm_gap/2, loop_h])",
        "    rotate([90, 0, 90])",
        "      rotate_extrude(angle=180, $fn=32)",
        "        translate([bend_r, 0, 0])",
        "          square([loop_t, loop_w], center=true);",
        "  translate([0, loop_t + arm_gap, 0])",
        "    cube([loop_w, loop_t, loop_h]);",
        "}",
    ]

    tw = TAB_WIDTH
    tab_x = (w - tw) / 2

    lines = [
        "difference() {",
        f"  cube([hatch_w, hatch_l, hatch_t]);",
        f"  translate([(hatch_w - slit_w) / 2, 0, -1])",
        f"    cube([slit_w, {slit_depth:.3f}, hatch_t + 2]);",
        "}",
        "",
    ]

    if channels:
        clearance = 0.3
        r = channels.diameter_mm / 2 - clearance
        cz_local = CAVITY_START_MM + channels.center_z_mm

        lines.append("difference() {")
        lines.append(f"  translate([(hatch_w - loop_w) / 2, 0, 0])")
        lines.append(f"    spring_latch();")
        for i in range(channels.count):
            offset = (i - (channels.count - 1) / 2) * channels.spacing_mm
            cx = w / 2 + offset
            lines.append(f"  intersection() {{")
            lines.append(f"    translate([{cx:.3f}, -1, {cz_local:.3f}])")
            lines.append(f"      rotate([-90, 0, 0])")
            lines.append(f"        cylinder(h = {l + 2:.3f}, r = {r:.3f}, $fn = 32);")
            lines.append(f"    translate([{cx - r:.3f}, -1, 0])")
            lines.append(f"      cube([{2 * r:.3f}, {l + 2:.3f}, {cz_local:.3f}]);")
            lines.append(f"  }}")
        lines.append("}")
    else:
        lines.append(f"translate([(hatch_w - loop_w) / 2, 0, 0])")
        lines.append(f"  spring_latch();")

    lines.append("")
    lines.append(f"translate([{tab_x:.3f}, hatch_l, hatch_t])")
    lines.append(f"  cube([{tw:.3f}, tab_d, tab_h]);")

    if channels:
        mold_h = cz_local - t
        mold_w = (channels.count - 1) * channels.spacing_mm + 2 * r
        mold_x = (w - mold_w) / 2

        spring_clearance_w = slit_w + 0.4
        spring_clearance_y = slit_depth + bend_r + LOOP_THICKNESS
        spring_cx = (w - spring_clearance_w) / 2

        lines.append("")
        lines.append("difference() {")
        lines.append(f"  translate([{mold_x:.3f}, 0, {t:.3f}])")
        lines.append(f"    cube([{mold_w:.3f}, {l:.3f}, {mold_h:.3f}]);")
        lines.append(f"  translate([{spring_cx:.3f}, -1, 0])")
        lines.append(f"    cube([{spring_clearance_w:.3f}, {spring_clearance_y + 1:.3f}, {mold_h + t + 1:.3f}]);")
        for i in range(channels.count):
            offset = (i - (channels.count - 1) / 2) * channels.spacing_mm
            cx = w / 2 + offset
            lines.append(f"  intersection() {{")
            lines.append(f"    translate([{cx:.3f}, -1, {cz_local:.3f}])")
            lines.append(f"      rotate([-90, 0, 0])")
            lines.append(f"        cylinder(h = {l + 2:.3f}, r = {r:.3f}, $fn = 32);")
            lines.append(f"    translate([{cx - r:.3f}, -1, 0])")
            lines.append(f"      cube([{2 * r:.3f}, {l + 2:.3f}, {cz_local:.3f}]);")
            lines.append(f"  }}")
        lines.append("}")

    return PlacedExtra(
        label=extra.label,
        scad_lines=lines,
        footprint_x=w,
        footprint_y=l,
        preamble=preamble,
    )


def collect_and_generate_extras(
    components: list[PlacedComponent],
    catalog_index: dict[str, Component],
    outline: Outline,
    enclosure: Enclosure,
    ceil_start: float,
) -> str:
    """Collect all extra parts and generate a standalone SCAD file.

    Extra parts are laid out in a row on their own build plate,
    starting at the origin.  Returns a complete SCAD string for
    a separate ``extras.scad`` file, or "" if no extras exist.
    """
    extras: list[PlacedExtra] = []

    button_configs = build_button_configs(
        components, catalog_index, outline, enclosure, ceil_start,
    )
    btn_cfg_map: dict[str, ButtonConfig] = {
        cfg.instance_id: cfg for cfg in button_configs
    }

    for comp in components:
        cat = catalog_index.get(comp.catalog_id)
        if cat is None:
            continue

        for extra in cat.mounting.extras:
            if extra.shape == "button":
                cfg = btn_cfg_map.get(comp.instance_id)
                if cfg is None:
                    continue
                btn_lines = generate_button_scad(cfg)
                if cfg.outline:
                    radius = max(math.hypot(p[0], p[1]) for p in cfg.outline)
                else:
                    radius = 5.0
                diameter = radius * 2
                extras.append(PlacedExtra(
                    label=extra.label,
                    scad_lines=btn_lines,
                    footprint_x=diameter,
                    footprint_y=diameter,
                ))
            elif extra.shape == "hatch":
                resolved = _resolve_dimensions(extra, cat)
                extras.append(_generate_hatch_scad(resolved, cat.body.channels, cat.body.height_mm))
            else:
                resolved = _resolve_dimensions(extra, cat)
                extras.append(_generate_shape_scad(resolved))

    if not extras:
        return ""

    parts: list[str] = []
    parts.append("// ============================================================")
    parts.append("// Extra parts — separate build plate")
    parts.append("// ============================================================")
    parts.append("")

    for extra in extras:
        if extra.preamble:
            parts.extend(extra.preamble)
            parts.append("")

    current_x = 0.0

    for extra in extras:
        place_x = current_x + extra.footprint_x / 2

        parts.append(f"// {extra.label}")
        parts.append(f"translate([{place_x:.3f}, 0, 0]) {{")
        for line in extra.scad_lines:
            parts.append(f"  {line}")
        parts.append("}")
        parts.append("")

        current_x = place_x + extra.footprint_x / 2 + PART_GAP

    log.info("Extra parts: %d generated", len(extras))
    return "\n".join(parts)
