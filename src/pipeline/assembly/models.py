"""Assembly guide data models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WiringConnection:
    """A single wire/trace between two pins."""
    net_id: str
    from_instance: str
    from_pin: str
    to_instance: str
    to_pin: str
    trace_length_mm: float | None = None


@dataclass
class ComponentStep:
    """One assembly step for a component (or group of same-type components)."""
    step_number: int
    title: str
    component_type: str          # controller, button, led, resistor, ...
    instances: list[dict]        # [{instance_id, catalog_id, x_mm, y_mm, rotation_deg}]
    instructions: list[str]      # ordered instruction strings
    warnings: list[str] = field(default_factory=list)
    wiring: list[WiringConnection] = field(default_factory=list)


@dataclass
class AssemblyGuide:
    """Complete assembly guide for a session."""
    total_components: int
    total_connections: int
    checklist: list[dict]        # [{type, label, count}]
    steps: list[ComponentStep]
    final_checks: list[str]
