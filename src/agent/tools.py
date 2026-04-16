"""Tool definitions for the design and circuit agents."""

from __future__ import annotations

from typing import Any


# ── Shared tools (used by both agents) ────────────────────────────

_LIST_COMPONENTS = {
    "name": "list_components",
    "description": "Return a summary table of all catalog components.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

_GET_COMPONENT = {
    "name": "get_component",
    "description": "Return full details for a catalog component.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component_id": {
                "type": "string",
                "description": "Catalog component ID",
            },
        },
        "required": ["component_id"],
    },
}


# ── Design agent tools ────────────────────────────────────────────

DESIGN_TOOLS: list[dict[str, Any]] = [
    _LIST_COMPONENTS,
    _GET_COMPONENT,
    {
        "name": "edit_design",
        "description": "Find-and-replace edit on the design document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find (must match one location)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["old_string", "new_string"],
        },
    },
]


# ── Circuit agent tools ───────────────────────────────────────────

CIRCUIT_TOOLS: list[dict[str, Any]] = [
    _LIST_COMPONENTS,
    _GET_COMPONENT,
    {
        "name": "submit_circuit",
        "description": "Submit a circuit design for validation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "components": {
                    "type": "array",
                    "description": "Component instances.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "catalog_id": {
                                "type": "string",
                                "description": "Catalog component ID",
                            },
                            "instance_id": {
                                "type": "string",
                                "description": "Unique instance name",
                            },
                            "config": {
                                "type": "object",
                                "description": "Config overrides",
                            },
                            "mounting_style": {
                                "type": "string",
                                "description": "Mounting style override",
                            },
                        },
                        "required": ["catalog_id", "instance_id"],
                    },
                },
                "nets": {
                    "type": "array",
                    "description": "Electrical nets.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Net name",
                            },
                            "pins": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Pin references as 'instance_id:pin_id' or 'instance_id:group_id'",
                            },
                        },
                        "required": ["id", "pins"],
                    },
                },
            },
            "required": ["components", "nets"],
        },
    },
]


# ── Setup (firmware) agent tools ──────────────────────────────────

SETUP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_firmware",
        "description": "Submit an Arduino sketch for compilation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Complete .ino file contents",
                },
            },
            "required": ["code"],
        },
    },
]
