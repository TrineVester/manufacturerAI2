"""Flashlight test fixture — hardcoded DesignSpec for end-to-end pipeline testing.

The flashlight is the simplest possible device:
    Battery → Button → Resistor → LED → Ground

No MCU, no dynamic pin allocation, just 4 components and 4 two-pin nets.
This fixture exercises the full pipeline without needing an LLM.

Components:
  - bat_1:  2xAAA battery holder (bottom mount, auto-placed)
  - btn_1:  tactile button       (top mount, UI-placed at (22.5, 70))
  - r_1:    50Ω resistor         (internal, auto-placed)
  - led_1:  red LED              (top mount, UI-placed at (22.5, 100))

Outline: 45 × 120 mm rectangle.  Wide enough that VCC and GND can
route on opposite sides of the 25×48mm battery holder with the default
trace clearance (3 mm) and edge clearance (2 mm).
"""

from __future__ import annotations

from src.pipeline.design.models import (
    ComponentInstance, Net, OutlineVertex, Outline, UIPlacement, DesignSpec,
)


def make_flashlight_design() -> DesignSpec:
    """Return the hardcoded flashlight DesignSpec."""
    return DesignSpec(
        components=[
            ComponentInstance(
                catalog_id="battery_holder_2xAAA",
                instance_id="bat_1",
            ),
            ComponentInstance(
                catalog_id="tactile_button_6x6",
                instance_id="btn_1",
            ),
            ComponentInstance(
                catalog_id="resistor_axial",
                instance_id="r_1",
                config={"resistance_ohms": 50},
            ),
            ComponentInstance(
                catalog_id="led_5mm",
                instance_id="led_1",
                mounting_style="top",
                config={"color": "red"},
            ),
        ],
        nets=[
            Net(id="VCC",       pins=["bat_1:V+",      "btn_1:A"]),
            Net(id="BTN_GND",   pins=["btn_1:B",       "r_1:1"]),
            Net(id="LED_DRIVE", pins=["r_1:2",         "led_1:anode"]),
            Net(id="GND",       pins=["led_1:cathode", "bat_1:GND"]),
        ],
        outline=Outline(points=[
            OutlineVertex(x=0,  y=0),
            OutlineVertex(x=45, y=0),
            OutlineVertex(x=45, y=120),
            OutlineVertex(x=0,  y=120),
        ]),
        ui_placements=[
            UIPlacement(instance_id="btn_1", x_mm=22.5, y_mm=70),
            UIPlacement(instance_id="led_1", x_mm=22.5, y_mm=100),
        ],
    )
