"""Placer output dataclasses and configuration constants."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.pipeline.design.models import Outline, Net, Enclosure


# ── Output dataclasses ─────────────────────────────────────────────


@dataclass
class PlacedComponent:
    """A component with a resolved world position and rotation."""

    instance_id: str
    catalog_id: str
    x_mm: float
    y_mm: float
    rotation_deg: float   # degrees, arbitrary for side-mount; 0/90/180/270 for internal
    pin_positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    mounting_style: str = "top"
    button_outline: list[list[float]] | None = None  # custom button shape [[x,y], ...]


@dataclass
class FullPlacement:
    """Complete placement of all components, ready for the router."""

    components: list[PlacedComponent]
    outline: Outline
    nets: list[Net]
    enclosure: Enclosure = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.enclosure is None:
            self.enclosure = Enclosure()


class PlacementError(Exception):
    """Raised when a component cannot be placed inside the outline."""

    def __init__(self, instance_id: str, catalog_id: str, reason: str) -> None:
        self.instance_id = instance_id
        self.catalog_id = catalog_id
        self.reason = reason
        super().__init__(f"Cannot place '{instance_id}' ({catalog_id}): {reason}")


# ── Configuration ──────────────────────────────────────────────────

from src.pipeline.config import TRACE_RULES

GRID_STEP_MM = 1.0          # grid scan resolution (mm)
VALID_ROTATIONS = (0, 90, 180, 270)

# ── Derived from shared TraceRules (src.pipeline.config) ───────────
# Changing TRACE_RULES automatically updates these.
MIN_EDGE_CLEARANCE_MM = TRACE_RULES.min_edge_clearance_mm
ROUTING_CHANNEL_MM = TRACE_RULES.routing_channel_mm
MIN_PIN_CLEARANCE_MM = TRACE_RULES.min_pin_clearance_mm


@dataclass
class Placed:
    """Tracking info for a placed component during the algorithm."""

    instance_id: str
    catalog_id: str
    x: float
    y: float
    rotation: int
    hw: float       # half width (rotated body)
    hh: float       # half height (rotated body)
    keepout: float   # keepout_margin_mm
    env_hw: float = 0.0   # half width of pin-inclusive envelope
    env_hh: float = 0.0   # half height of pin-inclusive envelope

    def __post_init__(self) -> None:
        if self.env_hw == 0.0:
            self.env_hw = self.hw
        if self.env_hh == 0.0:
            self.env_hh = self.hh
