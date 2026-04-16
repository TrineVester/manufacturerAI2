"""
Static validation of generated Arduino firmware against routing pin assignments.

Runs *before* compilation to catch pin mismatches, missing includes, and
conflicting pin usage. Returns a list of human-readable error strings that
can be fed directly back to the LLM for self-correction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.pipeline.firmware.firmware_generator import ATMEGA_TO_ARDUINO, PWM_PINS


# ── Public API ──────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        parts: list[str] = []
        if self.errors:
            parts.append("ERRORS:\n" + "\n".join(f"  • {e}" for e in self.errors))
        if self.warnings:
            parts.append("WARNINGS:\n" + "\n".join(f"  • {w}" for w in self.warnings))
        return "\n".join(parts) if parts else "All checks passed."


def validate_firmware(
    code: str,
    circuit: dict[str, Any],
    routing: dict[str, Any],
    catalog_map: dict[str, dict[str, Any]],
) -> ValidationResult:
    """Validate firmware source against the routed circuit.

    Parameters
    ----------
    code : str
        The Arduino .ino source code.
    circuit : dict
        circuit.json — components and nets.
    routing : dict
        routing.json — pin_assignments.
    catalog_map : dict
        Catalog keyed by catalog_id.

    Returns
    -------
    ValidationResult
        Errors (must fix) and warnings (should fix).
    """
    result = ValidationResult()

    expected = _build_expected_pins(circuit, routing, catalog_map)
    code_pins = _extract_pins_from_code(code)

    _check_pin_assignments(expected, code_pins, result)
    _check_pwm_usage(expected, code_pins, result)
    _check_pin_conflicts(code_pins, result)
    _check_includes(code, circuit, catalog_map, result)

    return result


# ── Expected pin map from routing ───────────────────────────────────────────

@dataclass
class ExpectedPin:
    """A pin the firmware *must* use, derived from routing.json."""
    instance_id: str
    role: str          # "button", "led", "ir_led", etc.
    arduino_pin: int
    port_name: str     # e.g. "PB1"


def _build_expected_pins(
    circuit: dict[str, Any],
    routing: dict[str, Any],
    catalog_map: dict[str, dict[str, Any]],
) -> list[ExpectedPin]:
    """Build the list of pins the firmware is expected to reference."""
    from src.pipeline.firmware.context_builder import _component_role

    pin_assigns = routing.get("pin_assignments", {})
    components = circuit.get("components", [])
    nets = circuit.get("nets", [])

    # net_id → MCU port name (e.g. "PB1")
    net_to_port: dict[str, str] = {}
    for key, value in pin_assigns.items():
        parts = key.split("|", 1)
        if len(parts) != 2:
            continue
        net_id = parts[0]
        _, port = value.rsplit(":", 1)
        port = port.upper()
        if port in ATMEGA_TO_ARDUINO:
            net_to_port[net_id] = port

    # Identify resistors for indirect tracing
    resistor_ids: set[str] = set()
    for comp in components:
        if "resistor" in comp.get("catalog_id", "").lower():
            resistor_ids.add(comp["instance_id"])

    # net membership and instance-to-nets
    net_members: dict[str, set[str]] = {}
    inst_nets: dict[str, list[str]] = {}
    for net in nets:
        members: set[str] = set()
        for pin_ref in net.get("pins", []):
            iid = pin_ref.split(":")[0]
            members.add(iid)
            inst_nets.setdefault(iid, []).append(net["id"])
        net_members[net["id"]] = members

    def _resolve_port(iid: str) -> str | None:
        """Direct lookup then resistor-chain trace."""
        for nid in inst_nets.get(iid, []):
            port = net_to_port.get(nid)
            if port:
                return port
        # Trace through resistors
        for nid in inst_nets.get(iid, []):
            for res_id in net_members.get(nid, set()) & resistor_ids:
                for other_nid in inst_nets.get(res_id, []):
                    if other_nid != nid:
                        port = net_to_port.get(other_nid)
                        if port:
                            return port
        return None

    expected: list[ExpectedPin] = []
    for comp in components:
        iid = comp["instance_id"]
        cat_id = comp.get("catalog_id", "")
        cat_entry = catalog_map.get(cat_id, {})
        role = _component_role(cat_id, cat_entry, iid)

        if role in ("mcu", "resistor", "battery", "other"):
            continue

        port = _resolve_port(iid)
        if not port:
            continue

        arduino_pin = ATMEGA_TO_ARDUINO.get(port)
        if arduino_pin is None:
            continue

        expected.append(ExpectedPin(
            instance_id=iid,
            role=role,
            arduino_pin=arduino_pin,
            port_name=port,
        ))

    return expected


# ── Extract pins referenced in the source code ─────────────────────────────

# Match `#define NAME 3` but NOT `#define NAME 0x04` (hex) or `#define NAME 50UL` (large)
_DEFINE_RE = re.compile(
    r"#define\s+(\w+)\s+(\d+)(?:UL)?\s*(?://|/\*|$)",
)
_CONST_PIN_RE = re.compile(
    r"(?:const\s+)?(?:uint8_t|int|byte)\s+(\w+)\s*=\s*(\d+)\s*;",
)
# Detect hex defines so we can skip them
_HEX_DEFINE_RE = re.compile(
    r"#define\s+(\w+)\s+0x[0-9A-Fa-f]+",
)

# Only symbols whose name suggests a pin assignment
_PIN_NAME_RE = re.compile(
    r"(?:pin|btn|button|led|send|ir_send|output|input)",
    re.IGNORECASE,
)


def _extract_pins_from_code(code: str) -> dict[str, int]:
    """Return a mapping of symbol name → pin number from the source."""
    # Collect hex-defined names so we never treat them as pin numbers
    hex_names: set[str] = {m.group(1) for m in _HEX_DEFINE_RE.finditer(code)}

    pins: dict[str, int] = {}

    for m in _DEFINE_RE.finditer(code):
        name, val = m.group(1), int(m.group(2))
        if name in hex_names:
            continue
        # Only include if the value is in Arduino pin range AND the name
        # looks like a pin definition (contains "pin", "btn", "led", etc.)
        if val <= 19 and _PIN_NAME_RE.search(name):
            pins[name] = val

    for m in _CONST_PIN_RE.finditer(code):
        name, val = m.group(1), int(m.group(2))
        if name in hex_names:
            continue
        if val <= 19 and _PIN_NAME_RE.search(name):
            pins[name] = val

    return pins


# ── Validation checks ──────────────────────────────────────────────────────

def _check_pin_assignments(
    expected: list[ExpectedPin],
    code_pins: dict[str, int],
    result: ValidationResult,
) -> None:
    """Verify every routed peripheral has a matching pin in the code."""
    code_pin_values = set(code_pins.values())

    for ep in expected:
        if ep.arduino_pin not in code_pin_values:
            result.errors.append(
                f"{ep.instance_id} ({ep.role}) is routed to Arduino pin "
                f"{ep.arduino_pin} ({ep.port_name}), but pin {ep.arduino_pin} "
                f"does not appear in the code. "
                f"Add or fix the #define for this component."
            )


def _check_pwm_usage(
    expected: list[ExpectedPin],
    code_pins: dict[str, int],
    result: ValidationResult,
) -> None:
    """Check that IR LEDs and PWM outputs use PWM-capable pins."""
    for ep in expected:
        if ep.role == "ir_led" and ep.arduino_pin not in PWM_PINS:
            result.warnings.append(
                f"{ep.instance_id} (IR LED) is routed to pin {ep.arduino_pin} "
                f"which is NOT PWM-capable. 38kHz carrier requires a PWM pin. "
                f"This is a routing issue, not a firmware issue."
            )

    # Check if the code assigns an IR send pin to a non-PWM pin
    for name, pin in code_pins.items():
        name_lower = name.lower()
        if ("ir" in name_lower and "pin" in name_lower) or "send_pin" in name_lower:
            if pin not in PWM_PINS:
                for ep in expected:
                    if ep.role == "ir_led" and ep.arduino_pin in PWM_PINS:
                        result.errors.append(
                            f"Code defines {name}={pin} but routing assigns the IR LED "
                            f"to pin {ep.arduino_pin} (PWM-capable). Use pin "
                            f"{ep.arduino_pin} instead."
                        )
                        break


def _check_pin_conflicts(
    code_pins: dict[str, int],
    result: ValidationResult,
) -> None:
    """Detect multiple symbols mapped to the same physical pin."""
    pin_to_names: dict[int, list[str]] = {}
    for name, pin in code_pins.items():
        pin_to_names.setdefault(pin, []).append(name)

    for pin, names in pin_to_names.items():
        if len(names) > 1:
            result.errors.append(
                f"Pin conflict: Arduino pin {pin} is used by multiple "
                f"definitions: {', '.join(names)}. Each component needs "
                f"its own unique pin."
            )


# Library requirements by component role
_ROLE_INCLUDES: dict[str, tuple[str, ...]] = {
    "ir_led": ("IRremote",),
}


def _check_includes(
    code: str,
    circuit: dict[str, Any],
    catalog_map: dict[str, dict[str, Any]],
    result: ValidationResult,
) -> None:
    """Verify required #include statements are present."""
    from src.pipeline.firmware.context_builder import _component_role

    roles_present: set[str] = set()
    for comp in circuit.get("components", []):
        cat_id = comp.get("catalog_id", "")
        cat_entry = catalog_map.get(cat_id, {})
        role = _component_role(cat_id, cat_entry, comp["instance_id"])
        roles_present.add(role)

    for role in roles_present:
        required_libs = _ROLE_INCLUDES.get(role, ())
        for lib in required_libs:
            if lib not in code:
                result.errors.append(
                    f"Circuit contains a {role} component but the code does "
                    f"not #include <{lib}.hpp> (or similar). Add the include."
                )
