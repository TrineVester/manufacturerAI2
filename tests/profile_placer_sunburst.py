"""Hierarchical placer profiler with interactive sunburst visualization.

Instruments every significant operation in the placement pipeline with nested
timers.  Produces an interactive HTML sunburst chart plus a console tree and
sorted category table.

Run:  python tests/profile_placer_sunburst.py [--large]
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reuse HTimer infrastructure from the base profiler
from tests.profile_base import (
    HTimer, TimerNode, print_tree, build_sunburst_html,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


# ── Build design specs ────────────────────────────────────────────


def _flashlight_design():
    from src.catalog.loader import load_catalog
    from tests.flashlight_fixture import make_flashlight_design
    catalog = load_catalog()
    design = make_flashlight_design()
    return design, catalog


def _large_design():
    from src.catalog.loader import load_catalog
    from src.pipeline.design.parsing import parse_physical_design, parse_circuit
    from src.pipeline.design.models import DesignSpec
    catalog = load_catalog()
    d = json.loads((FIXTURE_DIR / "large_design.json").read_text(encoding="utf-8"))
    c = json.loads((FIXTURE_DIR / "large_circuit.json").read_text(encoding="utf-8"))
    physical = parse_physical_design(d)
    circuit = parse_circuit(c)
    design = DesignSpec(
        components=circuit.components,
        nets=circuit.nets,
        outline=physical.outline,
        ui_placements=physical.ui_placements,
        enclosure=physical.enclosure,
    )
    return design, catalog


# ── Profiled placement ────────────────────────────────────────────


def run_profiled_placement(use_large: bool = False):
    """Run placement with full hierarchical instrumentation."""

    if use_large:
        design, catalog = _large_design()
        label = "large"
    else:
        design, catalog = _flashlight_design()
        label = "flashlight"

    print(f"Profiling {label} placement...")
    print(f"  Components: {len(design.components)}")
    print(f"  UI placements: {len(design.ui_placements)}")
    print(f"  Nets: {len(design.nets)}")

    # ── Import placer internals ──
    from shapely.geometry import Polygon, box as shapely_box
    from shapely.prepared import prep as shapely_prep
    from src.catalog.models import CatalogResult
    from src.pipeline.design.models import Outline

    import src.pipeline.placer.engine as engine_mod
    import src.pipeline.placer.scoring as scoring_mod
    import src.pipeline.placer.candidates as cand_mod
    import src.pipeline.placer.congestion as cong_mod
    import src.pipeline.placer.annealing as anneal_mod
    import src.pipeline.placer.geometry as geom_mod
    import src.pipeline.placer.nets as nets_mod

    from src.pipeline.placer.geometry import (
        footprint_halfdims, footprint_envelope_halfdims, footprint_area,
        rect_inside_polygon, rect_edge_clearance, aabb_gap, pin_world_xy,
    )
    from src.pipeline.placer.models import (
        PlacedComponent, FullPlacement, PlacementError, Placed,
        GRID_STEP_MM, VALID_ROTATIONS, MIN_EDGE_CLEARANCE_MM,
        ROUTING_CHANNEL_MM, MIN_PIN_CLEARANCE_MM,
    )
    from src.pipeline.placer.nets import (
        build_net_graph, count_shared_nets, build_placement_groups,
        resolve_pin_positions,
    )
    from src.pipeline.placer.candidates import generate_candidates
    from src.pipeline.placer.congestion import CongestionGrid
    from src.pipeline.placer.scoring import score_candidate
    from src.pipeline.placer.annealing import sa_refine

    ht = HTimer()
    catalog_map = {c.id: c for c in catalog.components}

    with ht.section("place_components"):

        # ── Initialization ──
        with ht.section("initialization"):
            outline_poly = Polygon(design.outline.vertices)
            outline_verts = design.outline.vertices
            xmin, ymin, xmax, ymax = outline_poly.bounds
            outline_bounds = (xmin, ymin, xmax, ymax)

            if not outline_poly.is_valid or outline_poly.area <= 0:
                raise PlacementError("_outline", "_outline",
                                     "Outline polygon is invalid")

            _has_raised_bottom = any(
                getattr(p, 'z_bottom', None) for p in design.outline.points
            ) or design.enclosure.bottom_surface is not None
            _floor_threshold = 0.0
            _pcb_contour_verts = None
            if _has_raised_bottom:
                from src.pipeline.design.height_field import (
                    blended_bottom_height, sample_bottom_height_grid,
                    pcb_contour_from_bottom_grid,
                )
                from src.pipeline.config import FLOOR_MM
                _floor_threshold = FLOOR_MM - 0.1
                _bot_grid = sample_bottom_height_grid(design.outline, design.enclosure)
                if _bot_grid is not None:
                    _contour_pts = pcb_contour_from_bottom_grid(
                        _bot_grid, design.outline, FLOOR_MM,
                    )
                    if _contour_pts is not None and len(_contour_pts) >= 3:
                        _pcb_contour_verts = [(p[0], p[1]) for p in _contour_pts]

            _ebot = design.enclosure.edge_bottom
            _raw_inset = _ebot.size_mm if _ebot.type in ("fillet", "chamfer") else 0.0
            _max_inset = design.enclosure.height_mm * 0.42
            floor_inset = min(_raw_inset, _max_inset)

            with ht.section("net_graph"):
                net_graph = build_net_graph(design.nets)

            with ht.section("congestion_grid"):
                cg = CongestionGrid(outline_poly)

            effective_style = {}
            up_style_map = {up.instance_id: up.mounting_style
                            for up in design.ui_placements if up.mounting_style}
            for ci in design.components:
                cat = catalog_map.get(ci.catalog_id)
                if cat:
                    effective_style[ci.instance_id] = (
                        up_style_map.get(ci.instance_id)
                        or ci.mounting_style
                        or cat.mounting.style
                    )

        # ── UI placement ──
        with ht.section("ui_placement"):
            placed = []
            placed_map = {}
            ui_ids = set()

            for up in design.ui_placements:
                ci = next(c for c in design.components
                          if c.instance_id == up.instance_id)
                cat = catalog_map[ci.catalog_id]
                style = effective_style.get(ci.instance_id, cat.mounting.style)

                if style == "side" and up.edge_index is not None:
                    half_depth = (cat.body.length_mm or 1.0) / 2
                    x, y, rot = engine_mod._snap_to_edge(
                        up.x_mm, up.y_mm, design.outline,
                        up.edge_index, inward_offset=half_depth)
                else:
                    x, y, rot = up.x_mm, up.y_mm, 0

                hw, hh = footprint_halfdims(cat, rot)
                ehw, ehh = footprint_envelope_halfdims(cat, rot)
                p = Placed(
                    instance_id=ci.instance_id, catalog_id=ci.catalog_id,
                    x=x, y=y, rotation=rot,
                    hw=hw, hh=hh,
                    keepout=cat.mounting.keepout_margin_mm,
                    env_hw=ehw, env_hh=ehh,
                )
                placed.append(p)
                placed_map[ci.instance_id] = p
                ui_ids.add(ci.instance_id)
                cg.block_component(ci.instance_id, x, y, ehw, ehh)

        # ── Sort remaining ──
        with ht.section("placement_groups"):
            to_place_ids = [ci.instance_id for ci in design.components
                            if ci.instance_id not in ui_ids]
            area_map = {
                ci.instance_id: footprint_area(catalog_map[ci.catalog_id])
                for ci in design.components if ci.instance_id not in ui_ids
            }
            groups = build_placement_groups(to_place_ids, net_graph, area_map)
            ordered_ids = [iid for group in groups for iid in group]
            ci_map = {ci.instance_id: ci for ci in design.components}
            to_place = [ci_map[iid] for iid in ordered_ids]

        print(f"  Auto-placing: {len(to_place)} components")

        # ── Auto-place loop ──
        prep_poly = shapely_prep(outline_poly)
        _min_pin_sq = MIN_PIN_CLEARANCE_MM * MIN_PIN_CLEARANCE_MM

        from src.pipeline.placer.geometry import _is_aabb
        _outline_aabb = _is_aabb(outline_verts)

        with ht.section("auto_place_loop"):
            for ci_idx, ci in enumerate(to_place):
                with ht.section(f"place_{ci.instance_id}"):
                    cat = catalog_map[ci.catalog_id]
                    style = effective_style.get(ci.instance_id, cat.mounting.style)
                    keepout = cat.mounting.keepout_margin_mm
                    grid_step = GRID_STEP_MM

                    best_pos = None
                    best_rot = 0
                    best_score = -float("inf")

                    _placed_pins_world = []
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
                        (True, 1.0, MIN_EDGE_CLEARANCE_MM + floor_inset),
                        (True, 0.5, max(MIN_EDGE_CLEARANCE_MM * 0.5, 0.5) + floor_inset),
                    ]

                    total_candidates = 0
                    total_checked = 0

                    for _pass, (ignore_channel_gap, keepout_scale, edge_clr) in enumerate(_PASSES):
                        if best_pos is not None:
                            break

                        for rotation in VALID_ROTATIONS:
                            hw, hh = footprint_halfdims(cat, rotation)
                            ehw, ehh = footprint_envelope_halfdims(cat, rotation)
                            ihw = ehw + edge_clr
                            ihh = ehh + edge_clr

                            with ht.section("generate_candidates"):
                                candidates = generate_candidates(
                                    ci.instance_id, cat, rotation,
                                    ehw, ehh, ihw, ihh,
                                    placed, placed_map, net_graph, catalog_map,
                                    outline_bounds, edge_clr, grid_step, style,
                                )
                            total_candidates += len(candidates)

                            _rad = math.radians(rotation)
                            _cos_r, _sin_r = math.cos(_rad), math.sin(_rad)
                            my_pin_offsets = [
                                (pin.position_mm[0] * _cos_r - pin.position_mm[1] * _sin_r,
                                 pin.position_mm[0] * _sin_r + pin.position_mm[1] * _cos_r)
                                for pin in cat.pins
                            ]

                            for cx, cy in candidates:
                                total_checked += 1

                                with ht.section("contains_check"):
                                    if _outline_aabb is not None:
                                        _oxmin, _oymin, _oxmax, _oymax = _outline_aabb
                                        inside = not (cx - ihw < _oxmin or cx + ihw > _oxmax
                                                      or cy - ihh < _oymin or cy + ihh > _oymax)
                                    else:
                                        inside = prep_poly.contains(
                                            shapely_box(cx - ihw, cy - ihh,
                                                        cx + ihw, cy + ihh))
                                if not inside:
                                    continue

                                if _has_raised_bottom:
                                    with ht.section("raised_floor_check"):
                                        from src.pipeline.design.height_field import blended_bottom_height
                                        _raised = False
                                        _check_pts = [
                                            (cx, cy),
                                            (cx - ehw, cy - ehh),
                                            (cx + ehw, cy - ehh),
                                            (cx - ehw, cy + ehh),
                                            (cx + ehw, cy + ehh),
                                        ]
                                        for _pox, _poy in my_pin_offsets:
                                            _check_pts.append((cx + _pox, cy + _poy))
                                        for _px, _py in _check_pts:
                                            if blended_bottom_height(
                                                _px, _py, design.outline,
                                                design.enclosure,
                                            ) >= _floor_threshold:
                                                _raised = True
                                                break
                                    if _raised:
                                        continue

                                with ht.section("overlap_check"):
                                    overlap = False
                                    for p in placed:
                                        n_channels = count_shared_nets(
                                            ci.instance_id, p.instance_id, net_graph)
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

                                with ht.section("edge_clearance"):
                                    edge_dist = rect_edge_clearance(
                                        cx, cy, ehw, ehh, outline_verts)
                                if edge_dist < edge_clr:
                                    continue

                                if _pcb_contour_verts is not None:
                                    with ht.section("contour_clearance"):
                                        contour_edge_dist = rect_edge_clearance(
                                            cx, cy, ehw, ehh, _pcb_contour_verts)
                                    if contour_edge_dist < edge_clr:
                                        continue

                                with ht.section("pin_clash_check"):
                                    pin_clash = False
                                    my_pins_world = [(cx + ox, cy + oy)
                                                     for ox, oy in my_pin_offsets]
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

                                with ht.section("score_candidate"):
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

                    print(f"    {ci.instance_id}: {total_candidates} candidates, "
                          f"{total_checked} checked, "
                          f"best_score={best_score:.1f}")

                    if best_pos is None:
                        body_w = cat.body.width_mm or cat.body.diameter_mm or 0
                        body_h = cat.body.length_mm or cat.body.diameter_mm or 0
                        raise PlacementError(
                            ci.instance_id, ci.catalog_id,
                            f"No valid position found inside "
                            f"{xmax - xmin:.0f}x{ymax - ymin:.0f}mm outline."
                        )

                    hw_final, hh_final = footprint_halfdims(cat, best_rot)
                    ehw_final, ehh_final = footprint_envelope_halfdims(cat, best_rot)
                    _new = Placed(
                        instance_id=ci.instance_id, catalog_id=ci.catalog_id,
                        x=best_pos[0], y=best_pos[1], rotation=best_rot,
                        hw=hw_final, hh=hh_final, keepout=keepout,
                        env_hw=ehw_final, env_hh=ehh_final,
                    )
                    placed.append(_new)
                    placed_map[ci.instance_id] = _new

                    with ht.section("congestion_update"):
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
                            other_pins = resolve_pin_positions(
                                edge.other_pins, other_cat_c)
                            if not other_pins:
                                continue
                            mx = sum(pin_world_xy(p, best_pos[0], best_pos[1],
                                                  best_rot)[0]
                                     for p in my_pins) / len(my_pins)
                            my_ = sum(pin_world_xy(p, best_pos[0], best_pos[1],
                                                   best_rot)[1]
                                      for p in my_pins) / len(my_pins)
                            ox = sum(pin_world_xy(p, other_p.x, other_p.y,
                                                  other_p.rotation)[0]
                                     for p in other_pins) / len(other_pins)
                            oy = sum(pin_world_xy(p, other_p.x, other_p.y,
                                                  other_p.rotation)[1]
                                     for p in other_pins) / len(other_pins)
                            coarse_path = cg.route_coarse(mx, my_, ox, oy)
                            if coarse_path is not None:
                                cg.commit_net(edge.net_id, coarse_path)

        # ── SA refinement ──
        with ht.section("sa_refinement"):
            if len(placed) - len(ui_ids) >= 2:
                # Instrument SA cost functions
                orig_hpwl = anneal_mod._hpwl
                orig_overlap = anneal_mod._overlap_penalty
                orig_outline = anneal_mod._outline_penalty_fast
                orig_pin_clr = anneal_mod._pin_clearance_penalty
                orig_cong_cost = anneal_mod._congestion_cost
                orig_crossing = anneal_mod._crossing_count

                def timed_hpwl(*a, **kw):
                    with ht.section("hpwl"):
                        return orig_hpwl(*a, **kw)

                def timed_overlap(*a, **kw):
                    with ht.section("overlap_penalty"):
                        return orig_overlap(*a, **kw)

                def timed_outline(*a, **kw):
                    with ht.section("outline_penalty"):
                        return orig_outline(*a, **kw)

                def timed_pin_clr(*a, **kw):
                    with ht.section("pin_clr_penalty"):
                        return orig_pin_clr(*a, **kw)

                def timed_cong_cost(*a, **kw):
                    with ht.section("congestion_cost"):
                        return orig_cong_cost(*a, **kw)

                def timed_crossing(*a, **kw):
                    with ht.section("crossing_count"):
                        return orig_crossing(*a, **kw)

                anneal_mod._hpwl = timed_hpwl
                anneal_mod._overlap_penalty = timed_overlap
                anneal_mod._outline_penalty_fast = timed_outline
                anneal_mod._pin_clearance_penalty = timed_pin_clr
                anneal_mod._congestion_cost = timed_cong_cost
                anneal_mod._crossing_count = timed_crossing

                try:
                    placed = sa_refine(
                        placed, ui_ids, design.nets, catalog_map,
                        outline_poly, cg,
                    )
                finally:
                    anneal_mod._hpwl = orig_hpwl
                    anneal_mod._overlap_penalty = orig_overlap
                    anneal_mod._outline_penalty_fast = orig_outline
                    anneal_mod._pin_clearance_penalty = orig_pin_clr
                    anneal_mod._congestion_cost = orig_cong_cost
                    anneal_mod._crossing_count = orig_crossing

                placed_map = {p.instance_id: p for p in placed}

    # Close the root timer
    ht.root.elapsed = sum(c.elapsed for c in ht.root.children)

    return ht, label


# ── Main ──────────────────────────────────────────────────────────


def main():
    use_large = "--large" in sys.argv
    ht, label = run_profiled_placement(use_large=use_large)

    print()
    print("=" * 80)
    print_tree(ht.root)
    print("=" * 80)

    out_path = Path(__file__).resolve().parent.parent / "outputs" / "profile_placer_sunburst.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_sunburst_html(ht.root, title=f"Placer Profile ({label})")
    out_path.write_text(html, encoding="utf-8")
    print(f"\nInteractive sunburst chart saved to: {out_path}")


if __name__ == "__main__":
    main()
