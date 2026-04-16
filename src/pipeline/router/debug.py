"""Self-contained debug grid generation for the routing viewport.

Builds a combined ownership overlay from scratch using only the routing
inputs and outputs — completely independent of the live routing grid.
"""

from __future__ import annotations

import base64
import math

from shapely.geometry import Polygon

from src.catalog.models import CatalogResult
from src.pipeline.placer.models import FullPlacement
from src.pipeline.placer.geometry import footprint_halfdims

from .grid import RoutingGrid
from .models import RouterConfig
from .pins import pin_world_xy


def build_debug_grids(
    placement: FullPlacement,
    catalog: CatalogResult,
    routed_paths: dict[str, list[list[tuple[int, int]]]],
    routed_pads: dict[str, list],
    *,
    config: RouterConfig | None = None,
    grid: RoutingGrid | None = None,
) -> list[dict]:
    """Build a single combined-ownership overlay bitmap.

    If *grid* is provided it is used directly (fast path).
    Otherwise a new grid is constructed from scratch (standalone mode).
    """
    if config is None:
        config = RouterConfig()

    if grid is None:
        catalog_map = {c.id: c for c in catalog.components}
        outline_poly = Polygon(
            placement.outline.vertices,
            placement.outline.hole_vertices or None,
        )

        if not outline_poly.is_valid or outline_poly.area <= 0:
            return []

        grid = RoutingGrid(
            outline_poly,
            resolution=config.grid_resolution_mm,
            edge_clearance=config.edge_clearance_mm,
            trace_width_mm=config.trace_width_mm,
            trace_clearance_mm=config.trace_clearance_mm,
        )
        pad_radius = max(1, math.ceil(
            (config.trace_width_mm / 2 + config.trace_clearance_mm)
            / config.grid_resolution_mm
        ))
        _block_components(grid, placement, catalog_map, pad_radius)

        for nid, paths in routed_paths.items():
            for path in paths:
                grid.block_trace(path, net_id=nid)

    W = grid.width
    H = grid.height

    net_ids = sorted(routed_paths.keys())
    net_index = {nid: i + 1 for i, nid in enumerate(net_ids)}

    combined_map = bytearray(W * H)
    for flat, owner in grid._trace_owner.items():
        idx = net_index.get(owner, 0)
        if idx:
            combined_map[flat] = idx
    for flat, owners in grid._clearance_owner.items():
        if combined_map[flat]:
            continue
        for owner in owners:
            idx = net_index.get(owner, 0)
            if idx:
                combined_map[flat] = idx
                break

    palette = {nid: i + 1 for i, nid in enumerate(net_ids)}

    return [{
        "layer": "combined_owner",
        "width": W,
        "height": H,
        "origin_x": grid.origin_x,
        "origin_y": grid.origin_y,
        "resolution": grid.resolution,
        "cells": base64.b64encode(bytes(combined_map)).decode("ascii"),
        "palette": palette,
    }]


# ── Internal helpers (mirror of engine logic, kept minimal) ────────


def _block_components(
    grid: RoutingGrid,
    placement: FullPlacement,
    catalog_map: dict,
    pad_radius: int,
) -> None:
    from .engine import _pin_grid_halfdims
    res = grid.resolution

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
            wx, wy = pin_world_xy(
                pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg,
            )
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
        grid.block_rect_world(pc.x_mm, pc.y_mm, hw, hh, permanent=True)

    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.mounting.blocks_routing:
            continue
        for pin in cat.pins:
            wx, wy = pin_world_xy(
                pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg,
            )
            gx, gy = grid.world_to_grid(wx, wy)
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    grid.force_free_cell(gx + dx, gy + dy)
                    grid.protect_cell(gx + dx, gy + dy)
