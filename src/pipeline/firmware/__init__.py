"""Firmware module — ATmega328P pin mappings and firmware pipeline utilities."""

from .firmware_generator import (
    atmega_port_to_arduino_pin,
    arduino_pin_to_physical,
    is_pwm_pin,
    FIRMWARE_DIR,
)

__all__ = [
    "atmega_port_to_arduino_pin",
    "arduino_pin_to_physical",
    "is_pwm_pin",
    "FIRMWARE_DIR",
]
