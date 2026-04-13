"""Printer and filament definitions for PrusaSlicer integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrinterDef:
    """Physical printer description."""

    id: str
    label: str
    nominal_bed_width: float   # mm
    nominal_bed_depth: float   # mm
    keepout_left: float = 0.0
    keepout_right: float = 0.0
    keepout_front: float = 0.0
    keepout_back: float = 0.0
    max_z_mm: float = 210.0
    profile_filename: str = ""

    @property
    def usable_width(self) -> float:
        return self.nominal_bed_width - self.keepout_left - self.keepout_right

    @property
    def usable_depth(self) -> float:
        return self.nominal_bed_depth - self.keepout_front - self.keepout_back

    @property
    def usable_center(self) -> tuple[float, float]:
        cx = self.keepout_left + self.usable_width / 2
        cy = self.keepout_front + self.usable_depth / 2
        return cx, cy


@dataclass(frozen=True)
class FilamentDef:
    """PrusaSlicer filament overrides."""

    id: str
    label: str
    overrides: dict[str, str] = field(default_factory=dict)


# ── Built-in printers ─────────────────────────────────────────────

MK3S = PrinterDef(
    id="mk3s",
    label="Original Prusa i3 MK3S",
    nominal_bed_width=250.0,
    nominal_bed_depth=210.0,
    keepout_left=5.0,
    keepout_right=5.0,
    keepout_front=3.0,
    keepout_back=3.0,
    max_z_mm=210.0,
    profile_filename="slicer_profile_mk3s.ini",
)

MK3S_PLUS = PrinterDef(
    id="mk3s_plus",
    label="Original Prusa i3 MK3S+",
    nominal_bed_width=250.0,
    nominal_bed_depth=210.0,
    keepout_left=5.0,
    keepout_right=5.0,
    keepout_front=3.0,
    keepout_back=3.0,
    max_z_mm=210.0,
    profile_filename="slicer_profile_mk3s_plus.ini",
)

PRINTERS: dict[str, PrinterDef] = {
    p.id: p for p in [MK3S, MK3S_PLUS]
}

# ── Built-in filaments ────────────────────────────────────────────

PLA = FilamentDef(
    id="pla",
    label="PLA",
    overrides={
        "temperature": "215",
        "first_layer_temperature": "220",
        "bed_temperature": "60",
        "first_layer_bed_temperature": "60",
    },
)

PETG = FilamentDef(
    id="petg",
    label="PETG",
    overrides={
        "temperature": "240",
        "first_layer_temperature": "245",
        "bed_temperature": "85",
        "first_layer_bed_temperature": "85",
    },
)

FILAMENTS: dict[str, FilamentDef] = {
    f.id: f for f in [PLA, PETG]
}


def get_printer(printer_id: str = "mk3s") -> PrinterDef:
    """Look up a printer definition by ID."""
    p = PRINTERS.get(printer_id)
    if p is None:
        raise ValueError(f"Unknown printer '{printer_id}'. Known: {list(PRINTERS)}")
    return p


def get_filament(filament_id: str = "pla") -> FilamentDef:
    """Look up a filament definition by ID."""
    f = FILAMENTS.get(filament_id)
    if f is None:
        raise ValueError(f"Unknown filament '{filament_id}'. Known: {list(FILAMENTS)}")
    return f
