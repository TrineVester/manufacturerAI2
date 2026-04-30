"""Main placement engine — candidate-based placer with hard/soft constraints."""

from __future__ import annotations

import logging
import math

from shapely.geometry import Polygon, box as shapely_box
from shapely.prepared import prep as shapely_prep

from src.catalog.models import CatalogResult
from src.pipeline.design.models import DesignSpec, Outline

from .geometry import (
    footprint_halfdims, footprint_envelope_halfdims, footprint_area,
    rect_inside_polygon, rect_edge_clearance, aabb_gap,
    pin_world_xy,
)
from .models import (
    PlacedComponent, FullPlacement, PlacementError,
    GRID_STEP_MM, VALID_ROTATIONS, MIN_EDGE_CLEARANCE_MM,
    ROUTING_CHANNEL_MM, MIN_PIN_CLEARANCE_MM,
)
from .nets import build_net_graph, count_shared_nets, build_placement_groups, resolve_pin_positions
from .models import Placed
from .candidates import generate_candidates
from .congestion import CongestionGrid
from .scoring import score_candidate
from .annealing import sa_refine


log = logging.getLogger(__name__)


# ── Side-mount helpers ─────────────────────────────────────────────


def _edge_direction(
    outline: Outline, edge_index: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (start_vertex, end_vertex) for an outline edge."""
    pts = outline.vertices
    n = len(pts)
    return pts[edge_index % n], pts[(edge_index + 1) % n]


def _edge_rotation(
    p1: tuple[float, float], p2: tuple[float, float],
) -> float:
    """Return the edge tangent angle in degrees (0–360).

    This is the direction of the vector from *p1* to *p2*.
    The component's local +X axis aligns with this direction
    (along the wall).  For CW-wound outlines the outward
    normal points to the right of the edge direction, so
    local −Y faces the enclosure interior.
    """
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    angle = math.degrees(math.atan2(dy, dx))
    return angle % 360


def _snap_to_edge(
    x_mm: float, y_mm: float,
    outline: Outline, edge_index: int,
    inward_offset: float = 0.0,
) -> tuple[float, float, float]:
    """Snap a point to the nearest position on an outline edge.

    If *inward_offset* is given the snapped point is shifted toward
    the interior of the outline by that many mm (along the inward
    normal, which for clockwise winding is the *left* side of the
    edge direction vector).

    Returns (snapped_x, snapped_y, rotation_deg).
    """
    p1, p2 = _edge_direction(outline, edge_index)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return (p1[0], p1[1], 0)

    t = max(0.0, min(1.0, ((x_mm - p1[0]) * dx + (y_mm - p1[1]) * dy) / length_sq))
    snap_x = p1[0] + t * dx
    snap_y = p1[1] + t * dy
    rotation = _edge_rotation(p1, p2)

    if inward_offset:
        length = math.sqrt(length_sq)
        # Inward normal: right-hand perpendicular of edge direction for CW winding.
        # snap += normal * offset moves the point toward the interior.
        nx = dy / length
        ny = -dx / length
        snap_x += nx * inward_offset
        snap_y += ny * inward_offset

    return (snap_x, snap_y, rotation)


# ── Main placement function ───────────────────────────────────────


def place_components(
    design: DesignSpec,
    catalog: CatalogResult,
    *,
    grid_step: float = GRID_STEP_MM,
) -> FullPlacement:
    """Place all components inside the outline.

    UI components are fixed at their agent-specified positions.
    Non-UI components are auto-placed via candidate search around
    net-connected neighbours and strip-packing fallback.

    Parameters
    ----------
    design : DesignSpec
        The agent's design specification.
    catalog : CatalogResult
        The loaded component catalog.
    grid_step : float
        Grid step for fallback scan (mm, default 1.0).

    Returns
    -------
    FullPlacement
        All components positioned with (x, y, rotation).

    Raises
    ------
    PlacementError
        If a component cannot be legally placed.
    """
    catalog_map = {c.id: c for c in catalog.components}
    outline_poly = Polygon(
        design.outline.vertices,
        design.outline.hole_vertices or None,
    )
    outline_verts = design.outline.vertices
    xmin, ymin, xmax, ymax = outline_poly.bounds
    outline_bounds = (xmin, ymin, xmax, ymax)

    if not outline_poly.is_valid or outline_poly.area <= 0:
        raise PlacementError("_outline", "_outline",
                             "Outline polygon is invalid or has zero area")

    # ── Raised-floor detection ─────────────────────────────────────
    # If any vertex has z_bottom that would push the floor at or above the
    # trace-layer height (FLOOR_MM), components cannot be placed there.
    _has_raised_bottom = any(
        getattr(p, 'z_bottom', None) for p in design.outline.points
    ) or design.enclosure.bottom_surface is not None
    _floor_threshold: float = 0.0
    _pcb_contour_verts: list[tuple[float, float]] | None = None
    if _has_raised_bottom:
        from src.pipeline.design.height_field import (
            blended_bottom_height, sample_bottom_height_grid,
            pcb_contour_from_bottom_grid,
        )
        from src.pipeline.config import FLOOR_MM
        _floor_threshold = FLOOR_MM - 0.1

        # Derive PCB contour polygon so edge clearance can also be
        # enforced against the flat→raised boundary, not just the
        # raw outline.  Without this a component could sit right at
        # the transition line with zero clearance.
        _bot_grid = sample_bottom_height_grid(design.outline, design.enclosure)
        if _bot_grid is not None:
            _contour_pts = pcb_contour_from_bottom_grid(
                _bot_grid, design.outline, FLOOR_MM,
            )
            if _contour_pts is not None and len(_contour_pts) >= 3:
                _pcb_contour_verts = [(p[0], p[1]) for p in _contour_pts]
                log.info("PCB contour edge-clearance polygon: %d vertices",
                         len(_pcb_contour_verts))

    # A bottom fillet or chamfer curves the wall inward at floor level by
    # exactly size_mm.  Components placed within that zone would sit inside
    # the curved wall material, so we add it to every pass's edge clearance.
    # Cap to 42% of height_mm — same rule the 3-D renderer uses in JS.
    _ebot = design.enclosure.edge_bottom
    _raw_inset = _ebot.size_mm if _ebot.type in ("fillet", "chamfer") else 0.0
    _max_inset = design.enclosure.height_mm * 0.42
    floor_inset = min(_raw_inset, _max_inset)

    # Build net connectivity graph
    net_graph = build_net_graph(design.nets)

    # Build coarse congestion grid for routing-aware scoring
    cg = CongestionGrid(outline_poly)

    # Resolve effective mounting style for each instance
    # Priority: UIPlacement.mounting_style > ComponentInstance.mounting_style > catalog default
    effective_style: dict[str, str] = {}
    up_style_map = {up.instance_id: up.mounting_style for up in design.ui_placements if up.mounting_style}
    for ci in design.components:
        cat = catalog_map.get(ci.catalog_id)
        if cat:
            effective_style[ci.instance_id] = (
                up_style_map.get(ci.instance_id)
                or ci.mounting_style
                or cat.mounting.style
            )

    # ── 1. Place UI components (fixed positions) ───────────────────

    placed: list[Placed] = []
    placed_map: dict[str, Placed] = {}
    ui_ids: set[str] = set()

    for up in design.ui_placements:
        ci = next(c for c in design.components if c.instance_id == up.instance_id)
        cat = catalog_map[ci.catalog_id]
        style = effective_style.get(ci.instance_id, cat.mounting.style)

        if style == "side" and up.edge_index is not None:
            is_reoriented = cat.mounting.style != "side"
            if is_reoriented:
                half_depth = (cat.body.height_mm or 1.0) / 2
            else:
                half_depth = (cat.body.length_mm or 1.0) / 2
            x, y, rot = _snap_to_edge(up.x_mm, up.y_mm, design.outline, up.edge_index, inward_offset=half_depth)
        else:
            x, y, rot = up.x_mm, up.y_mm, 0

        hw, hh = footprint_halfdims(cat, rot)
        ehw, ehh = footprint_envelope_halfdims(cat, rot)
        p = Placed(
            instance_id=ci.instance_id,
            catalog_id=ci.catalog_id,
            x=x, y=y, rotation=rot,
            hw=hw, hh=hh,
            keepout=cat.mounting.keepout_margin_mm,
            env_hw=ehw, env_hh=ehh,
        )
        placed.append(p)
        placed_map[ci.instance_id] = p
        ui_ids.add(ci.instance_id)
        cg.block_component(ci.instance_id, x, y, ehw, ehh)
        log.info("UI-placed %s at (%.1f, %.1f) rot=%d°",
                 ci.instance_id, x, y, rot)

    # ── 2. Sort remaining by connectivity group, then area ─────────

    to_place_ids = [
        ci.instance_id for ci in design.components
        if ci.instance_id not in ui_ids
    ]
    area_map = {
        ci.instance_id: footprint_area(catalog_map[ci.catalog_id])
        for ci in design.components
        if ci.instance_id not in ui_ids
    }
    groups = build_placement_groups(to_place_ids, net_graph, area_map)

    ordered_ids = [iid for group in groups for iid in group]
    ci_map = {ci.instance_id: ci for ci in design.components}
    to_place = [ci_map[iid] for iid in ordered_ids]

    # ── 3. Auto-place each component ──────────────────────────────

    prep_poly = shapely_prep(outline_poly)
    _min_pin_sq = MIN_PIN_CLEARANCE_MM * MIN_PIN_CLEARANCE_MM

    # Fast-path: detect axis-aligned rectangular outlines
    from .geometry import _is_aabb
    _outline_aabb = _is_aabb(outline_verts)

    for ci in to_place:
        cat = catalog_map[ci.catalog_id]
        style = effective_style.get(ci.instance_id, cat.mounting.style)
        keepout = cat.mounting.keepout_margin_mm

        best_pos: tuple[float, float] | None = None
        best_rot = 0
        best_score = -float("inf")

        _placed_pins_world: list[list[tuple[float, float]]] = []
        for _pp in placed:
            _pcat = catalog_map.get(_pp.catalog_id)
            if not _pcat or not _pcat.pins:
                _placed_pins_world.append([])
                continue
            _pc, _ps = math.cos(math.radians(_pp.rotation)), math.sin(math.radians(_pp.rotation))
            _placed_pins_world.append([
                (_pp.x + _pin.position_mm[0] * _pc - _pin.position_mm[1] * _ps,
                 _pp.y + _pin.position_mm[0] * _ps + _pin.position_mm[1] * _pc)
                for _pin in _pcat.pins
            ])

        _PASSES = [
            (False, 1.0, MIN_EDGE_CLEARANCE_MM + floor_inset),
            (True,  1.0, MIN_EDGE_CLEARANCE_MM + floor_inset),
            (True,  0.5, max(MIN_EDGE_CLEARANCE_MM * 0.5, 0.5) + floor_inset),
        ]

        for _pass, (ignore_channel_gap, keepout_scale, edge_clr) in enumerate(_PASSES):
            if best_pos is not None:
                break

            for rotation in VALID_ROTATIONS:
                hw, hh = footprint_halfdims(cat, rotation)
                ehw, ehh = footprint_envelope_halfdims(cat, rotation)
                ihw = ehw + edge_clr
                ihh = ehh + edge_clr

                candidates = generate_candidates(
                    ci.instance_id, cat, rotation, ehw, ehh, ihw, ihh,
                    placed, placed_map, net_graph, catalog_map,
                    outline_bounds, edge_clr, grid_step, style,
                )

                _rad = math.radians(rotation)
                _cos_r, _sin_r = math.cos(_rad), math.sin(_rad)
                my_pin_offsets = [
                    (pin.position_mm[0] * _cos_r - pin.position_mm[1] * _sin_r,
                     pin.position_mm[0] * _sin_r + pin.position_mm[1] * _cos_r)
                    for pin in cat.pins
                ]

                for cx, cy in candidates:
                    if _outline_aabb is not None:
                        _oxmin, _oymin, _oxmax, _oymax = _outline_aabb
                        if (cx - ihw < _oxmin or cx + ihw > _oxmax
                                or cy - ihh < _oymin or cy + ihh > _oymax):
                            continue
                    elif not prep_poly.contains(
                        shapely_box(cx - ihw, cy - ihh, cx + ihw, cy + ihh)
                    ):
                        continue

                    # Reject positions in the raised-floor zone
                    # Check body corners AND all pin positions — a pin landing
                    # in the raised zone can't have traces connected to it.
                    if _has_raised_bottom:
                        _raised = False
                        _check_pts = [
                            (cx, cy),
                            (cx - ehw, cy - ehh), (cx + ehw, cy - ehh),
                            (cx - ehw, cy + ehh), (cx + ehw, cy + ehh),
                        ]
                        for _pox, _poy in my_pin_offsets:
                            _check_pts.append((cx + _pox, cy + _poy))
                        for _px, _py in _check_pts:
                            if blended_bottom_height(
                                _px, _py, design.outline, design.enclosure,
                            ) >= _floor_threshold:
                                _raised = True
                                break
                        if _raised:
                            continue

                    overlap = False
                    for p in placed:
                        n_channels = count_shared_nets(
                            ci.instance_id, p.instance_id, net_graph,
                        )
                        channel_gap = (
                            0.0 if ignore_channel_gap
                            else n_channels * ROUTING_CHANNEL_MM
                        )
                        my_ko = max(keepout * keepout_scale, 1.0)
                        her_ko = max(p.keepout * keepout_scale, 1.0)
                        required_gap = max(my_ko, her_ko, channel_gap)
                        actual_gap = aabb_gap(
                            cx, cy, ehw, ehh,
                            p.x, p.y, p.env_hw, p.env_hh,
                        )
                        if actual_gap < required_gap:
                            overlap = True
                            break
                    if overlap:
                        continue

                    edge_dist = rect_edge_clearance(
                        cx, cy, ehw, ehh, outline_verts)
                    if edge_dist < edge_clr:
                        continue

                    # Also enforce clearance from the PCB contour
                    # boundary (flat→raised transition) when present.
                    if _pcb_contour_verts is not None:
                        contour_edge_dist = rect_edge_clearance(
                            cx, cy, ehw, ehh, _pcb_contour_verts)
                        if contour_edge_dist < edge_clr:
                            continue

                    pin_clash = False
                    my_pins_world = [(cx + ox, cy + oy) for ox, oy in my_pin_offsets]
                    for _pp, _ppw in zip(placed, _placed_pins_world):
                        if pin_clash:
                            break
                        if not _ppw:
                            continue
                        if abs(cx - _pp.x) > ehw + _pp.env_hw + MIN_PIN_CLEARANCE_MM:
                            continue
                        if abs(cy - _pp.y) > ehh + _pp.env_hh + MIN_PIN_CLEARANCE_MM:
                            continue
                        for opx, opy in _ppw:
                            if pin_clash:
                                break
                            for mpx, mpy in my_pins_world:
                                dx, dy = mpx - opx, mpy - opy
                                if dx * dx + dy * dy < _min_pin_sq:
                                    pin_clash = True
                                    break
                    if pin_clash:
                        continue

                    score = score_candidate(
                        cx, cy, rotation, ehw, ehh, keepout,
                        ci.instance_id, cat, placed, placed_map,
                        catalog_map, net_graph,
                        outline_verts, outline_bounds, style,
                        congestion_grid=cg,
                        pcb_contour_verts=_pcb_contour_verts,
                    )

                    if score > best_score:
                        best_score = score
                        best_pos = (cx, cy)
                        best_rot = rotation

        if best_pos is None:
            body_w = cat.body.width_mm or cat.body.diameter_mm or 0
            body_h = cat.body.length_mm or cat.body.diameter_mm or 0
            raise PlacementError(
                ci.instance_id, ci.catalog_id,
                f"No valid position found inside the "
                f"{xmax - xmin:.0f}\u00d7{ymax - ymin:.0f}mm outline.  "
                f"Body is {body_w:.1f}\u00d7{body_h:.1f}mm with "
                f"{keepout:.1f}mm keepout.  "
                f"If UI-placed components (buttons/LEDs) block placement, "
                f"reposition them to leave a contiguous clear zone. "
                f"If the board shape is too small or narrow, "
                f"widen the outline or reduce the component count.",
            )

        hw_final, hh_final = footprint_halfdims(cat, best_rot)
        ehw_final, ehh_final = footprint_envelope_halfdims(cat, best_rot)
        _new = Placed(
            instance_id=ci.instance_id,
            catalog_id=ci.catalog_id,
            x=best_pos[0], y=best_pos[1],
            rotation=best_rot,
            hw=hw_final, hh=hh_final,
            keepout=keepout,
            env_hw=ehw_final, env_hh=ehh_final,
        )
        placed.append(_new)
        placed_map[ci.instance_id] = _new

        # Update congestion grid: block body & commit coarse routes
        cg.block_component(ci.instance_id, best_pos[0], best_pos[1],
                           ehw_final, ehh_final)
        for edge in net_graph.get(ci.instance_id, []):
            other_p = placed_map.get(edge.other_iid)
            if other_p is None:
                continue
            my_pins = resolve_pin_positions(edge.my_pins, cat)
            other_cat_c = catalog_map.get(other_p.catalog_id)
            if not my_pins or other_cat_c is None:
                continue
            other_pins = resolve_pin_positions(edge.other_pins, other_cat_c)
            if not other_pins:
                continue
            # Use centroid of pin positions for coarse route
            mx = sum(pin_world_xy(p, best_pos[0], best_pos[1], best_rot)[0] for p in my_pins) / len(my_pins)
            my = sum(pin_world_xy(p, best_pos[0], best_pos[1], best_rot)[1] for p in my_pins) / len(my_pins)
            ox = sum(pin_world_xy(p, other_p.x, other_p.y, other_p.rotation)[0] for p in other_pins) / len(other_pins)
            oy = sum(pin_world_xy(p, other_p.x, other_p.y, other_p.rotation)[1] for p in other_pins) / len(other_pins)
            coarse_path = cg.route_coarse(mx, my, ox, oy)
            if coarse_path is not None:
                cg.commit_net(edge.net_id, coarse_path)

        log.info(
            "Auto-placed %s at (%.1f, %.1f) rot=%d° score=%.2f",
            ci.instance_id, best_pos[0], best_pos[1], best_rot, best_score,
        )

    # ── 3b. SA refinement ──────────────────────────────────────────
    # The constructive loop above is greedy and order-dependent.
    # Simulated annealing globally refines positions to reduce total
    # wirelength and routing congestion.
    if len(placed) - len(ui_ids) >= 2:
        placed = sa_refine(
            placed,
            ui_ids,
            design.nets,
            catalog_map,
            outline_poly,
            cg,
        )
        placed_map = {p.instance_id: p for p in placed}

    # ── 4. Build output ────────────────────────────────────────────

    result_components = []
    for p in placed:
        cat = catalog_map.get(p.catalog_id)
        x = round(p.x, 2)
        y = round(p.y, 2)
        rot = p.rotation
        style = effective_style.get(p.instance_id, "top")
        side_y_offset = (cat.body.height_mm / 2
                         if style == "side" and cat.mounting.style != "side"
                         else 0)

        pin_positions: dict[str, tuple[float, float]] = {}
        if cat is not None:
            rad = math.radians(rot)
            cos_r = math.cos(rad)
            sin_r = math.sin(rad)
            for pin in cat.pins:
                # Negate side_y_offset: the edge-tangent rotation makes local +y
                # point outward, so -offset moves the pin inward into the cavity.
                px, py = pin.position_mm[0], pin.position_mm[1] - side_y_offset
                pin_positions[pin.id] = (
                    round(x + px * cos_r - py * sin_r, 4),
                    round(y + px * sin_r + py * cos_r, 4),
                )

        result_components.append(PlacedComponent(
            instance_id=p.instance_id,
            catalog_id=p.catalog_id,
            x_mm=x,
            y_mm=y,
            rotation_deg=rot,
            pin_positions=pin_positions,
            mounting_style=style,
        ))

    return FullPlacement(
        components=result_components,
        outline=design.outline,
        nets=design.nets,
        enclosure=design.enclosure,
    )
