"""
Generate sim_config.json for the simavr harness from routing.json pin assignments.

This is a deterministic step — no LLM involved. It reads the routed pin
assignments and component catalog to produce a config file that tells the
simavr harness which virtual peripherals to attach to which AVR pins.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.pipeline.firmware.firmware_generator import ATMEGA_TO_ARDUINO

log = logging.getLogger(__name__)

# ATmega328P port pin → simavr port letter + pin number
_PORT_MAP: dict[str, tuple[str, int]] = {}
for port_name, arduino_pin in ATMEGA_TO_ARDUINO.items():
    # port_name is like "PD2", "PB1", "PC5"
    port_letter = port_name[1]  # D, B, C
    pin_num = int(port_name[2])
    _PORT_MAP[port_name] = (port_letter, pin_num)


def generate_sim_config(
    circuit: dict[str, Any],
    routing: dict[str, Any],
    catalog_map: dict[str, dict[str, Any]],
    elf_path: str | None = "firmware_build/firmware.ino.elf",
) -> dict[str, Any]:
    """Build a sim_config.json dict from pipeline artifacts.

    Parameters
    ----------
    circuit : dict
        The circuit.json artifact.
    routing : dict
        The routing.json artifact with pin_assignments.
    catalog_map : dict
        Catalog keyed by catalog_id.
    elf_path : str
        Relative path to the ELF binary within the session dir.

    Returns
    -------
    dict
        The simulation config ready to write as JSON.
    """
    pin_assigns = routing.get("pin_assignments", {})
    components = circuit.get("components", [])
    nets = circuit.get("nets", [])

    # Resolve net → ATmega port
    net_to_port: dict[str, str] = {}
    for key, value in pin_assigns.items():
        parts = key.split("|", 1)
        if len(parts) != 2:
            continue
        net_id = parts[0]
        _, port = value.rsplit(":", 1)
        net_to_port[net_id] = port.upper()

    # Build instance → catalog_id lookup
    inst_to_catalog: dict[str, str] = {}
    for comp in components:
        inst_to_catalog[comp["instance_id"]] = comp.get("catalog_id", "")

    # Build instance → net → port mapping
    inst_net_port: dict[str, list[tuple[str, str | None]]] = {}
    for net in nets:
        for pin_ref in net.get("pins", []):
            iid = pin_ref.split(":")[0]
            port = net_to_port.get(net["id"])
            inst_net_port.setdefault(iid, []).append((net["id"], port))

    # Identify resistor instance IDs for indirect tracing
    resistor_ids: set[str] = set()
    for comp in components:
        cid = comp.get("catalog_id", "").lower()
        if "resistor" in cid:
            resistor_ids.add(comp["instance_id"])

    # Build net membership: net_id → set of instance_ids on that net
    net_members: dict[str, set[str]] = {}
    for net in nets:
        members: set[str] = set()
        for pin_ref in net.get("pins", []):
            members.add(pin_ref.split(":")[0])
        net_members[net["id"]] = members

    def _resolve_port_through_resistor(iid: str) -> str | None:
        """Trace through series resistors to find the MCU port driving a component.

        If *iid* shares a net with a resistor, and that resistor is also on
        another net that has an MCU port assignment, return that port name.
        """
        # Nets that `iid` participates in
        my_nets = {nid for nid, _ in inst_net_port.get(iid, [])}

        for net_id in my_nets:
            for res_id in net_members.get(net_id, set()) & resistor_ids:
                # Follow the resistor to its other nets
                for other_net_id, other_port in inst_net_port.get(res_id, []):
                    if other_net_id != net_id and other_port and other_port in _PORT_MAP:
                        return other_port
        return None

    peripherals: list[dict[str, Any]] = []

    for comp in components:
        iid = comp["instance_id"]
        cat_id = comp.get("catalog_id", "")
        role = _classify(cat_id, catalog_map.get(cat_id, {}), iid)

        if role == "mcu" or role == "other":
            continue

        # Find the MCU port for this peripheral – direct or through a resistor
        entries = inst_net_port.get(iid, [])
        port_name: str | None = None
        for _net_id, pn in entries:
            if pn and pn in _PORT_MAP:
                port_name = pn
                break

        if not port_name:
            port_name = _resolve_port_through_resistor(iid)

        if not port_name or port_name not in _PORT_MAP:
            continue

        port_letter, pin_num = _PORT_MAP[port_name]

        if role == "button":
            peripherals.append({
                "instance_id": iid,
                "type": "button",
                "port": port_letter,
                "pin": pin_num,
                "active_low": True,
            })
        elif role == "led":
            peripherals.append({
                "instance_id": iid,
                "type": "led",
                "port": port_letter,
                "pin": pin_num,
                "pwm": ATMEGA_TO_ARDUINO.get(port_name, -1) in {3, 5, 6, 9, 10, 11},
            })
        elif role == "ir_led":
            peripherals.append({
                "instance_id": iid,
                "type": "ir_output",
                "port": port_letter,
                "pin": pin_num,
                "carrier_freq": 38000,
            })

    return {
        "mcu": "atmega328p",
        "frequency": 8000000,
        "elf_path": elf_path,
        "peripherals": peripherals,
    }


def write_sim_config(config: dict[str, Any], output_dir: Path) -> Path:
    """Write sim_config.json to the session directory."""
    path = output_dir / "sim_config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    log.info("Wrote sim_config.json with %d peripherals", len(config.get("peripherals", [])))
    return path


def _classify(cat_id: str, cat_entry: dict, instance_id: str) -> str:
    """Classify a component for simulation purposes."""
    cat_lower = cat_id.lower()
    inst_lower = instance_id.lower()
    name_lower = cat_entry.get("name", "").lower()

    if "atmega" in cat_lower or "mcu" in inst_lower:
        return "mcu"
    if "tactile" in cat_lower or "button" in cat_lower or "btn" in inst_lower:
        return "button"
    if "ir" in inst_lower and ("led" in cat_lower or "diode" in cat_lower or "led" in name_lower):
        return "ir_led"
    if "led" in cat_lower or "led" in inst_lower:
        return "led"
    return "other"
