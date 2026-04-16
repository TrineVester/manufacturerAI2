"""Catalog dataclasses — typed representations of catalog/*.json entries."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BodyChannels:
    """Cylindrical channels carved into the body pocket (e.g. battery cells)."""
    axis: str                           # "x" | "y" — cylinder axis direction
    count: int
    diameter_mm: float
    spacing_mm: float                   # center-to-center distance between channels
    length_mm: float                    # cylinder length along *axis*
    center_z_mm: float                  # Z offset from cavity_start to cylinder centre


@dataclass
class Body:
    shape: str                          # "rect" | "circle"
    height_mm: float
    width_mm: float | None = None       # rect only
    length_mm: float | None = None      # rect only
    diameter_mm: float | None = None    # circle only
    channels: BodyChannels | None = None


@dataclass
class SwitchActuator:
    """Dimensions of the switch actuator the custom button snaps onto."""
    total_height_mm: float          # total switch height from PCB surface
    base_height_mm: float           # height of the square base portion
    cylinder_height_mm: float       # height of the cylinder on top of the base
    cylinder_diameter_mm: float     # outer diameter of the top cylinder


@dataclass
class Cap:
    diameter_mm: float
    height_mm: float
    hole_clearance_mm: float
    actuator: SwitchActuator | None = None


@dataclass
class ExtraPart:
    """A separate part printed on the same build plate alongside the enclosure.

    Geometry is described by shape + dimensions.  The generator renders
    any ExtraPart without needing to know its purpose.

    Special case: shape="button" delegates to the complex button generator
    (socket + stem + cap with surface tilt) since that cannot be described
    by simple extrusion.
    """
    label: str                          # human-readable, e.g. "battery hatch"
    shape: str                          # "rect" | "circle" | "button"
    width_mm: float | None = None
    length_mm: float | None = None
    thickness_mm: float | None = None
    diameter_mm: float | None = None


@dataclass
class Mounting:
    style: str                          # "top" | "side" | "internal" | "bottom"
    allowed_styles: list[str]
    blocks_routing: bool
    keepout_margin_mm: float
    cap: Cap | None = None
    installed_height_mm: float | None = None  # height of component top above cavity floor when installed (for through-hole DIP etc.)
    extras: list[ExtraPart] = field(default_factory=list)


@dataclass
class PinShape:
    """Optional non-circular pin geometry.

    type:
      "circle" — default round hole (uses Pin.hole_diameter_mm).
      "rect"   — rectangular pad / contact area.
      "slot"   — elongated slot (width × length, rounded ends).
    """
    type: str = "circle"                # "circle" | "rect" | "slot"
    width_mm: float | None = None       # rect / slot width
    length_mm: float | None = None      # rect / slot length


@dataclass
class Pin:
    id: str
    label: str
    position_mm: tuple[float, float]
    direction: str                      # "in" | "out" | "bidirectional"
    hole_diameter_mm: float
    description: str
    voltage_v: float | None = None
    current_max_ma: float | None = None
    shape: PinShape | None = None       # None → default circle from hole_diameter_mm


@dataclass
class PinGroup:
    id: str
    pin_ids: list[str]
    description: str = ""
    fixed_net: str | None = None
    allocatable: bool = False
    capabilities: list[str] | None = None


@dataclass
class ScadPattern:
    """Repeat pattern for a scad feature (e.g. grid of sound holes)."""
    type: str                           # "grid"
    spacing_mm: float
    clip_to_body: bool = True


@dataclass
class ScadFeature:
    """Additional cutout feature described in catalog JSON."""
    shape: str                          # "rect" | "circle"
    label: str
    position_mm: tuple[float, float]    # relative to component center
    width_mm: float | None = None       # rect
    length_mm: float | None = None      # rect
    diameter_mm: float | None = None    # circle
    depth_mm: float | None = None       # override; else uses cavity_depth
    z_anchor: str = "cavity_start"      # "ground" | "floor" | "cavity_start" | "ceil_start"
    z_center_mm: float | None = None    # explicit Z center offset from anchor (for rotated features)
    through_surface: bool = False       # cut through dome (e.g. shaft hole)
    rotate: tuple[float, float, float] | None = None  # [rx, ry, rz] 3-D rotation
    pattern: ScadPattern | None = None  # repeat pattern (e.g. grid of holes)


@dataclass
class Component:
    id: str
    name: str
    description: str
    ui_placement: bool
    body: Body
    mounting: Mounting
    pins: list[Pin]
    pin_length_mm: float | None = None
    internal_nets: list[list[str]] = field(default_factory=list)
    pin_groups: list[PinGroup] | None = None
    configurable: dict | None = None
    scad_features: list[ScadFeature] = field(default_factory=list)
    source_file: str = ""               # path of the JSON file (for error reporting)

    @property
    def protrusion_height_mm(self) -> float:
        """Tallest point above the cavity floor, including cap/actuator.

        Used for pause-Z calculation so the nozzle clears the full
        component (not just the body) after insertion.
        """
        base = self.mounting.installed_height_mm or self.body.height_mm
        cap = self.mounting.cap
        if cap is not None and cap.actuator is not None:
            top = cap.actuator.total_height_mm + cap.height_mm
            return max(top, base)
        return base


@dataclass
class ValidationError:
    component_id: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.component_id}] {self.field}: {self.message}"


@dataclass
class CatalogResult:
    """Result of loading the catalog — components + any validation errors."""
    components: list[Component]
    errors: list[ValidationError]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0
