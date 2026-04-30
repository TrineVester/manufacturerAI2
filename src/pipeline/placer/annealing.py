"""Simulated-annealing refinement for placement.

After the greedy constructive placer produces an initial layout, this
module globally optimises component positions to minimise wirelength
and routing congestion.  The SA loop can escape local minima that the
one-at-a-time greedy strategy cannot.

Cost function (evaluated over the *entire* placement):
  1. Half-perimeter wirelength (HPWL) — standard EDA proxy for total
     trace length.  Sum over all nets of (bbox width + bbox height).
  2. Congestion — coarse global-routing demand vs. capacity (expensive,
     computed periodically rather than every iteration).
  3. Overlap penalty — body/keepout overlap between any pair.
  4. Outline violation — any body corner outside the outline.

Perturbation moves:
  - Displace  (60 %) — shift one component by a random offset.
  - Swap      (20 %) — exchange two non-UI components.
  - Rotate    (20 %) — change one component's rotation.
"""

from __future__ import annotations

import logging
import math
import random

from shapely.geometry import Polygon, box as shapely_box
from shapely.prepared import prep as shapely_prep

from src.catalog.models import Component
from src.pipeline.design.models import Net

from .congestion import CongestionGrid
from .geometry import (
    footprint_halfdims,
    footprint_envelope_halfdims,
    footprint_area,
    pin_world_xy,
    aabb_gap,
    rect_edge_clearance,
)
from .models import Placed, VALID_ROTATIONS, MIN_PIN_CLEARANCE_MM


def _build_net_seg_bboxes(
    entries: list[tuple[str, tuple[float, float] | None]],
    positions: dict[str, Placed],
) -> list[tuple[float, float, float, float]]:
    """Build (xmin, xmax, ymin, ymax) bbox tuples for one net's segments."""
    points: list[tuple[float, float]] = []
    for iid, local in entries:
        p = positions.get(iid)
        if p is None:
            continue
        if local is not None:
            c, s = _cos_sin(p.rotation)
            points.append((p.x + local[0] * c - local[1] * s,
                           p.y + local[0] * s + local[1] * c))
        else:
            points.append((p.x, p.y))
    segs: list[tuple[float, float, float, float]] = []
    np_len = len(points)
    for i in range(np_len):
        x1, y1 = points[i]
        for j in range(i + 1, np_len):
            x2, y2 = points[j]
            segs.append((
                x1 if x1 < x2 else x2,
                x1 if x1 > x2 else x2,
                y1 if y1 < y2 else y2,
                y1 if y1 > y2 else y2,
            ))
    return segs


def _count_pair_bboxes(
    segs_a: list[tuple[float, float, float, float]],
    segs_b: list[tuple[float, float, float, float]],
) -> int:
    """Count bbox overlaps between segments of two different nets."""
    count = 0
    for axmin, axmax, aymin, aymax in segs_a:
        for bxmin, bxmax, bymin, bymax in segs_b:
            if axmax > bxmin and bxmax > axmin and aymax > bymin and bymax > aymin:
                count += 1
    return count


_cc_state: dict | None = None


def _crossing_count(
    net_pin_data: list[list[tuple[str, tuple[float, float] | None]]],
    positions: dict[str, Placed],
) -> int:
    """Count bounding-box crossings with incremental updates across SA calls."""
    global _cc_state

    if _cc_state is not None and _cc_state['npd'] is not net_pin_data:
        _cc_state = None

    if _cc_state is not None:
        snap = _cc_state['snap']
        changed: set[str] = set()
        for iid, p in positions.items():
            old = snap.get(iid)
            if old is None or p.x != old[0] or p.y != old[1] or p.rotation != old[2]:
                changed.add(iid)

        if not changed:
            return _cc_state['count']

        inst_nets = _cc_state['inst_nets']
        affected: set[int] = set()
        for iid in changed:
            affected.update(inst_nets.get(iid, set()))

        if affected:
            net_segs = _cc_state['net_segs']
            n_nets = len(net_segs)
            cnt = _cc_state['count']
            for ni in affected:
                segs_a = net_segs[ni]
                for nj in range(n_nets):
                    if ni == nj:
                        continue
                    if nj in affected and nj < ni:
                        continue
                    for axmin, axmax, aymin, aymax in segs_a:
                        for bxmin, bxmax, bymin, bymax in net_segs[nj]:
                            if axmax > bxmin and bxmax > axmin and aymax > bymin and bymax > aymin:
                                cnt -= 1
            for ni in affected:
                net_segs[ni] = _build_net_seg_bboxes(
                    net_pin_data[ni], positions)
            for ni in affected:
                segs_a = net_segs[ni]
                for nj in range(n_nets):
                    if ni == nj:
                        continue
                    if nj in affected and nj < ni:
                        continue
                    for axmin, axmax, aymin, aymax in segs_a:
                        for bxmin, bxmax, bymin, bymax in net_segs[nj]:
                            if axmax > bxmin and bxmax > axmin and aymax > bymin and bymax > aymin:
                                cnt += 1
            _cc_state['count'] = cnt

        for iid in changed:
            p = positions[iid]
            snap[iid] = (p.x, p.y, p.rotation)

        return _cc_state['count']

    inst_nets: dict[str, set[int]] = {}
    net_segs: list[list[tuple[float, float, float, float]]] = []
    for ni, entries in enumerate(net_pin_data):
        for iid, _local in entries:
            inst_nets.setdefault(iid, set()).add(ni)
        net_segs.append(_build_net_seg_bboxes(entries, positions))

    count = 0
    n_nets = len(net_segs)
    for i in range(n_nets):
        for j in range(i + 1, n_nets):
            count += _count_pair_bboxes(net_segs[i], net_segs[j])

    _cc_state = {
        'npd': net_pin_data,
        'net_segs': net_segs,
        'count': count,
        'snap': {iid: (p.x, p.y, p.rotation)
                 for iid, p in positions.items()},
        'inst_nets': inst_nets,
    }
    return count

log = logging.getLogger(__name__)


def _preparse_net_pins(
    nets: list[Net],
    pin_cache: dict[str, dict[str, tuple[float, float]]],
    positions: dict[str, Placed],
) -> list[list[tuple[str, tuple[float, float] | None]]]:
    """Pre-parse net pin references once for reuse across SA iterations."""
    result: list[list[tuple[str, tuple[float, float] | None]]] = []
    for net in nets:
        entries: list[tuple[str, tuple[float, float] | None]] = []
        for ref in net.pins:
            if ":" not in ref:
                continue
            iid, pid = ref.split(":", 1)
            if iid not in positions:
                continue
            cat_id = positions[iid].catalog_id
            local = pin_cache.get(cat_id, {}).get(pid)
            entries.append((iid, local))
        result.append(entries)
    return result


# ── Cost helpers ───────────────────────────────────────────────────

def _hpwl(
    net_pin_data: list[list[tuple[str, tuple[float, float] | None]]],
    positions: dict[str, Placed],
) -> float:
    """Half-perimeter wirelength over all nets using pre-parsed pin data."""
    total = 0.0
    for entries in net_pin_data:
        xmin_n = math.inf
        xmax_n = -math.inf
        ymin_n = math.inf
        ymax_n = -math.inf
        count = 0
        for iid, local in entries:
            p = positions.get(iid)
            if p is None:
                continue
            if local is not None:
                c, s = _cos_sin(p.rotation)
                wx = p.x + local[0] * c - local[1] * s
                wy = p.y + local[0] * s + local[1] * c
            else:
                wx, wy = p.x, p.y
            if wx < xmin_n:
                xmin_n = wx
            if wx > xmax_n:
                xmax_n = wx
            if wy < ymin_n:
                ymin_n = wy
            if wy > ymax_n:
                ymax_n = wy
            count += 1
        if count >= 2:
            total += (xmax_n - xmin_n) + (ymax_n - ymin_n)
    return total


def _overlap_penalty(
    all_ids: list[str],
    positions: dict[str, Placed],
) -> float:
    """Sum of overlap depths between all component pairs."""
    penalty = 0.0
    n = len(all_ids)
    for i in range(n):
        a = positions[all_ids[i]]
        for j in range(i + 1, n):
            b = positions[all_ids[j]]
            gap = aabb_gap(a.x, a.y, a.env_hw, a.env_hh,
                           b.x, b.y, b.env_hw, b.env_hh)
            required = max(a.keepout, b.keepout, 1.0)
            violation = required - gap
            if violation > 0:
                penalty += violation
    return penalty


def _outline_penalty_fast(
    movable: list[str],
    positions: dict[str, Placed],
    prep_poly,
    edge_clearance: float,
    *,
    outline_aabb: tuple[float, float, float, float] | None = None,
) -> float:
    """Fast outline check — flat penalty per component outside."""
    penalty = 0.0
    if outline_aabb is not None:
        oxmin, oymin, oxmax, oymax = outline_aabb
        for iid in movable:
            p = positions[iid]
            ihw = p.env_hw + edge_clearance
            ihh = p.env_hh + edge_clearance
            if (p.x - ihw < oxmin or p.x + ihw > oxmax
                    or p.y - ihh < oymin or p.y + ihh > oymax):
                penalty += 10.0
    else:
        for iid in movable:
            p = positions[iid]
            ihw = p.env_hw + edge_clearance
            ihh = p.env_hh + edge_clearance
            rect = shapely_box(p.x - ihw, p.y - ihh, p.x + ihw, p.y + ihh)
            if not prep_poly.contains(rect):
                penalty += 10.0
    return penalty


_TRIG_TABLE: dict[int, tuple[float, float]] = {}


def _cos_sin(rotation: int) -> tuple[float, float]:
    t = _TRIG_TABLE.get(rotation)
    if t is None:
        rad = math.radians(rotation)
        t = (math.cos(rad), math.sin(rad))
        _TRIG_TABLE[rotation] = t
    return t


def _pin_clearance_penalty(
    all_ids: list[str],
    positions: dict[str, Placed],
    catalog_map: dict[str, Component],
) -> float:
    """Penalty for pin-to-pin clearance violations."""
    min_sq = MIN_PIN_CLEARANCE_MM * MIN_PIN_CLEARANCE_MM
    penalty = 0.0
    n = len(all_ids)
    for i in range(n):
        a = positions[all_ids[i]]
        cat_a = catalog_map.get(a.catalog_id)
        if not cat_a or not cat_a.pins:
            continue
        a_cos, a_sin = _cos_sin(a.rotation)
        a_world = [
            (a.x + pa.position_mm[0] * a_cos - pa.position_mm[1] * a_sin,
             a.y + pa.position_mm[0] * a_sin + pa.position_mm[1] * a_cos)
            for pa in cat_a.pins
        ]
        for j in range(i + 1, n):
            b = positions[all_ids[j]]
            if abs(a.x - b.x) > a.env_hw + b.env_hw + MIN_PIN_CLEARANCE_MM:
                continue
            if abs(a.y - b.y) > a.env_hh + b.env_hh + MIN_PIN_CLEARANCE_MM:
                continue
            cat_b = catalog_map.get(b.catalog_id)
            if not cat_b or not cat_b.pins:
                continue
            b_cos, b_sin = _cos_sin(b.rotation)
            for ax, ay in a_world:
                for pb in cat_b.pins:
                    bx = b.x + pb.position_mm[0] * b_cos - pb.position_mm[1] * b_sin
                    by = b.y + pb.position_mm[0] * b_sin + pb.position_mm[1] * b_cos
                    dsq = (ax - bx) ** 2 + (ay - by) ** 2
                    if dsq < min_sq:
                        penalty += MIN_PIN_CLEARANCE_MM - math.sqrt(dsq)
    return penalty


def _congestion_cost(
    nets: list[Net],
    positions: dict[str, Placed],
    cg: CongestionGrid,
) -> float:
    """Coarse-grid congestion: rebuild demand, return total overflow."""
    cg._demand = [0] * len(cg._demand)
    cg._net_routes.clear()

    for iid in list(cg._body_blocks.keys()):
        cg.unblock_component(iid)
    for iid, p in positions.items():
        cg.block_component(iid, p.x, p.y, p.env_hw, p.env_hh)

    total_overflow = 0.0
    for net in nets:
        by_inst: dict[str, list[str]] = {}
        for ref in net.pins:
            if ":" not in ref:
                continue
            iid, _pid = ref.split(":", 1)
            by_inst.setdefault(iid, []).append(_pid)

        iids = [i for i in by_inst if i in positions]
        if len(iids) < 2:
            continue

        anchor = iids[0]
        a = positions[anchor]
        for other_iid in iids[1:]:
            b = positions[other_iid]
            path = cg.route_coarse(a.x, a.y, b.x, b.y)
            if path is not None:
                cg.commit_net(f"{net.id}_{anchor}_{other_iid}", path)
                total_overflow += cg.congestion_along(path)
            else:
                total_overflow += 10.0

    return total_overflow


# ── SA Refiner ─────────────────────────────────────────────────────


def sa_refine(
    placed: list[Placed],
    ui_ids: set[str],
    nets: list[Net],
    catalog_map: dict[str, Component],
    outline_poly: Polygon,
    congestion_grid: CongestionGrid,
    *,
    n_iterations: int = 0,
    t_initial: float = 50.0,
    cooling: float = 0.9995,
) -> list[Placed]:
    """Refine placement via Simulated Annealing.

    Parameters
    ----------
    placed : list[Placed]
        Initial placement from the constructive engine.
    ui_ids : set[str]
        Instance IDs of UI-placed (frozen) components.
    nets : list[Net]
        Net list from the design spec.
    catalog_map : dict[str, Component]
        catalog_id -> Component lookup.
    outline_poly : Polygon
        The board outline polygon.
    congestion_grid : CongestionGrid
        Coarse routing grid (will be mutated during evaluation).
    n_iterations : int
        Number of SA iterations.  0 = auto-scale by component count.
    t_initial : float
        Starting temperature.
    cooling : float
        Multiplicative cooling factor per iteration.

    Returns
    -------
    list[Placed]
        Refined placement (same structure, updated positions/rotations).
    """
    movable = [p.instance_id for p in placed if p.instance_id not in ui_ids]
    if len(movable) < 2:
        return placed

    # Seed RNG from placement state for reproducibility
    seed_val = hash(tuple((p.instance_id, round(p.x, 1), round(p.y, 1)) for p in placed))
    rng = random.Random(seed_val)

    # Auto-scale iterations: ~2000 per movable component, capped
    if n_iterations <= 0:
        n_iterations = min(len(movable) * 2000, 15_000)

    all_ids = [p.instance_id for p in placed]
    xmin, ymin, xmax, ymax = outline_poly.bounds
    board_w = xmax - xmin
    board_h = ymax - ymin
    prep_poly = shapely_prep(outline_poly)
    edge_clearance = 1.5

    # Detect axis-aligned rectangular outline for fast containment
    from .geometry import _is_aabb
    outline_verts = list(outline_poly.exterior.coords[:-1])
    _outline_aabb = _is_aabb(outline_verts)

    # Build pin local-position cache for fast HPWL
    pin_cache: dict[str, dict[str, tuple[float, float]]] = {}
    for cat in catalog_map.values():
        pin_map: dict[str, tuple[float, float]] = {}
        for pin in cat.pins:
            pin_map[pin.id] = pin.position_mm
        pin_cache[cat.id] = pin_map

    # Build mutable position map (needed before _preparse_net_pins)
    positions: dict[str, Placed] = {}
    for p in placed:
        positions[p.instance_id] = Placed(
            instance_id=p.instance_id,
            catalog_id=p.catalog_id,
            x=p.x, y=p.y, rotation=p.rotation,
            hw=p.hw, hh=p.hh, keepout=p.keepout,
            env_hw=p.env_hw, env_hh=p.env_hh,
        )

    # Weights
    W_HPWL = 1.0
    W_CONGESTION = 10.0
    W_OVERLAP = 1000.0
    W_OUTLINE = 1000.0
    W_PIN_CLR = 500.0
    W_EDGE_PREF = 1.0
    W_CROSSING = 50.0

    # Identify large components that should prefer edges (>5% of outline area)
    outline_area = board_w * board_h
    large_comps: dict[str, float] = {}  # iid -> strength
    for iid in movable:
        p = positions[iid]
        cat = catalog_map.get(p.catalog_id)
        if cat is not None:
            area_ratio = footprint_area(cat) / outline_area if outline_area > 0 else 0
            if area_ratio > 0.05:
                large_comps[iid] = min(area_ratio / 0.05, 3.0)

    def _edge_pref_cost() -> float:
        """Penalise large components that are far from outline edges."""
        cost = 0.0
        for iid, strength in large_comps.items():
            p = positions[iid]
            edge_dist = rect_edge_clearance(
                p.x, p.y, p.env_hw, p.env_hh, outline_verts)
            cost += edge_dist * strength
        return cost

    # Pre-parse net pin references once (structure doesn't change during SA)
    net_pin_data = _preparse_net_pins(nets, pin_cache, positions)

    # Congestion is expensive — cache and refresh periodically
    CONG_INTERVAL = 50
    cached_cong = _congestion_cost(nets, positions, congestion_grid)

    def fast_cost() -> float:
        return (
            W_HPWL * _hpwl(net_pin_data, positions)
            + W_CONGESTION * cached_cong
            + W_OVERLAP * _overlap_penalty(all_ids, positions)
            + W_OUTLINE * _outline_penalty_fast(movable, positions, prep_poly, edge_clearance, outline_aabb=_outline_aabb)
            + W_PIN_CLR * _pin_clearance_penalty(all_ids, positions, catalog_map)
            + W_EDGE_PREF * _edge_pref_cost()
            + W_CROSSING * _crossing_count(net_pin_data, positions)
        )

    current_cost = fast_cost()
    best_cost = current_cost
    best_snapshot: dict[str, tuple[float, float, int]] = {
        iid: (positions[iid].x, positions[iid].y, positions[iid].rotation)
        for iid in all_ids
    }
    initial_cost = current_cost

    T = t_initial
    accepted = 0
    stagnant = 0
    STAGNANT_LIMIT = max(n_iterations // 4, 500)

    iid2: str = ""  # for rollback in swap branch

    for iteration in range(n_iterations):
        # Refresh expensive cost terms periodically
        if iteration % CONG_INTERVAL == 0 and iteration > 0:
            cached_cong = _congestion_cost(nets, positions, congestion_grid)

        # Early termination if stagnant
        if stagnant >= STAGNANT_LIMIT:
            break

        r = rng.random()
        iid = rng.choice(movable)
        p = positions[iid]
        cat = catalog_map.get(p.catalog_id)

        old_x, old_y, old_rot = p.x, p.y, p.rotation
        old_hw, old_hh = p.hw, p.hh
        old_ehw, old_ehh = p.env_hw, p.env_hh

        move_type = 0  # 0=displace, 1=swap, 2=rotate

        if r < 0.6:
            move_type = 0
            sigma = (T / t_initial) * max(board_w, board_h) * 0.3
            sigma = max(sigma, 0.5)
            p.x = max(xmin + p.env_hw,
                       min(xmax - p.env_hw, p.x + rng.gauss(0, sigma)))
            p.y = max(ymin + p.env_hh,
                       min(ymax - p.env_hh, p.y + rng.gauss(0, sigma)))

        elif r < 0.8 and len(movable) >= 2:
            move_type = 1
            iid2 = rng.choice(movable)
            while iid2 == iid:
                iid2 = rng.choice(movable)
            p2 = positions[iid2]
            p.x, p2.x = p2.x, p.x
            p.y, p2.y = p2.y, p.y

        else:
            move_type = 2
            if cat is not None:
                candidates = [rot for rot in VALID_ROTATIONS if rot != p.rotation]
                if candidates:
                    new_rot = rng.choice(candidates)
                    p.rotation = new_rot
                    p.hw, p.hh = footprint_halfdims(cat, new_rot)
                    p.env_hw, p.env_hh = footprint_envelope_halfdims(cat, new_rot)

        new_cost = fast_cost()
        delta = new_cost - current_cost

        if delta < 0 or rng.random() < math.exp(-delta / max(T, 1e-9)):
            current_cost = new_cost
            accepted += 1
            stagnant = 0
            if new_cost < best_cost and _overlap_penalty(all_ids, positions) <= 0.01:
                best_cost = new_cost
                best_snapshot = {
                    i: (positions[i].x, positions[i].y, positions[i].rotation)
                    for i in all_ids
                }
        else:
            stagnant += 1
            if move_type == 0:
                p.x, p.y, p.rotation = old_x, old_y, old_rot
            elif move_type == 1:
                p2 = positions[iid2]
                p.x, p2.x = p2.x, p.x
                p.y, p2.y = p2.y, p.y
            else:
                p.x, p.y, p.rotation = old_x, old_y, old_rot
                p.hw, p.hh = old_hw, old_hh
                p.env_hw, p.env_hh = old_ehw, old_ehh

        T *= cooling

    # ── Restore best and verify feasibility ────────────────────────
    for iid in all_ids:
        bx, by, brot = best_snapshot[iid]
        p = positions[iid]
        p.x, p.y, p.rotation = bx, by, brot
        cat = catalog_map.get(p.catalog_id)
        if cat is not None:
            p.hw, p.hh = footprint_halfdims(cat, brot)
            p.env_hw, p.env_hh = footprint_envelope_halfdims(cat, brot)

    # Feasibility check
    overlap = _overlap_penalty(all_ids, positions)
    outline_viol = _outline_penalty_fast(movable, positions, prep_poly, edge_clearance, outline_aabb=_outline_aabb)
    pin_viol = _pin_clearance_penalty(all_ids, positions, catalog_map)

    if overlap > 0.01 or outline_viol > 0.01 or pin_viol > 0.01:
        log.warning(
            "SA best has constraint violations (overlap=%.2f outline=%.2f pin=%.2f); "
            "falling back to constructive placement",
            overlap, outline_viol, pin_viol,
        )
        return placed

    log.info(
        "SA refinement: %d iters, %d accepted, cost %.1f → %.1f",
        iteration + 1, accepted, initial_cost, best_cost,
    )

    result: list[Placed] = []
    for orig in placed:
        p = positions[orig.instance_id]
        result.append(Placed(
            instance_id=p.instance_id,
            catalog_id=p.catalog_id,
            x=round(p.x, 2),
            y=round(p.y, 2),
            rotation=p.rotation,
            hw=p.hw, hh=p.hh,
            keepout=p.keepout,
            env_hw=p.env_hw, env_hh=p.env_hh,
        ))
    return result
