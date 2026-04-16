"""
ATmega328P pin mapping constants and utilities.

Used by the firmware pipeline (context_builder, validate_firmware, sim_config)
to convert between ATmega port names, Arduino pin numbers, and physical DIP-28
pin positions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

FIRMWARE_DIR = Path(__file__).parent

# ATmega328 port name → Arduino pin number mapping
ATMEGA_TO_ARDUINO: dict[str, int] = {
    # Port D (digital 0-7)
    "PD0": 0, "PD1": 1, "PD2": 2, "PD3": 3,
    "PD4": 4, "PD5": 5, "PD6": 6, "PD7": 7,
    # Port B (digital 8-13)
    "PB0": 8, "PB1": 9, "PB2": 10, "PB3": 11, "PB4": 12, "PB5": 13,
    # Port C (analog 0-5, can be used as digital 14-19)
    "PC0": 14, "PC1": 15, "PC2": 16, "PC3": 17, "PC4": 18, "PC5": 19,
}

# Arduino pin number → ATmega physical DIP-28 pin
ARDUINO_TO_PHYSICAL: dict[int, int] = {
    0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 11, 6: 12, 7: 13,
    8: 14, 9: 15, 10: 16, 11: 17, 12: 18, 13: 19,
    14: 23, 15: 24, 16: 25, 17: 26, 18: 27, 19: 28,
}

# PWM-capable Arduino pins (required for IR LED)
PWM_PINS = {3, 5, 6, 9, 10, 11}


def atmega_port_to_arduino_pin(port_name: str) -> Optional[int]:
    """Convert ATmega port name (e.g., 'PD3') to Arduino pin number (e.g., 3)."""
    return ATMEGA_TO_ARDUINO.get(port_name.upper())


def arduino_pin_to_physical(arduino_pin: int) -> Optional[int]:
    """Convert Arduino pin number to ATmega328 DIP-28 physical pin number."""
    return ARDUINO_TO_PHYSICAL.get(arduino_pin)


def is_pwm_pin(arduino_pin: int) -> bool:
    """Check if an Arduino pin is PWM-capable (required for IR LED)."""
    return arduino_pin in PWM_PINS
