"""
Build a structured behavioral-context document for the Setup (firmware) agent.

Reads design.json, circuit.json, routing.json, and the component catalog,
then produces a plain-text summary that the LLM can use to write firmware.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.firmware.firmware_generator import ATMEGA_TO_ARDUINO, PWM_PINS

log = logging.getLogger(__name__)


def build_firmware_context(
    design: dict[str, Any],
    circuit: dict[str, Any],
    routing: dict[str, Any],
    catalog_map: dict[str, dict[str, Any]],
) -> str:
    """Return a human-readable context string for the firmware agent.

    Parameters
    ----------
    design : dict
        The design.json artifact.
    circuit : dict
        The circuit.json artifact (components + nets).
    routing : dict
        The routing.json artifact (pin_assignments).
    catalog_map : dict
        Catalog keyed by catalog_id → component dict.
    """
    sections: list[str] = []

    # ── Device description ─────────────────────────────────────────
    desc = design.get("device_description", "Unknown device")
    sections.append(f"DEVICE DESCRIPTION:\n  {desc}")

    # ── Resolve pin assignments ────────────────────────────────────
    pin_assigns = routing.get("pin_assignments", {})
    # pin_assigns keys: "net_id|instance:group" → "instance:physical_pin"
    net_to_port: dict[str, str] = {}
    for key, value in pin_assigns.items():
        parts = key.split("|", 1)
        if len(parts) != 2:
            continue
        net_id = parts[0]
        _, port = value.rsplit(":", 1)
        net_to_port[net_id] = port.upper()

    # ── Build instance maps ────────────────────────────────────────
    components = circuit.get("components", [])
    nets = circuit.get("nets", [])
    ui_placements = design.get("ui_placements", [])

    inst_to_catalog: dict[str, str] = {}
    inst_to_config: dict[str, dict] = {}
    for comp in components:
        iid = comp["instance_id"]
        inst_to_catalog[iid] = comp.get("catalog_id", "")
        inst_to_config[iid] = comp.get("config", {})

    ui_ids = {p["instance_id"] for p in ui_placements}

    # ── Resistor-chain tracing helpers ─────────────────────────────
    # Identify resistor instances so we can trace through them
    resistor_ids: set[str] = set()
    for comp in components:
        if "resistor" in comp.get("catalog_id", "").lower():
            resistor_ids.add(comp["instance_id"])

    # net_id → set of instance_ids on that net
    net_members: dict[str, set[str]] = {}
    for net in nets:
        members: set[str] = set()
        for pin_ref in net.get("pins", []):
            members.add(pin_ref.split(":")[0])
        net_members[net["id"]] = members

    # instance_id → list of net_ids the instance appears on
    inst_nets: dict[str, list[str]] = {}
    for net in nets:
        for pin_ref in net.get("pins", []):
            iid = pin_ref.split(":")[0]
            inst_nets.setdefault(iid, []).append(net["id"])

    def _resolve_port_through_resistor(iid: str) -> str | None:
        """Follow a resistor bridge to find the MCU port driving *iid*.

        When a component is on a net with a resistor, and that resistor is
        also on another net that has a known MCU port, return that port.
        """
        for nid in inst_nets.get(iid, []):
            for res_id in net_members.get(nid, set()) & resistor_ids:
                for other_nid in inst_nets.get(res_id, []):
                    if other_nid != nid:
                        port = net_to_port.get(other_nid)
                        if port and port in ATMEGA_TO_ARDUINO:
                            return port
        return None

    # ── Components & pin map ───────────────────────────────────────
    lines: list[str] = []

    for comp in components:
        iid = comp["instance_id"]
        cat_id = comp.get("catalog_id", "")
        cat_entry = catalog_map.get(cat_id, {})
        comp_name = cat_entry.get("name", cat_id)
        is_ui = iid in ui_ids

        # Find which net(s) this component is on, and the MCU pin
        comp_nets: list[tuple[str, str | None]] = []  # (net_id, port|None)
        for net in nets:
            for pin_ref in net.get("pins", []):
                if pin_ref.startswith(iid + ":"):
                    port = net_to_port.get(net["id"])
                    comp_nets.append((net["id"], port))
                    break

        # Determine component role
        role = _component_role(cat_id, cat_entry, iid)

        if role == "mcu":
            lines.append(f"  {iid} ({comp_name}) — Microcontroller")
            continue

        # Resolve the MCU port for this component.
        # First try a direct net lookup; fall back to resistor-chain tracing.
        resolved_port: str | None = None
        resolved_net: str | None = None
        for net_id, port in comp_nets:
            if port and port in ATMEGA_TO_ARDUINO:
                resolved_port = port
                resolved_net = net_id
                break

        if not resolved_port:
            resolved_port = _resolve_port_through_resistor(iid)
            # Pick a representative net (first signal net, not GND/VCC)
            for net_id, _ in comp_nets:
                if net_id.upper() not in ("GND", "VCC", "5V", "3V3"):
                    resolved_net = net_id
                    break
            if not resolved_net and comp_nets:
                resolved_net = comp_nets[0][0]

        arduino_pin = ATMEGA_TO_ARDUINO.get(resolved_port) if resolved_port else None
        pin_str = f"Arduino pin {arduino_pin}" if arduino_pin is not None else "unrouted"
        pwm_note = " (PWM capable)" if arduino_pin is not None and arduino_pin in PWM_PINS else ""
        net_label = resolved_net or "?"

        if role == "button":
            lines.append(f"  {iid} ({comp_name}) → {pin_str}{pwm_note}  [INPUT_PULLUP, active LOW]  net: {net_label}")
        elif role == "led":
            lines.append(f"  {iid} ({comp_name}) → {pin_str}{pwm_note}  [OUTPUT]  net: {net_label}")
        elif role == "ir_led":
            lines.append(f"  {iid} ({comp_name}) → {pin_str}{pwm_note}  [OUTPUT, 38kHz PWM carrier]  net: {net_label}")
        elif role == "resistor":
            config = inst_to_config.get(iid, {})
            val = config.get("resistance", config.get("value", "?"))
            lines.append(f"  {iid} ({comp_name}, {val}) — current limiter  net: {net_label}")
        else:
            lines.append(f"  {iid} ({comp_name}) → {pin_str}{pwm_note}  net: {net_label}")

    sections.append("COMPONENTS & PIN MAP:\n" + "\n".join(lines))

    # ── Power info ─────────────────────────────────────────────────
    battery = None
    for comp in components:
        cat_id = comp.get("catalog_id", "")
        if "battery" in cat_id.lower():
            cat_entry = catalog_map.get(cat_id, {})
            battery = cat_entry.get("name", cat_id)
            break

    power_lines = []
    if battery:
        power_lines.append(f"  Battery: {battery}")
    power_lines.append("  MCU: ATmega328P running at 8MHz internal oscillator (no external crystal)")
    sections.append("POWER:\n" + "\n".join(power_lines))

    # ── Nets ───────────────────────────────────────────────────────
    net_lines = []
    for net in nets:
        pin_strs = ", ".join(net.get("pins", []))
        net_lines.append(f"  {net['id']}: {pin_strs}")
    sections.append("NETS:\n" + "\n".join(net_lines))

    return "\n\n".join(sections)


def _component_role(cat_id: str, cat_entry: dict, instance_id: str) -> str:
    """Classify a component into a role for the firmware context."""
    cat_lower = cat_id.lower()
    name_lower = cat_entry.get("name", "").lower()
    inst_lower = instance_id.lower()

    if "atmega" in cat_lower or "mcu" in inst_lower:
        return "mcu"
    if "tactile" in cat_lower or "button" in cat_lower or "btn" in inst_lower:
        return "button"
    if "ir" in inst_lower and ("led" in cat_lower or "diode" in cat_lower or "led" in name_lower):
        return "ir_led"
    if "led" in cat_lower or "led" in inst_lower:
        return "led"
    if "resistor" in cat_lower or "res" in inst_lower:
        return "resistor"
    if "battery" in cat_lower:
        return "battery"
    return "other"
