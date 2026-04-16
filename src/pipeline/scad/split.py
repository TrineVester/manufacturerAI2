"""split.py — compute the Z split height for two-part enclosures."""

from __future__ import annotations

import logging

from src.catalog.models import Component
from src.pipeline.config import CAVITY_START_MM, CEILING_MM, FLOOR_MM, TRACE_RULES
from src.pipeline.design.models import Enclosure
from src.pipeline.placer.models import PlacedComponent

log = logging.getLogger(__name__)

_DEFAULT_SPLIT_Z: float = CAVITY_START_MM + TRACE_RULES.pinhole_taper_depth_mm + 0.5


def compute_split_z(
    enclosure: Enclosure,
    components: list[PlacedComponent],
    cat_index: dict[str, Component],
) -> float:
    """Compute the Z height where bottom and top halves meet.

    If ``enclosure.split_z_mm`` is explicitly set, use that (clamped).
    Otherwise the split is at CAVITY_START_MM + funnel_taper + margin
    (≈4.2 mm) so the bottom tray is thick enough for pin funnels and
    trace channels on its top surface.

    Returns the split Z in mm from the build plate.
    """
    base_h = enclosure.height_mm
    ceil_start = base_h - CEILING_MM

    if enclosure.split_z_mm is not None:
        split_z = enclosure.split_z_mm
    else:
        split_z = _DEFAULT_SPLIT_Z

    # Clamp: at least FLOOR_MM, at least 3 mm below ceiling
    lo = FLOOR_MM
    hi = ceil_start - 3.0
    split_z = max(lo, min(split_z, hi))

    log.info(
        "Two-part split Z: %.2f mm (default=%.2f, ceil_start=%.2f)",
        split_z, _DEFAULT_SPLIT_Z, ceil_start,
    )
    return split_z
