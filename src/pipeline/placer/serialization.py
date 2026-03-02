"""Placement serialization — JSON conversion."""

from __future__ import annotations

from src.pipeline.design.models import Net

from .models import PlacedComponent, FullPlacement


def placement_to_dict(fp: FullPlacement) -> dict:
    """Serialize a FullPlacement to a JSON-safe dict."""
    return {
        "components": [
            {
                "instance_id": c.instance_id,
                "catalog_id": c.catalog_id,
                "x_mm": c.x_mm,
                "y_mm": c.y_mm,
                "rotation_deg": c.rotation_deg,
            }
            for c in fp.components
        ],
        # Use to_dict() so z_top and any future vertex fields propagate automatically
        "outline": [p.to_dict() for p in fp.outline.points],
        "nets": [
            {"id": n.id, "pins": n.pins}
            for n in fp.nets
        ],
        "enclosure": fp.enclosure.to_dict(),
    }


def parse_placement(data: dict) -> FullPlacement:
    """Parse a placement.json dict back into a FullPlacement."""
    from src.pipeline.design.parsing import _parse_outline, _parse_enclosure

    components = [
        PlacedComponent(
            instance_id=c["instance_id"],
            catalog_id=c["catalog_id"],
            x_mm=c["x_mm"],
            y_mm=c["y_mm"],
            rotation_deg=c["rotation_deg"],
        )
        for c in data["components"]
    ]

    outline = _parse_outline(data["outline"])
    enclosure = _parse_enclosure(data.get("enclosure") or {})

    nets = [
        Net(id=n["id"], pins=list(n["pins"]))
        for n in data["nets"]
    ]

    return FullPlacement(
        components=components,
        outline=outline,
        nets=nets,
        enclosure=enclosure,
    )
