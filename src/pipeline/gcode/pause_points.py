"""Pause-point computation for the multi-pause printing workflow.

The enclosure is printed in stages:
  1. Print floor (0 → FLOOR_MM)
  2. Print trace channels (FLOOR_MM → FLOOR_MM + TRACE_HEIGHT_MM)
  3. **INK PAUSE** — slide bed under Xaar 128, deposit silver ink
  4. Print cavity walls around components
  5. **COMPONENT PAUSES** — user inserts through-hole components
  6. Print ceiling / top surface

This module computes the Z-heights and layer numbers for each pause.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# ── Physical Z constants (must match cutouts.py / SCAD layers) ────

FLOOR_MM: float = 2.0            # solid floor height
TRACE_HEIGHT_MM: float = 1.0     # trace channel depth above floor
CAVITY_START_MM: float = 3.0     # = FLOOR_MM + TRACE_HEIGHT_MM (approximately)
CEILING_MM: float = 2.0          # solid ceiling thickness


@dataclass
class PausePoint:
    """A single pause in the G-code stream."""

    z: float                      # Z-height in mm where pause is inserted
    layer_number: int             # layer index at this Z (0-based)
    label: str                    # "ink", "components", "jumpers"
    components: list[str] = field(default_factory=list)  # component IDs for user


@dataclass
class PausePoints:
    """All computed pauses for a print."""

    pauses: list[PausePoint]
    total_height: float           # enclosure total Z in mm
    layer_height: float           # slicer layer height

    @property
    def ink_pause(self) -> PausePoint | None:
        for p in self.pauses:
            if p.label == "ink":
                return p
        return None

    @property
    def ink_layer_z(self) -> float:
        p = self.ink_pause
        return p.z if p else FLOOR_MM

    @property
    def component_pauses(self) -> list[PausePoint]:
        return [p for p in self.pauses if p.label == "components"]


@dataclass
class ComponentPauseInfo:
    """Per-component info needed to compute insertion pauses."""

    instance_id: str
    body_height_mm: float
    pin_length_mm: float = 0.0
    mounting_style: str = "internal"  # "top", "internal", "bottom"


def _snap_to_layer(z: float, layer_h: float) -> tuple[float, int]:
    """Snap a Z-height to the nearest layer boundary.

    Returns (snapped_z, layer_number).
    """
    layer = max(0, round(z / layer_h))
    return round(layer * layer_h, 4), layer


def compute_pause_points(
    shell_height: float,
    layer_height: float = 0.2,
    components: list[ComponentPauseInfo] | None = None,
) -> PausePoints:
    """Compute all pause Z-heights for the enclosure.

    Parameters
    ----------
    shell_height : float
        Total enclosure height in mm.
    layer_height : float
        Slicer layer height in mm (default 0.2).
    components : list[ComponentPauseInfo] | None
        Components that need insertion pauses.

    Returns
    -------
    PausePoints with ink pause + one or more component insertion pauses.
    """
    pauses: list[PausePoint] = []

    # ── Ink pause: one layer above trace channel roof ──
    trace_roof_z = FLOOR_MM + TRACE_HEIGHT_MM
    ink_z, ink_layer = _snap_to_layer(trace_roof_z, layer_height)
    # Insert pause one layer above the trace roof so ink is sealed
    ink_z += layer_height
    ink_layer += 1
    pauses.append(PausePoint(
        z=round(ink_z, 4),
        layer_number=ink_layer,
        label="ink",
    ))

    # ── Component insertion pauses ──
    if components:
        ceil_start = shell_height - CEILING_MM

        # Group components by their insertion Z
        z_groups: dict[float, list[str]] = {}
        for c in components:
            if c.mounting_style == "top":
                # Top-mount: inserted from above after ceiling is printed
                insert_z = shell_height
            else:
                # Internal: body sits above floor, insert when cavity reaches body top
                body_floor = FLOOR_MM + c.pin_length_mm
                body_top = body_floor + c.body_height_mm
                insert_z = min(body_top + 0.5, ceil_start)  # +0.5mm margin

            snapped_z, _ = _snap_to_layer(insert_z, layer_height)
            z_groups.setdefault(snapped_z, []).append(c.instance_id)

        for z in sorted(z_groups):
            _, layer_num = _snap_to_layer(z, layer_height)
            pauses.append(PausePoint(
                z=z,
                layer_number=layer_num,
                label="components",
                components=z_groups[z],
            ))

    # Sort all pauses by Z
    pauses.sort(key=lambda p: p.z)

    return PausePoints(
        pauses=pauses,
        total_height=shell_height,
        layer_height=layer_height,
    )
