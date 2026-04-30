"""Router output dataclasses and configuration constants."""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.pipeline.config import TRACE_RULES


# ── Output dataclasses ─────────────────────────────────────────────


@dataclass
class Trace:
    """A routed trace segment belonging to a net."""

    net_id: str
    path: list[tuple[float, float]]    # waypoints in mm, Manhattan segments


@dataclass
class InflatedTrace:
    """A trace after Voronoi inflation — variable-width 2D footprint."""

    net_id: str
    centreline: list[tuple[float, float]]
    polygon: Polygon


@dataclass
class RoutingResult:
    """Complete routing result, ready for the SCAD generator."""

    traces: list[Trace]
    pin_assignments: dict[str, str]     # "mcu_1:gpio" -> "mcu_1:PD2"
    failed_nets: list[str]
    debug_grids: list[dict] = field(default_factory=list)
    inflated_traces: list[InflatedTrace] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.failed_nets) == 0


# ── Router configuration ──────────────────────────────────────────
#
# Physical trace rules come from the shared pipeline config
# (src.pipeline.config.TRACE_RULES).  Router-only knobs live here.


@dataclass
class RouterConfig:
    """All tuneable router parameters in one place.

    Physical dimensions (trace width, clearances, grid resolution)
    are read from ``TRACE_RULES`` so they stay in sync with the placer.
    """

    # ── Physical rules (from shared config) ─────────────────────
    grid_resolution_mm: float = TRACE_RULES.grid_resolution_mm
    trace_width_mm: float = TRACE_RULES.trace_width_mm
    trace_clearance_mm: float = TRACE_RULES.trace_clearance_mm
    pin_clearance_mm: float = TRACE_RULES.pin_clearance_mm
    edge_clearance_mm: float = TRACE_RULES.edge_clearance_mm

    # ── Router-only knobs ──────────────────────────────────────
    turn_penalty: int = 5                # A* cost penalty for changing direction
    crossing_cost: int = 50              # A* cost for traversing foreign trace cells (exploration only)
    max_improve_iterations: int = 20     # iterative improvement rounds
    stall_limit: int = 8                # stop after this many rounds with no improvement
    elite_pool_size: int = 5             # number of top solutions kept for crossover
    max_routing_seconds: float = 20.0   # hard wall-clock cap on improvement loop


# Module-level defaults (used when no RouterConfig is passed)
_DEFAULT_CFG = RouterConfig()

GRID_RESOLUTION_MM = _DEFAULT_CFG.grid_resolution_mm
TRACE_WIDTH_MM = _DEFAULT_CFG.trace_width_mm
TRACE_CLEARANCE_MM = _DEFAULT_CFG.trace_clearance_mm
EDGE_CLEARANCE_MM = _DEFAULT_CFG.edge_clearance_mm

TURN_PENALTY = _DEFAULT_CFG.turn_penalty
