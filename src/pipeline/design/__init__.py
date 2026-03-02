"""Design spec — dataclasses, parsing, validation, and serialization."""

from .models import (
    ComponentInstance, Net, OutlineVertex, Outline, UIPlacement, DesignSpec,
    TopSurface, Enclosure,
)
from .parsing import parse_design
from .validation import validate_design
from .serialization import design_to_dict
from .height_field import (
    blended_height, sample_height_grid, surface_normal_at,
)

__all__ = [
    # Models
    "ComponentInstance", "Net", "OutlineVertex", "Outline",
    "UIPlacement", "DesignSpec", "TopSurface", "Enclosure",
    # Parsing / Validation / Serialization
    "parse_design", "validate_design", "design_to_dict",
    # Height field
    "blended_height", "sample_height_grid", "surface_normal_at",
]
