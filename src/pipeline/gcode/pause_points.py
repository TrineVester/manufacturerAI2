"""
Compute pause Z-heights from the enclosure geometry and component data.

The print has multiple pause stages:

1. **Ink layer** — the top of the solid floor, where traces start.
   The printer irons this surface, then conductive ink is deposited.

2. **Component insertion pauses** — one or more pauses at increasing
   Z-heights where groups of components are inserted.  Short
   components go in early (low walls, easy access); tall components
   wait for later pauses.

Heights are in mm from Z=0 (build plate) and snapped to the nearest
layer boundary for the configured layer height.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.pipeline.config import (
    FLOOR_MM,
    CAVITY_START_MM,
    CEILING_MM,
    component_z_range,
)


@dataclass
class ComponentPauseInfo:
    """Minimal data needed for pause-point grouping."""

    instance_id: str
    body_height_mm: float
    mounting_style: str = "internal"
    pin_length_mm: float | None = None


@dataclass
class PausePoint:
    """A single pause in the multi-stage print."""

    z: float
    layer_number: int
    label: str
    components: list[str] = field(default_factory=list)


@dataclass
class PausePoints:
    """All pause points for a multi-stage print."""

    pauses: list[PausePoint]
    total_height: float
    layer_height: float

    @property
    def ink_layer_z(self) -> float:
        return self.pauses[0].z

    @property
    def ink_layer_number(self) -> int:
        return self.pauses[0].layer_number

    @property
    def component_pauses(self) -> list[PausePoint]:
        """All non-ink, non-jumper pauses (component insertion stages)."""
        return [p for p in self.pauses if p.label not in ("ink", "jumpers")]


def _snap_to_layer(z: float, layer_h: float) -> float:
    """Round *z* down to the nearest layer boundary."""
    return math.floor(z / layer_h) * layer_h


def pause_z_for_component(
    body_height_mm: float,
    shell_height: float,
    layer_height: float = 0.2,
    mounting_style: str = "internal",
    pin_length_mm: float | None = None,
) -> float:
    """Return the pause Z at which a component is inserted.

    The pause is placed at the component body top — the last printed
    layer before the component cavity is closed off.
    """
    ceil_start = shell_height - CEILING_MM
    _, body_top = component_z_range(mounting_style, body_height_mm, pin_length_mm, ceil_start)
    return _snap_to_layer(min(body_top, ceil_start), layer_height)


def compute_pause_points(
    shell_height: float,
    layer_height: float = 0.2,
    components: list[ComponentPauseInfo] | None = None,
    jumper_count: int = 0,
) -> PausePoints:
    """Determine pause Z-heights for the multi-stage print.

    Each component's pause Z is derived from its body top via
    ``component_z_range``.  Components sharing the same Z are
    merged into a single pause.

    When *jumper_count* > 0 a "jumpers" pause is inserted at
    ``CAVITY_START_MM`` so jumper wires can be placed in their
    channels before cavity walls cover them.
    """
    ceil_start = shell_height - CEILING_MM

    ink_z = _snap_to_layer(FLOOR_MM, layer_height)
    ink_layer = round(ink_z / layer_height)

    pauses: list[PausePoint] = [
        PausePoint(z=ink_z, layer_number=ink_layer, label="ink"),
    ]

    if jumper_count > 0:
        jumper_z = _snap_to_layer(CAVITY_START_MM, layer_height)
        jumper_layer = round(jumper_z / layer_height)
        pauses.append(PausePoint(
            z=jumper_z, layer_number=jumper_layer, label="jumpers",
        ))

    if components:
        z_groups: dict[float, list[str]] = {}
        for c in components:
            z = pause_z_for_component(
                c.body_height_mm, shell_height, layer_height,
                mounting_style=c.mounting_style, pin_length_mm=c.pin_length_mm,
            )
            z_groups.setdefault(z, []).append(c.instance_id)

        for z in sorted(z_groups):
            layer_num = round(z / layer_height)
            pauses.append(PausePoint(
                z=z, layer_number=layer_num,
                label="components",
                components=z_groups[z],
            ))
    else:
        comp_z = _snap_to_layer(ceil_start, layer_height)
        comp_layer = round(comp_z / layer_height)
        pauses.append(PausePoint(
            z=comp_z, layer_number=comp_layer,
            label="components",
        ))

    return PausePoints(
        pauses=pauses,
        total_height=shell_height,
        layer_height=layer_height,
    )
