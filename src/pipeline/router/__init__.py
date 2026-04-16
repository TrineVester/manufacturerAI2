"""Router — Manhattan trace routing between component pads.

Submodules:
  models        Output dataclasses and configuration constants.
  grid          Discretized routing grid (free/blocked cells).
  pathfinder    A* pathfinding (point-to-point and point-to-tree).
  pins          Pin resolution and dynamic pin allocation.
  engine        Main routing algorithm (greedy with retry).
  serialization JSON conversion (routing_to_dict, parse_routing).
  bitmap        Trace bitmap generation (full-bed bitmap at nozzle pitch).
"""

from .models import Trace, InflatedTrace, RoutingResult, RouterConfig
from .engine import route_traces
from .serialization import routing_to_dict, parse_routing
from .bitmap import generate_trace_bitmap, generate_fixed_width_bitmap, write_trace_bitmap
from .drc import run_drc, DRCReport, Violation

__all__ = [
    # Models
    "Trace", "InflatedTrace", "RoutingResult", "RouterConfig",
    # Engine
    "route_traces",
    # Serialization
    "routing_to_dict", "parse_routing",
    # Bitmap
    "generate_trace_bitmap", "generate_fixed_width_bitmap", "write_trace_bitmap",
    # DRC
    "run_drc", "DRCReport", "Violation",
]
