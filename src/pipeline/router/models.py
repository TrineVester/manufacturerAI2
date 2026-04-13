"""Router output dataclasses and configuration constants."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.pipeline.config import TRACE_RULES


# ── Output dataclasses ─────────────────────────────────────────────


@dataclass
class Trace:
    """A routed trace segment belonging to a net."""

    net_id: str
    path: list[tuple[float, float]]    # waypoints in mm, Manhattan segments


@dataclass
class RoutingResult:
    """Complete routing result, ready for the SCAD generator."""

    traces: list[Trace]
    pin_assignments: dict[str, str]     # "mcu_1:gpio" -> "mcu_1:PD2"
    failed_nets: list[str]

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
    crossing_penalty: int = 500          # A* cost for crossing an occupied cell (rip-up)

    max_rip_up_attempts: int = 200       # outer random-ordering attempts (pruning skips dead orderings)
    inner_rip_up_limit: int = 100        # inner rip-up iterations per attempt
    time_budget_s: float = 60.0          # maximum wall-clock time for routing
    elite_pool_size: int = 5             # best solutions retained for crossover/refine
    stall_limit: int = 15               # attempts without improvement before escalation
    drc_repair_rounds: int = 5           # post-routing clearance repair iterations


# Module-level defaults (used when no RouterConfig is passed)
_DEFAULT_CFG = RouterConfig()

GRID_RESOLUTION_MM = _DEFAULT_CFG.grid_resolution_mm
TRACE_WIDTH_MM = _DEFAULT_CFG.trace_width_mm
TRACE_CLEARANCE_MM = _DEFAULT_CFG.trace_clearance_mm
EDGE_CLEARANCE_MM = _DEFAULT_CFG.edge_clearance_mm

TURN_PENALTY = _DEFAULT_CFG.turn_penalty
CROSSING_PENALTY = _DEFAULT_CFG.crossing_penalty

MAX_RIP_UP_ATTEMPTS = _DEFAULT_CFG.max_rip_up_attempts
INNER_RIP_UP_LIMIT = _DEFAULT_CFG.inner_rip_up_limit
TIME_BUDGET_S = _DEFAULT_CFG.time_budget_s
