"""Candidate position generation for the placement engine."""

from __future__ import annotations

from src.catalog.models import Component

from .models import Placed
from .nets import NetEdge


def generate_candidates(
    instance_id: str,
    cat: Component,
    rotation: int,
    ehw: float, ehh: float,
    ihw: float, ihh: float,
    placed: list[Placed],
    placed_map: dict[str, Placed],
    net_graph: dict[str, list[NetEdge]],
    catalog_map: dict[str, Component],
    outline_bounds: tuple[float, float, float, float],
    edge_clr: float,
    grid_step: float,
    mounting_style: str,
) -> list[tuple[float, float]]:
    """Generate a focused set of candidate positions.

    Instead of scanning every mm of the outline, we generate candidates
    from three sources:
      1. Adjacent positions near net-connected already-placed components
      2. Positions along the outline edges (for large/edge-preferring parts)
      3. A coarse grid fallback to guarantee coverage
    """
    xmin, ymin, xmax, ymax = outline_bounds
    scan_xmin = xmin + ihw
    scan_xmax = xmax - ihw
    scan_ymin = ymin + ihh
    scan_ymax = ymax - ihh

    if scan_xmin > scan_xmax or scan_ymin > scan_ymax:
        return []

    candidates: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()

    def _add(x: float, y: float) -> None:
        x = max(scan_xmin, min(scan_xmax, x))
        y = max(scan_ymin, min(scan_ymax, y))
        key = (round(x * 2), round(y * 2))
        if key not in seen:
            seen.add(key)
            candidates.append((x, y))

    offsets = [0.0, -grid_step, grid_step, -2 * grid_step, 2 * grid_step]

    # 1. Near net-connected placed components — the main source
    for edge in net_graph.get(instance_id, []):
        other = placed_map.get(edge.other_iid)
        if other is None:
            continue
        spread = max(other.env_hw + ehw, other.env_hh + ehh) + 3.0
        for dx_mult in (-1.0, 0.0, 1.0):
            for dy_mult in (-1.0, 0.0, 1.0):
                if dx_mult == 0 and dy_mult == 0:
                    continue
                bx = other.x + dx_mult * spread
                by = other.y + dy_mult * spread
                for ox in offsets:
                    for oy in offsets:
                        _add(bx + ox, by + oy)

    # 2. Near the centroid of already-placed components
    if placed:
        cx_c = sum(p.x for p in placed) / len(placed)
        cy_c = sum(p.y for p in placed) / len(placed)
        for ox in offsets:
            for oy in offsets:
                _add(cx_c + ox, cy_c + oy)

    # 3. Edge-hugging candidates (for large components and general coverage)
    edge_steps = [0.0, grid_step, 2 * grid_step]
    for edge_x in (scan_xmin, scan_xmax):
        y = scan_ymin
        while y <= scan_ymax + 1e-6:
            for inset in edge_steps:
                if edge_x == scan_xmin:
                    _add(edge_x + inset, y)
                else:
                    _add(edge_x - inset, y)
            y += grid_step
    for edge_y in (scan_ymin, scan_ymax):
        x = scan_xmin
        while x <= scan_xmax + 1e-6:
            for inset in edge_steps:
                if edge_y == scan_ymin:
                    _add(x, edge_y + inset)
                else:
                    _add(x, edge_y - inset)
            x += grid_step

    # 4. Bottom-preference strip for bottom-mount components
    if mounting_style == "bottom":
        x = scan_xmin
        while x <= scan_xmax + 1e-6:
            for y_off in (scan_ymin, scan_ymin + grid_step,
                          scan_ymin + 2 * grid_step):
                _add(x, y_off)
            x += grid_step

    # 5. Coarse grid fallback (ensures we always find something if it exists)
    coarse = max(grid_step * 4, 4.0)
    x = scan_xmin
    while x <= scan_xmax + 1e-6:
        y = scan_ymin
        while y <= scan_ymax + 1e-6:
            _add(x, y)
            y += coarse
        x += coarse

    return candidates
