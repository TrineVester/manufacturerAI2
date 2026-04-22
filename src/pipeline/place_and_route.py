"""Place-and-route coordinator.

Runs the placer, then attempts routing.  If routing fails it tries
rotating each ``blocks_routing=True`` auto-placed component through its
alternative orientations (0/90/180/270°) to open routing corridors,
and picks the variant that fully routes (or has fewest failed nets).

The caller receives a ``PlaceAndRouteResult`` that bundles the final
``FullPlacement`` and ``RoutingResult``, plus a list of any rotations
that were changed, so the caller can persist both artifacts.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from src.catalog.models import CatalogResult
from src.pipeline.design.models import DesignSpec  # noqa: F401 (type hint only)
from src.pipeline.placer import place_components
from src.pipeline.placer.models import PlacedComponent, FullPlacement, VALID_ROTATIONS
from src.pipeline.router import route_traces
from src.pipeline.router.models import RoutingResult, RouterConfig

log = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]

# Maximum number of rotation-variant attempts before giving up.
MAX_ROTATION_ATTEMPTS = 16


@dataclass
class RotationChange:
    """Records a rotation that was changed by the coordinator."""
    instance_id: str
    original_deg: float
    new_deg: float


@dataclass
class PlaceAndRouteResult:
    """Combined result from the coordination layer."""
    placement: FullPlacement
    routing: RoutingResult
    rotation_changes: list[RotationChange] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.routing.ok


# ── Internal helpers ───────────────────────────────────────────────

def _recompute_pin_positions(
    comp: PlacedComponent,
    cat_map: dict,
    new_rotation: float,
) -> PlacedComponent:
    """Return a copy of *comp* with a new rotation and recomputed pin_positions."""
    cat = cat_map.get(comp.catalog_id)
    comp2 = copy.copy(comp)
    comp2.rotation_deg = new_rotation

    pin_positions: dict[str, tuple[float, float]] = {}
    if cat is not None:
        x, y = comp.x_mm, comp.y_mm
        # side-mount components have a y-offset applied in the placer;
        # reproduce it here for consistency.
        side_y_offset = (
            cat.body.height_mm / 2
            if comp.mounting_style == "side" and cat.mounting.style != "side"
            else 0
        )
        rad = math.radians(new_rotation)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        for pin in cat.pins:
            px, py = pin.position_mm[0], pin.position_mm[1] + side_y_offset
            pin_positions[pin.id] = (
                round(x + px * cos_r - py * sin_r, 4),
                round(y + px * sin_r + py * cos_r, 4),
            )
    comp2.pin_positions = pin_positions
    return comp2


def _routing_score(result: RoutingResult) -> tuple[int, int]:
    """Lower is better: (failed_nets, total_trace_cells)."""
    total_cells = sum(len(t.path) for t in result.traces)
    return (len(result.failed_nets), total_cells)


def _candidates_to_try(
    placement: FullPlacement,
    cat_map: dict,
) -> list[tuple[int, list[float]]]:
    """Return [(component_index, [alt_rotations]), ...] for blocking auto-placed components.

    Only components whose catalog entry has ``blocks_routing=True`` and
    that are *not* UI-placed (i.e. they were auto-placed) are candidates.
    The UI-placed components (buttons, LEDs) are fixed by the Design Agent
    and must not be moved.
    """
    # Build the set of UI-placed instance IDs from the placement itself.
    # UI-placed components are those with mounting_style "top" whose
    # catalog entry has ui_placement=True.
    ui_placed_ids: set[str] = set()
    for c in placement.components:
        cat = cat_map.get(c.catalog_id)
        if cat and cat.ui_placement:
            ui_placed_ids.add(c.instance_id)

    candidates = []
    for i, comp in enumerate(placement.components):
        if comp.instance_id in ui_placed_ids:
            continue
        cat = cat_map.get(comp.catalog_id)
        if cat is None:
            continue
        if not (cat.mounting and cat.mounting.blocks_routing):
            continue
        current = comp.rotation_deg % 360
        alts = [r for r in VALID_ROTATIONS if r != current]
        if alts:
            candidates.append((i, alts))

    return candidates


# ── Public entry point ─────────────────────────────────────────────

def place_and_route(
    design: DesignSpec,
    catalog: CatalogResult,
    *,
    router_config: RouterConfig | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> PlaceAndRouteResult:
    """Run placement then route, with automatic rotation recovery on failure.

    Steps:
      1. Place all components via ``place_components``.
      2. Route via ``route_traces``.
      3. If routing is complete → return immediately.
      4. Otherwise: for each ``blocks_routing=True`` auto-placed component,
         try each of its alternative rotations (keeping everything else fixed)
         and re-route.  Accept the first fully-routed variant; if none fully
         route, keep the variant with the fewest failed nets.
      5. If any rotation changes improved routing, write a note into the
         result so the caller can surface it to the user.
    """
    def _progress(msg: str, partial: RoutingResult | None = None) -> None:
        if on_progress:
            on_progress({"message": msg, "partial_result": partial})

    # ── Step 1: place ──────────────────────────────────────────────
    _progress("Placing components…")
    placement = place_components(design, catalog)

    # ── Step 2: initial route ──────────────────────────────────────
    _progress("Routing traces…")
    result = route_traces(
        placement, catalog,
        config=router_config,
        on_progress=on_progress,
        cancel=cancel,
    )

    if result.ok:
        return PlaceAndRouteResult(placement=placement, routing=result)

    if cancel and cancel.is_set():
        return PlaceAndRouteResult(placement=placement, routing=result)

    # ── Step 3: rotation recovery ──────────────────────────────────
    log.info(
        "Routing failed (%d nets unrouted). Trying rotation variants…",
        len(result.failed_nets),
    )

    cat_map = {c.id: c for c in catalog.components}
    candidates = _candidates_to_try(placement, cat_map)

    if not candidates:
        log.info("No blocking auto-placed components found; cannot recover.")
        return PlaceAndRouteResult(placement=placement, routing=result)

    best_score = _routing_score(result)
    best_placement = placement
    best_result = result
    rotation_changes: list[RotationChange] = []
    attempts = 0

    for comp_idx, alt_rotations in candidates:
        if cancel and cancel.is_set():
            break

        original_comp = placement.components[comp_idx]
        original_rot = original_comp.rotation_deg

        for new_rot in alt_rotations:
            if attempts >= MAX_ROTATION_ATTEMPTS:
                break
            attempts += 1

            _progress(
                f"Trying {original_comp.instance_id} at {int(new_rot)}° "
                f"(attempt {attempts})…"
            )

            # Build a variant placement with only this component rotated
            new_comp = _recompute_pin_positions(original_comp, cat_map, new_rot)
            variant_components = list(placement.components)
            variant_components[comp_idx] = new_comp
            variant_placement = FullPlacement(
                components=variant_components,
                outline=placement.outline,
                nets=placement.nets,
                enclosure=placement.enclosure,
            )

            variant_result = route_traces(
                variant_placement, catalog,
                config=router_config,
                cancel=cancel,
            )

            score = _routing_score(variant_result)
            log.debug(
                "  %s @ %d°: failed=%d cells=%d",
                original_comp.instance_id, new_rot,
                score[0], score[1],
            )

            if score < best_score:
                best_score = score
                best_placement = variant_placement
                best_result = variant_result
                rotation_changes = [
                    RotationChange(
                        instance_id=original_comp.instance_id,
                        original_deg=original_rot,
                        new_deg=new_rot,
                    )
                ]

            if variant_result.ok:
                log.info(
                    "Routing succeeded after rotating %s to %d°.",
                    original_comp.instance_id, new_rot,
                )
                return PlaceAndRouteResult(
                    placement=best_placement,
                    routing=best_result,
                    rotation_changes=rotation_changes,
                )

        if attempts >= MAX_ROTATION_ATTEMPTS:
            break

    if rotation_changes:
        log.info(
            "Best rotation variant reduced failed nets to %d (was %d).",
            best_score[0], len(result.failed_nets),
        )
    else:
        log.info("No rotation variant improved routing.")

    return PlaceAndRouteResult(
        placement=best_placement,
        routing=best_result,
        rotation_changes=rotation_changes,
    )
