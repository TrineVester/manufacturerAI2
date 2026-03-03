"""Tool definitions for the Anthropic API (list_components, get_component, submit_design)."""

from __future__ import annotations

from typing import Any


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_components",
        "description": (
            "List all available components in the catalog with summary info "
            "(ID, category, name, pin count, mounting style, whether it needs "
            "UI placement). Already shown in your system prompt — use this "
            "only if you need a refresher."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_component",
        "description": (
            "Get full details for a specific component: all pins with "
            "positions/directions/voltage/current, mounting details, "
            "internal_nets, pin_groups, and configurable fields. "
            "Always read component details before using it in a design."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "component_id": {
                    "type": "string",
                    "description": "Component ID from the catalog (e.g. 'led_5mm')",
                },
            },
            "required": ["component_id"],
        },
    },
    {
        "name": "submit_design",
        "description": (
            "Submit a complete device design for validation. If validation "
            "fails, you'll receive error details — fix and resubmit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "components": {
                    "type": "array",
                    "description": "Component instances to use.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "catalog_id": {
                                "type": "string",
                                "description": "Component ID from the catalog",
                            },
                            "instance_id": {
                                "type": "string",
                                "description": "Unique instance name (e.g. 'led_1', 'r_1')",
                            },
                            "config": {
                                "type": "object",
                                "description": "Config overrides for configurable components",
                            },
                            "mounting_style": {
                                "type": "string",
                                "description": "Override from allowed_styles",
                            },
                        },
                        "required": ["catalog_id", "instance_id"],
                    },
                },
                "nets": {
                    "type": "array",
                    "description": "Electrical nets connecting component pins.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Net name (e.g. 'VCC', 'GND')",
                            },
                            "pins": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Pin references as 'instance_id:pin_id'. "
                                    "Use 'instance_id:group_id' for MCU dynamic "
                                    "pin allocation."
                                ),
                            },
                        },
                        "required": ["id", "pins"],
                    },
                },
                "outline": {
                    "type": "array",
                    "description": (
                        "Device outline as a list of vertex objects (clockwise winding). "
                        "Coordinate system: screen convention — x increases rightward, "
                        "y increases downward (y=0 is the top of the device). "
                        "Each vertex has x, y (mm), optional ease_in / ease_out "
                        "(mm) for corner rounding, and optional z_top (mm) for "
                        "per-vertex ceiling height. Omit z_top to inherit from "
                        "the enclosure height_mm."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {
                                "type": "number",
                                "description": "X coordinate in mm",
                            },
                            "y": {
                                "type": "number",
                                "description": "Y coordinate in mm",
                            },
                            "ease_in": {
                                "type": "number",
                                "description": (
                                    "Distance in mm along the incoming edge "
                                    "(from previous vertex) where the curve "
                                    "begins. If omitted, defaults to ease_out "
                                    "when ease_out is set, otherwise 0."
                                ),
                            },
                            "ease_out": {
                                "type": "number",
                                "description": (
                                    "Distance in mm along the outgoing edge "
                                    "(toward next vertex) where the curve "
                                    "ends. If omitted, defaults to ease_in "
                                    "when ease_in is set, otherwise 0."
                                ),
                            },
                            "z_top": {
                                "type": "number",
                                "description": (
                                    "Ceiling height (mm) at this vertex. "
                                    "Omit to inherit from enclosure.height_mm. "
                                    "Must be >= floor(2mm) + tallest component + ceiling(2mm)."
                                ),
                            },
                        },
                        "required": ["x", "y"],
                    },
                },
                "enclosure": {
                    "type": "object",
                    "description": (
                        "3D enclosure shape descriptor. The floor is always flat. "
                        "height_mm sets the default ceiling height for vertices without "
                        "z_top, and is the minimum height everywhere. "
                        "top_surface adds an optional smooth bump over the vertex heights."
                    ),
                    "properties": {
                        "height_mm": {
                            "type": "number",
                            "description": (
                                "Default ceiling height (mm) and minimum height. "
                                "Must be >= 2 (floor) + tallest_component + 2 (ceiling). "
                                "Example: battery_holder_9v is ~30mm tall so height_mm >= 34."
                            ),
                        },
                        "top_surface": {
                            "type": "object",
                            "description": "Optional smooth bump added over the per-vertex interpolation.",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "description": "Shape type: 'flat' (default), 'dome', or 'ridge'.",
                                },
                                "peak_x_mm": {"type": "number", "description": "Dome: X of peak"},
                                "peak_y_mm": {"type": "number", "description": "Dome: Y of peak"},
                                "peak_height_mm": {"type": "number", "description": "Dome: absolute Z at peak"},
                                "base_height_mm": {"type": "number", "description": "Dome/ridge: Z level the bump rises from"},
                                "x1": {"type": "number", "description": "Ridge: crest line start X"},
                                "y1": {"type": "number", "description": "Ridge: crest line start Y"},
                                "x2": {"type": "number", "description": "Ridge: crest line end X"},
                                "y2": {"type": "number", "description": "Ridge: crest line end Y"},
                                "crest_height_mm": {"type": "number", "description": "Ridge: absolute Z at the crest"},
                                "falloff_mm": {"type": "number", "description": "Ridge: distance from crest where surface reaches base_height_mm"},
                            },
                            "required": ["type"],
                        },
                    },
                },
                "ui_placements": {
                    "type": "array",
                    "description": (
                        "Positions for UI-facing components (buttons, LEDs, "
                        "switches). Only for ui_placement=true components. "
                        "Side-mount components must include edge_index."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "instance_id": {"type": "string"},
                            "x_mm": {
                                "type": "number",
                                "description": (
                                    "X position in mm. For side-mount: "
                                    "approximate position along the edge."
                                ),
                            },
                            "y_mm": {
                                "type": "number",
                                "description": (
                                    "Y position in mm. For side-mount: "
                                    "approximate position along the edge."
                                ),
                            },
                            "edge_index": {
                                "type": "integer",
                                "description": (
                                    "Required for side-mount components. "
                                    "Which outline edge (0-based) to mount on. "
                                    "Edge i goes from vertices[i] to "
                                    "vertices[(i+1) % n]. The component "
                                    "protrudes through this wall."
                                ),
                            },
                            "conform_to_surface": {
                                "type": "boolean",
                                "description": (
                                    "Whether to angle the component cutout to "
                                    "follow the local surface curvature (default: true). "
                                    "Set to false for a vertical hole regardless of "
                                    "the ceiling angle."
                                ),
                            },
                        },
                        "required": ["instance_id", "x_mm", "y_mm"],
                    },
                },
            },
            "required": ["components", "nets", "outline", "ui_placements"],
        },
    },
    {
        "name": "check_placement_feasibility",
        "description": (
            "Run a fast pre-submit feasibility check to verify that every "
            "auto-placed component (MCU, battery, passives) has at least one "
            "valid position inside the outline given the proposed ui_placements. "
            "Returns a per-component report: OK with candidate cell count, or "
            "FAIL with the specific UI components that are blocking it and a "
            "concrete fix suggestion. "
            "Call this BEFORE submit_design whenever you include a large "
            "auto-placed component (battery, MCU) so you can fix layout "
            "conflicts without wasting a full pipeline run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "components": {
                    "type": "array",
                    "description": "Same component list as submit_design.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "catalog_id": {"type": "string"},
                            "instance_id": {"type": "string"},
                            "config": {"type": "object"},
                            "mounting_style": {"type": "string"},
                        },
                        "required": ["catalog_id", "instance_id"],
                    },
                },
                "outline": {
                    "type": "array",
                    "description": "Same outline vertex list as submit_design.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["x", "y"],
                    },
                },
                "ui_placements": {
                    "type": "array",
                    "description": "Same ui_placements list as submit_design.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "instance_id": {"type": "string"},
                            "x_mm": {"type": "number"},
                            "y_mm": {"type": "number"},
                        },
                        "required": ["instance_id", "x_mm", "y_mm"],
                    },
                },
                "enclosure": {
                    "type": "object",
                    "description": (
                        "Same enclosure object as submit_design. "
                        "Required when edge_bottom is a fillet or chamfer so the "
                        "feasibility scan accounts for the reduced floor space."
                    ),
                },
            },
            "required": ["components", "outline", "ui_placements"],
        },
    },
]
