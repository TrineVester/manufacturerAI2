"""Assembly guide generator — builds step-by-step instructions from pipeline data."""

from __future__ import annotations

import math
from typing import Any

from src.catalog.models import CatalogResult, Component

from .models import AssemblyGuide, ComponentStep, WiringConnection


# ── Component classification ──────────────────────────────────────

_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("controller", ["atmega", "controller", "mcu"]),
    ("button",     ["button", "tactile", "switch"]),
    ("battery",    ["battery"]),
    ("led",        ["led"]),
    ("resistor",   ["resistor"]),
    ("capacitor",  ["capacitor", "cap_"]),
    ("crystal",    ["crystal", "oscillator"]),
    ("transistor", ["transistor", "npn", "pnp"]),
    ("motor",      ["motor", "fan"]),
    ("connector",  ["usb", "connector", "plug", "socket"]),
    ("lamp",       ["lamp"]),
    ("ir",         ["ir_receiver", "ir_transmitter", "tsop"]),
]

_TYPE_LABELS: dict[str, str] = {
    "controller":  "Microcontroller",
    "button":      "Push Button",
    "battery":     "Battery Holder",
    "led":         "LED",
    "resistor":    "Resistor",
    "capacitor":   "Capacitor",
    "crystal":     "Crystal Oscillator",
    "transistor":  "Transistor",
    "motor":       "Motor / Fan",
    "connector":   "Connector",
    "lamp":        "Lamp Socket",
    "ir":          "IR Component",
    "component":   "Component",
}

# Insertion order — large/anchor components first, small passives last
_TYPE_ORDER = [
    "battery", "controller", "connector", "lamp", "motor",
    "crystal", "ir", "transistor", "button", "led",
    "resistor", "capacitor", "component",
]


def _classify(catalog_id: str) -> str:
    cid = catalog_id.lower()
    for ctype, keywords in _TYPE_KEYWORDS:
        if any(kw in cid for kw in keywords):
            return ctype
    return "component"


# ── Per-type assembly instructions ────────────────────────────────

def _instructions_for(ctype: str, comps: list[dict], catalog: dict[str, Component]) -> tuple[list[str], list[str]]:
    """Return (instructions, warnings) for a component type group."""
    instructions: list[str] = []
    warnings: list[str] = []

    sample = comps[0] if comps else {}
    cat_entry = catalog.get(sample.get("catalog_id", ""))

    if ctype == "controller":
        instructions = [
            "Locate the rectangular DIP pocket with pin holes.",
            "Find the pin-1 marker on the chip (notch or dot on one end).",
            "Align the notch with the marker on the enclosure pocket.",
            "Carefully align ALL pins with their holes before pressing down.",
            "Press gently and evenly — do not force the chip.",
        ]
        warnings = [
            "Incorrect orientation will permanently damage the chip.",
            "Bent pins: gently straighten with needle-nose pliers before insertion.",
        ]

    elif ctype == "button":
        instructions = [
            "Locate the square pocket on the enclosure surface.",
            "Orient the button so pins align with the holes.",
            "Press firmly until the button sits flush with the surface.",
            "Verify the button cap protrudes through the top opening.",
            "Test the click action — it should feel crisp.",
        ]

    elif ctype == "battery":
        mount = cat_entry.mounting.style if cat_entry else "internal"
        instructions = [
            f"Locate the {'internal' if mount == 'internal' else 'bottom'} battery pocket.",
            "Insert the battery holder with contacts facing the marked direction.",
            "Press down until the holder clips into place.",
            "Do NOT insert batteries until all soldering is complete.",
        ]
        warnings = ["Observe polarity markings — reversed batteries can damage circuits."]

    elif ctype == "led":
        instructions = [
            "Locate the round pocket or wall-mount slot.",
            "Identify the longer leg (anode, +) and shorter leg (cathode, −).",
            "Insert the longer leg into the hole marked '+' or 'A'.",
            "Push the LED flush into the pocket.",
        ]
        warnings = ["Reversed polarity: LED will not light. No damage, but won't work."]

    elif ctype == "resistor":
        instructions = [
            "Resistors are NOT polarized — either direction works.",
            "Bend leads at 90° to match the hole spacing.",
            "Insert leads through both holes and push body flush.",
        ]
        if cat_entry and cat_entry.configurable:
            conf = cat_entry.configurable
            if "resistance_ohms" in conf:
                instructions.insert(0, "Verify the resistance value matches the design (check color bands).")

    elif ctype == "capacitor":
        instructions = [
            "Ceramic capacitors are NOT polarized — either direction works.",
            "Insert leads through the holes and seat the body flush.",
        ]

    elif ctype == "crystal":
        instructions = [
            "Crystal oscillators are NOT polarized.",
            "Insert the two leads into the marked holes.",
            "The metal can should sit above the board surface.",
        ]
        warnings = ["Keep crystal leads as short as possible for signal integrity."]

    elif ctype == "transistor":
        instructions = [
            "Match the flat side of the TO-92 package to the pocket shape.",
            "The three pins are Emitter, Base, Collector (E-B-C) — match to the labels.",
            "Push gently until seated.",
        ]
        warnings = ["Incorrect pin order will prevent the circuit from working."]

    elif ctype == "motor":
        instructions = [
            "Insert the motor/fan into its mounting pocket.",
            "Route the power wires to the solder pads.",
            "Secure with friction fit or adhesive if needed.",
        ]

    elif ctype == "connector":
        instructions = [
            "Align the connector with its pocket.",
            "Ensure the port opening faces the enclosure edge.",
            "Press firmly — connectors need a solid mechanical connection.",
        ]

    elif ctype == "lamp":
        instructions = [
            "Screw or press-fit the lamp socket into the opening.",
            "Route the two power wires to the solder pads.",
        ]

    elif ctype == "ir":
        instructions = [
            "Locate the IR window pocket on the enclosure.",
            "Insert with the lens dome facing outward.",
            "Ensure clear line-of-sight through the window.",
        ]

    else:
        instructions = [
            "Insert the component into its marked pocket.",
            "Align pins with the corresponding holes.",
            "Push until flush.",
        ]

    # Add position info
    for comp in comps:
        rot = comp.get("rotation_deg", 0)
        rot_note = f", rotated {rot}°" if rot else ""
        instructions.append(
            f"  → {comp['instance_id']}: position ({comp['x_mm']:.1f}, {comp['y_mm']:.1f}) mm{rot_note}"
        )

    return instructions, warnings


# ── Wiring extraction ─────────────────────────────────────────────

def _extract_wiring(
    nets: list[dict],
    traces: list[dict],
    pin_assignments: dict[str, str],
    component_ids: set[str],
) -> list[WiringConnection]:
    """Extract wiring connections relevant to a set of component instance IDs."""
    # Build trace length lookup: net_id -> total path length
    trace_lengths: dict[str, float] = {}
    for t in traces:
        path = t.get("path", [])
        length = 0.0
        for i in range(1, len(path)):
            dx = path[i][0] - path[i - 1][0]
            dy = path[i][1] - path[i - 1][1]
            length += math.sqrt(dx * dx + dy * dy)
        trace_lengths[t["net_id"]] = length

    # Resolve pin assignments (dynamic group → physical pin)
    resolved: dict[str, str] = {}
    for abstract, physical in pin_assignments.items():
        resolved[abstract] = physical

    connections: list[WiringConnection] = []
    for net in nets:
        net_id = net["id"]
        pins = net.get("pins", [])
        if len(pins) < 2:
            continue

        # Resolve each pin ref
        resolved_pins = []
        for p in pins:
            rp = resolved.get(p, p)
            resolved_pins.append(rp)

        # Find which pins belong to our component set
        involved = [(rp, rp.split(":")[0]) for rp in resolved_pins if rp.split(":")[0] in component_ids]
        others = [(rp, rp.split(":")[0]) for rp in resolved_pins if rp.split(":")[0] not in component_ids]

        # Create connections from involved components to their net peers
        for inv_pin, inv_inst in involved:
            for other_pin, other_inst in others:
                connections.append(WiringConnection(
                    net_id=net_id,
                    from_instance=inv_inst,
                    from_pin=inv_pin.split(":")[-1],
                    to_instance=other_inst,
                    to_pin=other_pin.split(":")[-1],
                    trace_length_mm=trace_lengths.get(net_id),
                ))
            # Also connections between involved components (same net)
            for inv2_pin, inv2_inst in involved:
                if inv_pin < inv2_pin:  # avoid duplicates
                    connections.append(WiringConnection(
                        net_id=net_id,
                        from_instance=inv_inst,
                        from_pin=inv_pin.split(":")[-1],
                        to_instance=inv2_inst,
                        to_pin=inv2_pin.split(":")[-1],
                        trace_length_mm=trace_lengths.get(net_id),
                    ))

    return connections


# ── Main generator ────────────────────────────────────────────────

def generate_assembly_guide(
    placement: dict,
    routing: dict | None,
    design: dict | None,
    catalog: CatalogResult,
) -> dict:
    """Generate a structured assembly guide from pipeline data.

    Parameters
    ----------
    placement : dict
        Parsed placement.json (components, outline, nets, enclosure).
    routing : dict | None
        Parsed routing.json (traces, pin_assignments, failed_nets).
        May be None if routing hasn't been run yet.
    design : dict | None
        Parsed design.json. Used for net/component extra info.
    catalog : CatalogResult
        Loaded catalog for component metadata.

    Returns
    -------
    dict
        JSON-serializable assembly guide.
    """
    components = placement.get("components", [])
    nets = placement.get("nets", []) or (design or {}).get("nets", [])
    traces = (routing or {}).get("traces", [])
    pin_assignments = (routing or {}).get("pin_assignments", {})

    # Build catalog lookup
    cat_map: dict[str, Component] = {c.id: c for c in catalog.components}

    # Group components by type
    grouped: dict[str, list[dict]] = {}
    for comp in components:
        ctype = _classify(comp.get("catalog_id", ""))
        grouped.setdefault(ctype, []).append(comp)

    # Build checklist
    checklist = []
    for ctype in _TYPE_ORDER:
        if ctype in grouped:
            comps = grouped[ctype]
            checklist.append({
                "type": ctype,
                "label": _TYPE_LABELS.get(ctype, ctype),
                "count": len(comps),
            })

    # Build steps in insertion order
    steps: list[dict] = []
    step_num = 1
    total_connections = 0

    for ctype in _TYPE_ORDER:
        if ctype not in grouped:
            continue
        comps = grouped[ctype]
        label = _TYPE_LABELS.get(ctype, ctype)
        instructions, warnings = _instructions_for(ctype, comps, cat_map)

        # Extract wiring for these components
        comp_ids = {c["instance_id"] for c in comps}
        wiring = _extract_wiring(nets, traces, pin_assignments, comp_ids)
        total_connections += len(wiring)

        steps.append({
            "step_number": step_num,
            "title": f"{label} Placement",
            "component_type": ctype,
            "instances": [
                {
                    "instance_id": c["instance_id"],
                    "catalog_id": c.get("catalog_id", ""),
                    "x_mm": c.get("x_mm", 0),
                    "y_mm": c.get("y_mm", 0),
                    "rotation_deg": c.get("rotation_deg", 0),
                }
                for c in comps
            ],
            "instructions": instructions,
            "warnings": warnings,
            "wiring": [
                {
                    "net_id": w.net_id,
                    "from_instance": w.from_instance,
                    "from_pin": w.from_pin,
                    "to_instance": w.to_instance,
                    "to_pin": w.to_pin,
                    "trace_length_mm": round(w.trace_length_mm, 1) if w.trace_length_mm else None,
                }
                for w in wiring
            ],
        })
        step_num += 1

    # Final safety checks
    final_checks = [
        "All components are seated flush in their pockets.",
        "Pin 1 / polarity markers are correctly oriented.",
        "All leads are fully inserted into their holes.",
        "No bent or crossed pins.",
    ]

    if any(c.get("catalog_id", "").lower().startswith("battery") for c in components):
        final_checks.append("Batteries are NOT inserted yet (insert after soldering).")

    if routing and routing.get("failed_nets"):
        failed = routing["failed_nets"]
        final_checks.append(
            f"WARNING: {len(failed)} net(s) failed routing ({', '.join(failed[:3])}). "
            "Manual wiring may be needed for these connections."
        )

    return {
        "total_components": len(components),
        "total_connections": total_connections,
        "checklist": checklist,
        "steps": steps,
        "final_checks": final_checks,
    }
