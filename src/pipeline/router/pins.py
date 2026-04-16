"""Pin resolution — convert net pin references to world coordinates.

Handles:
  - Direct pin references ("bat_1:V+")
  - Group references for components with internal_nets ("btn_1:A", "btn_1:B")
  - Dynamic group references for MCU ("mcu_1:gpio", "mcu_1:pwm")
  - Pin world position computation (local → rotated → translated)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.catalog.models import Component, CatalogResult
from src.pipeline.placer.models import PlacedComponent, FullPlacement


@dataclass
class ResolvedPin:
    """A pin reference resolved to world coordinates."""

    instance_id: str
    pin_id: str             # physical pin ID (e.g. "PD2", "1", "anode")
    group_id: str | None    # original group ID if this was a group ref, else None
    world_x: float
    world_y: float
    is_dynamic: bool        # True if this is a dynamic (MCU gpio) allocation


@dataclass
class PinPool:
    """Tracks available pins in allocatable groups for an instance."""

    instance_id: str
    # group_id -> list of remaining (unallocated) physical pin IDs
    pools: dict[str, list[str]]


def pin_world_xy(
    pin_local: tuple[float, float],
    cx: float, cy: float,
    rotation_deg: float,
) -> tuple[float, float]:
    """Transform a component-local pin position to world coordinates."""
    px, py = pin_local
    rad = math.radians(rotation_deg)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    return (
        cx + px * cos_r - py * sin_r,
        cy + px * sin_r + py * cos_r,
    )


def build_pin_pools(
    placement: FullPlacement,
    catalog: CatalogResult,
) -> dict[str, PinPool]:
    """Build dynamic pin pools for all instances with allocatable pin groups.

    Returns a map: instance_id -> PinPool.
    Only instances that have at least one allocatable or fixed_net
    PinGroup are included.  Fixed-net groups (e.g. power, ground)
    need pool entries so the router can pick specific physical pins.
    """
    catalog_map = {c.id: c for c in catalog.components}
    placed_map = {p.instance_id: p for p in placement.components}
    pools: dict[str, PinPool] = {}

    for pc in placement.components:
        cat = catalog_map.get(pc.catalog_id)
        if cat is None or not cat.pin_groups:
            continue

        inst_pools: dict[str, list[str]] = {}
        for pg in cat.pin_groups:
            if pg.allocatable or pg.fixed_net:
                inst_pools[pg.id] = list(pg.pin_ids)

        if inst_pools:
            pools[pc.instance_id] = PinPool(
                instance_id=pc.instance_id,
                pools=inst_pools,
            )

    return pools


def resolve_pin_ref(
    ref: str,
    placement: FullPlacement,
    catalog: CatalogResult,
) -> tuple[str, str, bool]:
    """Parse a pin reference and classify it.

    Returns (instance_id, pin_or_group_id, is_group).

    A reference is a group reference if pin_or_group_id matches a
    pin_group.id on the component's catalog entry.  Direct pin IDs
    take priority — if a pin ID matches, it's a direct ref even if
    a group with the same name exists.
    """
    iid, pid = ref.split(":", 1)
    catalog_map = {c.id: c for c in catalog.components}

    # Find the component instance
    pc = next((p for p in placement.components if p.instance_id == iid), None)
    if pc is None:
        return (iid, pid, False)

    cat = catalog_map.get(pc.catalog_id)
    if cat is None:
        return (iid, pid, False)

    # Check if pid is a direct pin ID
    pin_ids = {p.id for p in cat.pins}
    if pid in pin_ids:
        return (iid, pid, False)

    # Check if pid is a group ID
    if cat.pin_groups:
        group_ids = {g.id for g in cat.pin_groups}
        if pid in group_ids:
            return (iid, pid, True)

    return (iid, pid, False)


def get_pin_world_pos(
    instance_id: str,
    pin_id: str,
    placement: FullPlacement,
    catalog: CatalogResult,
) -> tuple[float, float] | None:
    """Get the world position of a specific physical pin."""
    pc = next((p for p in placement.components if p.instance_id == instance_id), None)
    if pc is None:
        return None

    pos = pc.pin_positions.get(pin_id)
    if pos is not None:
        return pos

    # Fallback: compute from catalog (e.g. legacy placement without pin_positions)
    catalog_map = {c.id: c for c in catalog.components}
    cat = catalog_map.get(pc.catalog_id)
    if cat is None:
        return None
    pin = next((p for p in cat.pins if p.id == pin_id), None)
    if pin is None:
        return None
    return pin_world_xy(pin.position_mm, pc.x_mm, pc.y_mm, pc.rotation_deg)


def get_group_pin_positions(
    instance_id: str,
    group_id: str,
    placement: FullPlacement,
    catalog: CatalogResult,
) -> list[tuple[str, float, float]]:
    """Get world positions of all pins in a pin group.

    Returns list of (pin_id, world_x, world_y).
    """
    catalog_map = {c.id: c for c in catalog.components}
    pc = next((p for p in placement.components if p.instance_id == instance_id), None)
    if pc is None:
        return []

    cat = catalog_map.get(pc.catalog_id)
    if cat is None or not cat.pin_groups:
        return []

    group = next((g for g in cat.pin_groups if g.id == group_id), None)
    if group is None:
        return []

    result: list[tuple[str, float, float]] = []
    for pid in group.pin_ids:
        pos = pc.pin_positions.get(pid)
        if pos is not None:
            result.append((pid, pos[0], pos[1]))
        else:
            pin_map = {p.id: p.position_mm for p in cat.pins}
            if pid in pin_map:
                wx, wy = pin_world_xy(pin_map[pid], pc.x_mm, pc.y_mm, pc.rotation_deg)
                result.append((pid, wx, wy))

    return result


def allocate_best_pin(
    instance_id: str,
    group_id: str,
    target_x: float,
    target_y: float,
    pool: PinPool,
    placement: FullPlacement,
    catalog: CatalogResult,
) -> str | None:
    """Pick the pin in the group's pool closest to a target position.

    Removes the chosen pin from the pool. Returns the chosen pin_id,
    or None if the pool is empty.
    """
    available = pool.pools.get(group_id, [])
    if not available:
        return None

    pc = next((p for p in placement.components if p.instance_id == instance_id), None)
    if pc is None:
        return None

    best_pin: str | None = None
    best_dist = float("inf")

    for pid in available:
        pos = pc.pin_positions.get(pid)
        if pos is not None:
            wx, wy = pos
        else:
            catalog_map = {c.id: c for c in catalog.components}
            cat = catalog_map.get(pc.catalog_id)
            if cat is None:
                continue
            pin_map = {p.id: p.position_mm for p in cat.pins}
            if pid not in pin_map:
                continue
            wx, wy = pin_world_xy(pin_map[pid], pc.x_mm, pc.y_mm, pc.rotation_deg)

        d = math.hypot(wx - target_x, wy - target_y)
        if d < best_dist:
            best_dist = d
            best_pin = pid

    if best_pin is not None:
        available.remove(best_pin)
        for other_group, other_pins in pool.pools.items():
            if other_group != group_id and best_pin in other_pins:
                other_pins.remove(best_pin)

    return best_pin
