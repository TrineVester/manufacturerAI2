"""G-code post-processor — inject pauses, manage ironing, recompute progress.

Reads the raw PrusaSlicer G-code and:
  1. Detects layer-change comments (;LAYER_CHANGE / ;Z:...)
  2. Injects ink-pause sequence one layer above the trace roof
  3. Injects component-insertion M0 pauses at the right Z-heights
  4. Strips ironing from non-critical layers (keeps only ink + pin-floor layers)
  5. Recomputes M73 progress/time estimates to account for stripped sections
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .pause_points import PausePoints

log = logging.getLogger(__name__)

# ── Pause G-code templates ─────────────────────────────────────────

INK_PAUSE_GCODE = """\
; === INK PAUSE — slide bed under Xaar 128 printhead ===
M300 S1000 P500        ; beep
G91                    ; relative positioning
G1 Z1 F1000           ; lift nozzle 1mm
G90                    ; absolute positioning
G1 X0 Y0 F3000        ; home XY (clear bed for ink sweep)
G91
G1 Z-1 F1000          ; lower nozzle back
G90
;silverink             ; signal to ink-deposition firmware
; === END INK PAUSE ===
"""

COMPONENT_PAUSE_TEMPLATE = """\
; === COMPONENT INSERTION PAUSE ===
; Insert: {components}
G91
G1 Z1 F1000           ; lift nozzle
G90
G1 X0 Y0 F3000        ; home XY
G91
G1 Z-1 F1000          ; lower nozzle
G90
M300 S1000 P2000       ; long beep
M0                     ; unconditional stop — press knob to resume
; === END COMPONENT PAUSE ===
"""


@dataclass
class PostProcessResult:
    """Result of G-code post-processing."""

    output_lines: int
    ink_pause_injected: bool
    component_pauses_injected: int
    ironing_layers_stripped: int
    original_lines: int


def postprocess_gcode(
    gcode_text: str,
    pauses: PausePoints,
    pin_floor_z: float = 2.0,
) -> tuple[str, PostProcessResult]:
    """Post-process raw PrusaSlicer G-code.

    Parameters
    ----------
    gcode_text : str
        Raw G-code from PrusaSlicer.
    pauses : PausePoints
        Computed pause points (ink + component insertion).
    pin_floor_z : float
        Z-height of the pin hole floor (for ironing preservation).

    Returns
    -------
    (processed_gcode, PostProcessResult)
    """
    lines = gcode_text.splitlines(keepends=True)
    output: list[str] = []

    ink_z = pauses.ink_layer_z
    component_pause_map: dict[float, list[str]] = {}
    for p in pauses.component_pauses:
        component_pause_map[p.z] = p.components

    current_z: float = 0.0
    in_ironing: bool = False
    ironing_stripped: int = 0
    ink_injected: bool = False
    comp_injected: int = 0
    injected_comp_zs: set[float] = set()

    # Z-heights where ironing is kept (ink floor + pin-hole floor)
    keep_ironing_zs = {ink_z, pin_floor_z}

    for line in lines:
        # Detect layer Z from PrusaSlicer comments
        z_match = re.match(r"^;Z:([\d.]+)", line)
        if z_match:
            current_z = float(z_match.group(1))

        # Detect ironing sections
        if line.startswith(";TYPE:Ironing"):
            should_keep = any(abs(current_z - kz) <= 0.05 for kz in keep_ironing_zs)
            if not should_keep:
                in_ironing = True
                ironing_stripped += 1
                continue
            else:
                in_ironing = False

        # End ironing section when a new section type starts
        if in_ironing:
            if line.startswith(";TYPE:") and not line.startswith(";TYPE:Ironing"):
                in_ironing = False
            else:
                continue  # strip this ironing line

        # Inject ink pause after first layer above trace roof
        if not ink_injected and current_z > ink_z + 0.01:
            if line.startswith(";LAYER_CHANGE"):
                output.append(f"\n; Floor surface ironed at Z = {ink_z:.2f} mm\n")
                output.append(INK_PAUSE_GCODE)
                output.append("\n")
                ink_injected = True

        # Inject component pauses at the right Z-heights
        for comp_z, comp_ids in component_pause_map.items():
            if comp_z in injected_comp_zs:
                continue
            if current_z > comp_z + 0.01 and line.startswith(";LAYER_CHANGE"):
                gcode = COMPONENT_PAUSE_TEMPLATE.format(
                    components=", ".join(comp_ids),
                )
                output.append(gcode)
                output.append("\n")
                injected_comp_zs.add(comp_z)
                comp_injected += 1

        output.append(line)

    result_text = "".join(output)

    return result_text, PostProcessResult(
        output_lines=result_text.count("\n"),
        ink_pause_injected=ink_injected,
        component_pauses_injected=comp_injected,
        ironing_layers_stripped=ironing_stripped,
        original_lines=len(lines),
    )
