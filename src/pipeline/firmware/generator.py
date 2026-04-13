"""Firmware generator — produces Arduino sketch from routing pin assignments.

Takes the routing result (pin_assignments, nets) and design data to generate
an Arduino .ino sketch with correct pin definitions for the specific PCB layout.

Unlike the old firmware_generator.py which only supported a hardcoded IR remote,
this version generates a generic device sketch from the design's component list
and net topology.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from src.catalog.models import CatalogResult, Component

from .pin_mapping import ATMEGA_TO_ARDUINO, ARDUINO_TO_PHYSICAL, PWM_PINS

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


# ── Pin resolution ────────────────────────────────────────────────

def _resolve_arduino_pin(port_name: str) -> int | None:
    """Convert ATmega port name (e.g. 'PD3') to Arduino pin number."""
    return ATMEGA_TO_ARDUINO.get(port_name.upper())


def _classify_component(catalog_id: str) -> str:
    """Classify a component by catalog ID for firmware purposes."""
    cid = catalog_id.lower()
    if "atmega" in cid or "mcu" in cid or "controller" in cid:
        return "mcu"
    if "button" in cid or "tactile" in cid or "switch" in cid:
        return "button"
    if "led" in cid:
        return "led"
    if "motor" in cid or "fan" in cid:
        return "motor"
    if "ir_receiver" in cid or "tsop" in cid:
        return "ir_receiver"
    if "ir" in cid and "led" in cid:
        return "ir_led"
    if "resistor" in cid:
        return "passive"
    if "capacitor" in cid or "crystal" in cid:
        return "passive"
    if "battery" in cid:
        return "power"
    if "usb" in cid:
        return "usb"
    return "other"


# ── Main generator ────────────────────────────────────────────────

def generate_firmware(
    design: dict,
    routing: dict,
    catalog: CatalogResult,
) -> dict:
    """Generate firmware sketch from design + routing data.

    Parameters
    ----------
    design : dict
        Parsed design.json — components, nets, outline.
    routing : dict
        Parsed routing.json — traces, pin_assignments, failed_nets.
    catalog : CatalogResult
        Loaded catalog.

    Returns
    -------
    dict
        {
            "sketch": str,           # Complete .ino file content
            "pin_report": str,       # Human-readable pin assignment report
            "pin_map": list[dict],   # Structured per-component pin info
            "warnings": list[str],
        }
    """
    cat_map: dict[str, Component] = {c.id: c for c in catalog.components}
    components = design.get("components", [])
    nets = design.get("nets", [])
    pin_assignments = routing.get("pin_assignments", {})
    failed_nets = routing.get("failed_nets", [])

    warnings: list[str] = []
    pin_defs: list[str] = []       # #define lines
    setup_lines: list[str] = []    # pinMode() calls
    loop_sections: list[str] = []  # loop() code blocks
    pin_map: list[dict] = []       # structured report

    # Build net membership: instance:pin -> net_id
    net_lookup: dict[str, str] = {}
    for net in nets:
        net_id = net.get("id", "")
        for pin_ref in net.get("pins", []):
            # Resolve dynamic assignments
            resolved = pin_assignments.get(pin_ref, pin_ref)
            net_lookup[resolved] = net_id

    # Track used Arduino pins
    used_pins: set[int] = set()
    pin_num = 0

    # Process each component
    for comp in components:
        instance_id = comp.get("instance_id", "")
        catalog_id = comp.get("catalog_id", "")
        ctype = _classify_component(catalog_id)
        cat_entry = cat_map.get(catalog_id)

        if ctype in ("mcu", "passive", "power"):
            # MCU is the target, passives/power don't need firmware defs
            continue

        if not cat_entry:
            continue

        # Find which MCU pins this component connects to via nets
        comp_pins: list[dict] = []
        for pin in cat_entry.pins:
            pin_ref = f"{instance_id}:{pin.id}"
            resolved_ref = pin_assignments.get(pin_ref, pin_ref)
            net_id = net_lookup.get(resolved_ref, "")

            if not net_id:
                continue

            # Find the MCU pin on this net
            mcu_pin_ref = None
            for net in nets:
                if net.get("id") != net_id:
                    continue
                for p in net.get("pins", []):
                    rp = pin_assignments.get(p, p)
                    inst = rp.split(":")[0]
                    # Check if this is an MCU component
                    inst_cat = None
                    for c in components:
                        if c.get("instance_id") == inst:
                            inst_cat = c.get("catalog_id", "")
                            break
                    if inst_cat and _classify_component(inst_cat) == "mcu":
                        mcu_pin_ref = rp
                        break
                break

            if not mcu_pin_ref:
                continue

            # Extract the ATmega port from the MCU pin reference
            mcu_pin_name = mcu_pin_ref.split(":")[-1]
            arduino_pin = _resolve_arduino_pin(mcu_pin_name)
            if arduino_pin is None:
                warnings.append(f"{instance_id}:{pin.id} → {mcu_pin_name}: unknown ATmega port")
                continue

            physical_pin = ARDUINO_TO_PHYSICAL.get(arduino_pin, -1)
            is_pwm = arduino_pin in PWM_PINS

            comp_pins.append({
                "pin_id": pin.id,
                "pin_label": pin.label,
                "net_id": net_id,
                "atmega_port": mcu_pin_name,
                "arduino_pin": arduino_pin,
                "physical_pin": physical_pin,
                "is_pwm": is_pwm,
                "direction": pin.direction,
            })
            used_pins.add(arduino_pin)

        if not comp_pins:
            continue

        # Generate firmware code for this component
        safe_id = re.sub(r'[^a-zA-Z0-9_]', '_', instance_id).upper()

        pin_map.append({
            "instance_id": instance_id,
            "catalog_id": catalog_id,
            "type": ctype,
            "pins": comp_pins,
        })

        for cp in comp_pins:
            define_name = f"PIN_{safe_id}_{cp['pin_id'].upper()}"
            pin_defs.append(
                f"#define {define_name:<30} {cp['arduino_pin']:<3} "
                f"// ATmega pin {cp['physical_pin']} ({cp['atmega_port']})"
                f"{' [PWM]' if cp['is_pwm'] else ''}"
            )

            if ctype == "button":
                setup_lines.append(f"  pinMode({define_name}, INPUT_PULLUP);")
            elif ctype in ("led", "ir_led"):
                setup_lines.append(f"  pinMode({define_name}, OUTPUT);")
            elif ctype == "motor":
                setup_lines.append(f"  pinMode({define_name}, OUTPUT);")
            elif ctype == "ir_receiver":
                setup_lines.append(f"  pinMode({define_name}, INPUT);")
            else:
                setup_lines.append(f"  pinMode({define_name}, INPUT);  // {ctype}")

        # Generate loop code based on type
        if ctype == "button":
            for cp in comp_pins:
                define = f"PIN_{safe_id}_{cp['pin_id'].upper()}"
                loop_sections.append(
                    f"  // Read {instance_id}\n"
                    f"  if (digitalRead({define}) == LOW) {{\n"
                    f"    // TODO: {instance_id} pressed\n"
                    f"    delay(50);  // debounce\n"
                    f"  }}"
                )

        elif ctype == "led":
            for cp in comp_pins:
                if cp["direction"] == "in":  # anode
                    define = f"PIN_{safe_id}_{cp['pin_id'].upper()}"
                    loop_sections.append(
                        f"  // Control {instance_id}\n"
                        f"  // digitalWrite({define}, HIGH);  // LED on\n"
                        f"  // digitalWrite({define}, LOW);   // LED off"
                    )

        elif ctype == "motor":
            for cp in comp_pins:
                define = f"PIN_{safe_id}_{cp['pin_id'].upper()}"
                if cp["is_pwm"]:
                    loop_sections.append(
                        f"  // Control {instance_id} (PWM)\n"
                        f"  // analogWrite({define}, 128);  // 50% speed"
                    )
                else:
                    loop_sections.append(
                        f"  // Control {instance_id}\n"
                        f"  // digitalWrite({define}, HIGH);  // on"
                    )

    # Build the sketch
    sketch = _build_sketch(pin_defs, setup_lines, loop_sections, components)

    # Build human-readable report
    report = _build_report(pin_map, failed_nets, warnings)

    if failed_nets:
        warnings.append(
            f"{len(failed_nets)} net(s) failed routing: {', '.join(failed_nets[:5])}"
        )

    return {
        "sketch": sketch,
        "pin_report": report,
        "pin_map": pin_map,
        "warnings": warnings,
    }


def _build_sketch(
    pin_defs: list[str],
    setup_lines: list[str],
    loop_sections: list[str],
    components: list[dict],
) -> str:
    """Build a complete .ino sketch."""
    comp_names = [c.get("instance_id", "?") for c in components if _classify_component(c.get("catalog_id", "")) not in ("passive", "power")]
    comp_list = ", ".join(comp_names[:10])

    lines = [
        "/*",
        " * Auto-generated firmware for ManufacturerAI device",
        f" * Components: {comp_list}",
        " *",
        " * Pin assignments derived from PCB routing — do not edit manually.",
        " * Re-generate after routing changes.",
        " */",
        "",
        "// ============== PIN DEFINITIONS ==============",
    ]

    if pin_defs:
        lines.extend(pin_defs)
    else:
        lines.append("// No routed pins detected")

    lines.extend([
        "",
        "// ============== SETUP ==============",
        "",
        "void setup() {",
        "  Serial.begin(9600);",
    ])

    if setup_lines:
        lines.append("")
        lines.extend(setup_lines)

    lines.extend([
        "}",
        "",
        "// ============== MAIN LOOP ==============",
        "",
        "void loop() {",
    ])

    if loop_sections:
        lines.append("")
        lines.extend(loop_sections)
        lines.append("")
    else:
        lines.append("  // No active components detected")

    lines.extend([
        "  delay(10);  // main loop throttle",
        "}",
        "",
    ])

    return "\n".join(lines)


def _build_report(
    pin_map: list[dict],
    failed_nets: list[str],
    warnings: list[str],
) -> str:
    """Build a human-readable pin assignment report."""
    lines = [
        "=" * 70,
        "FIRMWARE PIN ASSIGNMENT REPORT",
        "=" * 70,
        "",
    ]

    if not pin_map:
        lines.append("No component→MCU pin mappings found.")
        lines.append("This may mean routing has not assigned pins yet,")
        lines.append("or no components connect to the microcontroller.")
    else:
        lines.append(
            f"{'Component':<20} {'Pin':<10} {'Net':<15} {'Arduino':<10} {'ATmega':<10} {'Physical'}"
        )
        lines.append("-" * 70)

        for entry in pin_map:
            for pin in entry["pins"]:
                pwm = " [PWM]" if pin["is_pwm"] else ""
                lines.append(
                    f"{entry['instance_id']:<20} "
                    f"{pin['pin_id']:<10} "
                    f"{pin['net_id']:<15} "
                    f"D{pin['arduino_pin']:<9} "
                    f"{pin['atmega_port']:<10} "
                    f"Pin {pin['physical_pin']}{pwm}"
                )

    if failed_nets:
        lines.extend(["", f"FAILED NETS ({len(failed_nets)}):"])
        for fn in failed_nets:
            lines.append(f"  - {fn}")

    if warnings:
        lines.extend(["", "WARNINGS:"])
        for w in warnings:
            lines.append(f"  ⚠ {w}")

    lines.extend(["", "=" * 70])
    return "\n".join(lines)
