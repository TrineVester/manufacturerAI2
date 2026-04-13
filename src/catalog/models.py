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
class Cap:
    diameter_mm: float
    height_mm: float
    hole_clearance_mm: float


@dataclass
class Hatch:
    enabled: bool
    clearance_mm: float
    thickness_mm: float


@dataclass
class SoundHoles:
    enabled: bool
    pattern: str = "grid"              # "grid" | "ring"
    hole_diameter_mm: float = 1.5
    hole_spacing_mm: float = 3.0


@dataclass
class Mounting:
    style: str                          # "top" | "side" | "internal" | "bottom"
    allowed_styles: list[str]
    blocks_routing: bool
    keepout_margin_mm: float
    cap: Cap | None = None
    hatch: Hatch | None = None
    sound_holes: SoundHoles | None = None


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
    shape: str = "round"                # "round" | "rect" | "slot"
    shape_width_mm: float | None = None # for rect/slot: width (x dimension)
    shape_length_mm: float | None = None  # for rect/slot: length (y dimension)


@dataclass
class PinGroup:
    id: str
    pin_ids: list[str]
    description: str = ""
    fixed_net: str | None = None
    allocatable: bool = False
    capabilities: list[str] | None = None


@dataclass
class ExtraPart:
    """A companion printed piece (e.g. button cap, battery door)."""
    id: str
    name: str
    description: str = ""
    scad_module: str = ""               # OpenSCAD module name to generate shape


@dataclass
class Component:
    id: str
    name: str
    description: str
    ui_placement: bool
    body: Body
    mounting: Mounting
    pins: list[Pin]
    internal_nets: list[list[str]] = field(default_factory=list)
    pin_groups: list[PinGroup] | None = None
    configurable: dict | None = None
    extra_parts: list[ExtraPart] = field(default_factory=list)
    source_file: str = ""               # path of the JSON file (for error reporting)


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
