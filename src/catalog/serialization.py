"""Catalog serialization — convert dataclasses to JSON-safe dicts."""

from __future__ import annotations

from typing import Any

from .models import Component, CatalogResult


def catalog_to_dict(result: CatalogResult) -> dict:
    """Serialize a CatalogResult to a JSON-safe dict for the web API."""
    return {
        "ok": result.ok,
        "component_count": len(result.components),
        "components": [component_to_dict(c) for c in result.components],
        "errors": [{"component_id": e.component_id, "field": e.field, "message": e.message}
                   for e in result.errors],
    }


def component_to_dict(c: Component) -> dict:
    """Serialize a Component to a JSON-safe dict."""
    d: dict[str, Any] = {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "ui_placement": c.ui_placement,
        "body": {
            "shape": c.body.shape,
            "height_mm": c.body.height_mm,
        },
        "mounting": {
            "style": c.mounting.style,
            "allowed_styles": c.mounting.allowed_styles,
            "blocks_routing": c.mounting.blocks_routing,
            "keepout_margin_mm": c.mounting.keepout_margin_mm,
        },
        "pins": [
            {
                "id": p.id,
                "label": p.label,
                "position_mm": list(p.position_mm),
                "direction": p.direction,
                "voltage_v": p.voltage_v,
                "current_max_ma": p.current_max_ma,
                "hole_diameter_mm": p.hole_diameter_mm,
                "description": p.description,
                **({
                    "shape": p.shape,
                    "shape_width_mm": p.shape_width_mm,
                    "shape_length_mm": p.shape_length_mm,
                } if p.shape != "round" else {}),
            }
            for p in c.pins
        ],
        "internal_nets": c.internal_nets,
        "source_file": c.source_file,
    }

    # Body shape-specific fields
    if c.body.width_mm is not None:
        d["body"]["width_mm"] = c.body.width_mm
    if c.body.length_mm is not None:
        d["body"]["length_mm"] = c.body.length_mm
    if c.body.diameter_mm is not None:
        d["body"]["diameter_mm"] = c.body.diameter_mm

    # Body channels
    if c.body.channels:
        ch = c.body.channels
        d["body"]["channels"] = {
            "axis": ch.axis,
            "count": ch.count,
            "diameter_mm": ch.diameter_mm,
            "spacing_mm": ch.spacing_mm,
            "length_mm": ch.length_mm,
            "center_z_mm": ch.center_z_mm,
        }

    # Optional mounting sub-objects
    if c.mounting.cap:
        d["mounting"]["cap"] = {
            "diameter_mm": c.mounting.cap.diameter_mm,
            "height_mm": c.mounting.cap.height_mm,
            "hole_clearance_mm": c.mounting.cap.hole_clearance_mm,
        }
    if c.mounting.hatch:
        d["mounting"]["hatch"] = {
            "enabled": c.mounting.hatch.enabled,
            "clearance_mm": c.mounting.hatch.clearance_mm,
            "thickness_mm": c.mounting.hatch.thickness_mm,
        }
    if c.mounting.sound_holes:
        d["mounting"]["sound_holes"] = {
            "enabled": c.mounting.sound_holes.enabled,
            "pattern": c.mounting.sound_holes.pattern,
            "hole_diameter_mm": c.mounting.sound_holes.hole_diameter_mm,
            "hole_spacing_mm": c.mounting.sound_holes.hole_spacing_mm,
        }

    # Optional fields
    if c.pin_groups:
        d["pin_groups"] = [
            {
                "id": g.id,
                "pin_ids": g.pin_ids,
                "description": g.description,
                "fixed_net": g.fixed_net,
                "allocatable": g.allocatable,
                "capabilities": g.capabilities,
            }
            for g in c.pin_groups
        ]
    if c.configurable:
        d["configurable"] = c.configurable

    # Extra parts (companion printed pieces)
    if c.extra_parts:
        d["extra_parts"] = [
            {
                "id": ep.id,
                "name": ep.name,
                "description": ep.description,
                "scad_module": ep.scad_module,
            }
            for ep in c.extra_parts
        ]

    return d
