"""Candidate position scoring for the placement engine."""

from __future__ import annotations

import math
from collections import defaultdict

from src.catalog.models import Component

from .geometry import rect_edge_clearance, aabb_gap
from .models import Placed
from .nets import NetEdge, resolve_pin_positions

if __name__ != "__never__":
    # Avoid circular import — congestion is optional at runtime
    from .congestion import CongestionGrid


def score_candidate(
    cx: float, cy: float, rotation: int,
    ehw: float, ehh: float, keepout: float,
    instance_id: str,
    cat: Component,
    placed: list[Placed],
    placed_map: dict[str, Placed],
    catalog_map: dict[str, Component],
    net_graph: dict[str, list[NetEdge]],
    outline_verts: list[tuple[float, float]],
    outline_bounds: tuple[float, float, float, float],
    mounting_style: str,
    congestion_grid: CongestionGrid | None = None,
    pcb_contour_verts: list[tuple[float, float]] | None = None,
) -> float:
    """Lightweight scoring: net proximity + edge/bottom preference + spacing."""
    score = 0.0

    # Precompute trig for candidate rotation
    _rad = math.radians(rotation)
    _cos_r = math.cos(_rad)
    _sin_r = math.sin(_rad)

    # 1. Net proximity — dominant term.
    _MAX_EDGES_PER_NET = 2

    edges_by_net: dict[str, list[NetEdge]] = defaultdict(list)
    for edge in net_graph.get(instance_id, []):
        if placed_map.get(edge.other_iid) is not None:
            edges_by_net[edge.net_id].append(edge)

    best_pin_pairs: list[tuple[tuple[float, float], tuple[float, float],
                                float, float, float, float]] = []

    # Cache trig values per rotation for placed components
    _trig_cache: dict[int, tuple[float, float]] = {}

    for _net_id, edges in edges_by_net.items():
        edge_dists: list[tuple[float, NetEdge,
                               tuple[float, float], tuple[float, float]]] = []
        for edge in edges:
            other = placed_map[edge.other_iid]
            my_positions = resolve_pin_positions(edge.my_pins, cat)
            other_cat = catalog_map.get(other.catalog_id)
            if other_cat is None:
                continue
            other_positions = resolve_pin_positions(edge.other_pins, other_cat)

            o_rot = other.rotation
            o_trig = _trig_cache.get(o_rot)
            if o_trig is None:
                o_rad = math.radians(o_rot)
                o_trig = (math.cos(o_rad), math.sin(o_rad))
                _trig_cache[o_rot] = o_trig
            o_cos, o_sin = o_trig
            o_x, o_y = other.x, other.y

            best_dist = float("inf")
            best_wp: tuple[float, float] = (cx, cy)
            best_op: tuple[float, float] = (o_x, o_y)
            for mp in my_positions:
                wx = cx + mp[0] * _cos_r - mp[1] * _sin_r
                wy = cy + mp[0] * _sin_r + mp[1] * _cos_r
                for op in other_positions:
                    owx = o_x + op[0] * o_cos - op[1] * o_sin
                    owy = o_y + op[0] * o_sin + op[1] * o_cos
                    dx = wx - owx
                    dy = wy - owy
                    d = math.sqrt(dx * dx + dy * dy)
                    if d < best_dist:
                        best_dist = d
                        best_wp = (wx, wy)
                        best_op = (owx, owy)
            if best_dist < float("inf"):
                edge_dists.append((best_dist, edge, best_wp, best_op))

        edge_dists.sort(key=lambda t: t[0])
        for best_dist, edge, best_wp, best_op in edge_dists[:_MAX_EDGES_PER_NET]:
            other = placed_map[edge.other_iid]
            fanout_boost = 1.0 + math.log2(max(edge.fanout, 2)) - 1.0
            score -= best_dist * 5.0 * fanout_boost
            best_pin_pairs.append((best_wp, best_op,
                                   cx, cy, other.x, other.y))

    # 2. Edge clearance (small reward for safe distance)
    edge_dist = rect_edge_clearance(cx, cy, ehw, ehh, outline_verts)
    if pcb_contour_verts is not None:
        contour_edge_dist = rect_edge_clearance(
            cx, cy, ehw, ehh, pcb_contour_verts)
        edge_dist = min(edge_dist, contour_edge_dist)
    score += min(edge_dist, 5.0) * 0.5

    # 3. Bottom preference
    if mounting_style == "bottom":
        _, ymin_b, _, _ = outline_bounds
        score -= (cy - ymin_b) * 0.08

    # 4. Large component → prefer edges
    outline_area = (outline_bounds[2] - outline_bounds[0]) * (outline_bounds[3] - outline_bounds[1])
    if outline_area > 0:
        comp_area = ehw * 2 * ehh * 2
        area_ratio = comp_area / outline_area
        if area_ratio > 0.05:
            strength = min(area_ratio / 0.05, 3.0)
            score -= edge_dist * 1.0 * strength

    # 5. Spacing reward — prefer staying spread from neighbours
    if placed:
        min_gap = float("inf")
        for p in placed:
            g = aabb_gap(cx, cy, ehw, ehh, p.x, p.y, p.env_hw, p.env_hh)
            if g < min_gap:
                min_gap = g
        if min_gap < float("inf"):
            score += min(min_gap, 15.0) * 0.4

    # 6. Compactness (mild)
    if placed:
        centroid_x = sum(p.x for p in placed) / len(placed)
        centroid_y = sum(p.y for p in placed) / len(placed)
        score -= math.hypot(cx - centroid_x, cy - centroid_y) * 0.2

    # 7. Pin-facing bonus — reward when connected pins face each other
    for (wp, op, mcx, mcy, ocx, ocy) in best_pin_pairs:
        # Vector from my centre to my pin
        my_dx = wp[0] - mcx
        my_dy = wp[1] - mcy
        # Vector from my centre towards the other component
        to_other_dx = ocx - mcx
        to_other_dy = ocy - mcy
        # Dot product > 0 means my pin faces towards the partner
        dot = my_dx * to_other_dx + my_dy * to_other_dy
        norm = math.hypot(my_dx, my_dy) * math.hypot(to_other_dx, to_other_dy)
        if norm > 1e-6:
            facing = dot / norm   # cosine: +1 = facing, -1 = away
            score += facing * 8.0

    # 8. Congestion penalty — check coarse tile demand along Manhattan path
    if congestion_grid is not None and best_pin_pairs:
        total_cong = 0.0
        for (wp, op, _mcx, _mcy, _ocx, _ocy) in best_pin_pairs:
            total_cong += congestion_grid.congestion_manhattan(
                wp[0], wp[1], op[0], op[1])
        score -= total_cong * 20.0

    # 9. IC-anchor proximity — pull small passives toward their most
    #    pin-dense connected IC.  Bypass caps and current-limiting
    #    resistors should sit adjacent to the chip's pins, not in the
    #    middle of the board where they create long traces through
    #    congested areas.  Uses pin count (routing complexity) rather
    #    than body area to identify the routing hub.
    my_pins = len(cat.pins)
    best_anchor: Placed | None = None
    best_anchor_pins = 0
    for edge in net_graph.get(instance_id, []):
        other = placed_map.get(edge.other_iid)
        if other is None:
            continue
        other_cat_a = catalog_map.get(other.catalog_id)
        if other_cat_a is None:
            continue
        other_pins = len(other_cat_a.pins)
        if other_pins > best_anchor_pins:
            best_anchor_pins = other_pins
            best_anchor = other
    if best_anchor is not None and best_anchor_pins > my_pins * 3:
        ratio = min(best_anchor_pins / max(my_pins, 1), 10.0)
        dist = math.hypot(cx - best_anchor.x, cy - best_anchor.y)
        score -= dist * 1.5 * math.log2(ratio)

    return score
