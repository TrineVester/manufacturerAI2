"""Main routing engine — greedy Manhattan trace routing with iterative improvement.

Algorithm:
  1. Build routing grid, block component bodies, protect pin cells.
  2. Resolve pin positions for all nets (with dynamic MCU allocation).
  3. Sort nets by priority (pin count, then HPWL).
  4. Route all nets via the Solution class (clean A* with crossing rip-up).
  5. Iteratively improve: rip up worst nets + neighbors, re-route in
     perturbed order. Keep best result seen. Stop when perfect or stalled.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from shapely.geometry import Polygon

from src.catalog.models import CatalogResult
from src.pipeline.placer.models import FullPlacement
from src.pipeline.placer.geometry import footprint_halfdims

from .grid import RoutingGrid
from .models import RoutingResult, RouterConfig
from .pins import (
    PinPool,
    pin_world_xy, build_pin_pools,
    resolve_pin_ref, get_pin_world_pos,
    allocate_best_pin,
)
from .solution import Solution, Snapshot, NetPad, _PinRef


log = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


# ── Main entry point ───────────────────────────────────────────────


def route_traces(
    placement: FullPlacement,
    catalog: CatalogResult,
    *,
    config: RouterConfig | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> RoutingResult:
    """Route all nets using iterative improvement."""
    if config is None:
        config = RouterConfig()

    catalog_map = {c.id: c for c in catalog.components}
    outline_poly = Polygon(
        placement.outline.vertices,
        placement.outline.hole_vertices or None,
    )

    log.info("Router: %d components, %d nets, area=%.1f mm²",
             len(placement.components), len(placement.nets), outline_poly.area)

    if not outline_poly.is_valid or outline_poly.area <= 0:
        return RoutingResult(
            traces=[], pin_assignments={},
            failed_nets=[n.id for n in placement.nets],
        )

    # 1. Build grid & block component bodies
    grid = RoutingGrid(
        outline_poly,
        resolution=config.grid_resolution_mm,
        edge_clearance=config.edge_clearance_mm,
        trace_width_mm=config.trace_width_mm,
        trace_clearance_mm=config.trace_clearance_mm,
    )

    raised_blocked = grid.block_raised_floor(placement.outline, placement.enclosure)
    if raised_blocked:
        log.info("Router: blocked %d cells in raised-floor zone", raised_blocked)

    pad_radius = _compute_pad_radius(config)
    _block_components(grid, placement, catalog_map, pad_radius)

    # 2. Prepare pin cell map, Voronoi pin proximity
    all_pin_cells = _build_all_pin_cells(placement, catalog, grid)
    pin_clearance_cells = _compute_pin_clearance_cells(config)
    pin_voronoi = _build_pin_voronoi(all_pin_cells, grid, pin_clearance_cells)

    # 3. Parse net pin references
    net_pad_map = _parse_net_refs(placement, catalog, catalog_map)

    # 4. Collect routable net IDs and compute priority ordering
    net_ids = [
        n.id for n in placement.nets
        if len(net_pad_map.get(n.id, [])) >= 2
    ]

    pin_pools = build_pin_pools(placement, catalog)
    pads_map, pin_assignments = _resolve_all_pads(
        net_ids, net_pad_map, placement, catalog, grid, pin_pools,
    )
    ordering = _priority_order(net_ids, net_pad_map, pads_map)

    # 5. Build solution and route initial pass
    solution = Solution(
        grid, config, placement, catalog,
        net_pad_map, pin_voronoi, all_pin_cells,
    )
    solution.expected_nets = set(net_ids)
    solution.pin_assignments = pin_assignments

    solution.route_nets(ordering, pads_map)
    log.info("Initial solution: score=%s", solution.score())

    _emit_progress(on_progress, solution, net_ids, config,
                   iteration=0, phase="initial", stall=0,
                   best_score=solution.score())

    # 6. Iterative improvement — GA-inspired loop with elite pool,
    #    local refinement, and blocker-aware exploration.
    improve_deadline = time.monotonic() + config.max_routing_seconds
    best = solution.snapshot()
    best_score = solution.score()
    best_pads_map = dict(pads_map)
    best_pin_assignments = dict(pin_assignments)
    best_pin_pools = _copy_pin_pools(pin_pools)
    stall = 0
    iteration = 0

    elites: list[_Elite] = [_Elite(
        score=best_score,
        snapshot=solution.snapshot(),
        pads_map=dict(pads_map),
        pin_assignments=dict(pin_assignments),
        pin_pools=_copy_pin_pools(pin_pools),
    )]

    while True:
        if cancel and cancel.is_set():
            break
        if time.monotonic() > improve_deadline:
            log.info("Router: wall-clock budget exhausted after %d iterations", iteration)
            break
        all_routed = best_score[0] == 0
        if iteration >= config.max_improve_iterations:
            break

        before = solution.score()
        missing = [nid for nid in net_ids if nid not in solution.routes]
        phase = _pick_phase(iteration, stall, missing, all_routed, len(elites))

        if phase == "refine":
            improved_any = _refine_pass(solution, pads_map, net_ids)
            after = solution.score()
            if not improved_any:
                stall += 1
                log.info("Iter %d [refine]: no shorter paths (stall %d/%d)",
                         iteration + 1, stall, config.stall_limit)
                _emit_progress(on_progress, solution, net_ids, config,
                               iteration=iteration + 1, phase="no_improvement",
                               stall=stall, best_score=best_score)
                if stall >= config.stall_limit:
                    break
                iteration += 1
                continue
        elif phase == "restart":
            all_net_ids = list(solution.routes.keys())
            solution.rip_up(all_net_ids)
            rand_order = list(net_ids)
            random.shuffle(rand_order)
            solution.route_nets(rand_order, pads_map)
            unrouted_still = [nid for nid in net_ids
                             if nid not in solution.routes]
            if unrouted_still:
                _re_resolve_and_route(
                    unrouted_still, net_pad_map, placement, catalog,
                    grid, pin_pools, pin_assignments, pads_map, solution,
                )
            after = solution.score()
            log.info("Iter %d [restart]: rerouted all → %s", iteration + 1, after)
        elif phase == "crossover" and len(elites) > 1:
            donor = _pick_donor(elites, best_score)
            solution.restore(donor.snapshot)
            pads_map.update(donor.pads_map)
            pin_assignments.update(donor.pin_assignments)
            _restore_pin_pools(pin_pools, donor.pin_pools)
            log.info("Iter %d [crossover]: restored elite with score %s",
                     iteration + 1, donor.score)
            phase = "explore"

        if phase != "refine":
            if missing:
                blockers = solution.find_blockers(missing, pads_map)
                targets = list(blockers) if blockers else solution.worst_nets(k=3)
            else:
                targets = (solution.random_nets(k=3) if iteration % 3 == 0
                           else solution.worst_nets(k=3))
            if not targets:
                break

            neighborhood = solution.neighborhood(targets)
            solution.rip_up(neighborhood)
            new_order = _perturb(neighborhood, targets, iteration)
            solution.route_nets(new_order, pads_map)

            unrouted = [nid for nid in net_ids
                        if nid not in solution.routes and nid in pads_map]
            if unrouted:
                solution.route_nets(unrouted, pads_map)

            unrouted_still = [nid for nid in net_ids
                             if nid not in solution.routes]
            if unrouted_still:
                _re_resolve_and_route(
                    unrouted_still, net_pad_map, placement, catalog,
                    grid, pin_pools, pin_assignments, pads_map, solution,
                )

            after = solution.score()

        if after < before:
            best = solution.snapshot()
            best_score = after
            best_pads_map = dict(pads_map)
            best_pin_assignments = dict(pin_assignments)
            best_pin_pools = _copy_pin_pools(pin_pools)
            _update_elites(elites, _Elite(
                score=after,
                snapshot=solution.snapshot(),
                pads_map=dict(pads_map),
                pin_assignments=dict(pin_assignments),
                pin_pools=_copy_pin_pools(pin_pools),
            ), config.elite_pool_size)
            stall = 0
            log.info("Iter %d [%s]: improved %s → %s",
                     iteration + 1, phase, before, after)
            _emit_progress(on_progress, solution, net_ids, config,
                           iteration=iteration + 1, phase="improved", stall=0,
                           best_score=best_score, prev_score=before)
        else:
            _maybe_add_diverse_elite(
                elites, solution, pads_map, pin_assignments,
                pin_pools, config.elite_pool_size,
            )
            solution.restore(best)
            pads_map.update(best_pads_map)
            pin_assignments.update(best_pin_assignments)
            _restore_pin_pools(pin_pools, best_pin_pools)
            stall += 1
            log.info("Iter %d [%s]: no improvement %s (stall %d/%d)",
                     iteration + 1, phase, before, stall, config.stall_limit)
            _emit_progress(on_progress, solution, net_ids, config,
                           iteration=iteration + 1, phase="no_improvement",
                           stall=stall, best_score=best_score)
            effective_limit = config.stall_limit
            if not all_routed:
                effective_limit *= 3
            if stall >= effective_limit:
                log.info(
                    "Stalled for %d iterations at iteration %d, stopping",
                    stall, iteration + 1,
                )
                break

        iteration += 1

    solution.restore(best)
    pin_assignments.update(best_pin_assignments)
    solution.pin_assignments = pin_assignments
    routed = len(solution.routes)
    missing = len(net_ids) - routed
    if missing > 0:
        log.warning("Router: %d/%d nets routed (%d missing)",
                    routed, len(net_ids), missing)
    else:
        log.info("Router: all %d nets routed", len(net_ids))

    # 7. DRC repair pass — rip up violating nets and reroute with avoidance
    pin_positions = _collect_pin_positions(placement, catalog)
    from .drc import run_drc

    pin_to_net: dict[str, str] = {}
    for net_id_pk, pad_list in pads_map.items():
        for pad in pad_list:
            pin_to_net[f"{pad.instance_id}:{pad.pin_id}"] = net_id_pk

    for repair_round in range(5):
        result = solution.to_result()
        drc_report = run_drc(result, pin_positions, outline_poly, config)
        if drc_report.ok:
            break

        errors_before = len(drc_report.errors)
        violated_nets: set[str] = set()
        penalty_pins: dict[str, tuple[float, float]] = {}

        for v in drc_report.errors:
            if v.rule == "trace_pin_clearance":
                if v.net_id:
                    violated_nets.add(v.net_id)
                if v.details:
                    pkey = v.details.get("pin")
                    if pkey and pkey in pin_positions:
                        penalty_pins[pkey] = pin_positions[pkey]
                    if pkey and pkey in pin_to_net:
                        violated_nets.add(pin_to_net[pkey])
            elif v.rule == "trace_trace_clearance" and v.details:
                na = v.details.get("net_a")
                nb = v.details.get("net_b")
                if na:
                    violated_nets.add(na)
                if nb:
                    violated_nets.add(nb)

        if not violated_nets:
            break

        cost_map = _build_pin_avoidance_cost(
            grid, penalty_pins, all_pin_cells, config,
        )

        to_rip = sorted(violated_nets & set(solution.routes))
        if not to_rip:
            break

        neighborhood = solution.neighborhood(to_rip)
        to_rip_expanded = sorted(set(to_rip) | set(neighborhood))

        score_before = solution.score()
        snap_before = solution.snapshot()
        solution.rip_up(to_rip_expanded)
        for nid in to_rip_expanded:
            pads = pads_map.get(nid)
            if pads:
                solution.route_net(nid, pads, cost_map=cost_map)

        score_after = solution.score()
        result_after = solution.to_result()
        drc_after = run_drc(result_after, pin_positions, outline_poly, config)
        errors_after = len(drc_after.errors)

        if errors_after < errors_before:
            log.info("DRC repair round %d: %d→%d errors, rerouted %s",
                     repair_round + 1, errors_before, errors_after, to_rip)
        elif score_after <= score_before:
            log.info("DRC repair round %d: score held, rerouted %s",
                     repair_round + 1, to_rip)
        else:
            solution.restore(snap_before)
            log.info("DRC repair round %d: reverted (score worse, no DRC gain)",
                     repair_round + 1)
            break

    result = solution.to_result()

    drc_report = run_drc(result, pin_positions, outline_poly, config)
    if drc_report.ok:
        log.info("Router DRC: PASS (0 errors)")
    else:
        log.warning("Router DRC: FAIL (%d errors, %d warnings)",
                    len(drc_report.errors), len(drc_report.warnings))
        for v in drc_report.errors:
            log.warning("  DRC %s | %s: %s", v.rule, v.net_id, v.message)

    return result


# ── Progress reporting ─────────────────────────────────────────────

def _emit_progress(
    on_progress: ProgressCallback | None,
    solution: Solution,
    net_ids: list[str],
    config: RouterConfig,
    *,
    iteration: int,
    phase: str,
    stall: int,
    best_score: tuple[int, int],
    prev_score: tuple[int, int] | None = None,
) -> None:
    if on_progress is None:
        return

    try:
        routed = len(solution.routes)
        total_nets = len(net_ids)
        failed = sorted(solution.expected_nets - set(solution.routes))
        trace_lengths = solution.trace_lengths_mm()
        total_length = round(sum(trace_lengths.values()), 2)

        lines: list[str] = []

        if phase == "initial":
            lines.append(f"Initial pass — {routed}/{total_nets} nets, {total_length} mm")
        elif phase == "improved":
            lines.append(
                f"Iter {iteration}/{config.max_improve_iterations}"
                f" — {routed}/{total_nets} nets, {total_length} mm  ★ improved"
            )
        else:
            lines.append(
                f"Iter {iteration}/{config.max_improve_iterations}"
                f" — {routed}/{total_nets} nets, {total_length} mm"
                f"  stall {stall}/{config.stall_limit}"
            )

        if failed:
            lines.append(f"Failed: {', '.join(failed)}")

        sorted_nets = sorted(trace_lengths.items(), key=lambda x: -x[1])
        for net_id, length in sorted_nets:
            lines.append(f"  {net_id}: {length} mm")

        msg = "\n".join(lines)

        partial_result = solution.to_result(include_debug=False)

        on_progress({
            "message": msg,
            "partial_result": partial_result,
        })
    except Exception:
        log.debug("Progress callback failed", exc_info=True)


# ── Net reference parsing ──────────────────────────────────────────

def _parse_net_refs(
    placement: FullPlacement,
    catalog: CatalogResult,
    catalog_map: dict,
) -> dict[str, list[_PinRef]]:
    catalog_map_groups: dict[str, dict[str, list[str]]] = {}
    for cat_comp in catalog.components:
        if cat_comp.pin_groups:
            fixed: dict[str, list[str]] = {}
            for pg in cat_comp.pin_groups:
                if pg.fixed_net:
                    fixed[pg.id] = list(pg.pin_ids)
            if fixed:
                catalog_map_groups[cat_comp.id] = fixed

    net_pad_map: dict[str, list[_PinRef]] = {}
    for net in placement.nets:
        refs: list[_PinRef] = []
        for pin_ref_str in net.pins:
            iid, pid, is_group = resolve_pin_ref(pin_ref_str, placement, catalog)
            if is_group:
                pc = next((p for p in placement.components if p.instance_id == iid), None)
                cat_id = pc.catalog_id if pc else None
                fixed_pins = catalog_map_groups.get(cat_id, {}).get(pid)
                if fixed_pins:
                    for fpin in fixed_pins:
                        refs.append(_PinRef(
                            raw=f"{iid}:{fpin}",
                            instance_id=iid,
                            pin_or_group=fpin,
                            is_group=False,
                        ))
                    continue
            refs.append(_PinRef(
                raw=pin_ref_str, instance_id=iid,
                pin_or_group=pid, is_group=is_group,
            ))
        net_pad_map[net.id] = refs
    return net_pad_map


# ── Pad resolution ─────────────────────────────────────────────────

def _snap_pad_to_free(grid: RoutingGrid, gx: int, gy: int) -> tuple[int, int]:
    """If (gx, gy) is outside the routable inset, return the nearest FREE cell
    that lies strictly inside the routing inset polygon.

    This correctly handles side-mount component pins whose world coordinates
    fall outside the outline (e.g. an IR LED mounted through the wall).  Such
    pins get clamped to the grid boundary and then force-freed by
    ``_block_components``, but those cells form an isolated island below the
    edge-clearance zone — the A* pathfinder can never escape them.  Requiring
    the snapped cell to be inside the inset polygon guarantees it is in the
    connected routing interior.

    Searches in expanding Chebyshev rings up to 20 cells out.  Falls back to
    the original cell only if nothing is found (extremely congested grids).
    """
    from .grid import FREE
    from shapely import contains_xy as _cxy
    W = grid.width
    H = grid.height
    cells = grid._cells
    inset = grid._inset_poly
    ox, oy, res = grid.origin_x, grid.origin_y, grid.resolution

    def _in_inset(cx: int, cy: int) -> bool:
        wx = ox + (cx + 0.5) * res
        wy = oy + (cy + 0.5) * res
        return bool(_cxy(inset, wx, wy))

    if cells[gy * W + gx] == FREE and _in_inset(gx, gy):
        return (gx, gy)

    for r in range(1, 21):
        for dx in range(-r, r + 1):
            for dy in (-r, r):
                nx, ny = gx + dx, gy + dy
                if 0 <= nx < W and 0 <= ny < H and cells[ny * W + nx] == FREE and _in_inset(nx, ny):
                    return (nx, ny)
        for dy in range(-r + 1, r):
            for dx in (-r, r):
                nx, ny = gx + dx, gy + dy
                if 0 <= nx < W and 0 <= ny < H and cells[ny * W + nx] == FREE and _in_inset(nx, ny):
                    return (nx, ny)

    log.warning("_snap_pad_to_free: no inset-interior free cell within 20 cells of (%d, %d)", gx, gy)
    return (gx, gy)


def _resolve_all_pads(
    net_ids: list[str],
    net_pad_map: dict[str, list[_PinRef]],
    placement: FullPlacement,
    catalog: CatalogResult,
    grid: RoutingGrid,
    pin_pools: dict[str, PinPool],
) -> tuple[dict[str, list[NetPad]], dict[str, str]]:
    pin_assignments: dict[str, str] = {}
    pads_map: dict[str, list[NetPad]] = {}

    for nid in net_ids:
        refs = net_pad_map[nid]
        pads = _resolve_pads(
            refs, nid, placement, catalog, pin_pools, grid, pin_assignments,
        )
        if pads is not None and len(pads) >= 2:
            pads_map[nid] = pads

    return pads_map, pin_assignments


def _resolve_pads(
    refs: list[_PinRef],
    net_id: str,
    placement: FullPlacement,
    catalog: CatalogResult,
    pin_pools: dict[str, PinPool],
    grid: RoutingGrid,
    pin_assignments: dict[str, str],
) -> list[NetPad] | None:
    pads: list[NetPad | None] = [None] * len(refs)
    unresolved_indices: list[int] = []

    for i, ref in enumerate(refs):
        if not ref.is_group:
            pos = get_pin_world_pos(
                ref.instance_id, ref.pin_or_group, placement, catalog,
            )
            if pos is None:
                log.warning("Net %s: cannot resolve pin %s", net_id, ref.raw)
                return None
            gx, gy = grid.world_to_grid(pos[0], pos[1])
            gx, gy = _snap_pad_to_free(grid, gx, gy)
            pads[i] = NetPad(
                instance_id=ref.instance_id,
                pin_id=ref.pin_or_group,
                group_id=None,
                gx=gx, gy=gy,
                world_x=pos[0], world_y=pos[1],
            )
        else:
            assignment_key = f"{net_id}|{ref.raw}"
            if assignment_key in pin_assignments:
                assigned_pin = pin_assignments[assignment_key].split(":", 1)[1]
                pos = get_pin_world_pos(
                    ref.instance_id, assigned_pin, placement, catalog,
                )
                if pos is not None:
                    gx, gy = grid.world_to_grid(pos[0], pos[1])
                    gx, gy = _snap_pad_to_free(grid, gx, gy)
                    pads[i] = NetPad(
                        instance_id=ref.instance_id,
                        pin_id=assigned_pin,
                        group_id=ref.pin_or_group,
                        gx=gx, gy=gy,
                        world_x=pos[0], world_y=pos[1],
                    )
                    continue
            unresolved_indices.append(i)

    resolved_pads = [p for p in pads if p is not None]
    if resolved_pads:
        centroid_x = sum(p.world_x for p in resolved_pads) / len(resolved_pads)
        centroid_y = sum(p.world_y for p in resolved_pads) / len(resolved_pads)
    else:
        centroid_x = grid.origin_x + grid.width * grid.resolution / 2
        centroid_y = grid.origin_y + grid.height * grid.resolution / 2

    for i in unresolved_indices:
        ref = refs[i]
        pool = pin_pools.get(ref.instance_id)
        if pool is None:
            log.warning("Net %s: no pin pool for %s", net_id, ref.raw)
            return None

        other_pads = [p for p in pads if p is not None]
        if other_pads:
            target_x = sum(p.world_x for p in other_pads) / len(other_pads)
            target_y = sum(p.world_y for p in other_pads) / len(other_pads)
        else:
            target_x, target_y = centroid_x, centroid_y

        chosen_pin = allocate_best_pin(
            ref.instance_id, ref.pin_or_group,
            target_x, target_y,
            pool, placement, catalog,
        )
        if chosen_pin is None:
            log.warning("Net %s: pool exhausted for %s:%s",
                        net_id, ref.instance_id, ref.pin_or_group)
            return None

        pos = get_pin_world_pos(ref.instance_id, chosen_pin, placement, catalog)
        if pos is None:
            return None

        gx, gy = grid.world_to_grid(pos[0], pos[1])
        gx, gy = _snap_pad_to_free(grid, gx, gy)
        pads[i] = NetPad(
            instance_id=ref.instance_id,
            pin_id=chosen_pin,
            group_id=ref.pin_or_group,
            gx=gx, gy=gy,
            world_x=pos[0], world_y=pos[1],
        )
        pin_assignments[f"{net_id}|{ref.raw}"] = f"{ref.instance_id}:{chosen_pin}"

    result = [p for p in pads if p is not None]
    return result if len(result) == len(refs) else None


# ── Pin pool snapshot helpers ──────────────────────────────────────


def _copy_pin_pools(pools: dict[str, PinPool]) -> dict[str, list[tuple[str, list[str]]]]:
    return {
        iid: [(gid, list(pins)) for gid, pins in pool.pools.items()]
        for iid, pool in pools.items()
    }


def _restore_pin_pools(
    pools: dict[str, PinPool],
    saved: dict[str, list[tuple[str, list[str]]]],
) -> None:
    for iid, groups in saved.items():
        pool = pools.get(iid)
        if pool is None:
            continue
        pool.pools = {gid: list(pins) for gid, pins in groups}


# ── Re-resolve failed nets with fresh pin assignments ──────────────


def _re_resolve_and_route(
    unrouted: list[str],
    net_pad_map: dict[str, list[_PinRef]],
    placement: FullPlacement,
    catalog: CatalogResult,
    grid: RoutingGrid,
    pin_pools: dict[str, PinPool],
    pin_assignments: dict[str, str],
    pads_map: dict[str, list[NetPad]],
    solution: Solution,
) -> None:
    """Release group-pin assignments for unrouted nets and re-resolve them.

    This lets the router try different physical pins (e.g. a different
    MCU GPIO pin) when the originally-assigned pin was unreachable.
    """
    for nid in unrouted:
        refs = net_pad_map.get(nid, [])
        has_group = any(r.is_group for r in refs)
        if not has_group:
            continue

        for r in refs:
            if not r.is_group:
                continue
            key = f"{nid}|{r.raw}"
            prev = pin_assignments.pop(key, None)
            if prev is not None:
                inst, pin = prev.split(":", 1)
                pool = pin_pools.get(inst)
                if pool and r.pin_or_group in pool.pools:
                    if pin not in pool.pools[r.pin_or_group]:
                        pool.pools[r.pin_or_group].append(pin)

        new_pads = _resolve_pads(
            refs, nid, placement, catalog, pin_pools, grid, pin_assignments,
        )
        if new_pads is not None and len(new_pads) >= 2:
            pads_map[nid] = new_pads
            solution.route_net(nid, new_pads)


# ── Priority ordering ──────────────────────────────────────────────


def _priority_order(
    net_ids: list[str],
    net_pad_map: dict[str, list[_PinRef]],
    pads_map: dict[str, list[NetPad]],
) -> list[str]:
    hpwl: dict[str, int] = {}
    for nid in net_ids:
        pads = pads_map.get(nid)
        if pads is None or len(pads) < 2:
            hpwl[nid] = 0
            continue
        xs = [p.gx for p in pads]
        ys = [p.gy for p in pads]
        hpwl[nid] = (max(xs) - min(xs)) + (max(ys) - min(ys))

    def net_priority(nid: str) -> tuple[int, int]:
        pin_count = len(net_pad_map.get(nid, []))
        return (-pin_count, -hpwl.get(nid, 0))

    ordered = sorted(net_ids, key=net_priority)
    log.debug("Initial ordering (HPWL): %s",
             ", ".join(f"{nid}({len(net_pad_map[nid])}p/{hpwl[nid]}hpwl)"
                       for nid in ordered))
    return ordered


# ── GA-inspired helpers ────────────────────────────────────────────


@dataclass
class _Elite:
    score: tuple[int, int]
    snapshot: Snapshot
    pads_map: dict[str, list[NetPad]]
    pin_assignments: dict[str, str]
    pin_pools: dict[str, list[tuple[str, list[str]]]]


def _update_elites(pool: list[_Elite], elite: _Elite, max_size: int) -> None:
    pool.append(elite)
    pool.sort(key=lambda e: e.score)
    while len(pool) > max_size:
        pool.pop()


def _pick_donor(pool: list[_Elite], current_best: tuple[int, int]) -> _Elite:
    others = [e for e in pool if e.score != current_best]
    if not others:
        return random.choice(pool)
    return random.choice(others)


def _pick_phase(
    iteration: int,
    stall: int,
    missing: list[str],
    all_routed: bool,
    n_elites: int,
) -> str:
    if missing:
        if stall >= 8 and n_elites > 1:
            return "crossover"
        if stall >= 5 and n_elites <= 1:
            return "restart"
        return "explore"

    if stall >= 6 and n_elites > 1 and random.random() < 0.4:
        return "crossover"

    if iteration % 3 == 0 and all_routed:
        return "refine"

    return "explore"


def _refine_pass(
    solution: Solution,
    pads_map: dict[str, list[NetPad]],
    net_ids: list[str],
) -> bool:
    candidates = [nid for nid in net_ids
                  if nid in solution.routes and nid in pads_map]
    random.shuffle(candidates)
    improved = False
    for nid in candidates:
        if solution.refine_single_net(nid, pads_map[nid]):
            improved = True
    return improved


def _maybe_add_diverse_elite(
    pool: list[_Elite],
    solution: Solution,
    pads_map: dict[str, list[NetPad]],
    pin_assignments: dict[str, str],
    pin_pools: dict[str, PinPool],
    max_size: int,
) -> None:
    """Add a non-improving solution to the elite pool if it routes a
    different set of nets than any existing elite (diversity)."""
    current_routed = frozenset(solution.routes.keys())
    for e in pool:
        if frozenset(e.snapshot.routes.keys()) == current_routed:
            return
    elite = _Elite(
        score=solution.score(),
        snapshot=solution.snapshot(),
        pads_map=dict(pads_map),
        pin_assignments=dict(pin_assignments),
        pin_pools=_copy_pin_pools(pin_pools),
    )
    pool.append(elite)
    pool.sort(key=lambda e: e.score)
    while len(pool) > max_size:
        pool.pop()


# ── Ordering perturbation ──────────────────────────────────────────


def _perturb(
    neighborhood: list[str],
    targets: list[str],
    iteration: int,
) -> list[str]:
    ordering = list(neighborhood)
    if not targets:
        random.shuffle(ordering)
        return ordering

    n = len(ordering)
    half = max(1, (1 + n) // 2)

    if iteration < half:
        for nid in targets:
            if nid not in ordering:
                continue
            idx = ordering.index(nid)
            new_idx = max(0, idx - (iteration + 1))
            ordering.pop(idx)
            ordering.insert(new_idx, nid)
    else:
        non_targets = [nid for nid in ordering if nid not in targets]
        random.shuffle(non_targets)
        target_copy = list(targets)
        random.shuffle(target_copy)
        ordering = list(non_targets)
        for nid in target_copy:
            pos = random.randint(0, max(0, n // 2))
            ordering.insert(pos, nid)

    return ordering


# ── Component blocking ─────────────────────────────────────────────


def _block_components(
    grid: RoutingGrid,
    placement: FullPlacement,
    catalog_map: dict,
    pad_radius: int,
) -> None:
    res = grid.resolution

    def _pin_xy(pc, pin):
        pos = pc.pin_positions.get(pin.id)
        if pos is not None:
            return pos
        return pin_world_xy(pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg)

    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.mounting.blocks_routing:
            continue
        hw, hh = footprint_halfdims(cat, pc.rotation_deg)
        keepout = cat.mounting.keepout_margin_mm
        grid.block_rect_world(
            pc.x_mm, pc.y_mm,
            hw + keepout, hh + keepout,
            permanent=True,
        )

    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        for pin in cat.pins:
            wx, wy = _pin_xy(pc, pin)
            gx, gy = grid.world_to_grid(wx, wy)
            rx, ry = _pin_grid_halfdims(pin, pc.rotation_deg, res, pad_radius)
            for dx in range(-rx, rx + 1):
                for dy in range(-ry, ry + 1):
                    grid.force_free_cell(gx + dx, gy + dy)
                    grid.protect_cell(gx + dx, gy + dy)

    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.mounting.blocks_routing:
            continue
        hw, hh = footprint_halfdims(cat, pc.rotation_deg)
        keepout = cat.mounting.keepout_margin_mm
        grid.block_rect_world(
            pc.x_mm, pc.y_mm,
            hw + keepout, hh + keepout,
            permanent=True,
        )

    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.mounting.blocks_routing:
            continue
        for pin in cat.pins:
            wx, wy = _pin_xy(pc, pin)
            gx, gy = grid.world_to_grid(wx, wy)
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    grid.force_free_cell(gx + dx, gy + dy)
                    grid.protect_cell(gx + dx, gy + dy)


# ── Helpers ────────────────────────────────────────────────────────


def _compute_pad_radius(cfg: RouterConfig) -> int:
    return max(1, math.ceil(
        (cfg.trace_width_mm / 2 + cfg.trace_clearance_mm) / cfg.grid_resolution_mm
    ))


def _compute_pin_clearance_cells(cfg: RouterConfig) -> int:
    return max(1, math.ceil(
        (cfg.trace_width_mm / 2 + cfg.pin_clearance_mm) / cfg.grid_resolution_mm
    ))


def _pin_grid_halfdims(pin, rotation_deg: float, res: float, default_r: int) -> tuple[int, int]:
    shape = pin.shape
    if shape and shape.type == "rect" and shape.width_mm and shape.length_mm:
        hw, hl = shape.width_mm / 2, shape.length_mm / 2
    elif shape and shape.type == "slot" and shape.width_mm and shape.length_mm:
        hw, hl = shape.width_mm / 2, shape.length_mm / 2
    elif pin.hole_diameter_mm > res:
        r = pin.hole_diameter_mm / 2
        return max(default_r, math.ceil(r / res)), max(default_r, math.ceil(r / res))
    else:
        return default_r, default_r
    rot = rotation_deg % 360
    if rot in (90, 270):
        hw, hl = hl, hw
    return max(default_r, math.ceil(hw / res)), max(default_r, math.ceil(hl / res))


def _build_pin_voronoi(
    all_pin_cells: dict[str, set[tuple[int, int]]],
    grid: RoutingGrid,
    pin_clearance_cells: int,
) -> dict[int, str]:
    W = grid.width
    H = grid.height
    r = pin_clearance_cells
    r2 = r * r
    nearest: dict[int, tuple[int, str]] = {}

    for pin_key, cells in all_pin_cells.items():
        for (px, py) in cells:
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    d2 = dx * dx + dy * dy
                    if d2 > r2:
                        continue
                    nx, ny = px + dx, py + dy
                    if not (0 <= nx < W and 0 <= ny < H):
                        continue
                    flat = ny * W + nx
                    if flat not in nearest or d2 < nearest[flat][0]:
                        nearest[flat] = (d2, pin_key)

    return {flat: key for flat, (_, key) in nearest.items()}


def _build_all_pin_cells(
    placement: FullPlacement,
    catalog: CatalogResult,
    grid: RoutingGrid,
) -> dict[str, set[tuple[int, int]]]:
    catalog_map = {c.id: c for c in catalog.components}
    result: dict[str, set[tuple[int, int]]] = {}
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        for pin in cat.pins:
            pos = pc.pin_positions.get(pin.id)
            if pos is not None:
                wx, wy = pos
            else:
                wx, wy = pin_world_xy(
                    pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg,
                )
            gx, gy = grid.world_to_grid(wx, wy)
            result[f"{pc.instance_id}:{pin.id}"] = {(gx, gy)}
    return result


def _collect_pin_positions(
    placement: FullPlacement,
    catalog: CatalogResult,
) -> dict[str, tuple[float, float]]:
    catalog_map = {c.id: c for c in catalog.components}
    positions: dict[str, tuple[float, float]] = {}
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        for pin in cat.pins:
            pos = pc.pin_positions.get(pin.id)
            if pos is not None:
                wx, wy = pos
            else:
                wx, wy = pin_world_xy(
                    pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg,
                )
            positions[f"{pc.instance_id}:{pin.id}"] = (wx, wy)
    return positions


def _build_pin_avoidance_cost(
    grid: RoutingGrid,
    penalty_pins: dict[str, tuple[float, float]],
    all_pin_cells: dict[str, set[tuple[int, int]]],
    config: RouterConfig,
) -> dict[int, float]:
    if not penalty_pins:
        return {}

    W = grid.width
    radius = max(1, math.ceil(
        (config.trace_width_mm / 2 + config.pin_clearance_mm * 2)
        / config.grid_resolution_mm
    ))
    r2 = radius * radius
    cost_map: dict[int, float] = {}

    for pin_key, (wx, wy) in penalty_pins.items():
        cells = all_pin_cells.get(pin_key, set())
        for gx, gy in cells:
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    d2 = dx * dx + dy * dy
                    if d2 > r2:
                        continue
                    nx, ny = gx + dx, gy + dy
                    if not (0 <= nx < W and 0 <= ny < grid.height):
                        continue
                    flat = ny * W + nx
                    penalty = 50 * (1 - d2 / r2)
                    if flat not in cost_map or penalty > cost_map[flat]:
                        cost_map[flat] = penalty

    return cost_map
