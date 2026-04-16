"""Design spec parsing — convert raw dicts/JSON into DesignSpec."""

from __future__ import annotations

from .models import (
    ComponentInstance, Net, OutlineVertex, Outline, UIPlacement, DesignSpec,
    Enclosure, TopSurface, BottomSurface, EdgeProfile,
    PhysicalDesign, CircuitDesign,
)
from .shape2d import tessellate_shape


def parse_design(data: dict) -> DesignSpec:
    """Parse a raw dict (from JSON / tool input) into a full DesignSpec.

    Expects a dict with both physical (shape/enclosure/ui_placements)
    and circuit (components/nets) fields. For parsing just one side,
    use parse_physical_design() or parse_circuit().
    """
    outline = tessellate_shape(data["shape"])
    return DesignSpec(
        components=_parse_components(data.get("components", [])),
        nets=_parse_nets(data.get("nets", [])),
        outline=outline,
        ui_placements=_parse_ui_placements(data.get("ui_placements", [])),
        enclosure=_parse_enclosure(data.get("enclosure") or {}),
    )


def _parse_outline(data: list, holes_data: list | None = None) -> Outline:
    """Parse outline from a flat list of vertex objects.

    Format:
        [{"x": 0, "y": 0}, {"x": 30, "y": 0}, {"x": 30, "y": 80, "ease_in": 8}]
        or with per-vertex ceiling heights:
        [{"x": 0, "y": 0, "z_top": 30}, {"x": 30, "y": 0, "z_top": 20}, ...]
    """
    points = _parse_vertex_list(data)
    holes: list[list[OutlineVertex]] = []
    if holes_data:
        for hole_ring in holes_data:
            holes.append(_parse_vertex_list(hole_ring))
    return Outline(points=points, holes=holes)


def _parse_vertex_list(data: list) -> list[OutlineVertex]:
    """Parse a list of vertex dicts into OutlineVertex objects."""
    points = []
    for v in data:
        raw_in = v.get("ease_in")
        raw_out = v.get("ease_out")
        if raw_in is not None and raw_out is None:
            raw_out = raw_in
        elif raw_out is not None and raw_in is None:
            raw_in = raw_out
        z_top_raw = v.get("z_top")
        z_bottom_raw = v.get("z_bottom")
        points.append(OutlineVertex(
            x=float(v["x"]),
            y=float(v["y"]),
            ease_in=float(raw_in) if raw_in else 0,
            ease_out=float(raw_out) if raw_out else 0,
            z_top=float(z_top_raw) if z_top_raw is not None else None,
            z_bottom=float(z_bottom_raw) if z_bottom_raw is not None else None,
        ))
    return points


def _parse_top_surface(data: dict) -> TopSurface:
    """Parse a top_surface descriptor dict into a TopSurface dataclass."""
    t = data.get("type", "flat")
    return TopSurface(
        type=t,
        peak_x_mm=data.get("peak_x_mm"),
        peak_y_mm=data.get("peak_y_mm"),
        peak_height_mm=data.get("peak_height_mm"),
        base_height_mm=data.get("base_height_mm"),
        x1=data.get("x1"),
        y1=data.get("y1"),
        x2=data.get("x2"),
        y2=data.get("y2"),
        crest_height_mm=data.get("crest_height_mm"),
        falloff_mm=data.get("falloff_mm"),
    )


def _parse_bottom_surface(data: dict) -> BottomSurface:
    """Parse a bottom_surface descriptor dict into a BottomSurface dataclass."""
    t = data.get("type", "flat")
    return BottomSurface(
        type=t,
        peak_x_mm=data.get("peak_x_mm"),
        peak_y_mm=data.get("peak_y_mm"),
        peak_height_mm=data.get("peak_height_mm"),
        base_height_mm=data.get("base_height_mm"),
        x1=data.get("x1"),
        y1=data.get("y1"),
        x2=data.get("x2"),
        y2=data.get("y2"),
        crest_height_mm=data.get("crest_height_mm"),
        falloff_mm=data.get("falloff_mm"),
    )


def _parse_edge_profile(data: dict | None) -> EdgeProfile:
    """Parse an edge_top/edge_bottom block. Returns a sharp (none) profile if absent."""
    if not data:
        return EdgeProfile()
    return EdgeProfile(
        type=str(data.get("type", "none")),
        size_mm=float(data.get("size_mm", 2.0)),
    )


def _parse_enclosure(data: dict) -> Enclosure:
    """Parse an enclosure block. Returns defaults if data is empty/missing."""
    height_mm = float(data.get("height_mm", 25.0))
    top_surface: TopSurface | None = None
    if "top_surface" in data and data["top_surface"]:
        top_surface = _parse_top_surface(data["top_surface"])
    bottom_surface: BottomSurface | None = None
    if "bottom_surface" in data and data["bottom_surface"]:
        bottom_surface = _parse_bottom_surface(data["bottom_surface"])
    edge_top    = _parse_edge_profile(data.get("edge_top"))
    edge_bottom = _parse_edge_profile(data.get("edge_bottom"))
    style_raw = data.get("enclosure_style", "solid")
    enclosure_style = style_raw if style_raw in ("solid", "two_part") else "solid"
    split_z_raw = data.get("split_z_mm")
    split_z_mm = float(split_z_raw) if split_z_raw is not None else None

    return Enclosure(
        height_mm=height_mm,
        top_surface=top_surface,
        bottom_surface=bottom_surface,
        edge_top=edge_top,
        edge_bottom=edge_bottom,
        enclosure_style=enclosure_style,
        split_z_mm=split_z_mm,
    )


def _parse_ui_placements(data: list) -> list[UIPlacement]:
    return [
        UIPlacement(
            instance_id=p["instance_id"],
            x_mm=float(p["x_mm"]),
            y_mm=float(p["y_mm"]),
            catalog_id=p.get("catalog_id"),
            edge_index=p.get("edge_index"),
            conform_to_surface=bool(p.get("conform_to_surface", True)),
            mounting_style=p.get("mounting_style"),
            button_shape=p.get("button_shape"),
            button_outline=p.get("button_outline"),
        )
        for p in data
    ]


def _parse_components(data: list) -> list[ComponentInstance]:
    return [
        ComponentInstance(
            catalog_id=c["catalog_id"],
            instance_id=c["instance_id"],
            config=c.get("config"),
            mounting_style=c.get("mounting_style"),
        )
        for c in data
    ]


def _parse_nets(data: list) -> list[Net]:
    return [Net(id=n["id"], pins=list(n["pins"])) for n in data]


def parse_physical_design(data: dict) -> PhysicalDesign:
    """Parse a design.json dict into a PhysicalDesign (no components/nets)."""
    if "shape" in data:
        outline = tessellate_shape(data["shape"])
    elif "outline" in data:
        outline = _parse_outline(data["outline"], data.get("holes"))
    else:
        raise KeyError("design must contain 'shape' or 'outline'")
    return PhysicalDesign(
        outline=outline,
        enclosure=_parse_enclosure(data.get("enclosure") or {}),
        ui_placements=_parse_ui_placements(data.get("ui_placements", [])),
        device_description=data.get("device_description", ""),
        name=data.get("name", ""),
    )


def parse_circuit(data: dict) -> CircuitDesign:
    """Parse a circuit.json dict into a CircuitDesign."""
    return CircuitDesign(
        components=_parse_components(data.get("components", [])),
        nets=_parse_nets(data.get("nets", [])),
    )


def build_design_spec(
    physical: PhysicalDesign,
    circuit: CircuitDesign,
) -> DesignSpec:
    """Merge a PhysicalDesign and CircuitDesign into a full DesignSpec.

    This is the explicit merge point for downstream pipeline steps
    (placer, router, validator) that need the complete picture.
    """
    return DesignSpec(
        components=circuit.components,
        nets=circuit.nets,
        outline=physical.outline,
        ui_placements=physical.ui_placements,
        enclosure=physical.enclosure,
    )
