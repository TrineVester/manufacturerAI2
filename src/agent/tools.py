"""Tool definitions for the Anthropic API — split by agent role."""

from __future__ import annotations

from typing import Any


# -- Shared tools (used by both DesignAgent and CircuitAgent) -------

_TOOL_LIST_COMPONENTS = {
    "name": "list_components",
    "description": (
        "List all available components in the catalog with summary info "
        "(ID, category, name, pin count, mounting style, whether it needs "
        "UI placement). Already shown in your system prompt - use this "
        "only if you need a refresher."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

_TOOL_GET_COMPONENT = {
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
}


# -- Design agent tools --------------------------------------------

_TOOL_SUBMIT_DESIGN = {
    "name": "submit_design",
    "description": (
        "Submit the physical device design for validation. Includes the "
        "shape (outline, enclosure), UI component placements, and UI "
        "components only. Internal components (MCU, resistors, battery) "
        "and nets are handled by the circuit agent later - do NOT "
        "include them here. "
        "If validation fails, you'll receive error details - fix and resubmit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "components": {
                "type": "array",
                "description": (
                    "UI component instances only (buttons, LEDs, switches - "
                    "things the user interacts with). Do NOT include internal "
                    "components like MCU, resistors, capacitors, or batteries."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "catalog_id": {
                            "type": "string",
                            "description": "Component ID from the catalog",
                        },
                        "instance_id": {
                            "type": "string",
                            "description": "Unique instance name (e.g. 'led_1', 'btn_1')",
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
            "outline": {
                "type": "array",
                "description": (
                    "Device outline as a list of vertex objects (clockwise winding). "
                    "Coordinate system: screen convention - x increases rightward, "
                    "y increases downward (y=0 is the top of the device). "
                    "Each vertex has x, y (mm), optional ease_in / ease_out "
                    "(mm) for corner rounding, and optional z_top (mm) for "
                    "per-vertex ceiling height."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate in mm"},
                        "y": {"type": "number", "description": "Y coordinate in mm"},
                        "ease_in": {
                            "type": "number",
                            "description": "Distance in mm along incoming edge where curve begins.",
                        },
                        "ease_out": {
                            "type": "number",
                            "description": "Distance in mm along outgoing edge where curve ends.",
                        },
                        "z_top": {
                            "type": "number",
                            "description": "Ceiling height (mm) at this vertex. Omit to inherit from enclosure.height_mm.",
                        },
                    },
                    "required": ["x", "y"],
                },
            },
            "enclosure": {
                "type": "object",
                "description": (
                    "3D enclosure shape descriptor. height_mm sets the default "
                    "ceiling height. top_surface adds an optional smooth bump."
                ),
                "properties": {
                    "height_mm": {
                        "type": "number",
                        "description": "Default ceiling height (mm). Must be >= floor + tallest component + ceiling.",
                    },
                    "top_surface": {
                        "type": "object",
                        "description": "Optional smooth bump.",
                        "properties": {
                            "type": {"type": "string", "description": "'flat', 'dome', or 'ridge'."},
                            "peak_x_mm": {"type": "number"},
                            "peak_y_mm": {"type": "number"},
                            "peak_height_mm": {"type": "number"},
                            "base_height_mm": {"type": "number"},
                            "x1": {"type": "number"},
                            "y1": {"type": "number"},
                            "x2": {"type": "number"},
                            "y2": {"type": "number"},
                            "crest_height_mm": {"type": "number"},
                            "falloff_mm": {"type": "number"},
                        },
                        "required": ["type"],
                    },
                    "edge_top": {
                        "type": "object",
                        "properties": {"type": {"type": "string"}, "size_mm": {"type": "number"}},
                    },
                    "edge_bottom": {
                        "type": "object",
                        "description": "WARNING: each mm of size_mm removes 1mm of usable placement space.",
                        "properties": {"type": {"type": "string"}, "size_mm": {"type": "number"}},
                    },
                },
            },
            "ui_placements": {
                "type": "array",
                "description": "Positions for UI-facing components. Side-mount components must include edge_index.",
                "items": {
                    "type": "object",
                    "properties": {
                        "instance_id": {"type": "string"},
                        "x_mm": {"type": "number", "description": "X position in mm."},
                        "y_mm": {"type": "number", "description": "Y position in mm."},
                        "edge_index": {
                            "type": "integer",
                            "description": "Required for side-mount. Which outline edge (0-based).",
                        },
                        "conform_to_surface": {
                            "type": "boolean",
                            "description": "Angle cutout to follow surface curvature (default: true).",
                        },
                    },
                    "required": ["instance_id", "x_mm", "y_mm"],
                },
            },
        },
        "required": ["components", "outline", "ui_placements"],
    },
}

_TOOL_EDIT_DESIGN = {
    "name": "edit_design",
    "description": (
        "Make an incremental edit to the current design.json using "
        "find-and-replace. Use this to tweak the outline, adjust "
        "UI placements, change enclosure settings, etc. without "
        "resubmitting the entire design. "
        "Returns the validation result and the full current design. "
        "Only works if a design.json already exists (call submit_design first)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "old_string": {
                "type": "string",
                "description": (
                    "The exact JSON text to find in the current design. "
                    "Must match exactly one location."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "The replacement JSON text.",
            },
        },
        "required": ["old_string", "new_string"],
    },
}

_TOOL_CHECK_FEASIBILITY = {
    "name": "check_placement_feasibility",
    "description": (
        "Run a fast pre-submit feasibility check to verify that every "
        "auto-placed component (MCU, battery, passives) has at least one "
        "valid position inside the outline given the proposed ui_placements. "
        "Call this BEFORE submit_design whenever you include a large "
        "auto-placed component so you can fix layout conflicts early."
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
                "description": "Same enclosure object as submit_design. Required when edge_bottom is fillet/chamfer.",
            },
        },
        "required": ["components", "outline", "ui_placements"],
    },
}


# -- Circuit agent tools -------------------------------------------

_TOOL_SUBMIT_CIRCUIT = {
    "name": "submit_circuit",
    "description": (
        "Submit the circuit design: all component instances (including "
        "the UI components from the design stage) and the net list. "
        "The circuit is merged with the existing design (outline, "
        "enclosure, ui_placements) and fully validated. "
        "If validation fails, you'll receive error details - fix and resubmit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "components": {
                "type": "array",
                "description": (
                    "ALL component instances - both the UI components "
                    "from the design stage AND internal components you're "
                    "adding (MCU, resistors, capacitors, batteries, etc.). "
                    "Use the exact same instance_id for UI components."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "catalog_id": {
                            "type": "string",
                            "description": "Component ID from the catalog",
                        },
                        "instance_id": {
                            "type": "string",
                            "description": "Unique instance name (e.g. 'mcu_1', 'r_1')",
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
                                "Use 'instance_id:group_id' for dynamic "
                                "pin allocation."
                            ),
                        },
                    },
                    "required": ["id", "pins"],
                },
            },
        },
        "required": ["components", "nets"],
    },
}


# -- Public tool lists ---------------------------------------------

DESIGN_TOOLS: list[dict[str, Any]] = [
    _TOOL_LIST_COMPONENTS,
    _TOOL_GET_COMPONENT,
    _TOOL_SUBMIT_DESIGN,
    _TOOL_EDIT_DESIGN,
    _TOOL_CHECK_FEASIBILITY,
]

CIRCUIT_TOOLS: list[dict[str, Any]] = [
    _TOOL_LIST_COMPONENTS,
    _TOOL_GET_COMPONENT,
    _TOOL_SUBMIT_CIRCUIT,
]

# Backwards compatibility
TOOLS = DESIGN_TOOLS
