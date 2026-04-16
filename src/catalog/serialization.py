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


def resolve_config(config: dict, configurable: dict) -> dict:
    """Expand enum selections into their resolved property values.

    Given config {"color": "red"} and a configurable with an enum field,
    returns {"color": "red", "wavelength_nm": 620, "forward_voltage_v": 2.0}.
    """
    resolved: dict[str, Any] = {}
    for key, value in config.items():
        resolved[key] = value
        field_def = configurable.get(key)
        if isinstance(field_def, dict) and field_def.get("type") == "enum":
            options = field_def.get("options", {})
            if isinstance(value, str) and value in options:
                resolved.update(options[value])
    return resolved


def _design_configurable(configurable: dict) -> dict | None:
    """Extract design-relevant config fields, simplifying enums to option lists."""
    result: dict[str, Any] = {}
    for key, field_def in configurable.items():
        if isinstance(field_def, dict) and field_def.get("type") == "enum":
            result[key] = {
                "description": field_def.get("description", ""),
                "options": list(field_def["options"].keys()),
            }
        elif isinstance(field_def, dict) and not field_def.get("agent_decides"):
            continue
        else:
            result[key] = field_def
    return result or None


def component_to_design_dict(c: Component) -> dict:
    """Serialize a Component for the design agent — no dimensions, pins, or electrical details."""
    d: dict[str, Any] = {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "ui_placement": c.ui_placement,
        "mounting": {
            "style": c.mounting.style,
            "allowed_styles": c.mounting.allowed_styles,
        },
    }
    if c.configurable:
        filtered = _design_configurable(c.configurable)
        if filtered:
            d["configurable"] = filtered
    return d


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
                    "shape": {
                        "type": p.shape.type,
                        **(({"width_mm": p.shape.width_mm} if p.shape.width_mm is not None else {})),
                        **(({"length_mm": p.shape.length_mm} if p.shape.length_mm is not None else {})),
                    }
                } if p.shape is not None else {}),
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
    if c.mounting.installed_height_mm is not None:
        d["mounting"]["installed_height_mm"] = c.mounting.installed_height_mm
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

    if c.scad_features:
        d["scad"] = {
            "features": [
                {
                    "shape": f.shape,
                    "label": f.label,
                    "position_mm": list(f.position_mm),
                    **({} if f.width_mm is None else {"width_mm": f.width_mm}),
                    **({} if f.length_mm is None else {"length_mm": f.length_mm}),
                    **({} if f.diameter_mm is None else {"diameter_mm": f.diameter_mm}),
                    **({} if f.depth_mm is None else {"depth_mm": f.depth_mm}),
                    "z_anchor": f.z_anchor,
                    **({"through_surface": True} if f.through_surface else {}),
                    **({"z_center_mm": f.z_center_mm} if f.z_center_mm is not None else {}),
                    **({"rotate": list(f.rotate)} if f.rotate else {}),
                    **({"pattern": {
                        "type": f.pattern.type,
                        "spacing_mm": f.pattern.spacing_mm,
                        "clip_to_body": f.pattern.clip_to_body,
                    }} if f.pattern else {}),
                }
                for f in c.scad_features
            ],
        }

    return d
