"""Main routing engine — connects all net pins via Manhattan traces.

Algorithm overview:
  1. Resolve pin positions (world coords) for all nets.
  2. Build the routing grid (blocked: outside outline, routing-blocked
     component bodies, and component keepout zones).
  3. Decompose multi-pin nets into 2-pin segments via MST.
  4. Order nets: shorter/simpler first, power nets last.
  5. Route each net via A* (greedy spanning tree for 3+ pin nets).
  6. If routing fails, use rip-up and reroute with random orderings.

Dynamic pin allocation:
  When a net references a group ID (e.g. "mcu_1:gpio"), the router
  picks the best physical pin from that group to minimise trace length.
"""

from __future__ import annotations

import copy
import logging
import math
import random
import time
from dataclasses import dataclass

from shapely.geometry import Polygon, Point

from src.catalog.models import CatalogResult
from src.pipeline.placer.models import FullPlacement, PlacedComponent
from src.pipeline.placer.geometry import footprint_halfdims

from .grid import RoutingGrid, TRACE_PATH, FREE, BLOCKED, PERMANENTLY_BLOCKED
from .models import (
    Trace, RoutingResult, RouterConfig,
    GRID_RESOLUTION_MM, TRACE_WIDTH_MM, TRACE_CLEARANCE_MM,
    TURN_PENALTY,
    MAX_RIP_UP_ATTEMPTS, INNER_RIP_UP_LIMIT, TIME_BUDGET_S,
)
from .pathfinder import find_path, find_path_to_tree
from .pins import (
    ResolvedPin, PinPool,
    pin_world_xy, build_pin_pools,
    resolve_pin_ref, get_pin_world_pos,
    get_group_pin_positions, allocate_best_pin,
)


log = logging.getLogger(__name__)


# ── Data structures used during routing ────────────────────────────


@dataclass
class NetPad:
    """A pad (pin position) participating in a net, in grid coordinates."""

    instance_id: str
    pin_id: str             # resolved physical pin ID
    group_id: str | None    # original group ID if dynamic allocation
    gx: int
    gy: int
    world_x: float
    world_y: float


@dataclass
class NetSegment:
    """A 2-pin segment to route, derived from MST decomposition."""

    net_id: str
    pad_a: NetPad
    pad_b: NetPad
    manhattan_dist: int


# ── Main entry point ───────────────────────────────────────────────


def route_traces(
    placement: FullPlacement,
    catalog: CatalogResult,
    *,
    config: RouterConfig | None = None,
) -> RoutingResult:
    """Route all nets in the placement.

    Parameters
    ----------
    placement : FullPlacement
        Output from the placer (all components positioned).
    catalog : CatalogResult
        Loaded component catalog.
    config : RouterConfig | None
        Tuneable parameters.  Uses defaults when *None*.

    Returns
    -------
    RoutingResult
        Traces (in world mm), dynamic pin assignments, and failed nets.
    """
    if config is None:
        config = RouterConfig()

    catalog_map = {c.id: c for c in catalog.components}
    placed_map = {p.instance_id: p for p in placement.components}
    outline_poly = Polygon(placement.outline.vertices)

    log.info("Router: starting — %d components, %d nets, outline area=%.1f mm²",
             len(placement.components), len(placement.nets), outline_poly.area)
    log.info("Router config: grid=%.2fmm, trace_w=%.1fmm, clearance=%.1fmm, "
             "edge_clr=%.1fmm, time_budget=%.0fs, max_attempts=%d",
             config.grid_resolution_mm, config.trace_width_mm,
             config.trace_clearance_mm, config.edge_clearance_mm,
             config.time_budget_s, config.max_rip_up_attempts)

    if not outline_poly.is_valid or outline_poly.area <= 0:
        log.error("Router: invalid outline polygon (valid=%s, area=%.1f) — all nets fail",
                  outline_poly.is_valid, outline_poly.area)
        return RoutingResult(traces=[], pin_assignments={}, failed_nets=[
            n.id for n in placement.nets
        ])

    # ── 1. Build pin pools for dynamic allocation ──────────────────
    pin_pools = build_pin_pools(placement, catalog)

    # ── 2. Resolve net pads ────────────────────────────────────────
    #
    # For each net, resolve all pin references to NetPads.
    # Group references are resolved *lazily* during routing — the
    # exact pin is chosen to minimise trace length.
    #
    # At this stage we collect the pads with enough info to resolve
    # them during routing.

    net_pad_map: dict[str, list[_PinRef]] = {}
    for net in placement.nets:
        refs: list[_PinRef] = []
        for pin_ref_str in net.pins:
            iid, pid, is_group = resolve_pin_ref(
                pin_ref_str, placement, catalog,
            )
            refs.append(_PinRef(
                raw=pin_ref_str,
                instance_id=iid,
                pin_or_group=pid,
                is_group=is_group,
            ))
        net_pad_map[net.id] = refs

    # ── 3. Build grid + block components ───────────────────────────
    base_grid = RoutingGrid(
        outline_poly,
        resolution=config.grid_resolution_mm,
        edge_clearance=config.edge_clearance_mm,
    )
    pad_radius = _compute_pad_radius(config)
    _block_components(base_grid, placement, catalog_map, config.grid_resolution_mm, pad_radius)
    _register_avoidance_zones(base_grid, placement, catalog_map)

    # ── 4. Route with rip-up ──────────────────────────────────────
    result = _route_with_ripup(
        net_pad_map,
        base_grid,
        placement,
        catalog,
        pin_pools,
        outline_poly,
        config,
        pad_radius,
    )

    return result


# ── Internal types ─────────────────────────────────────────────────


@dataclass
class _PinRef:
    """Unresolved pin reference from the net list."""

    raw: str
    instance_id: str
    pin_or_group: str
    is_group: bool


# ── Component blocking ─────────────────────────────────────────────


def _block_components(
    grid: RoutingGrid,
    placement: FullPlacement,
    catalog_map: dict,
    resolution: float,
    pad_radius: int,
) -> None:
    """Block grid cells under component bodies that block routing.

    After blocking, force-frees all pin positions so traces can still
    reach them (pins poke through the floor even under routing-blocked
    components like battery holders).
    """
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue

        if not cat.mounting.blocks_routing:
            continue

        hw, hh = footprint_halfdims(cat, pc.rotation_deg)
        keepout = cat.mounting.keepout_margin_mm
        grid.block_rect_world(
            pc.x_mm, pc.y_mm,
            hw + keepout, hh + keepout,
            permanent=True,
        )

    # For routing-blocked components, carve escape channels from each
    # pin BEFORE freeing the 3x3 neighborhoods, so the scan correctly
    # identifies the boundary of the blocked zone.
    #
    # We also carve escape channels for ANY pin on ANY component that
    # sits in a permanently-blocked zone — this covers:
    #   - Pins of non-blocking components whose position falls inside
    #     another component's blocked body (e.g. resistor pin inside
    #     battery footprint).
    #   - Wall-mounted components whose pins are in the outline edge
    #     clearance zone or just outside the outline boundary.
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        for pin in cat.pins:
            wx, wy = pin_world_xy(pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg)
            gx, gy = grid.world_to_grid(wx, wy)
            if grid.is_permanently_blocked(gx, gy):
                _carve_escape_channel(grid, gx, gy)

    # Force-free all pin positions (on ALL components) so they're
    # always routable, and mark them as protected so trace clearance
    # doesn't block them.
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        for pin in cat.pins:
            wx, wy = pin_world_xy(pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg)
            gx, gy = grid.world_to_grid(wx, wy)
            for dx in range(-pad_radius, pad_radius + 1):
                for dy in range(-pad_radius, pad_radius + 1):
                    grid.force_free_cell(gx + dx, gy + dy)
                    grid.protect_cell(gx + dx, gy + dy)

    # ── Re-block body interiors of routing-blocked components ──
    #
    # The pad_radius freeing above may have freed cells deep inside
    # the physical component body (e.g. battery holder).  While the
    # keepout margin around the body should be routable near pins, the
    # body interior itself must remain a hard block so traces never
    # cross through a component.
    #
    # After re-blocking, pin cells at the body boundary are force-freed
    # again so they remain reachable from the keepout-zone side.
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.mounting.blocks_routing:
            continue
        hw, hh = footprint_halfdims(cat, pc.rotation_deg)
        # Re-permanently-block the body rectangle (no keepout margin)
        grid.block_rect_world(pc.x_mm, pc.y_mm, hw, hh, permanent=True)
        log.debug("Re-blocked body interior of %s (%s): "
                  "%.1f×%.1f mm at (%.1f,%.1f)",
                  pc.instance_id, pc.catalog_id,
                  hw * 2, hh * 2, pc.x_mm, pc.y_mm)

    # Re-free pin positions that may have been re-blocked by the body
    # re-blocking pass.  Only the pin cell + immediate 1-cell ring is
    # freed, NOT the full pad_radius zone, so body interior stays blocked.
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.mounting.blocks_routing:
            continue
        for pin in cat.pins:
            wx, wy = pin_world_xy(pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg)
            gx, gy = grid.world_to_grid(wx, wy)
            # Free just the pin cell and 1-cell ring for basic reachability
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    grid.force_free_cell(gx + dx, gy + dy)
                    grid.protect_cell(gx + dx, gy + dy)


def _register_avoidance_zones(
    grid: RoutingGrid,
    placement: FullPlacement,
    catalog_map: dict,
) -> None:
    """Register soft-cost avoidance zones around every component body.

    The body + keepout rectangle of each component is added to the grid's
    cost map so the A* pathfinder strongly prefers to route *around* it.
    The cost is high enough that any detour <BODY_EXTRA cells longer than
    the straight-through path will be taken instead, but not infinite —
    traces that genuinely must reach a pin through the body area can still
    do so.

    The keepout margin used here is half the component's keepout_margin_mm
    so the avoidance zone matches the clearance band the placer reserves.
    """
    from .pathfinder import BODY_EXTRA  # local import avoids circular dep
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        hw, hh = footprint_halfdims(cat, pc.rotation_deg)
        keepout = cat.mounting.keepout_margin_mm
        grid.add_cost_zone_rect_world(
            pc.x_mm, pc.y_mm,
            hw + keepout, hh + keepout,
            extra_cost=BODY_EXTRA,
        )


def _carve_escape_channel(
    grid: RoutingGrid,
    pin_gx: int,
    pin_gy: int,
) -> None:
    """Carve escape channels from a pin through permanently blocked cells.

    Scans outward from the pin in all 4 cardinal directions through
    permanently-blocked cells until reaching a non-permanently-blocked
    cell.  Frees all cells along the two shortest directions to ensure
    the pin has a clear path out of the blocked zone.

    Only frees cells whose world-space centre falls inside the outline
    polygon, preventing traces from clipping outside the board edge.
    """
    outline = grid.outline_poly
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    dir_dists: list[tuple[int, tuple[int, int]]] = []

    for dx, dy in directions:
        dist = 0
        gx, gy = pin_gx, pin_gy
        found = False
        while dist < 300:  # safety limit
            gx += dx
            gy += dy
            dist += 1
            if not grid.in_bounds(gx, gy):
                break
            if not grid.is_permanently_blocked(gx, gy):
                dir_dists.append((dist, (dx, dy)))
                found = True
                break
        # If we didn't find a free cell, skip this direction

    if not dir_dists:
        return

    # Sort by distance, carve the two shortest directions for flexibility
    dir_dists.sort()
    for _, (dx, dy) in dir_dists[:2]:
        gx, gy = pin_gx, pin_gy
        while True:
            gx += dx
            gy += dy
            if not grid.in_bounds(gx, gy):
                break
            if not grid.is_permanently_blocked(gx, gy):
                # Reached open space — done with this direction
                break
            # Only free cells whose world centre is inside the outline
            wx, wy = grid.grid_to_world(gx, gy)
            if not outline.contains(Point(wx, wy)):
                break
            grid.force_free_cell(gx, gy)
            # Free one cell on each side perpendicular for clearance
            perp_dx, perp_dy = dy, dx
            for pdx, pdy in [(perp_dx, perp_dy), (-perp_dx, -perp_dy)]:
                nx, ny = gx + pdx, gy + pdy
                pwx, pwy = grid.grid_to_world(nx, ny)
                if grid.in_bounds(nx, ny) and outline.contains(Point(pwx, pwy)):
                    grid.force_free_cell(nx, ny)


# ── Pad resolution (deferred for group pins) ──────────────────────


def _resolve_pads(
    refs: list[_PinRef],
    net_id: str,
    placement: FullPlacement,
    catalog: CatalogResult,
    pin_pools: dict[str, PinPool],
    grid: RoutingGrid,
    pin_assignments: dict[str, str],
) -> list[NetPad] | None:
    """Resolve all pin references in a net to NetPads with grid coords.

    For group references, allocates the best physical pin from the pool
    based on proximity to other pads in the net.

    Returns None if any pin cannot be resolved.
    """
    catalog_map = {c.id: c for c in catalog.components}

    # First pass: resolve all direct pins
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
            pads[i] = NetPad(
                instance_id=ref.instance_id,
                pin_id=ref.pin_or_group,
                group_id=None,
                gx=gx, gy=gy,
                world_x=pos[0], world_y=pos[1],
            )
        else:
            # Check if this group ref was already assigned (from a
            # previous routing attempt)
            assignment_key = f"{net_id}|{ref.raw}"
            if assignment_key in pin_assignments:
                assigned_pin = pin_assignments[assignment_key].split(":", 1)[1]
                pos = get_pin_world_pos(
                    ref.instance_id, assigned_pin, placement, catalog,
                )
                if pos is not None:
                    gx, gy = grid.world_to_grid(pos[0], pos[1])
                    pads[i] = NetPad(
                        instance_id=ref.instance_id,
                        pin_id=assigned_pin,
                        group_id=ref.pin_or_group,
                        gx=gx, gy=gy,
                        world_x=pos[0], world_y=pos[1],
                    )
                    continue
            unresolved_indices.append(i)

    # Second pass: resolve group references by proximity to known pads
    # Compute centroid of all already-resolved pads as fallback target
    resolved_pads = [p for p in pads if p is not None]
    if resolved_pads:
        centroid_x = sum(p.world_x for p in resolved_pads) / len(resolved_pads)
        centroid_y = sum(p.world_y for p in resolved_pads) / len(resolved_pads)
    else:
        # Fallback: center of outline
        bounds = grid.origin_x, grid.origin_y
        centroid_x = grid.origin_x + grid.width * grid.resolution / 2
        centroid_y = grid.origin_y + grid.height * grid.resolution / 2

    for i in unresolved_indices:
        ref = refs[i]
        pool = pin_pools.get(ref.instance_id)
        if pool is None:
            log.warning("Net %s: no pin pool for %s", net_id, ref.raw)
            return None

        # Use centroid of all other pads in this net as target
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
            log.warning("Net %s: resolved pin %s:%s has no position",
                        net_id, ref.instance_id, chosen_pin)
            return None

        gx, gy = grid.world_to_grid(pos[0], pos[1])
        pads[i] = NetPad(
            instance_id=ref.instance_id,
            pin_id=chosen_pin,
            group_id=ref.pin_or_group,
            gx=gx, gy=gy,
            world_x=pos[0], world_y=pos[1],
        )
        pin_assignments[f"{net_id}|{ref.raw}"] = f"{ref.instance_id}:{chosen_pin}"

    # All should be resolved
    result = [p for p in pads if p is not None]
    if len(result) != len(refs):
        return None
    return result


# ── MST decomposition ─────────────────────────────────────────────


def _compute_mst(pads: list[NetPad]) -> list[tuple[int, int]]:
    """Kruskal's MST on pads by Manhattan distance.

    Returns list of (pad_index_a, pad_index_b) edges.
    """
    n = len(pads)
    if n < 2:
        return []

    edges: list[tuple[int, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            d = abs(pads[i].gx - pads[j].gx) + abs(pads[i].gy - pads[j].gy)
            edges.append((d, i, j))
    edges.sort()

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        return True

    result: list[tuple[int, int]] = []
    for d, i, j in edges:
        if union(i, j):
            result.append((i, j))
            if len(result) == n - 1:
                break

    return result


# ── Single-net routing ─────────────────────────────────────────────


# ── Pad neighbourhood helpers ──────────────────────────────────────


def _compute_pad_radius(cfg: RouterConfig) -> int:
    """Compute the pad protection/freeing radius from config."""
    return max(1, math.ceil(
        (cfg.trace_width_mm / 2 + cfg.trace_clearance_mm) / cfg.grid_resolution_mm
    ))


# Module-level fallback (used by tests that call helpers directly)
_PAD_RADIUS = _compute_pad_radius(RouterConfig())


def _free_pad_neighborhood(
    grid: RoutingGrid,
    gx: int, gy: int,
    pad_radius: int = _PAD_RADIUS,
) -> list[tuple[int, int]]:
    """Temporarily free cells around a pad.

    Returns a list of cells that were changed (for later restore).
    Only frees temporarily-blocked cells, never permanently-blocked.
    """
    freed: list[tuple[int, int]] = []
    for dx in range(-pad_radius, pad_radius + 1):
        for dy in range(-pad_radius, pad_radius + 1):
            cx, cy = gx + dx, gy + dy
            if grid.is_blocked(cx, cy) and not grid.is_permanently_blocked(cx, cy):
                grid.free_cell(cx, cy)
                freed.append((cx, cy))
    return freed


def _restore_cells(grid: RoutingGrid, cells: list[tuple[int, int]]) -> None:
    """Re-block cells that were temporarily freed."""
    for cx, cy in cells:
        grid.block_cell(cx, cy)


# ── Pad reachability diagnostics ───────────────────────────────────


def _pad_cell_diagnostic(
    grid: RoutingGrid,
    gx: int, gy: int,
    pad_radius: int,
) -> str:
    """Return a short diagnostic string describing the cell state
    around a pad, useful for understanding routing failures."""
    cells = grid._cells
    W = grid.width
    H = grid.height
    state_names = {FREE: 'FREE', BLOCKED: 'BLK', PERMANENTLY_BLOCKED: 'PERM',
                   TRACE_PATH: 'TRACE'}
    if not (0 <= gx < W and 0 <= gy < H):
        return 'OUT_OF_BOUNDS'
    center_val = cells[gy * W + gx]
    center_str = state_names.get(center_val, f'?{center_val}')
    prot = (gx, gy) in grid._protected
    # Count free cells in pad_radius neighbourhood
    free_count = 0
    total_count = 0
    for dx in range(-pad_radius, pad_radius + 1):
        for dy in range(-pad_radius, pad_radius + 1):
            nx, ny = gx + dx, gy + dy
            if 0 <= nx < W and 0 <= ny < H:
                total_count += 1
                if cells[ny * W + nx] == FREE:
                    free_count += 1
    return (f"{center_str}{' prot' if prot else ''} "
            f"nbr={free_count}/{total_count}free")


# ── Foreign-pin blocking ──────────────────────────────────────────


def _build_all_pin_cells(
    placement: FullPlacement,
    catalog: CatalogResult,
    grid: RoutingGrid,
) -> dict[str, set[tuple[int, int]]]:
    """Build a map of instance_id:pin_id → grid cell for every component pin.

    Returns { "inst:pin": (gx, gy), ... } — one entry per physical pin.
    """
    catalog_map = {c.id: c for c in catalog.components}
    result: dict[str, set[tuple[int, int]]] = {}
    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None:
            continue
        for pin in cat.pins:
            wx, wy = pin_world_xy(pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg)
            gx, gy = grid.world_to_grid(wx, wy)
            key = f"{pc.instance_id}:{pin.id}"
            result[key] = {(gx, gy)}
    return result


def _compute_foreign_pin_radius(cfg: RouterConfig) -> int:
    """Blocking radius around foreign pins during routing.

    Ensures traces (with their physical width) maintain at least
    ``pin_clearance_mm`` (1.27 mm — half the DIP-28 pin pitch)
    between the trace edge and every foreign pin centre.  The radius
    is measured from a pin's grid cell to the nearest trace-path cell,
    so it must cover the trace half-width plus the desired clearance.
    """
    return max(1, math.ceil(
        (cfg.trace_width_mm / 2 + cfg.pin_clearance_mm) / cfg.grid_resolution_mm
    ))


def _block_foreign_pins(
    grid: RoutingGrid,
    all_pin_cells: dict[str, set[tuple[int, int]]],
    net_pads: list[NetPad],
    pin_radius: int = 1,
    same_component_radius: int = 1,
) -> list[tuple[int, int]]:
    """Temporarily block cells around pins not belonging to the current net.

    Blocks a *pin_radius* neighbourhood around each foreign pin so
    that traces cannot physically overlap with pin pads of other nets.

    For pins on the **same component** as any of the current net's
    pads, a reduced *same_component_radius* is used instead.  This
    prevents dense pin packages (e.g. DIP-28 with 2.54 mm pitch)
    from creating impassable barriers between their own net pads
    while still maintaining minimal clearance from adjacent pin holes.

    Returns the list of cells that were blocked (for later restore).
    """
    net_instances = {pad.instance_id for pad in net_pads}

    net_cells: set[tuple[int, int]] = set()
    for pad in net_pads:
        for dx in range(-pin_radius, pin_radius + 1):
            for dy in range(-pin_radius, pin_radius + 1):
                net_cells.add((pad.gx + dx, pad.gy + dy))

    blocked: list[tuple[int, int]] = []
    for key, cells in all_pin_cells.items():
        instance_id = key.split(":", 1)[0]
        r = same_component_radius if instance_id in net_instances else pin_radius
        for cx, cy in cells:
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    cell = (cx + dx, cy + dy)
                    if cell not in net_cells and grid.is_free(*cell):
                        grid.block_cell(*cell)
                        blocked.append(cell)
    return blocked


def _unblock_foreign_pins(
    grid: RoutingGrid,
    blocked: list[tuple[int, int]],
) -> None:
    """Restore previously blocked foreign pin cells."""
    for cx, cy in blocked:
        grid.free_cell(cx, cy)


# ── Single-net routing ─────────────────────────────────────────────


def _route_single_net(
    net_id: str,
    pads: list[NetPad],
    grid: RoutingGrid,
    pad_radius: int = _PAD_RADIUS,
    turn_penalty: int = TURN_PENALTY,
    *,
    all_pin_cells: dict[str, set[tuple[int, int]]] | None = None,
    foreign_pin_radius: int = 1,
) -> tuple[list[list[tuple[int, int]]], bool]:
    """Route a single net by connecting pads via greedy spanning tree.

    Returns (list_of_grid_paths, success).
    Each path is a list of (gx, gy) cells.

    If *all_pin_cells* is provided, foreign-pin blocking is applied
    AFTER pad neighbourhood freeing so that ``_free_pad_neighborhood``
    cannot erase the foreign-pin blocks (which happens when the pad
    radius overlaps neighbouring pins on the same component).
    """
    if len(pads) < 2:
        return ([], True)

    if len(pads) == 2:
        # Simple 2-pin net: direct A*
        # Temporarily free pad neighbourhoods so the pathfinder can
        # escape through trace-clearance zones that cover the pad area.
        src = (pads[0].gx, pads[0].gy)
        snk = (pads[1].gx, pads[1].gy)

        freed_src = _free_pad_neighborhood(grid, *src, pad_radius)
        freed_snk = _free_pad_neighborhood(grid, *snk, pad_radius)

        # Block foreign pins AFTER freeing pad neighbourhoods so the
        # 11×11 free zone cannot erase the 5×5 foreign-pin blocks.
        fp_blocked: list[tuple[int, int]] = []
        if all_pin_cells is not None:
            fp_blocked = _block_foreign_pins(
                grid, all_pin_cells, pads, foreign_pin_radius,
            )

        path = find_path(grid, src, snk, turn_penalty=turn_penalty)

        _unblock_foreign_pins(grid, fp_blocked)
        _restore_cells(grid, freed_src)
        _restore_cells(grid, freed_snk)

        if path is None:
            src_diag = _pad_cell_diagnostic(grid, src[0], src[1], pad_radius)
            snk_diag = _pad_cell_diagnostic(grid, snk[0], snk[1], pad_radius)
            log.info("  [2P] %-20s NO PATH  src=%s:%s @(%d,%d) [%s]  "
                     "snk=%s:%s @(%d,%d) [%s]",
                     net_id,
                     pads[0].instance_id, pads[0].pin_id,
                     src[0], src[1], src_diag,
                     pads[1].instance_id, pads[1].pin_id,
                     snk[0], snk[1], snk_diag)
            return ([], False)
        return ([path], True)

    # Multi-pin net: MST-guided Steiner tree with per-component
    # tree-cell tracking.
    #
    # Previous bug: `combined_target` only contained pad positions,
    # NOT the tree-path cells connecting them.  So find_path_to_tree
    # had to route all the way to a distant pad instead of connecting
    # to the nearby tree backbone.  This caused 9-pin GND to fail
    # even on a nearly empty grid.
    #
    # Fix: union-find tracks which pads are connected.  Each component
    # owns a set of tree cells (pad positions + all path cells routed
    # so far).  The pathfinder target includes the full tree, not just
    # pad positions.
    mst_edges = _compute_mst(pads)
    all_paths: list[list[tuple[int, int]]] = []

    # Union-find with path compression
    uf_parent = list(range(len(pads)))
    uf_rank = [0] * len(pads)

    def _uf_find(x: int) -> int:
        while uf_parent[x] != x:
            uf_parent[x] = uf_parent[uf_parent[x]]
            x = uf_parent[x]
        return x

    def _uf_union(a: int, b: int) -> None:
        ra, rb = _uf_find(a), _uf_find(b)
        if ra == rb:
            return
        if uf_rank[ra] < uf_rank[rb]:
            ra, rb = rb, ra
        uf_parent[rb] = ra
        if uf_rank[ra] == uf_rank[rb]:
            uf_rank[ra] += 1

    # Per-root tree cells: pad position + all path cells in this
    # component's sub-tree.
    comp_trees: dict[int, set[tuple[int, int]]] = {
        i: {(pads[i].gx, pads[i].gy)} for i in range(len(pads))
    }

    def _get_comp_tree(pad_idx: int) -> set[tuple[int, int]]:
        """Return the tree-cell set for the component containing pad_idx."""
        return comp_trees[_uf_find(pad_idx)]

    def _merge_comps(a: int, b: int, path_cells: list[tuple[int, int]]) -> None:
        """Union components of pads a and b, merging their tree cells."""
        ra, rb = _uf_find(a), _uf_find(b)
        if ra == rb:
            # Same component — just add path cells
            comp_trees[ra].update(path_cells)
            return
        tree_a = comp_trees.pop(ra)
        tree_b = comp_trees.pop(rb)
        _uf_union(a, b)
        new_root = _uf_find(a)
        # Merge into the larger set for efficiency
        if len(tree_a) >= len(tree_b):
            tree_a.update(tree_b)
            tree_a.update(path_cells)
            comp_trees[new_root] = tree_a
        else:
            tree_b.update(tree_a)
            tree_b.update(path_cells)
            comp_trees[new_root] = tree_b

    log.debug("  [MP] %-20s multi-pin (%d pads, %d MST edges)",
              net_id, len(pads), len(mst_edges))

    for edge_idx, (pa, pb) in enumerate(mst_edges):
        if _uf_find(pa) == _uf_find(pb):
            continue  # already connected

        # Use the larger tree as the A* target so the pathfinder can
        # connect to the nearest backbone cell.  The smaller tree
        # supplies multi-source starting points, preventing parallel
        # duplicate traces.
        tree_pa = _get_comp_tree(pa)
        tree_pb = _get_comp_tree(pb)
        if len(tree_pa) >= len(tree_pb):
            src_tree = tree_pb
            target_tree = tree_pa
            src_idx, tgt_idx = pb, pa
        else:
            src_tree = tree_pa
            target_tree = tree_pb
            src_idx, tgt_idx = pa, pb

        # Free target tree cells (may be blocked by other nets' clearance)
        freed: list[tuple[int, int]] = []
        for cell in target_tree:
            if grid.is_blocked(*cell) and not grid.is_permanently_blocked(*cell):
                grid.free_cell(*cell)
                freed.append(cell)

        # Free source tree cells too
        freed_src: list[tuple[int, int]] = []
        for cell in src_tree:
            if grid.is_blocked(*cell) and not grid.is_permanently_blocked(*cell):
                grid.free_cell(*cell)
                freed_src.append(cell)

        # Free pad neighbourhoods for all pads in BOTH components
        src_root = _uf_find(src_idx)
        tgt_root = _uf_find(tgt_idx)
        for pidx in range(len(pads)):
            proot = _uf_find(pidx)
            if proot == src_root:
                freed_src.extend(_free_pad_neighborhood(grid, pads[pidx].gx, pads[pidx].gy, pad_radius))
            elif proot == tgt_root:
                freed.extend(_free_pad_neighborhood(grid, pads[pidx].gx, pads[pidx].gy, pad_radius))

        # Block foreign pins AFTER pad freeing
        fp_blocked: list[tuple[int, int]] = []
        if all_pin_cells is not None:
            fp_blocked = _block_foreign_pins(
                grid, all_pin_cells, pads, foreign_pin_radius,
            )

        # Multi-source A*: search from all source tree cells to any
        # cell in the target tree (finds shortest bridge between the
        # two sub-trees).
        path = find_path_to_tree(grid, src_tree, target_tree,
                                 turn_penalty=turn_penalty)

        # Fallback: allow traversal of temporarily-blocked cells
        # (foreign-pin zones) with a heavy penalty.  Produces a path
        # that may violate pin clearance but keeps the net connected.
        if path is None:
            path = find_path_to_tree(grid, src_tree, target_tree,
                                     turn_penalty=turn_penalty,
                                     allow_crossings=True)
            if path is not None:
                log.debug("  [MP] %-20s edge %d: routed via "
                          "blocked-cell fallback", net_id, edge_idx)

        _unblock_foreign_pins(grid, fp_blocked)
        _restore_cells(grid, freed)
        _restore_cells(grid, freed_src)

        if path is not None:
            all_paths.append(path)
            _merge_comps(pa, pb, path)
            log.debug("  [MP] %-20s edge %d: src_tree=%d → tgt_tree=%d  OK "
                      "(path_len=%d, merged_tree=%d)",
                      net_id, edge_idx,
                      len(src_tree), len(target_tree),
                      len(path), len(_get_comp_tree(pa)))
        else:
            src_diag = _pad_cell_diagnostic(grid, pads[src_idx].gx, pads[src_idx].gy, pad_radius)
            tgt_diags = []
            for pidx in range(len(pads)):
                if _uf_find(pidx) == tgt_root:
                    td = _pad_cell_diagnostic(grid, pads[pidx].gx, pads[pidx].gy, pad_radius)
                    tgt_diags.append(f"{pads[pidx].instance_id}:{pads[pidx].pin_id}[{td}]")
            log.info("  [MP] %-20s edge %d: src=%s:%s @(%d,%d) [%s]  src_tree=%d  "
                     "→ tgt_tree=%d cells  NO PATH  targets: %s",
                     net_id, edge_idx,
                     pads[src_idx].instance_id, pads[src_idx].pin_id,
                     pads[src_idx].gx, pads[src_idx].gy, src_diag,
                     len(src_tree),
                     len(target_tree),
                     '; '.join(tgt_diags) if tgt_diags else 'none')

    # Check if all pads are connected (single component)
    roots = {_uf_find(i) for i in range(len(pads))}
    if len(roots) == 1:
        return (all_paths, True)

    # Some pads couldn't be reached — try greedy fallback for remaining
    # disconnected components against the largest component's tree.
    # Uses multi-source A* from the full disconnected sub-tree.
    main_root = max(roots, key=lambda r: len(comp_trees[r]))
    main_tree = comp_trees[main_root]

    remaining_roots = [r for r in roots if r != main_root]
    for rr in remaining_roots:
        comp_pads = [i for i in range(len(pads)) if _uf_find(i) == rr]
        comp_tree = comp_trees[rr]

        # Free main tree cells
        freed: list[tuple[int, int]] = []
        for cell in main_tree:
            if grid.is_blocked(*cell) and not grid.is_permanently_blocked(*cell):
                grid.free_cell(*cell)
                freed.append(cell)

        # Free source component's tree cells
        freed_src: list[tuple[int, int]] = []
        for cell in comp_tree:
            if grid.is_blocked(*cell) and not grid.is_permanently_blocked(*cell):
                grid.free_cell(*cell)
                freed_src.append(cell)

        # Free pad neighbourhoods for both components
        for pidx in range(len(pads)):
            proot = _uf_find(pidx)
            if proot == rr:
                freed_src.extend(_free_pad_neighborhood(grid, pads[pidx].gx, pads[pidx].gy, pad_radius))
            elif proot == main_root:
                freed.extend(_free_pad_neighborhood(grid, pads[pidx].gx, pads[pidx].gy, pad_radius))

        fp_blocked: list[tuple[int, int]] = []
        if all_pin_cells is not None:
            fp_blocked = _block_foreign_pins(
                grid, all_pin_cells, pads, foreign_pin_radius,
            )

        # Multi-source: route from comp_tree to main_tree
        path = find_path_to_tree(grid, comp_tree, main_tree,
                                 turn_penalty=turn_penalty)

        if path is None:
            path = find_path_to_tree(grid, comp_tree, main_tree,
                                     turn_penalty=turn_penalty,
                                     allow_crossings=True)
            if path is not None:
                log.debug("  [MP] %-20s fallback: routed via "
                          "blocked-cell fallback", net_id)

        _unblock_foreign_pins(grid, fp_blocked)
        _restore_cells(grid, freed)
        _restore_cells(grid, freed_src)

        if path is not None:
            all_paths.append(path)
            merge_pidx = comp_pads[0]
            _merge_comps(merge_pidx, main_root, path)
            main_root = _uf_find(merge_pidx)
            main_tree = comp_trees[main_root]
            log.debug("  [MP] %-20s fallback: comp_tree=%d → main_tree=%d  "
                      "OK (len=%d)",
                      net_id, len(comp_tree), len(main_tree), len(path))
        else:
            pad_diags = []
            for p in comp_pads:
                pd = _pad_cell_diagnostic(grid, pads[p].gx, pads[p].gy, pad_radius)
                pad_diags.append(f"{pads[p].instance_id}:{pads[p].pin_id}@({pads[p].gx},{pads[p].gy})[{pd}]")
            tree_sample = list(main_tree)[:5]
            log.info("  [MP] %-20s fallback FAIL: unreachable comp (tree=%d): %s  "
                     "main_tree=%d sample=%s",
                     net_id, len(comp_tree),
                     '; '.join(pad_diags),
                     len(main_tree), tree_sample)
            return (all_paths, False)

    return (all_paths, True)


# ── Elite pool, phase rotation, stall detection, DRC ──────────────


@dataclass
class _EliteEntry:
    """A stored routing solution in the elite pool."""

    score: int
    grid_snap: bytearray
    routed_paths: dict[str, list[list[tuple[int, int]]]]
    assignments: dict[str, str]
    failed_nets: list[str]
    ordering: list[str]


def _update_elites(
    elites: list[_EliteEntry],
    score: int,
    grid: RoutingGrid,
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    assignments: dict[str, str],
    failed_nets: set[str],
    ordering: list[str],
    pool_size: int,
) -> None:
    """Add a solution to the elite pool if it qualifies."""
    entry = _EliteEntry(
        score=score,
        grid_snap=grid.snapshot(),
        routed_paths={nid: [list(p) for p in paths]
                      for nid, paths in routed_paths.items()},
        assignments=dict(assignments),
        failed_nets=list(failed_nets),
        ordering=list(ordering),
    )
    elites.append(entry)
    elites.sort(key=lambda e: -e.score)
    while len(elites) > pool_size:
        elites.pop()


def _pick_phase(
    attempt: int,
    stall_count: int,
    elites: list[_EliteEntry],
    stall_limit: int,
) -> str:
    """Choose routing strategy for this attempt."""
    if attempt == 0:
        return 'restart'
    if stall_count >= stall_limit and elites:
        return 'explore'
    cycle = attempt % 4
    if cycle == 1 and elites:
        return 'refine'
    if cycle == 2 and len(elites) >= 2:
        return 'crossover'
    if cycle == 3 and elites:
        return 'explore'
    return 'restart'


def _sync_pools_with_assignments(
    pools: dict[str, PinPool],
    assignments: dict[str, str],
) -> None:
    """Remove already-assigned pins from pools to prevent double-allocation."""
    for value in assignments.values():
        parts = value.split(":", 1)
        if len(parts) != 2:
            continue
        iid, pin_id = parts
        pool = pools.get(iid)
        if pool is None:
            continue
        for group_pins in pool.pools.values():
            try:
                group_pins.remove(pin_id)
            except ValueError:
                pass


def _clear_failed_assignments(
    assignments: dict[str, str],
    failed_nets: set[str],
) -> None:
    """Clear pin assignments for failed nets to allow re-allocation."""
    to_remove = [k for k in assignments if k.split("|", 1)[0] in failed_nets]
    for k in to_remove:
        del assignments[k]


def _find_clearance_violations(
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    clearance_cells: int,
) -> set[str]:
    """Find nets whose traces violate clearance distance with another net."""
    cell_net: dict[tuple[int, int], str] = {}
    for net_id, paths in routed_paths.items():
        for path in paths:
            for cell in path:
                cell_net[cell] = net_id

    violators: set[str] = set()
    for (gx, gy), net_id in cell_net.items():
        if net_id in violators:
            continue
        for dx in range(-clearance_cells + 1, clearance_cells):
            found = False
            for dy in range(-clearance_cells + 1, clearance_cells):
                if dx == 0 and dy == 0:
                    continue
                other = cell_net.get((gx + dx, gy + dy))
                if other is not None and other != net_id:
                    violators.add(net_id)
                    violators.add(other)
                    found = True
                    break
            if found:
                break
    return violators


def _drc_repair(
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    grid: RoutingGrid,
    net_pad_map: dict[str, list[_PinRef]],
    placement: FullPlacement,
    catalog: CatalogResult,
    pin_pools: dict[str, PinPool],
    assignments: dict[str, str],
    all_pin_cells: dict[str, set[tuple[int, int]]],
    config: RouterConfig,
    pad_radius: int,
) -> set[str]:
    """Post-routing DRC repair: fix trace clearance violations.

    Iteratively rips up the longest violating trace, adds cost
    penalties near its old path, and re-routes.  Returns net IDs
    that failed to re-route.
    """
    clearance_cells = max(1, math.ceil(
        (config.trace_width_mm / 2 + config.trace_clearance_mm)
        / config.grid_resolution_mm
    ))
    foreign_pin_radius = _compute_foreign_pin_radius(config)
    repair_failed: set[str] = set()

    for round_idx in range(config.drc_repair_rounds):
        violations = _find_clearance_violations(routed_paths, clearance_cells)
        if not violations:
            break

        victim = max(
            violations,
            key=lambda nid: sum(len(p) for p in routed_paths.get(nid, [])),
        )
        log.info("DRC repair round %d: ripping %s (%d violation nets)",
                 round_idx + 1, victim, len(violations))

        # Add cost around the victim's current path to encourage detour
        for path in routed_paths[victim]:
            for gx, gy in path:
                idx = gy * grid.width + gx
                grid._cost_map[idx] = grid._cost_map.get(idx, 0) + 20

        # Rip up the victim
        for path in routed_paths[victim]:
            grid.free_trace(path)
        del routed_paths[victim]

        # Allow pin re-allocation
        _clear_failed_assignments(assignments, {victim})
        repair_pools = _copy_pools(pin_pools)
        _sync_pools_with_assignments(repair_pools, assignments)

        refs = net_pad_map[victim]
        pads = _resolve_pads(
            refs, victim, placement, catalog,
            repair_pools, grid, assignments,
        )
        if pads is None or len(pads) < 2:
            repair_failed.add(victim)
            continue

        paths, ok = _route_single_net(
            victim, pads, grid, pad_radius, config.turn_penalty,
            all_pin_cells=all_pin_cells, foreign_pin_radius=foreign_pin_radius,
        )
        if ok and paths:
            routed_paths[victim] = paths
            for p in paths:
                grid.block_trace(p)
            log.info("DRC repair round %d: %s re-routed OK",
                     round_idx + 1, victim)
        else:
            repair_failed.add(victim)
            log.warning("DRC repair round %d: %s FAILED to re-route",
                        round_idx + 1, victim)

    return repair_failed


# ── Routing orchestrator with rip-up ──────────────────────────────


def _route_with_ripup(
    net_pad_map: dict[str, list[_PinRef]],
    base_grid: RoutingGrid,
    placement: FullPlacement,
    catalog: CatalogResult,
    pin_pools: dict[str, PinPool],
    outline_poly: Polygon,
    config: RouterConfig,
    pad_radius: int,
) -> RoutingResult:
    """Route all nets with rip-up and reroute on failure.

    Tries multiple random net orderings.  For each ordering:
      Phase 1: route all nets in order, skip failures.
      Phase 2: rip-up and reroute failed nets.
    Returns the best result found.
    """
    net_ids = [n.id for n in placement.nets if len(net_pad_map.get(n.id, [])) >= 2]
    skipped_nets = [n.id for n in placement.nets if len(net_pad_map.get(n.id, [])) < 2]
    if skipped_nets:
        log.info("Router: skipping %d nets with <2 pins: %s", len(skipped_nets), skipped_nets)

    if not net_ids:
        log.info("Router: no nets to route")
        return RoutingResult(traces=[], pin_assignments={}, failed_nets=[])

    log.info("Router: routing %d nets", len(net_ids))
    start_time = time.monotonic()
    best_traces: list[Trace] = []
    best_assignments: dict[str, str] = {}
    best_failed: list[str] = list(net_ids)

    def _time_left() -> bool:
        return (time.monotonic() - start_time) < config.time_budget_s

    # Sort nets: multi-pin power nets first (they need the most
    # routing resources), then signal nets shortest-first.
    def net_priority(nid: str) -> tuple[int, int]:
        refs = net_pad_map.get(nid, [])
        is_power = nid in ("VCC", "GND", "VBAT")
        # Power nets first (0), then signals (1).
        # Within each group, more pins first (negative for descending).
        return (0 if is_power else 1, -len(refs))

    base_order = sorted(net_ids, key=net_priority)

    # ── Precompute per-attempt invariants ────────────────────
    # These depend only on placement, catalog, and the base grid's
    # coordinate mapping, which stay constant across attempts.
    all_pin_cells = _build_all_pin_cells(placement, catalog, base_grid)
    foreign_pin_radius = _compute_foreign_pin_radius(config)

    # ── Prefix-based search-space pruning ───────────────────────
    # After each failed attempt we record the Phase-1 ordering prefix
    # (the ordered subsequence of nets that were successfully routed).
    # Any future ordering that starts with the same prefix will produce
    # an identical grid state, guaranteeing the same nets fail again.
    dead_prefixes: list[tuple[str, ...]] = []
    pruned_count = 0  # how many orderings were skipped by pruning

    # ── Elite pool & stall detection ────────────────────────────
    elites: list[_EliteEntry] = []
    best_score = 0
    stall_count = 0

    def _starts_with_dead_prefix(ordering: list[str]) -> bool:
        """Return True if *ordering* starts with any known-dead prefix."""
        t = tuple(ordering)
        for pfx in dead_prefixes:
            if len(pfx) <= len(t) and t[: len(pfx)] == pfx:
                return True
        return False

    for attempt in range(config.max_rip_up_attempts):
        if not _time_left():
            log.info("Router: time budget exhausted after %d attempts "
                     "(%d pruned)", attempt, pruned_count)
            break

        phase = _pick_phase(attempt, stall_count, elites, config.stall_limit)
        log.debug("Router attempt %d: phase=%s stall=%d elites=%d",
                  attempt + 1, phase, stall_count, len(elites))

        # ── Initialize grid state based on phase ─────────────────
        routed_paths: dict[str, list[list[tuple[int, int]]]] = {}
        failed_set: set[str] = set()
        skip_phase1 = False

        if phase in ('refine', 'crossover'):
            # Restore from an elite solution
            if phase == 'refine':
                donor = elites[0]  # best
            else:
                donor = random.choice(elites[1:]) if len(elites) > 1 else elites[0]

            grid = base_grid.clone()
            grid.restore(donor.grid_snap)
            routed_paths = {nid: [list(p) for p in paths]
                           for nid, paths in donor.routed_paths.items()}
            attempt_assignments = dict(donor.assignments)
            attempt_pools = _copy_pools(pin_pools)
            _sync_pools_with_assignments(attempt_pools, attempt_assignments)
            failed_set = set(donor.failed_nets)
            order = list(donor.ordering)

            if phase == 'refine':
                # Rip up worst nets (longest traces) to create room
                candidates = sorted(
                    routed_paths,
                    key=lambda nid: sum(len(p) for p in routed_paths[nid]),
                    reverse=True,
                )
                n_rip = min(3, max(1, len(candidates) // 3))
                for nid in candidates[:n_rip]:
                    for path in routed_paths[nid]:
                        grid.free_trace(path)
                    del routed_paths[nid]
                    failed_set.add(nid)

            # Pin re-allocation for failed nets
            _clear_failed_assignments(attempt_assignments, failed_set)
            skip_phase1 = True

        else:
            # Fresh start (restart / explore)
            if phase == 'explore' and elites:
                order = list(elites[0].ordering)
                n_swaps = random.randint(2, min(4, max(2, len(order) // 2)))
                for _ in range(n_swaps):
                    i = random.randint(0, len(order) - 2)
                    order[i], order[i + 1] = order[i + 1], order[i]
            elif attempt == 0:
                order = list(base_order)
            else:
                order = list(base_order)
                random.shuffle(order)
                # Re-shuffle if the ordering starts with a dead prefix
                for _reshuffle in range(100):
                    if not _starts_with_dead_prefix(order):
                        break
                    random.shuffle(order)
                    pruned_count += 1
                else:
                    # Exhausted reshuffles — all orderings appear pruned.
                    # The search space is saturated; stop early.
                    log.info("Router: search space exhausted after %d attempts "
                             "(%d pruned, %d dead prefixes)",
                             attempt, pruned_count, len(dead_prefixes))
                    break

            # Fresh pin pools for this attempt (deep copy)
            attempt_pools = _copy_pools(pin_pools)
            attempt_assignments: dict[str, str] = {}

            # Fresh grid (restore to base state)
            grid = base_grid.clone()

        # ── Phase 1: Route all nets in order ───────────────────────
        if not skip_phase1:
            for nid in order:
                refs = net_pad_map[nid]
                pads = _resolve_pads(
                    refs, nid, placement, catalog,
                    attempt_pools, grid, attempt_assignments,
                )
                if pads is None or len(pads) < 2:
                    log.debug("  [P1] %-20s FAIL — pad resolution failed", nid)
                    failed_set.add(nid)
                    continue

                log.debug("  [P1] %-20s routing %d pads: %s", nid, len(pads),
                          ", ".join(f"{p.instance_id}:{p.pin_id}@({p.world_x:.1f},{p.world_y:.1f})" for p in pads))

                paths, ok = _route_single_net(
                    nid, pads, grid, pad_radius, config.turn_penalty,
                    all_pin_cells=all_pin_cells, foreign_pin_radius=foreign_pin_radius,
                )
                if ok and paths:
                    total_cells = sum(len(p) for p in paths)
                    routed_paths[nid] = paths
                    # Block trace cells
                    for path in paths:
                        grid.block_trace(path)
                    log.debug("  [P1] %-20s OK — %d segments, %d cells", nid, len(paths), total_cells)
                else:
                    failed_set.add(nid)
                    stats = _grid_stats(grid)
                    log.info("  [P1] %-20s FAIL — no route (grid %.1f%% free, "
                             "%d trace cells, %d blocked)",
                             nid, stats['free_pct'],
                             stats['trace_path'], stats['blocked'])

        phase1_stats = _grid_stats(grid)
        log.info("Router attempt %d: %d/%d nets routed (%s), "
                 "grid %.1f%% free",
                 attempt + 1, len(order) - len(failed_set), len(order),
                 phase, phase1_stats['free_pct'])
        if failed_set:
            log.info("  Phase 1 failed nets: %s", sorted(failed_set))

        if not failed_set:
            # All routed on first pass — validate no crossings
            stripped = _strip_crossing_traces(routed_paths, grid, config)
            if stripped:
                log.warning("Phase 1 crossing validation stripped %d nets", len(stripped))
                failed_set.update(stripped)
            else:
                traces = _grid_paths_to_traces(routed_paths, grid)
                return RoutingResult(
                    traces=traces,
                    pin_assignments=attempt_assignments,
                    failed_nets=[],
                )

        # ── Phase 2: Inner rip-up loop ─────────────────────────────
        # Dynamic limit: try 3× harder when stalled
        effective_inner = config.inner_rip_up_limit * (3 if stall_count >= config.stall_limit else 1)
        for inner_iter in range(effective_inner):
            if not failed_set or not _time_left():
                break

            progress = False
            failed_list = list(failed_set)
            random.shuffle(failed_list)

            for failed_net in failed_list:
                if failed_net not in failed_set:
                    continue

                refs = net_pad_map[failed_net]
                pads = _resolve_pads(
                    refs, failed_net, placement, catalog,
                    attempt_pools, grid, attempt_assignments,
                )
                if pads is None or len(pads) < 2:
                    continue

                # Try simple route first (foreign pins handled internally)
                paths, ok = _route_single_net(
                    failed_net, pads, grid, pad_radius, config.turn_penalty,
                    all_pin_cells=all_pin_cells, foreign_pin_radius=foreign_pin_radius,
                )
                if ok and paths:
                    routed_paths[failed_net] = paths
                    for path in paths:
                        grid.block_trace(path)
                    failed_set.discard(failed_net)
                    log.debug("  [P2] %-20s OK — simple re-route succeeded", failed_net)
                    progress = True
                    continue

                # Try crossing-aware route
                tree_cells: set[tuple[int, int]] = {(pads[0].gx, pads[0].gy)}
                connected: set[int] = {0}
                crossing_paths: list[list[tuple[int, int]]] = []
                crossed_cells: set[tuple[int, int]] = set()
                route_ok = True

                # Sort remaining pads by distance to tree for efficient
                # tree growth (nearest-first).
                remaining = list(range(1, len(pads)))

                while remaining:
                    # Pick the unconnected pad closest to the current tree
                    best_idx = -1
                    best_dist = float('inf')
                    for ri, pad_idx in enumerate(remaining):
                        px, py = pads[pad_idx].gx, pads[pad_idx].gy
                        for tx, ty in tree_cells:
                            d = abs(px - tx) + abs(py - ty)
                            if d < best_dist:
                                best_dist = d
                                best_idx = ri
                                if d == 0:
                                    break
                        if best_dist == 0:
                            break
                    pad_idx = remaining.pop(best_idx)

                    # Free tree cells
                    freed: list[tuple[int, int]] = []
                    for cell in tree_cells:
                        if grid.is_blocked(*cell) and not grid.is_permanently_blocked(*cell):
                            grid.free_cell(*cell)
                            freed.append(cell)

                    src = (pads[pad_idx].gx, pads[pad_idx].gy)
                    freed_src = _free_pad_neighborhood(grid, *src, pad_radius)

                    # Block foreign pins AFTER pad freeing
                    fp_blocked = _block_foreign_pins(
                        grid, all_pin_cells, pads, foreign_pin_radius,
                    )

                    path = find_path_to_tree(
                        grid, src, tree_cells,
                        turn_penalty=config.turn_penalty,
                        allow_crossings=True,
                    )

                    # Restore: unblock foreign pins first
                    _unblock_foreign_pins(grid, fp_blocked)
                    _restore_cells(grid, freed)
                    _restore_cells(grid, freed_src)

                    if path is None:
                        route_ok = False
                        break

                    for cell in path:
                        tree_cells.add(cell)
                        if grid.is_blocked(*cell) and not grid.is_permanently_blocked(*cell):
                            crossed_cells.add(cell)
                    crossing_paths.append(path)

                if not route_ok or not crossed_cells:
                    if not route_ok:
                        log.debug("  [P2] %-20s FAIL — crossing-aware pathfinder also failed", failed_net)
                    else:
                        log.debug("  [P2] %-20s SKIP — crossing path found no actual crossings", failed_net)
                    continue

                # Find which nets were crossed
                ripped_nets: set[str] = set()
                for nid, npaths in routed_paths.items():
                    if nid == failed_net:
                        continue
                    for npath in npaths:
                        for cell in npath:
                            if cell in crossed_cells:
                                ripped_nets.add(nid)
                                break
                        if nid in ripped_nets:
                            break

                if not ripped_nets:
                    continue

                log.debug("  [P2] %-20s rip-up: crosses %d nets (%s)",
                          failed_net, len(ripped_nets), sorted(ripped_nets))

                # Snapshot the grid so we can roll back if the ripped nets
                # fail to re-route (we must never leave crossings in place).
                snap_before_rip = grid.snapshot()
                saved_routed = {nid: list(ps) for nid, ps in routed_paths.items()}

                # Rip up crossed nets
                for ripped in ripped_nets:
                    if ripped in routed_paths:
                        for rpath in routed_paths[ripped]:
                            grid.free_trace(rpath)
                        del routed_paths[ripped]

                # Place the crossing net
                routed_paths[failed_net] = crossing_paths
                for cpath in crossing_paths:
                    grid.block_trace(cpath)

                # Try to re-route ALL ripped nets — must succeed for every
                # one, otherwise we roll back and leave the failed_net unrouted.
                rerouted: dict[str, list[list[tuple[int, int]]]] = {}
                all_rerouted = True
                for ripped in ripped_nets:
                    rrefs = net_pad_map[ripped]
                    rpads = _resolve_pads(
                        rrefs, ripped, placement, catalog,
                        attempt_pools, grid, attempt_assignments,
                    )
                    if rpads is None or len(rpads) < 2:
                        all_rerouted = False
                        break
                    rpaths, rok = _route_single_net(
                        ripped, rpads, grid, pad_radius, config.turn_penalty,
                        all_pin_cells=all_pin_cells, foreign_pin_radius=foreign_pin_radius,
                    )
                    if rok and rpaths:
                        rerouted[ripped] = rpaths
                        for rp in rpaths:
                            grid.block_trace(rp)
                    else:
                        all_rerouted = False
                        break

                if all_rerouted:
                    # Commit: update routed_paths, update failed_set
                    for ripped, rpaths in rerouted.items():
                        routed_paths[ripped] = rpaths
                    failed_set.discard(failed_net)
                    # ripped nets are now routed, remove from failed
                    for ripped in ripped_nets:
                        failed_set.discard(ripped)
                    log.debug("  [P2] %-20s COMMIT — rip-up succeeded, all %d ripped nets re-routed",
                              failed_net, len(ripped_nets))
                    progress = True
                    break  # restart inner loop
                else:
                    # Roll back — restore grid and routed_paths
                    log.debug("  [P2] %-20s ROLLBACK — ripped nets failed to re-route", failed_net)
                    grid.restore(snap_before_rip)
                    routed_paths.clear()
                    routed_paths.update(saved_routed)
                    # Restore ripped nets to failed_set only if they
                    # were not there before (they were routed before rip)
                    for ripped in ripped_nets:
                        if ripped not in routed_paths:
                            failed_set.add(ripped)
                    # failed_net stays in failed_set
                    # Don't count as progress — try next failed net
                    continue

            if not progress:
                break

        # Final crossing validation — strip any nets that still cross
        stripped = _strip_crossing_traces(routed_paths, grid, config)
        if stripped:
            log.warning("Attempt %d: crossing validation stripped %d nets: %s",
                        attempt + 1, len(stripped), stripped)
            failed_set.update(stripped)

        # Check if this attempt is best so far
        score = len(net_ids) - len(failed_set)
        if len(failed_set) < len(best_failed):
            best_traces = _grid_paths_to_traces(routed_paths, grid)
            best_assignments = dict(attempt_assignments)
            best_failed = list(failed_set)

        # ── Update elite pool ──────────────────────────────────
        _update_elites(
            elites, score, grid, routed_paths,
            attempt_assignments, failed_set, order,
            config.elite_pool_size,
        )

        # ── Stall detection ────────────────────────────────────
        if score > best_score:
            best_score = score
            stall_count = 0
        else:
            stall_count += 1

        if not failed_set:
            log.info("Router: all nets routed on attempt %d "
                     "(%d pruned)", attempt + 1, pruned_count)
            return RoutingResult(
                traces=best_traces,
                pin_assignments=best_assignments,
                failed_nets=[],
            )

        # ── Record dead prefix (restart/explore only) ──────────
        if phase in ('restart', 'explore'):
            routed_prefix = tuple(nid for nid in order if nid not in failed_set)
            if len(routed_prefix) >= 1:
                already_covered = False
                for pfx in dead_prefixes:
                    if (len(pfx) <= len(routed_prefix)
                            and routed_prefix[: len(pfx)] == pfx):
                        already_covered = True
                        break
                if not already_covered:
                    dead_prefixes.append(routed_prefix)
                    log.debug("Router: recorded dead prefix len=%d: %s",
                              len(routed_prefix), routed_prefix)

    # ── DRC repair on best result ─────────────────────────────────
    if elites and _time_left():
        best_elite = elites[0]
        drc_grid = base_grid.clone()
        drc_grid.restore(best_elite.grid_snap)
        drc_paths = {nid: [list(p) for p in paths]
                     for nid, paths in best_elite.routed_paths.items()}
        drc_assignments = dict(best_elite.assignments)

        drc_failed = _drc_repair(
            drc_paths, drc_grid, net_pad_map,
            placement, catalog, pin_pools, drc_assignments,
            all_pin_cells, config, pad_radius,
        )
        # Accept the DRC result if it didn't make things worse
        total_failed_after = set(best_elite.failed_nets) | drc_failed
        if len(total_failed_after) <= len(best_failed):
            best_traces = _grid_paths_to_traces(drc_paths, drc_grid)
            best_assignments = drc_assignments
            best_failed = list(total_failed_after)

    elapsed = time.monotonic() - start_time
    log.info("Router: finished in %.1fs with %d/%d nets routed, %d failed",
             elapsed, len(net_ids) - len(best_failed), len(net_ids), len(best_failed))
    if best_failed:
        log.warning("Router: FAILED nets: %s", best_failed)
        log.warning("Router: %d attempts total, %d pruned, %d dead prefixes",
                    min(attempt + 1, config.max_rip_up_attempts),
                    pruned_count, len(dead_prefixes))
        for fnid in best_failed:
            refs = net_pad_map.get(fnid, [])
            pin_desc = ", ".join(r.raw for r in refs)
            log.warning("  %s (%d pins): %s", fnid, len(refs), pin_desc)
            # Resolve pads on the base grid to show pad positions/states
            diag_pools = _copy_pools(pin_pools)
            diag_assigns: dict[str, str] = {}
            diag_pads = _resolve_pads(
                refs, fnid, placement, catalog,
                diag_pools, base_grid, diag_assigns,
            )
            if diag_pads:
                for dp in diag_pads:
                    pd = _pad_cell_diagnostic(base_grid, dp.gx, dp.gy, pad_radius)
                    log.warning("    pad %s:%s  world=(%.1f,%.1f)  grid=(%d,%d)  [%s]",
                                dp.instance_id, dp.pin_id,
                                dp.world_x, dp.world_y,
                                dp.gx, dp.gy, pd)
    return RoutingResult(
        traces=best_traces,
        pin_assignments=best_assignments,
        failed_nets=best_failed,
    )


# ── Helpers ────────────────────────────────────────────────────────


def _grid_stats(grid: RoutingGrid) -> dict[str, int | float]:
    """Count cell states in the grid for diagnostic logging."""
    total = grid.width * grid.height
    cells = grid._cells
    free = cells.count(FREE)
    blocked = cells.count(BLOCKED)
    perm = cells.count(PERMANENTLY_BLOCKED)
    trace = cells.count(TRACE_PATH)
    return {
        'total': total,
        'free': free,
        'free_pct': (free / total * 100) if total else 0.0,
        'blocked': blocked,
        'perm_blocked': perm,
        'trace_path': trace,
        'protected': len(grid._protected),
    }


def _grid_paths_to_traces(
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    grid: RoutingGrid,
) -> list[Trace]:
    """Convert grid-coordinate paths to world-coordinate Traces.

    Also simplifies paths: removes intermediate collinear points
    (keeps only waypoints where direction changes).

    Any waypoint that falls outside the outline polygon is snapped to
    the nearest point on the outline boundary.
    """
    outline = grid.outline_poly
    traces: list[Trace] = []
    for net_id, paths in routed_paths.items():
        for grid_path in paths:
            if len(grid_path) < 2:
                continue
            world_path = _simplify_path(grid_path, grid)
            # Clamp waypoints to outline
            clamped: list[tuple[float, float]] = []
            for wx, wy in world_path:
                pt = Point(wx, wy)
                if not outline.contains(pt):
                    nearest = outline.exterior.interpolate(
                        outline.exterior.project(pt)
                    )
                    clamped.append((nearest.x, nearest.y))
                else:
                    clamped.append((wx, wy))
            traces.append(Trace(net_id=net_id, path=clamped))
    return traces


def _simplify_path(
    grid_path: list[tuple[int, int]],
    grid: RoutingGrid,
) -> list[tuple[float, float]]:
    """Remove collinear intermediate points and convert to world coords.

    Keeps the start, end, and every point where the direction changes.
    """
    if len(grid_path) <= 2:
        return [grid.grid_to_world(gx, gy) for gx, gy in grid_path]

    waypoints: list[tuple[int, int]] = [grid_path[0]]

    for i in range(1, len(grid_path) - 1):
        prev = grid_path[i - 1]
        curr = grid_path[i]
        nxt = grid_path[i + 1]
        # Direction from prev to curr
        d1 = (curr[0] - prev[0], curr[1] - prev[1])
        # Direction from curr to next
        d2 = (nxt[0] - curr[0], nxt[1] - curr[1])
        if d1 != d2:
            waypoints.append(curr)

    waypoints.append(grid_path[-1])

    return [grid.grid_to_world(gx, gy) for gx, gy in waypoints]


def _copy_pools(pools: dict[str, PinPool]) -> dict[str, PinPool]:
    """Deep-copy pin pools for a fresh routing attempt."""
    return {
        iid: PinPool(
            instance_id=pool.instance_id,
            pools={gid: list(pins) for gid, pins in pool.pools.items()},
        )
        for iid, pool in pools.items()
    }


# ── Post-routing crossing validation ──────────────────────────────


def _find_crossing_nets(
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    clearance_cells: int,
    grid: RoutingGrid | None = None,
) -> list[str]:
    """Identify nets whose trace cells physically overlap another net.

    A crossing occurs when two different nets occupy the **same** grid
    cell.  Clearance-zone overlap near protected pin pads is acceptable
    (handled by the block_trace / protect_cell mechanism) and is NOT
    flagged here.

    Returns a list of net IDs involved in crossings.
    """
    # Build a map: grid cell -> first net ID that occupies it
    cell_owner: dict[tuple[int, int], str] = {}
    crossing_nets: set[str] = set()
    # Collect crossing details for diagnostics
    crossing_details: list[tuple[tuple[int, int], str, str]] = []

    for net_id, paths in routed_paths.items():
        for path in paths:
            for cell in path:
                existing = cell_owner.get(cell)
                if existing is not None and existing != net_id:
                    # Two different nets share the same physical cell
                    crossing_nets.add(net_id)
                    crossing_nets.add(existing)
                    crossing_details.append((cell, existing, net_id))
                else:
                    cell_owner[cell] = net_id

    # Log crossing diagnostics
    if crossing_details and grid is not None:
        state_names = {FREE: 'FREE', BLOCKED: 'BLOCKED',
                       PERMANENTLY_BLOCKED: 'PERM_BLOCKED',
                       TRACE_PATH: 'TRACE_PATH'}
        logged: set[tuple[int, int]] = set()
        for cell, net_a, net_b in crossing_details:
            if cell in logged:
                continue
            logged.add(cell)
            gx, gy = cell
            wx, wy = grid.grid_to_world(gx, gy)
            v = grid._cells[gy * grid.width + gx] if grid.in_bounds(gx, gy) else -1
            prot = cell in grid._protected
            log.warning(
                "  CROSSING: cell (%d,%d) world(%.1f,%.1f) state=%s "
                "protected=%s  nets: %s vs %s",
                gx, gy, wx, wy, state_names.get(v, f'?{v}'),
                prot, net_a, net_b,
            )
    elif crossing_details:
        for cell, net_a, net_b in crossing_details[:5]:
            log.warning("  CROSSING: cell %s  nets: %s vs %s",
                        cell, net_a, net_b)

    return list(crossing_nets)


def _strip_crossing_traces(
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    grid: RoutingGrid,
    config: RouterConfig,
) -> list[str]:
    """Remove traces that cross other nets, returning them to failed.

    Iteratively finds crossing nets and removes them until no crossings
    remain.  Removes the net with the longest total trace length in each
    iteration to preserve shorter (harder-to-reroute) nets.

    Returns the list of net IDs that were removed.
    """
    clearance_cells = max(1, math.ceil(
        (config.trace_width_mm / 2 + config.trace_clearance_mm) / config.grid_resolution_mm
    ))

    removed: list[str] = []
    max_iters = len(routed_paths) + 1  # safety bound

    for _ in range(max_iters):
        crossing = _find_crossing_nets(routed_paths, clearance_cells, grid)
        if not crossing:
            break

        # Remove the longest crossing net (least likely to re-route anyway)
        def net_length(nid: str) -> int:
            return sum(len(p) for p in routed_paths.get(nid, []))

        victim = max(crossing, key=net_length)
        log.info("Crossing validation: removing %s (crosses %s)",
                 victim, [n for n in crossing if n != victim])
        for path in routed_paths[victim]:
            grid.free_trace(path)
        del routed_paths[victim]
        removed.append(victim)

    return removed
