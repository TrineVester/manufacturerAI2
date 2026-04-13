"""Design spec parsing — convert raw dicts/JSON into DesignSpec."""

from __future__ import annotations

from .models import (
    ComponentInstance, Net, OutlineVertex, Outline, UIPlacement, DesignSpec,
    Enclosure, TopSurface, EdgeProfile,
)


def parse_design(data: dict) -> DesignSpec:
    """Parse a raw dict (from JSON / tool input) into a DesignSpec."""
    components = [
        ComponentInstance(
            catalog_id=c["catalog_id"],
            instance_id=c["instance_id"],
            config=c.get("config"),
            mounting_style=c.get("mounting_style"),
        )
        for c in data.get("components", [])
    ]

    nets = [
        Net(id=n["id"], pins=list(n["pins"]))
        for n in data.get("nets", [])
    ]

    outline_data = data.get("outline", [])
    outline = _parse_outline(outline_data)

    ui_placements = [
        UIPlacement(
            instance_id=p["instance_id"],
            x_mm=float(p["x_mm"]),
            y_mm=float(p["y_mm"]),
            edge_index=p.get("edge_index"),
            conform_to_surface=bool(p.get("conform_to_surface", True)),
        )
        for p in data.get("ui_placements", [])
    ]

    enclosure = _parse_enclosure(data.get("enclosure") or {})

    return DesignSpec(
        components=components,
        nets=nets,
        outline=outline,
        ui_placements=ui_placements,
        enclosure=enclosure,
    )


def _parse_outline(data: list) -> Outline:
    """Parse outline from a flat list of vertex objects.

    Format:
        [{"x": 0, "y": 0}, {"x": 30, "y": 0}, {"x": 30, "y": 80, "ease_in": 8}]
        or with per-vertex ceiling heights:
        [{"x": 0, "y": 0, "z_top": 30}, {"x": 30, "y": 0, "z_top": 20}, ...]
    """
    points = []
    for v in data:
        raw_in = v.get("ease_in")
        raw_out = v.get("ease_out")
        # If only one side is given, mirror it to the other
        if raw_in is not None and raw_out is None:
            raw_out = raw_in
        elif raw_out is not None and raw_in is None:
            raw_in = raw_out
        z_top_raw = v.get("z_top")
        points.append(OutlineVertex(
            x=float(v["x"]),
            y=float(v["y"]),
            ease_in=float(raw_in) if raw_in else 0,
            ease_out=float(raw_out) if raw_out else 0,
            z_top=float(z_top_raw) if z_top_raw is not None else None,
        ))
    return Outline(points=points)


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
    edge_top    = _parse_edge_profile(data.get("edge_top"))
    edge_bottom = _parse_edge_profile(data.get("edge_bottom"))
    return Enclosure(
        height_mm=height_mm,
        top_surface=top_surface,
        edge_top=edge_top,
        edge_bottom=edge_bottom,
    )
