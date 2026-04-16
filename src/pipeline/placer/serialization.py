"""Placement serialization — JSON conversion."""

from __future__ import annotations

from src.pipeline.design.models import Net, Outline, Enclosure

from .models import PlacedComponent, FullPlacement


def placement_to_dict(fp: FullPlacement) -> dict:
    """Serialize just the placer's own output (component positions)."""
    return {
        "components": [
            {
                "instance_id": c.instance_id,
                "catalog_id": c.catalog_id,
                "x_mm": c.x_mm,
                "y_mm": c.y_mm,
                "rotation_deg": c.rotation_deg,
                "pin_positions": {
                    pid: list(pos) for pid, pos in c.pin_positions.items()
                },
                "mounting_style": c.mounting_style,
                **({"button_outline": c.button_outline}
                   if c.button_outline is not None else {}),
            }
            for c in fp.components
        ],
    }


def parse_placed_components(data: dict) -> list[PlacedComponent]:
    """Parse just the component list from a placement artifact."""
    return [
        PlacedComponent(
            instance_id=c["instance_id"],
            catalog_id=c["catalog_id"],
            x_mm=c["x_mm"],
            y_mm=c["y_mm"],
            rotation_deg=c["rotation_deg"],
            pin_positions={
                pid: tuple(pos)
                for pid, pos in c.get("pin_positions", {}).items()
            },
            mounting_style=c.get("mounting_style", "top"),
            button_outline=c.get("button_outline"),
        )
        for c in data["components"]
    ]


def assemble_full_placement(
    placement_data: dict,
    outline: Outline,
    nets: list[Net],
    enclosure: Enclosure,
) -> FullPlacement:
    """Assemble a FullPlacement from lean placement data + upstream design/circuit data."""
    return FullPlacement(
        components=parse_placed_components(placement_data),
        outline=outline,
        nets=nets,
        enclosure=enclosure,
    )


def parse_placement(data: dict) -> FullPlacement:
    """Parse a legacy fat placement.json dict that includes outline/nets/enclosure.

    Kept for backward compatibility with existing session artifacts.
    Prefer assemble_full_placement() for new code paths.
    """
    from src.pipeline.design.parsing import _parse_outline, _parse_enclosure

    components = parse_placed_components(data)
    outline = _parse_outline(data["outline"])
    enclosure = _parse_enclosure(data.get("enclosure") or {})

    nets = [
        Net(id=n["id"], pins=list(n["pins"]))
        for n in data.get("nets", [])
    ]

    return FullPlacement(
        components=components,
        outline=outline,
        nets=nets,
        enclosure=enclosure,
    )
