"""Design spec dataclasses — the agent's output structure."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ComponentInstance:
    catalog_id: str
    instance_id: str
    config: dict | None = None
    mounting_style: str | None = None       # override from allowed_styles


@dataclass
class Net:
    id: str
    pins: list[str]     # "instance_id:pin_id" or "instance_id:group_id" for dynamic


@dataclass
class OutlineVertex:
    """A single vertex with optional corner easing and ceiling height.

    ease_in:  mm along the incoming edge (from prev vertex) where the
              curve begins.  0 = no easing on that side.
    ease_out: mm along the outgoing edge (to next vertex) where the
              curve ends.    0 = no easing on that side.
    z_top:    ceiling height (mm) at this vertex.  None means inherit
              from Enclosure.height_mm (the default enclosure height).
    z_bottom: floor height (mm) at this vertex.  None means 0.0
              (flat on the build plate).  When set the bottom
              surface is raised at this vertex, enabling sculpted
              undersides (boat-hull, raised pedestal, etc.).

    If both ease values are 0 the corner is sharp.  If only one is
    provided at parse time, the other defaults to the same value
    (symmetric).
    """
    x: float
    y: float
    ease_in: float = 0
    ease_out: float = 0
    z_top: float | None = None              # per-vertex ceiling height; None = use enclosure default
    z_bottom: float | None = None           # per-vertex floor height; None = 0.0

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict, omitting default/None fields."""
        d: dict = {"x": self.x, "y": self.y}
        if self.ease_in:
            d["ease_in"] = self.ease_in
        if self.ease_out:
            d["ease_out"] = self.ease_out
        if self.z_top is not None:
            d["z_top"] = self.z_top
        if self.z_bottom is not None:
            d["z_bottom"] = self.z_bottom
        return d


@dataclass
class Outline:
    """Device outline as a list of vertices, each with its own corner easing.

    holes: optional interior cutout rings.  Each hole is a closed polygon
    that is subtracted from the outer boundary, creating a through-hole in
    the enclosure (e.g. between tree branches, decorative openings).
    """
    points: list[OutlineVertex]
    holes: list[list[OutlineVertex]] = field(default_factory=list)

    @property
    def vertices(self) -> list[tuple[float, float]]:
        """List of (x, y) tuples for the outer ring."""
        return [(p.x, p.y) for p in self.points]

    @property
    def hole_vertices(self) -> list[list[tuple[float, float]]]:
        """List of (x, y) tuple lists for each interior hole."""
        return [[(p.x, p.y) for p in hole] for hole in self.holes]


# ── 3D shape descriptors ───────────────────────────────────────────────────────


@dataclass
class TopSurface:
    """An optional smooth bump layered on top of the per-vertex ceiling heights.

    type:
      "flat"  — no additional bump; the ceiling is purely the per-vertex z_top
                interpolation. This is the default.
      "dome"  — a Gaussian-like rounded peak rising above a flat base.
      "ridge" — a cylindrical crest running along an axis, falling off on
                each side.

    All positional fields are in mm, in the same screen-convention XY
    coordinate system as the outline (x right, y down).
    """
    type: str = "flat"                  # "flat" | "dome" | "ridge"

    # ── Dome params (used when type == "dome") ──
    peak_x_mm: float | None = None      # XY position of the peak
    peak_y_mm: float | None = None
    peak_height_mm: float | None = None # absolute Z height of the peak
    # base_height_mm: the flat Z level the dome rises *from* (usually == enclosure height_mm)
    base_height_mm: float | None = None

    # ── Ridge params (used when type == "ridge") ──
    x1: float | None = None             # crest line start
    y1: float | None = None
    x2: float | None = None             # crest line end
    y2: float | None = None
    crest_height_mm: float | None = None    # absolute Z at the crest
    falloff_mm: float | None = None     # distance (mm) from crest where height reaches base

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        for attr in (
            "peak_x_mm", "peak_y_mm", "peak_height_mm", "base_height_mm",
            "x1", "y1", "x2", "y2", "crest_height_mm", "falloff_mm",
        ):
            v = getattr(self, attr)
            if v is not None:
                d[attr] = v
        return d


@dataclass
class BottomSurface:
    """Optional smooth bump applied to the bottom (floor) of the enclosure.

    Mirrors :class:`TopSurface` but affects the *floor* height instead of
    the ceiling.  A bump here **raises** the floor (pushes it away from
    z=0), creating a contoured underside.

    type:
      "flat"  — no additional bump; the floor is purely the per-vertex
                z_bottom interpolation.  This is the default.
      "dome"  — a Gaussian-like rounded peak raising the floor at a point.
      "ridge" — a cylindrical crest raising the floor along a line.

    All positional fields are in mm, same coordinate system as the outline.
    """
    type: str = "flat"                  # "flat" | "dome" | "ridge"

    # ── Dome params (used when type == "dome") ──
    peak_x_mm: float | None = None      # XY position of the peak
    peak_y_mm: float | None = None
    peak_height_mm: float | None = None  # absolute Z height of the dome peak
    base_height_mm: float | None = None  # flat Z level the dome rises from (usually 0)

    # ── Ridge params (used when type == "ridge") ──
    x1: float | None = None
    y1: float | None = None
    x2: float | None = None
    y2: float | None = None
    crest_height_mm: float | None = None
    falloff_mm: float | None = None

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        for attr in (
            "peak_x_mm", "peak_y_mm", "peak_height_mm", "base_height_mm",
            "x1", "y1", "x2", "y2", "crest_height_mm", "falloff_mm",
        ):
            v = getattr(self, attr)
            if v is not None:
                d[attr] = v
        return d


@dataclass
class EdgeProfile:
    """Profile applied to the top or bottom edge of the enclosure wall.

    type:
      "none"    — a sharp right-angle edge (default).
      "chamfer" — a flat 45° bevel: size_mm wide and size_mm tall.
      "fillet"  — a smooth quarter-circle arc of radius size_mm.

    size_mm: size of the chamfer width / fillet radius in mm.  Defaults to
             2.0 mm.  Automatically clamped to at most 45% of the local
             wall height to prevent the top and bottom profiles overlapping.
    """
    type: str = "none"       # "none" | "chamfer" | "fillet"
    size_mm: float = 2.0

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        if self.type != "none":
            d["size_mm"] = self.size_mm
        return d


@dataclass
class Enclosure:
    """Top-level enclosure shape descriptor.

    height_mm:   default ceiling height for vertices that omit z_top.
                 Also the minimum Z level everywhere on the top surface
                 (the top_surface descriptor can only add height, never
                 subtract below this value).
    top_surface: optional smooth bump added on top of the vertex-height
                 linear interpolation.
    bottom_surface: optional smooth bump applied to the floor. Raises
                 the floor above the default z_bottom interpolation.
    edge_top:    profile applied to the top edge of the wall (wall-to-lid
                 junction).  A chamfer creates a bevelled shoulder; a fillet
                 gives a smooth rounded rim.
    edge_bottom: profile applied to the bottom edge of the wall (wall-to-
                 floor junction).
    """
    height_mm: float = 25.0
    top_surface: TopSurface | None = None
    bottom_surface: BottomSurface | None = None
    edge_top: EdgeProfile = field(default_factory=EdgeProfile)
    edge_bottom: EdgeProfile = field(default_factory=EdgeProfile)
    enclosure_style: str = "solid"          # "solid" | "two_part"
    split_z_mm: float | None = None         # custom split height (auto-computed if None)

    def to_dict(self) -> dict:
        d: dict = {"height_mm": self.height_mm}
        if self.top_surface is not None:
            d["top_surface"] = self.top_surface.to_dict()
        if self.bottom_surface is not None:
            d["bottom_surface"] = self.bottom_surface.to_dict()
        if self.edge_top.type != "none":
            d["edge_top"] = self.edge_top.to_dict()
        if self.edge_bottom.type != "none":
            d["edge_bottom"] = self.edge_bottom.to_dict()
        if self.enclosure_style != "solid":
            d["enclosure_style"] = self.enclosure_style
        if self.split_z_mm is not None:
            d["split_z_mm"] = self.split_z_mm
        return d


# ── Placement / layout ─────────────────────────────────────────────────────────


@dataclass
class UIPlacement:
    instance_id: str
    x_mm: float
    y_mm: float
    catalog_id: str | None = None       # which catalog component
    edge_index: int | None = None       # side-mount: which outline edge (0-based)
    conform_to_surface: bool = True     # angle cutout to follow local surface normal
    mounting_style: str | None = None   # override from allowed_styles
    button_shape: dict | None = None    # CSG shape tree for button cap
    button_outline: list[list[float]] | None = None  # raw point-list outline [[x,y], ...]


@dataclass
class PhysicalDesign:
    """What design.json stores — the physical shape and UI component placements.

    This is the output of the design agent. No electrical components or nets.
    """
    outline: Outline
    enclosure: Enclosure = field(default_factory=Enclosure)
    ui_placements: list[UIPlacement] = field(default_factory=list)
    device_description: str = ""
    name: str = ""


@dataclass
class CircuitDesign:
    """What circuit.json stores — component instances and electrical nets.

    This is the output of the circuit agent.
    """
    components: list[ComponentInstance] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)


@dataclass
class DesignSpec:
    """Full merged design — physical + circuit combined.

    Constructed via build_design_spec(physical, circuit) for downstream
    pipeline steps (placer, router, validator) that need everything.
    """
    components: list[ComponentInstance]
    nets: list[Net]
    outline: Outline
    ui_placements: list[UIPlacement]
    enclosure: Enclosure = field(default_factory=Enclosure)
