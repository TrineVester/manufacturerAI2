"""Placer output dataclasses and configuration constants."""

from __future__ import annotations

from dataclasses import dataclass

from src.pipeline.design.models import Outline, Net, Enclosure


# ── Output dataclasses ─────────────────────────────────────────────


@dataclass
class PlacedComponent:
    """A component with a resolved world position and rotation."""

    instance_id: str
    catalog_id: str
    x_mm: float
    y_mm: float
    rotation_deg: int   # 0, 90, 180, 270


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

# Scoring weights — higher absolute value = more influence.
W_NET_PROXIMITY = 5.0       # MAIN driver: connected components close
W_EDGE_CLEARANCE = 0.5      # prefer safe distance from outline
W_COMPACTNESS = 0.3          # weakly prefer compact layouts
W_CLEARANCE_UNIFORM = 1.0   # prefer uniform gaps between components
W_BOTTOM_PREFERENCE = 0.08  # bottom-mount components prefer low Y
W_CROSSING = 50.0            # heavy penalty per inter-net crossing
W_PIN_COLLOCATION = 40.0     # heavy penalty per near-colliding pin pair
W_SPREAD = 0.6               # reward for spreading out when space allows
W_LARGE_EDGE_PULL = 0.3      # pulls large components toward outline edges
W_PIN_SIDE = 2.0             # penalty for approaching placed comp from wrong side
W_GROUP_COHESION = 1.5       # reward for staying near group-mates
