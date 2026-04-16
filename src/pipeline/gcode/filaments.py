"""
Filament definitions — temperature / bed / cooling overrides per filament.

Each filament profile contains only the PrusaSlicer keys that differ
from the base printer profile.  At slice time the overrides are written
to a temporary ``.ini`` that is ``--load``'d *after* the printer
profile so they take precedence.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilamentDef:
    """A filament with its slicer overrides."""

    id: str                   # short key, e.g. "pla"
    label: str                # human-readable, e.g. "PLA"
    overrides: dict[str, str] # PrusaSlicer key→value pairs


# ── Filament catalogue ────────────────────────────────────────────

FILAMENTS: dict[str, FilamentDef] = {
    "pla": FilamentDef(
        id="pla",
        label="PLA",
        overrides={
            "temperature":                      "210",
            "first_layer_temperature":          "210",
            "filament_temperature":             "210",
            "filament_first_layer_temperature":  "210",
            "bed_temperature":                  "40",
            "first_layer_bed_temperature":      "40",
            "fan_always_on":                    "1",
            "min_fan_speed":                    "100",
            "max_fan_speed":                    "100",
            "disable_fan_first_layers":         "0",
            "full_fan_speed_layer":             "0",
        },
    ),
    "petg": FilamentDef(
        id="petg",
        label="PETG",
        overrides={
            "temperature":                      "250",
            "first_layer_temperature":          "250",
            "filament_temperature":             "250",
            "filament_first_layer_temperature":  "250",
            "bed_temperature":                  "80",
            "first_layer_bed_temperature":      "80",
            "fan_always_on":                    "1",
            "min_fan_speed":                    "50",
            "max_fan_speed":                    "50",
            "disable_fan_first_layers":         "3",
            "full_fan_speed_layer":             "4",
        },
    ),
    "tpu_95a": FilamentDef(
        id="tpu_95a",
        label="TPU",
        overrides={
            "temperature":                      "230",
            "first_layer_temperature":          "230",
            "filament_temperature":             "230",
            "filament_first_layer_temperature":  "230",
            "bed_temperature":                  "65",
            "first_layer_bed_temperature":      "65",
            "fan_always_on":                    "0",
            "min_fan_speed":                    "0",
            "max_fan_speed":                    "0",
            "disable_fan_first_layers":         "0",
        },
    ),
    "abs": FilamentDef(
        id="abs",
        label="ABS",
        overrides={
            "temperature":                      "255",
            "first_layer_temperature":          "255",
            "filament_temperature":             "255",
            "filament_first_layer_temperature":  "255",
            "bed_temperature":                  "110",
            "first_layer_bed_temperature":      "100",
            "fan_always_on":                    "0",
            "min_fan_speed":                    "15",
            "max_fan_speed":                    "15",
            "disable_fan_first_layers":         "3",
            "full_fan_speed_layer":             "4",
        },
    ),
    "nylon": FilamentDef(
        id="nylon",
        label="Nylon",
        overrides={
            "temperature":                      "250",
            "first_layer_temperature":          "250",
            "filament_temperature":             "250",
            "filament_first_layer_temperature":  "250",
            "bed_temperature":                  "100",
            "first_layer_bed_temperature":      "100",
            "fan_always_on":                    "0",
            "min_fan_speed":                    "0",
            "max_fan_speed":                    "0",
            "disable_fan_first_layers":         "3",
            "full_fan_speed_layer":             "4",
        },
    ),
}


def get_filament(filament_id: str) -> FilamentDef:
    """Return the *FilamentDef* for *filament_id*.

    Raises ``ValueError`` if *filament_id* is empty or unknown.
    """
    if not filament_id:
        raise ValueError("filament_id is required")
    fid = filament_id.lower().strip()
    if fid not in FILAMENTS:
        raise ValueError(f"Unknown filament '{filament_id}' — available: {', '.join(FILAMENTS)}")
    return FILAMENTS[fid]


def write_filament_overrides(filament_id: str, output_dir: Path) -> Path | None:
    """Write a temporary ``.ini`` with filament overrides.

    Returns the path to the override file, or *None* if no overrides
    are needed (i.e. the filament has no overrides).
    """
    fdef = get_filament(filament_id)
    if not fdef.overrides:
        return None

    ini_path = output_dir / f"_filament_{fdef.id}.ini"
    lines = [
        f"# Filament overrides — {fdef.label}",
        f"# Auto-generated by ManufacturerAI.  Do not edit.",
        "",
    ]
    for key, val in fdef.overrides.items():
        lines.append(f"{key} = {val}")
    lines.append("")

    ini_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote filament overrides: %s (%d keys)", ini_path, len(fdef.overrides))
    return ini_path
