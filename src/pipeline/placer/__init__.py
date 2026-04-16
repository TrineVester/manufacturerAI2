"""Placer — positions all components inside the device outline.

Submodules:
  models        Output dataclasses and configuration constants.
  geometry      Low-level geometry helpers (containment, clearance, AABB gaps).
  nets          Net connectivity graph for scoring.
  scoring       Candidate position scoring (net proximity, clearance, compactness).
  engine        Main placement algorithm (grid-search with hard/soft constraints).
  serialization JSON conversion (placement_to_dict, parse_placement).
"""

from .models import PlacedComponent, FullPlacement, PlacementError
from .engine import place_components
from .serialization import (
    placement_to_dict, parse_placement,
    parse_placed_components, assemble_full_placement,
)
from .geometry import (
    footprint_halfdims, footprint_envelope_halfdims,
    pin_world_xy, aabb_gap, rect_inside_polygon,
)

__all__ = [
    # Models
    "PlacedComponent", "FullPlacement", "PlacementError",
    # Engine
    "place_components",
    # Serialization
    "placement_to_dict", "parse_placement",
    "parse_placed_components", "assemble_full_placement",
    # Geometry (used by tests)
    "footprint_halfdims", "footprint_envelope_halfdims",
    "pin_world_xy", "aabb_gap", "rect_inside_polygon",
]
