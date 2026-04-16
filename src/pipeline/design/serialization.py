"""Design spec serialization — convert DesignSpec to JSON-safe dicts."""

from __future__ import annotations

from .models import DesignSpec


def design_to_dict(spec: DesignSpec) -> dict:
    """Convert a DesignSpec to a JSON-serializable dict.

    Uses OutlineVertex.to_dict() so any future fields on the vertex
    (z_top, etc.) are automatically included without touching this function.
    """
    return {
        "components": [
            {
                "catalog_id": ci.catalog_id,
                "instance_id": ci.instance_id,
                **({
                    "config": ci.config} if ci.config else {}),
                **({
                    "mounting_style": ci.mounting_style} if ci.mounting_style else {}),
            }
            for ci in spec.components
        ],
        "nets": [
            {"id": n.id, "pins": n.pins}
            for n in spec.nets
        ],
        "outline": [p.to_dict() for p in spec.outline.points],
        **({"holes": [
            [p.to_dict() for p in hole]
            for hole in spec.outline.holes
        ]} if spec.outline.holes else {}),
        "ui_placements": [
            {
                "instance_id": p.instance_id,
                "x_mm": p.x_mm,
                "y_mm": p.y_mm,
                **({
                    "edge_index": p.edge_index} if p.edge_index is not None else {}),
                **({
                    "conform_to_surface": p.conform_to_surface}
                   if not p.conform_to_surface else {}),  # only write if non-default
                **({
                    "button_shape": p.button_shape}
                   if p.button_shape is not None else {}),
            }
            for p in spec.ui_placements
        ],
        "enclosure": spec.enclosure.to_dict(),
    }
