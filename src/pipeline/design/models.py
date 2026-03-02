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

    If both ease values are 0 the corner is sharp.  If only one is
    provided at parse time, the other defaults to the same value
    (symmetric).
    """
    x: float
    y: float
    ease_in: float = 0
    ease_out: float = 0
    z_top: float | None = None              # per-vertex ceiling height; None = use enclosure default

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict, omitting default/None fields."""
        d: dict = {"x": self.x, "y": self.y}
        if self.ease_in:
            d["ease_in"] = self.ease_in
        if self.ease_out:
            d["ease_out"] = self.ease_out
        if self.z_top is not None:
            d["z_top"] = self.z_top
        return d


@dataclass
class Outline:
    """Device outline as a list of vertices, each with its own corner easing."""
    points: list[OutlineVertex]

    @property
    def vertices(self) -> list[tuple[float, float]]:
        """List of (x, y) tuples for polygon operations."""
        return [(p.x, p.y) for p in self.points]


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
    edge_top:    profile applied to the top edge of the wall (wall-to-lid
                 junction).  A chamfer creates a bevelled shoulder; a fillet
                 gives a smooth rounded rim.
    edge_bottom: profile applied to the bottom edge of the wall (wall-to-
                 floor junction).
    """
    height_mm: float = 25.0
    top_surface: TopSurface | None = None
    edge_top: EdgeProfile = field(default_factory=EdgeProfile)
    edge_bottom: EdgeProfile = field(default_factory=EdgeProfile)

    def to_dict(self) -> dict:
        d: dict = {"height_mm": self.height_mm}
        if self.top_surface is not None:
            d["top_surface"] = self.top_surface.to_dict()
        if self.edge_top.type != "none":
            d["edge_top"] = self.edge_top.to_dict()
        if self.edge_bottom.type != "none":
            d["edge_bottom"] = self.edge_bottom.to_dict()
        return d


# ── Placement / layout ─────────────────────────────────────────────────────────


@dataclass
class UIPlacement:
    instance_id: str
    x_mm: float
    y_mm: float
    edge_index: int | None = None       # side-mount: which outline edge (0-based)
    conform_to_surface: bool = True     # angle cutout to follow local surface normal


@dataclass
class DesignSpec:
    components: list[ComponentInstance]
    nets: list[Net]
    outline: Outline
    ui_placements: list[UIPlacement]
    enclosure: Enclosure = field(default_factory=Enclosure)
