"""generator.py — top-level SCAD generation step.

Reads session artifacts, runs per-component resolvers, and writes
``enclosure.scad`` (and optionally ``enclosure.stl``) to the session folder.

Public entry point
------------------
    from src.pipeline.scad import run_scad_step
    scad_path = run_scad_step(session)
    scad_path = run_scad_step(session, compile_stl=True)
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from src.catalog.loader import load_catalog
from src.catalog.models import Component
from src.pipeline.config import (
    CAVITY_START_MM, CEILING_MM, FLOOR_MM, PIN_FLOOR_PENETRATION,
    SPLIT_OVERLAP_MM, TRACE_HEIGHT_MM, TRACE_RULES,
    component_z_range,
)
from src.pipeline.design.parsing import parse_physical_design, parse_circuit, build_design_spec
from src.pipeline.design.height_field import blended_height, blended_bottom_height, sample_height_grid
from src.pipeline.design.models import Outline
from src.pipeline.placer.serialization import assemble_full_placement
from src.pipeline.router.models import RoutingResult
from src.pipeline.router.serialization import parse_routing
from src.pipeline.gcode.pause_points import (
    ComponentPauseInfo, pause_z_for_component,
)
from src.session import Session

from .outline import tessellate_outline
from .layers import shell_body_lines, _safe_inset_polygon_pts
from .emit import generate_scad
from .compiler import compile_scad
from .traces import build_trace_fragments
from .resolver import resolve_component, ResolverContext
from .fragment import ScadFragment, PolygonGeometry, RectGeometry
from .buttons import build_button_configs, generate_all_buttons_scad
from .extras import collect_and_generate_extras
from .split import compute_split_z
from .snap_fit import compute_snap_positions, snap_post_fragments, snap_clip_fragments

log = logging.getLogger(__name__)


def run_scad_step(
    session: Session,
    compile_stl: bool = False,
    enclosure_style_override: str | None = None,
) -> Path:
    """Generate ``enclosure.scad`` for the session.

    Parameters
    ----------
    session     : Session  The active session (must have placement + routing).
    compile_stl : bool     If True, also invoke OpenSCAD to render the STL.

    Returns the path to the written ``enclosure.scad``.

    Raises
    ------
    RuntimeError  If required upstream artifacts are missing.
    """

    # ── 1. Load artifacts ──────────────────────────────────────────
    placement_raw = session.read_artifact("placement.json")
    routing_raw   = session.read_artifact("routing.json")
    design_raw    = session.read_artifact("design.json")
    circuit_raw   = session.read_artifact("circuit.json")

    if placement_raw is None:
        raise RuntimeError("placement.json not found — run the placer step first.")
    if design_raw is None:
        raise RuntimeError("design.json not found — run the design step first.")

    physical  = parse_physical_design(design_raw)
    circuit   = parse_circuit(circuit_raw or {})
    design    = build_design_spec(physical, circuit)

    outline   = physical.outline
    enclosure = physical.enclosure

    if enclosure_style_override and enclosure_style_override in ("solid", "two_part"):
        enclosure.enclosure_style = enclosure_style_override

    placement = assemble_full_placement(placement_raw, outline, circuit.nets, enclosure)

    if routing_raw is not None:
        routing = parse_routing(routing_raw)
    else:
        log.warning(
            "routing.json not found — generating enclosure without trace channels."
        )
        routing = RoutingResult(traces=[], pin_assignments={}, failed_nets=[])

    catalog = load_catalog()

    if not catalog.ok:
        for err in catalog.errors:
            log.warning("Catalog validation: %s", err)

    log.info(
        "SCAD step: %d components  %d nets  base_height=%.1f mm",
        len(placement.components), len(placement.nets), enclosure.height_mm,
    )

    # ── 2. Tessellate footprint polygon ───────────────────────────
    flat_pts = tessellate_outline(outline)
    log.info("Footprint: %d vertices", len(flat_pts))

    # ── 3. Compute per-vertex ceiling heights ─────────────────────
    top_zs = [
        blended_height(x, y, outline, enclosure)
        for x, y in flat_pts
    ]
    z_min = min(top_zs)
    z_max = max(top_zs)
    variable_height = (z_max - z_min) >= 0.1
    log.info(
        "Ceiling heights: min=%.2f  max=%.2f mm  variable=%s",
        z_min, z_max, variable_height,
    )

    # ── 3b. Compute per-vertex floor heights ────────────────────
    bottom_zs = [
        blended_bottom_height(x, y, outline, enclosure)
        for x, y in flat_pts
    ]
    bz_min = min(bottom_zs)
    bz_max = max(bottom_zs)
    variable_bottom = (bz_max - bz_min) >= 0.1 or bz_max >= 0.1
    log.info(
        "Floor heights: min=%.2f  max=%.2f mm  variable=%s",
        bz_min, bz_max, variable_bottom,
    )

    # ── Clean up stale artifacts from the opposite mode ─────────────
    if enclosure.enclosure_style == "two_part":
        for name in ("enclosure.scad", "enclosure.stl"):
            p = session.artifact_path(name)
            if p.exists():
                p.unlink()
    else:
        for name in ("enclosure_bottom.scad", "enclosure_top.scad",
                      "enclosure_bottom.stl", "enclosure_top.stl"):
            p = session.artifact_path(name)
            if p.exists():
                p.unlink()

    # ── Branch: two-part enclosure mode ───────────────────────────
    if enclosure.enclosure_style == "two_part":
        return _generate_two_part(
            session, physical, outline, enclosure, placement, routing,
            catalog, flat_pts, top_zs, bottom_zs,
            z_max=z_max, variable_height=variable_height,
            compile_stl=compile_stl,
        )

    # ── 4. Compute shell body layers (solid mode) ─────────────────
    body_lines = shell_body_lines(outline, enclosure, flat_pts, top_zs=top_zs, bottom_zs=bottom_zs)
    log.info("Shell body: %d SCAD lines", len(body_lines))

    # ── 5. Resolve per-component fragments ────────────────────────
    base_h = enclosure.height_mm
    ceil_start = base_h - CEILING_MM
    cavity_depth = ceil_start - CAVITY_START_MM

    ctx = ResolverContext(
        outline=outline,
        enclosure=enclosure,
        base_h=base_h,
        ceil_start=ceil_start,
        cavity_depth=cavity_depth,
        blended_height_fn=blended_height,
    )

    cat_index: dict[str, Component] = {c.id: c for c in catalog.components}
    all_fragments: list[ScadFragment] = []

    # Build component pause info for multi-stage pause grouping
    comp_pause_infos: list[ComponentPauseInfo] = []
    for comp in placement.components:
        cat = cat_index.get(comp.catalog_id)
        if cat is not None:
            comp_pause_infos.append(ComponentPauseInfo(
                instance_id=comp.instance_id,
                body_height_mm=cat.protrusion_height_mm,
                mounting_style=comp.mounting_style or cat.mounting.style,
                pin_length_mm=cat.pin_length_mm,
            ))

    # Tessellate button shapes from UI placements into point-list outlines.
    # Two sources: button_shape (CSG dict, needs tessellation) or
    # button_outline (raw point list, used directly).
    from src.pipeline.design.shape2d import tessellate_shape
    ui_shape_map: dict[str, dict] = {}
    ui_outline_map: dict[str, list[list[float]]] = {}
    for up in physical.ui_placements:
        if up.button_shape is not None:
            ui_shape_map[up.instance_id] = up.button_shape
        elif up.button_outline is not None:
            ui_outline_map[up.instance_id] = up.button_outline
    for comp in placement.components:
        shape = ui_shape_map.get(comp.instance_id)
        if shape is not None:
            outline_obj = tessellate_shape(shape)
            comp.button_outline = [[v.x, v.y] for v in outline_obj.points]
        elif comp.instance_id in ui_outline_map:
            comp.button_outline = ui_outline_map[comp.instance_id]

    for comp in placement.components:
        cat = cat_index.get(comp.catalog_id)
        if cat is None:
            log.warning("Unknown catalog entry '%s' — skipping", comp.catalog_id)
            continue
        # Set per-component pause_z so pin grooves are capped
        ctx.pause_z = pause_z_for_component(
            cat.protrusion_height_mm, base_h,
            mounting_style=comp.mounting_style or cat.mounting.style,
            pin_length_mm=cat.pin_length_mm,
        )
        frags = resolve_component(comp, cat, ctx)
        all_fragments.extend(frags)
        log.debug("Component %s: %d fragments (pause_z=%.1f)", comp.instance_id, len(frags), ctx.pause_z)

    # ── 6. Trace channel fragments ────────────────────────────────
    trace_frags = build_trace_fragments(routing, ceil_start)
    all_fragments.extend(trace_frags)

    # ── 6b. Outline holes ───────────────────────────────────────
    # Holes are built directly into the shell body polyhedron by
    # shell_body_lines() — no cutout fragments needed.

    log.info("Fragments: %d component + %d trace = %d total",
             len(all_fragments) - len(trace_frags),
             len(trace_frags), len(all_fragments))

    # ── 7. Compute metadata for header comment ────────────────────
    height_grid = sample_height_grid(outline, enclosure, resolution_mm=2.0)
    max_h = z_max
    for row in height_grid["grid"]:
        for h in row:
            if h is not None and h > max_h:
                max_h = h

    metadata = {
        "components":       len(placement.components),
        "traces":           len(routing.traces),
        "fragments":        len(all_fragments),
        "base_height_mm":   enclosure.height_mm,
        "max_height_mm":    round(max_h, 1),
        "footprint_verts":  len(flat_pts),
        "variable_height":  variable_height,
    }

    # ── 8. Emit SCAD string ───────────────────────────────────────
    scad_str = generate_scad(
        body_lines, all_fragments,
        session_id=session.id,
        metadata=metadata,
        outline_pts=flat_pts,
    )

    # ── 8b. Generate extra parts (buttons, hatches, etc.) ───────────
    extras_scad = collect_and_generate_extras(
        placement.components, cat_index, outline, enclosure,
        ceil_start,
    )

    # ── 9. Write to session folder ────────────────────────────────
    scad_path: Path = session.artifact_path("enclosure.scad")
    scad_path.parent.mkdir(parents=True, exist_ok=True)
    scad_path.write_text(scad_str, encoding="utf-8")

    log.info(
        "Wrote %s (%.1f kB, %d lines)",
        scad_path.name,
        len(scad_str.encode()) / 1024,
        scad_str.count("\n"),
    )

    extras_path: Path | None = None
    if extras_scad:
        extras_path = session.artifact_path("extras.scad")
        extras_path.write_text(extras_scad, encoding="utf-8")
        log.info(
            "Wrote %s (%.1f kB, %d lines)",
            extras_path.name,
            len(extras_scad.encode()) / 1024,
            extras_scad.count("\n"),
        )

    session.pipeline_state["scad"] = "complete"
    session.save()

    # ── 10. Optional: compile to STL ──────────────────────────────
    if compile_stl:
        stl_path = session.artifact_path("enclosure.stl")
        ok, msg, out = compile_scad(scad_path, stl_path)
        if ok:
            log.info("STL rendered: %s", stl_path.name)
            session.pipeline_state["stl"] = "complete"
        else:
            log.error("STL render failed: %s", msg)
            session.pipeline_state["stl"] = "error"

        if extras_path is not None:
            extras_stl = session.artifact_path("extras.stl")
            ok_e, msg_e, _ = compile_scad(extras_path, extras_stl)
            if ok_e:
                log.info("Extras STL rendered: %s", extras_stl.name)
            else:
                log.error("Extras STL render failed: %s", msg_e)

        session.save()

    return scad_path


# ── Two-part enclosure generation ──────────────────────────────────────────────


def _resolve_components_for_part(
    part: str,
    placement, physical, outline, enclosure,
    routing, cat_index, flat_pts, top_zs,
    base_h, ceil_start, cavity_depth,
    split_z: float | None = None,
):
    """Resolve component fragments for a specific part ('bottom' or 'top')."""
    ctx = ResolverContext(
        outline=outline,
        enclosure=enclosure,
        base_h=base_h,
        ceil_start=ceil_start,
        cavity_depth=cavity_depth,
        blended_height_fn=blended_height,
        part=part,
        split_z=split_z,
    )

    # Tessellate button shapes / copy raw outlines
    from src.pipeline.design.shape2d import tessellate_shape
    ui_shape_map: dict[str, dict] = {}
    ui_outline_map: dict[str, list[list[float]]] = {}
    for up in physical.ui_placements:
        if up.button_shape is not None:
            ui_shape_map[up.instance_id] = up.button_shape
        elif up.button_outline is not None:
            ui_outline_map[up.instance_id] = up.button_outline
    for comp in placement.components:
        shape = ui_shape_map.get(comp.instance_id)
        if shape is not None:
            outline_obj = tessellate_shape(shape)
            comp.button_outline = [[v.x, v.y] for v in outline_obj.points]
        elif comp.instance_id in ui_outline_map:
            comp.button_outline = ui_outline_map[comp.instance_id]

    all_fragments: list[ScadFragment] = []
    for comp in placement.components:
        cat = cat_index.get(comp.catalog_id)
        if cat is None:
            continue
        ctx.pause_z = pause_z_for_component(
            cat.protrusion_height_mm, base_h,
            mounting_style=comp.mounting_style or cat.mounting.style,
            pin_length_mm=cat.pin_length_mm,
        )
        frags = resolve_component(comp, cat, ctx)
        all_fragments.extend(frags)

    return all_fragments


def _generate_two_part(
    session: Session,
    physical, outline, enclosure, placement, routing,
    catalog, flat_pts, top_zs, bottom_zs,
    *,
    z_max: float,
    variable_height: bool,
    compile_stl: bool,
) -> Path:
    """Generate ``enclosure_bottom.scad`` and ``enclosure_top.scad``.

    Called when ``enclosure.enclosure_style == "two_part"``.
    """
    base_h = enclosure.height_mm
    ceil_start = base_h - CEILING_MM
    cavity_depth = ceil_start - CAVITY_START_MM
    cat_index: dict[str, Component] = {c.id: c for c in catalog.components}

    # ── Compute split height ──────────────────────────────────────
    split_z = compute_split_z(enclosure, placement.components, cat_index)

    # ── BOTTOM part ───────────────────────────────────────────────
    bottom_top_zs = [split_z] * len(flat_pts)

    bottom_body_lines = shell_body_lines(
        outline, enclosure, flat_pts,
        top_zs=bottom_top_zs, bottom_zs=bottom_zs,
        skip_edge_top=True,
    )
    log.info("Bottom shell body: %d SCAD lines", len(bottom_body_lines))

    # Bottom fragments: floor-level stuff + support platforms
    bottom_frags = _resolve_components_for_part(
        "bottom", placement, physical, outline, enclosure,
        routing, cat_index, flat_pts, top_zs,
        base_h, ceil_start, cavity_depth,
        split_z=split_z,
    )

    # Add trace channels on the top surface of the bottom tray so
    # they are visible when the top half is removed.  Channels cut
    # as deep as pin holes (from FLOOR_MM to split_z) so traces and
    # pins sit at the same depth.
    trace_frags = build_trace_fragments(routing, ceil_start)
    trace_depth = split_z - FLOOR_MM
    for tf in trace_frags:
        tf.z_base = FLOOR_MM
        tf.depth = trace_depth
    bottom_frags.extend(trace_frags)

    # NO interior cavity — the bottom tray is solid so that:
    #   • pin shafts drill through real material (better grip)
    #   • trace channels recess into the top surface
    #   • funnel tapers widen into the solid at the top
    # The resolver already generates pin shafts from FLOOR_MM-penetration
    # upward; those cut narrow holes through the solid bottom.

    # Add funnel tapers at the TOP of the bottom tray for every pin
    # so components can be guided into the narrow pin shafts below.
    # Funnels widen from pin-shaft diameter at the bottom up to
    # (shaft + taper_extra) at split_z.
    from .fragment import rotate_point as _rot_pt
    from src.pipeline.pin_geometry import pin_shaft_dimensions
    funnel_z_base = max(split_z - TRACE_RULES.pinhole_taper_depth_mm, FLOOR_MM)
    funnel_depth = split_z - funnel_z_base
    if funnel_depth > 0.1:
        for comp in placement.components:
            cat = cat_index.get(comp.catalog_id)
            if cat is None:
                continue
            for pin in cat.pins:
                pos = comp.pin_positions.get(pin.id) if comp.pin_positions else None
                if pos is not None:
                    px, py = pos[0], pos[1]
                else:
                    px_rel = float(pin.position_mm[0])
                    py_rel = float(pin.position_mm[1])
                    rot_deg = comp.rotation_deg or 0
                    if rot_deg:
                        px_rel, py_rel = _rot_pt(px_rel, py_rel, rot_deg)
                    px = comp.x_mm + px_rel
                    py = comp.y_mm + py_rel

                shaft_w, shaft_h_dim = pin_shaft_dimensions(pin)

                extra = TRACE_RULES.pinhole_taper_extra_mm
                scale_x = (shaft_w + extra) / shaft_w
                scale_y = (shaft_h_dim + extra) / shaft_h_dim
                taper = max(scale_x, scale_y)
                bottom_frags.append(ScadFragment(
                    type="cutout",
                    geometry=RectGeometry(px, py, shaft_w, shaft_h_dim),
                    z_base=funnel_z_base,
                    depth=funnel_depth,
                    taper_scale=taper,
                    label=f"bottom funnel {comp.instance_id}:{pin.id}",
                ))

    log.info("Bottom fragments: %d total", len(bottom_frags))

    height_grid = sample_height_grid(outline, enclosure, resolution_mm=2.0)
    max_h = z_max
    for row in height_grid["grid"]:
        for h in row:
            if h is not None and h > max_h:
                max_h = h

    bottom_metadata = {
        "components":       len(placement.components),
        "traces":           len(routing.traces),
        "fragments":        len(bottom_frags),
        "base_height_mm":   enclosure.height_mm,
        "max_height_mm":    round(split_z, 1),
        "footprint_verts":  len(flat_pts),
        "variable_height":  False,
        "part":             "bottom",
        "split_z_mm":       round(split_z, 2),
    }

    bottom_scad = generate_scad(
        bottom_body_lines, bottom_frags,
        session_id=session.id,
        metadata=bottom_metadata,
        outline_pts=flat_pts,
    )

    # ── TOP part ──────────────────────────────────────────────────
    top_bottom_zs = [split_z - SPLIT_OVERLAP_MM] * len(flat_pts)

    top_body_lines = shell_body_lines(
        outline, enclosure, flat_pts,
        top_zs=top_zs, bottom_zs=top_bottom_zs,
        skip_edge_bottom=True,
    )
    log.info("Top shell body: %d SCAD lines", len(top_body_lines))

    # Top fragments: component cavities + ceiling cutouts
    top_frags = _resolve_components_for_part(
        "top", placement, physical, outline, enclosure,
        routing, cat_index, flat_pts, top_zs,
        base_h, ceil_start, cavity_depth,
        split_z=split_z,
    )

    # Interior cavity — hollow out the top shell, but keep walls
    # around component compartments so they have material.
    _TRAY_WALL_MM = 2.0
    _COMP_WALL_MM = 1.5          # extra wall around each component footprint
    top_interior_bottom = split_z - SPLIT_OVERLAP_MM
    top_cavity_depth = ceil_start - top_interior_bottom
    if top_cavity_depth > 0.5:
        from shapely.geometry import Polygon as _SPoly
        from shapely.ops import unary_union as _sunion

        cavity_pts = _safe_inset_polygon_pts(flat_pts, _TRAY_WALL_MM)
        cavity_poly = _SPoly(cavity_pts)
        if not cavity_poly.is_valid:
            cavity_poly = cavity_poly.buffer(0)

        # Subtract expanded footprint only for components with channels
        # (battery holder) so compartment walls remain for metal plates.
        # All other components are fully hollowed out by the cavity.
        comp_polys = []
        for comp in placement.components:
            cat = cat_index.get(comp.catalog_id)
            if cat is None:
                continue
            if not cat.body.channels:
                continue  # no compartment walls needed
            style = comp.mounting_style or cat.mounting.style
            _, body_top = component_z_range(
                style, cat.body.height_mm, cat.pin_length_mm, ceil_start,
            )
            if body_top <= split_z:
                continue  # entirely in bottom half
            body = cat.body
            bw = body.width_mm or 0.0
            bl = body.length_mm or bw

            # Start from body extents, then expand to cover SCAD features
            # (plate channels, spring slots, etc.) so the keep zone has
            # solid material for every slit to cut into.
            max_hx = bw / 2
            max_hy = bl / 2
            for feat in cat.scad_features:
                fx = abs(feat.position_mm[0]) if feat.position_mm else 0.0
                fy = abs(feat.position_mm[1]) if feat.position_mm else 0.0
                fw = (feat.width_mm or 0.0) / 2
                fl = (feat.length_mm or 0.0) / 2
                max_hx = max(max_hx, fx + fw)
                max_hy = max(max_hy, fy + fl)

            hw = max_hx + _COMP_WALL_MM
            hl = max_hy + _COMP_WALL_MM
            cx, cy = comp.x_mm, comp.y_mm
            corners = [(-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl)]
            rot = math.radians(comp.rotation_deg or 0)
            if abs(rot) > 1e-6:
                cos_r, sin_r = math.cos(rot), math.sin(rot)
                corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r)
                           for x, y in corners]
            footprint = _SPoly([(cx + x, cy + y) for x, y in corners])
            if footprint.is_valid and not footprint.is_empty:
                comp_polys.append(footprint)

        if comp_polys:
            keep_zone = _sunion(comp_polys)
            cavity_poly = cavity_poly.difference(keep_zone)

        # Add radial divider walls between top-mounted button clusters.
        # Walls go from the button-ring center toward the outline edge,
        # placed at the bisector angle between adjacent buttons.
        _WALL_HALF_W = 1.0  # half-width of each radial wall (mm)
        top_btn_angles: list[float] = []
        btn_cx_sum, btn_cy_sum, btn_count = 0.0, 0.0, 0
        for comp in placement.components:
            cat = cat_index.get(comp.catalog_id)
            if cat is None:
                continue
            style = comp.mounting_style or cat.mounting.style
            if style != "top":
                continue
            _, body_top = component_z_range(
                style, cat.body.height_mm, cat.pin_length_mm, ceil_start,
            )
            if body_top <= split_z:
                continue
            btn_cx_sum += comp.x_mm
            btn_cy_sum += comp.y_mm
            btn_count += 1

        if btn_count >= 3:
            bcx = btn_cx_sum / btn_count
            bcy = btn_cy_sum / btn_count
            for comp in placement.components:
                cat = cat_index.get(comp.catalog_id)
                if cat is None:
                    continue
                style = comp.mounting_style or cat.mounting.style
                if style != "top":
                    continue
                _, body_top = component_z_range(
                    style, cat.body.height_mm, cat.pin_length_mm, ceil_start,
                )
                if body_top <= split_z:
                    continue
                angle = math.atan2(comp.y_mm - bcy, comp.x_mm - bcx)
                top_btn_angles.append(angle)
            top_btn_angles.sort()

            from shapely.geometry import LineString as _SLine

            # Max radius: distance from center to farthest outline vertex
            max_r = max(
                math.hypot(px - bcx, py - bcy)
                for px, py in flat_pts
            ) + 5.0

            wall_polys = []
            for i in range(len(top_btn_angles)):
                a1 = top_btn_angles[i]
                a2 = top_btn_angles[(i + 1) % len(top_btn_angles)]
                # Bisector angle
                diff = a2 - a1
                if diff < 0:
                    diff += 2 * math.pi
                bisect = a1 + diff / 2
                ex = bcx + max_r * math.cos(bisect)
                ey = bcy + max_r * math.sin(bisect)
                wall_line = _SLine([(bcx, bcy), (ex, ey)])
                wall_poly = wall_line.buffer(_WALL_HALF_W, cap_style="flat")
                if wall_poly.is_valid and not wall_poly.is_empty:
                    wall_polys.append(wall_poly)

            if wall_polys:
                wall_zone = _sunion(wall_polys)
                # Clip to cavity area so walls don't extend outside
                wall_zone = wall_zone.intersection(cavity_poly.buffer(0))
                if not wall_zone.is_empty:
                    cavity_poly = cavity_poly.difference(wall_zone)

                # For components that overlap a wall, add a cutout from the
                # cavity bottom (split_z) up to the component's body top.
                # This removes the wall below and at the component, keeping
                # only the wall portion above.  The resolver's own body
                # cutout already covers the body Z-range, so the extra
                # cutout only extends the clearance downward.
                # Clearance must exceed wall half-width so the inflated
                # footprint fully encompasses the wall cross-section.
                _CLR = _WALL_HALF_W + 0.5
                for comp in placement.components:
                    cat = cat_index.get(comp.catalog_id)
                    if cat is None:
                        continue
                    body = cat.body
                    bw = body.width_mm or 0.0
                    bl = body.length_mm or bw
                    if bw < 0.1:
                        continue
                    style = comp.mounting_style or cat.mounting.style
                    _, body_top = component_z_range(
                        style, body.height_mm, cat.pin_length_mm, ceil_start,
                    )
                    if body_top <= split_z:
                        continue  # component not in top half
                    # Build rotated footprint rectangle with clearance
                    hw = bw / 2 + _CLR
                    hl = bl / 2 + _CLR
                    corners = [(-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl)]
                    rot = math.radians(comp.rotation_deg or 0)
                    if abs(rot) > 1e-6:
                        cos_r, sin_r = math.cos(rot), math.sin(rot)
                        corners = [
                            (x * cos_r - y * sin_r, x * sin_r + y * cos_r)
                            for x, y in corners
                        ]
                    cx, cy = comp.x_mm, comp.y_mm
                    fp = _SPoly([(cx + x, cy + y) for x, y in corners])
                    if not fp.is_valid or fp.is_empty:
                        continue
                    # Intersect footprint with wall zone — only cut where
                    # the wall actually exists.  Buffer slightly so rounded
                    # wall edges don't leave thin slivers of solid.
                    overlap = fp.intersection(wall_zone).buffer(0.15)
                    if overlap.is_empty:
                        continue
                    # Emit cutout from cavity bottom to body_top
                    cut_depth = body_top - top_interior_bottom
                    if cut_depth < 0.1:
                        continue
                    olap_geoms = (
                        [overlap] if overlap.geom_type == "Polygon"
                        else [g for g in overlap.geoms
                              if g.geom_type == "Polygon"]
                    )
                    for opoly in olap_geoms:
                        pts = [[round(x, 3), round(y, 3)]
                               for x, y in opoly.exterior.coords[:-1]]
                        if len(pts) >= 3:
                            top_frags.append(ScadFragment(
                                type="cutout",
                                geometry=PolygonGeometry(points=pts),
                                z_base=top_interior_bottom,
                                depth=round(cut_depth, 3),
                                label="wall clearance below component",
                            ))

        if not cavity_poly.is_empty:
            # Convert to PolygonGeometry fragments — the emit merger handles holes
            if cavity_poly.geom_type == 'Polygon':
                _cavity_geoms = [cavity_poly]
            else:
                _cavity_geoms = [g for g in cavity_poly.geoms if g.geom_type == 'Polygon']
            for cpoly in _cavity_geoms:
                pts = [[round(x, 3), round(y, 3)]
                       for x, y in cpoly.exterior.coords[:-1]]
                # Preserve interior holes (component footprints)
                poly_holes = []
                for interior in cpoly.interiors:
                    hpts = [[round(x, 3), round(y, 3)]
                            for x, y in interior.coords[:-1]]
                    if len(hpts) >= 3:
                        poly_holes.append(hpts)
                if len(pts) >= 3:
                    top_frags.append(ScadFragment(
                        type="cutout",
                        geometry=PolygonGeometry(
                            points=pts,
                            holes=poly_holes if poly_holes else None,
                        ),
                        z_base=top_interior_bottom,
                        depth=top_cavity_depth,
                        label="top interior cavity",
                    ))

    log.info("Top fragments: %d total", len(top_frags))

    top_metadata = {
        "components":       len(placement.components),
        "traces":           0,
        "fragments":        len(top_frags),
        "base_height_mm":   enclosure.height_mm,
        "max_height_mm":    round(max_h, 1),
        "footprint_verts":  len(flat_pts),
        "variable_height":  variable_height,
        "part":             "top",
        "split_z_mm":       round(split_z, 2),
    }

    top_scad = generate_scad(
        top_body_lines, top_frags,
        session_id=session.id,
        metadata=top_metadata,
        outline_pts=flat_pts,
    )

    # ── Generate extras (buttons, hatches — same for both modes) ──
    extras_scad = collect_and_generate_extras(
        placement.components, cat_index, outline, enclosure,
        ceil_start,
    )

    # ── Write files ───────────────────────────────────────────────
    out_dir = session.artifact_path("enclosure_bottom.scad").parent
    out_dir.mkdir(parents=True, exist_ok=True)

    bottom_path = session.artifact_path("enclosure_bottom.scad")
    bottom_path.write_text(bottom_scad, encoding="utf-8")
    log.info("Wrote %s (%.1f kB)", bottom_path.name, len(bottom_scad.encode()) / 1024)

    top_path = session.artifact_path("enclosure_top.scad")
    top_path.write_text(top_scad, encoding="utf-8")
    log.info("Wrote %s (%.1f kB)", top_path.name, len(top_scad.encode()) / 1024)

    extras_path: Path | None = None
    if extras_scad:
        extras_path = session.artifact_path("extras.scad")
        extras_path.write_text(extras_scad, encoding="utf-8")
        log.info("Wrote %s (%.1f kB)", extras_path.name, len(extras_scad.encode()) / 1024)

    session.pipeline_state["scad"] = "complete"
    session.save()

    # ── Optional: compile to STL ──────────────────────────────────
    if compile_stl:
        for scad_p, stl_name in [
            (bottom_path, "enclosure_bottom.stl"),
            (top_path, "enclosure_top.stl"),
        ]:
            stl_path = session.artifact_path(stl_name)
            ok, msg, _ = compile_scad(scad_p, stl_path)
            if ok:
                log.info("STL rendered: %s", stl_path.name)
            else:
                log.error("STL render failed for %s: %s", stl_name, msg)

        if extras_path is not None:
            extras_stl = session.artifact_path("extras.stl")
            ok_e, msg_e, _ = compile_scad(extras_path, extras_stl)
            if ok_e:
                log.info("Extras STL rendered: %s", extras_stl.name)
            else:
                log.error("Extras STL render failed: %s", msg_e)

        session.pipeline_state["stl"] = "complete"
        session.save()

    return bottom_path
