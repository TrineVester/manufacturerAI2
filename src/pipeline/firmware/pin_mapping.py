"""ATmega328P pin mapping tables.

All three lookup tables are derived from the official ATmega328P datasheet
(DIP-28 package).  They are used by the firmware generator to translate
between PCB router trace endpoints (ATmega port names) and the Arduino
abstraction layer used in the generated sketch.
"""

from __future__ import annotations

# ATmega port name → Arduino digital/analog pin number
ATMEGA_TO_ARDUINO: dict[str, int] = {
    # Port D (digital 0–7)
    "PD0": 0,  "PD1": 1,  "PD2": 2,  "PD3": 3,
    "PD4": 4,  "PD5": 5,  "PD6": 6,  "PD7": 7,
    # Port B (digital 8–13)
    "PB0": 8,  "PB1": 9,  "PB2": 10, "PB3": 11,
    "PB4": 12, "PB5": 13,
    # Port C (analog 0–5 = digital 14–19)
    "PC0": 14, "PC1": 15, "PC2": 16, "PC3": 17,
    "PC4": 18, "PC5": 19,
}

# Arduino pin number → ATmega328 DIP-28 physical pin
ARDUINO_TO_PHYSICAL: dict[int, int] = {
    0: 2,   1: 3,   2: 4,   3: 5,   4: 6,   5: 11,  6: 12,  7: 13,
    8: 14,  9: 15,  10: 16, 11: 17, 12: 18, 13: 19,
    14: 23, 15: 24, 16: 25, 17: 26, 18: 27, 19: 28,
}

# PWM-capable Arduino pins (Timer OC outputs)
PWM_PINS: frozenset[int] = frozenset({3, 5, 6, 9, 10, 11})

# DIP-28 physical pin → descriptive label (for documentation)
PHYSICAL_PIN_LABELS: dict[int, str] = {
    1: "RESET",  2: "PD0/RXD",  3: "PD1/TXD",  4: "PD2/INT0",
    5: "PD3/INT1/OC2B",  6: "PD4/T0",  7: "VCC",  8: "GND",
    9: "PB6/XTAL1",  10: "PB7/XTAL2",  11: "PD5/OC0B/T1",
    12: "PD6/OC0A/AIN0",  13: "PD7/AIN1",  14: "PB0/ICP1",
    15: "PB1/OC1A",  16: "PB2/OC1B/SS",  17: "PB3/OC2A/MOSI",
    18: "PB4/MISO",  19: "PB5/SCK",  20: "AVCC",  21: "AREF",
    22: "GND",  23: "PC0/ADC0",  24: "PC1/ADC1",  25: "PC2/ADC2",
    26: "PC3/ADC3",  27: "PC4/ADC4/SDA",  28: "PC5/ADC5/SCL",
}
