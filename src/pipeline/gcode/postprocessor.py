"""
G-code post-processor — splits a slicer G-code file at pause points
and injects ironing, ink deposition, and component-insertion pauses.

PrusaSlicer emits layer-change markers as comments:

    ;LAYER_CHANGE
    ;Z:3.200
    ;HEIGHT:0.2

The post-processor walks through the G-code line by line, watches for
these markers, and inserts custom blocks at the correct Z-heights.

Print stages (bottom to top):
  1. Print floor layers (Z = 0 → ink_z)
  2. Iron the ink layer surface (skipping trace channels)
  3. Pause — deposit conductive ink
  4. Resume printing cavity walls (ink_z → component_z)
  5. Pause — insert diode, switches, ATmega328P
  6. Resume and print ceiling to completion

The MK3S firmware supports ``M601`` for filament-change pause (LCD
prompt, beep, wait for user) — we use this for pauses.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Regex for PrusaSlicer layer-change Z comment
_Z_RE = re.compile(r"^;Z:([\d.]+)")
# Regex to extract X/Y from a G0/G1 move
_MOVE_RE = re.compile(
    r"^G[01]\s+"
    r"(?:.*?X(?P<x>[\d.]+))?"
    r"(?:.*?Y(?P<y>[\d.]+))?",
)


@dataclass
class PostProcessResult:
    """Output of the post-processing step."""

    output_path: Path
    total_layers: int
    ink_layer: int
    component_layer: int
    stages: list[str] = field(default_factory=list)


# ── Bed-offset detection ─────────────────────────────────────────


def _stl_bbox_center(stl_path: Path) -> tuple[float, float]:
    """Read an STL (binary or ASCII) and return ``(center_x, center_y)``."""
    import struct

    data = stl_path.read_bytes()
    is_ascii = data.lstrip()[:6].lower() == b"solid " and b"facet" in data[:1000]

    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    if is_ascii:
        _VERTEX_RE = re.compile(
            r"vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)"
        )
        for m in _VERTEX_RE.finditer(data.decode("utf-8", errors="replace")):
            x, y = float(m.group(1)), float(m.group(2))
            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y
    else:
        import io
        f = io.BytesIO(data)
        f.read(80)  # header
        (num_tri,) = struct.unpack("<I", f.read(4))
        for _ in range(num_tri):
            f.read(12)  # normal vector
            for _v in range(3):
                x, y, _z = struct.unpack("<fff", f.read(12))
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y
            f.read(2)  # attribute byte count

    return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)


def compute_bed_offset(
    stl_path: Path,
    bed_size: tuple[float, float],
    *,
    center: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Compute offset from model-local coords to bed coords.

    When *center* is given it must match the ``--center`` argument
    passed to PrusaSlicer.  Otherwise the nominal bed centre is used
    (PrusaSlicer's default auto-centre behaviour).

    Parameters
    ----------
    stl_path : Path
        The STL file that PrusaSlicer is slicing.
    bed_size : tuple
        ``(width, depth)`` of the nominal bed in mm.
    center : tuple, optional
        Explicit ``(x, y)`` centre passed to PrusaSlicer via
        ``--center``.

    Returns ``(offset_x, offset_y)`` in mm.
    """
    model_cx, model_cy = _stl_bbox_center(stl_path)

    if center is not None:
        bed_cx, bed_cy = center
    else:
        bed_cx = bed_size[0] / 2.0
        bed_cy = bed_size[1] / 2.0

    offset_x = bed_cx - model_cx
    offset_y = bed_cy - model_cy

    log.info(
        "Bed offset: STL bbox centre (%.3f, %.3f) → bed centre (%.1f, %.1f) "
        "⇒ offset (%.3f, %.3f)  [%s]",
        model_cx, model_cy, bed_cx, bed_cy, offset_x, offset_y,
        stl_path.name,
    )
    return offset_x, offset_y


def _offset_ink_gcode(
    lines: list[str],
    dx: float,
    dy: float,
) -> list[str]:
    """Shift X/Y coordinates in ink G-code lines by (dx, dy)."""
    result: list[str] = []
    for line in lines:
        if line.startswith(("G0 ", "G1 ")) and ("X" in line or "Y" in line):
            def _shift_coord(m: re.Match) -> str:
                axis = m.group(1)
                val = float(m.group(2))
                offset = dx if axis == "X" else dy
                return f"{axis}{val + offset:.3f}"
            line = re.sub(r"([XY])([\d.]+)", _shift_coord, line)
        result.append(line)
    return result


def _ironing_block(z: float) -> list[str]:
    """Emit a comment block noting the floor was ironed."""
    return [
        "",
        "; " + "-" * 40,
        f"; Floor surface was ironed at Z = {z:.2f} mm",
        "; Surface ready for conductive ink deposition.",
        "; " + "-" * 40,
        "",
    ]




def _ink_pause_block(
    label: str,
    z: float,
    instructions: list[str],
    display_msg: str | None = None,
) -> list[str]:
    """Generate an ink deposition block with head repositioning.

    Lifts the head, homes X/Y, lowers back down, then emits
    the ``;silverink`` marker that tells the Pi firmware to begin the ink sweep.
    """
    lines = [
        "",
        "; " + "=" * 50,
        f"; PAUSE: {label}",
        f"; Z = {z:.2f} mm",
    ]
    for instr in instructions:
        lines.append(f"; >> {instr}")
    lines.append("; " + "=" * 50)

    lines.extend([
        "",
        "M300 S1000 P500 ; beep before silverink",
        "G91 ; relative positioning",
        "G1 Z1 F1000 ; lift head",
        "G90 ; absolute positioning",
        "",
        "G1 X0 Y0 F3000 ; move to home",
        "",
        "G91 ; relative positioning",
        "G1 Z-1 F1000 ; lower head back down",
        "G90 ; absolute positioning",
        "",
    ])
    if display_msg:
        lines.append(";silverink")
    lines.append("")
    return lines


def _pause_block(label: str, z: float, instructions: list[str]) -> list[str]:
    """Generate a component-insertion pause block.

    Homes the head, beeps, and issues ``M0`` (unconditional stop) so the
    user can insert components.  Pressing the knob resumes the print.
    """
    lines = [
        "",
        "; " + "=" * 50,
        f"; PAUSE: {label}",
        f"; Z = {z:.2f} mm",
    ]
    for instr in instructions:
        lines.append(f"; >> {instr}")
    lines.extend([
        "; " + "=" * 50,
        "",
        "G91 ; relative positioning",
        "G1 Z1 F1000 ; lift head",
        "G90 ; absolute positioning",
        "",
        "G1 X0 Y0 F3000 ; move to home",
        "",
        "G91 ; relative positioning",
        "G1 Z-1 F1000 ; lower head back down",
        "G90 ; absolute positioning",
        "",
        "M300 S1000 P2000 ; beep",
        "M0 ; wait for user — press knob to resume",
        "",
    ])
    return lines


# ── M73 recalculation ─────────────────────────────────────────────

_M73_P_RE = re.compile(r"^M73\s+P(\d+)\s+R(\d+)")   # normal mode
_M73_Q_RE = re.compile(r"^M73\s+Q(\d+)\s+S(\d+)")   # silent mode
_TIME_META_RE = re.compile(
    r"^;\s*estimated printing time \((\w+ mode)\)\s*=\s*(.+)",
)


def _recalculate_m73(lines: list[str]) -> list[str]:
    """Recalculate M73 progress/remaining-time commands.

    After ironing is stripped the original M73 commands no longer
    reflect reality — progress jumps from ~74% straight to 100% and
    the initial ``R`` value is far too high.

    Strategy
    --------
    1. Find the original total time from the first ``M73 P0 Rxxx``.
    2. Count *move lines* (G0/G1) as a proxy for elapsed time.
    3. For each M73 command, compute the fraction of move lines that
       precede it and derive new P (progress %) and R (remaining min).
    4. Update the ``estimated printing time`` metadata comments in the
       footer to match.
    """
    # -- Pass 1: count total move lines and find original times -----
    total_moves = 0
    orig_total_normal = 0    # minutes, from first M73 P0 R...
    orig_total_silent = 0    # minutes, from first M73 Q0 S...

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("G0 ") or stripped.startswith("G1 "):
            total_moves += 1
        if not orig_total_normal:
            m = _M73_P_RE.match(stripped)
            if m and int(m.group(1)) == 0:
                orig_total_normal = int(m.group(2))
        if not orig_total_silent:
            m = _M73_Q_RE.match(stripped)
            if m and int(m.group(1)) == 0:
                orig_total_silent = int(m.group(2))

    if total_moves == 0 or (orig_total_normal == 0 and orig_total_silent == 0):
        return lines  # nothing to recalculate

    # The original total time included ironing that we stripped.
    # We need the *new* total time.  Use the last real (pre-final)
    # M73 to figure out how much time the surviving code represents.
    # Walk backwards to find the second-to-last M73 P line.
    last_real_p, last_real_r = 0, 0
    last_real_q, last_real_s = 0, 0
    for line in reversed(lines):
        stripped = line.strip()
        if not last_real_p:
            m = _M73_P_RE.match(stripped)
            if m and int(m.group(1)) < 100:
                last_real_p = int(m.group(1))
                last_real_r = int(m.group(2))
        if not last_real_q:
            m = _M73_Q_RE.match(stripped)
            if m and int(m.group(1)) < 100:
                last_real_q = int(m.group(1))
                last_real_s = int(m.group(2))
        if last_real_p and last_real_q:
            break

    # New total time = time elapsed up to last real marker + remaining
    # time_elapsed = orig_total - last_real_r
    # But last_real_p% of orig was completed, meaning the actual
    # content is (orig_total - last_real_r) in real moves.
    # The stripped ironing accounts for (100 - last_real_p)% of orig.
    # New total ≈ orig_total - (100 - last_real_p)/100 * orig_total
    #           = orig_total * last_real_p / 100 + last_real_r
    # But that double-counts remaining.  Simpler:
    #   new_total = orig_total - stripped_time
    #   stripped_time ≈ last_real_r  (the jump from last_real to end)
    # Actually: new_total = (orig_total - last_real_r)
    # because the remaining last_real_r minutes were all ironing.
    # But that's not quite right either — last_real_r has some real
    # printing too.
    # Best approach: new_total_normal = orig_total_normal * last_real_p / 100 + last_real_r
    # Wait no.  Let's think clearly:
    #   At last_real M73: P=74, R=61 out of orig 238
    #   Time elapsed so far = 238 - 61 = 177 min
    #   Progress = 74%, so 74% of the original print took 177 min
    #   The remaining 26% (ironing) would take 61 min
    #   After stripping, the total print is just those 177 min
    #   new_total = orig_total - last_real_r = 238 - 61 = 177

    new_total_normal = max(orig_total_normal - last_real_r, 1) if last_real_p else orig_total_normal
    new_total_silent = max(orig_total_silent - last_real_s, 1) if last_real_q else orig_total_silent

    # -- Pass 2: rewrite M73 and metadata lines ---------------------
    moves_so_far = 0
    result: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Count moves before this line
        if stripped.startswith("G0 ") or stripped.startswith("G1 "):
            moves_so_far += 1

        # M73 P... R... (normal mode)
        m = _M73_P_RE.match(stripped)
        if m:
            frac = moves_so_far / total_moves if total_moves else 0
            pct = min(int(frac * 100), 100)
            remaining = max(int(new_total_normal * (1.0 - frac) + 0.5), 0)
            result.append(f"M73 P{pct} R{remaining}")
            continue

        # M73 Q... S... (silent mode)
        m = _M73_Q_RE.match(stripped)
        if m:
            frac = moves_so_far / total_moves if total_moves else 0
            pct = min(int(frac * 100), 100)
            remaining = max(int(new_total_silent * (1.0 - frac) + 0.5), 0)
            result.append(f"M73 Q{pct} S{remaining}")
            continue

        # Update estimated printing time metadata in footer
        mt = _TIME_META_RE.match(stripped)
        if mt:
            mode = mt.group(1)
            if mode == "normal mode":
                result.append(f"; estimated printing time ({mode}) = {_fmt_time(new_total_normal)}")
            elif mode == "silent mode":
                result.append(f"; estimated printing time ({mode}) = {_fmt_time(new_total_silent)}")
            else:
                result.append(line)
            continue

        result.append(line)

    log.info(
        "Recalculated M73: normal %dmin→%dmin, silent %dmin→%dmin (%d moves)",
        orig_total_normal, new_total_normal,
        orig_total_silent, new_total_silent,
        total_moves,
    )
    return result


def _fmt_time(minutes: int) -> str:
    """Format minutes as ``Xh Ym Zs`` like PrusaSlicer."""
    h = minutes // 60
    m = minutes % 60
    if h > 0:
        return f"{h}h {m}m 0s"
    return f"{m}m 0s"


def postprocess_gcode(
    gcode_path: Path,
    output_path: Path | None,
    ink_z: float,
    component_pauses: list[tuple[float, str, list[str]]] | None = None,
    silverink_only: bool = False,
) -> PostProcessResult:
    """Read slicer G-code, inject pauses and ink, write result.

    PrusaSlicer ironing is kept on the ink layer (traces / pin holes)
    and stripped from all other layers.

    Parameters
    ----------
    gcode_path : Path
        Input ``.gcode`` from PrusaSlicer.
    output_path : Path or None
        Where to write the modified G-code.  Defaults to
        ``<input>_staged.gcode``.
    ink_z : float
        Z-height for the ink layer (top of floor).
    component_pauses : list of (z, label, component_ids) or None
        Component insertion pauses ordered by Z.  Each tuple contains
        the pause Z-height, a human-readable label, and a list of
        instance_ids to insert.  Falls back to a single pause at the
        highest Z if *None*.

    Returns
    -------
    PostProcessResult
    """
    if output_path is None:
        output_path = gcode_path.with_name(
            gcode_path.stem + "_staged" + gcode_path.suffix
        )

    # Normalise component pauses into a sorted list of (z, label, ids)
    pending_comp_pauses: list[tuple[float, str, list[str]]] = sorted(
        component_pauses or [],
        key=lambda t: t[0],
    )

    raw_lines = gcode_path.read_text(encoding="utf-8").splitlines()

    from src.pipeline.config import TRACE_HEIGHT_MM, PIN_FLOOR_PENETRATION
    trace_roof_z = ink_z + TRACE_HEIGHT_MM
    pin_floor_z = ink_z - PIN_FLOOR_PENETRATION

    out: list[str] = []
    total_layers = 0
    ironing_injected = False
    ink_injected = False
    comp_pause_idx = 0           # index into pending_comp_pauses
    ink_layer_num = -1
    comp_layer_nums: list[int] = []
    ironing_layers_stripped = 0
    ironing_lines_stripped = 0
    current_z = 0.0

    # silverink_only: skip all layers before the ink layer, keeping
    # only the startup preamble (before the first ;LAYER_CHANGE) and
    # the ink layer itself (at ink_z).
    past_preamble = False       # True once we've seen the first ;LAYER_CHANGE
    at_ink_layer = False        # True once we reach the ink layer (Z ≈ ink_z)

    stages = []

    track_x, track_y = 0.0, 0.0

    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]

        # Detect when the preamble ends (first ;LAYER_CHANGE)
        if silverink_only and not past_preamble and line.strip() == ';LAYER_CHANGE':
            past_preamble = True

        # In silverink_only mode, detect when we arrive at the ink layer
        if silverink_only and past_preamble and not at_ink_layer:
            z_peek = _Z_RE.match(line)
            if z_peek:
                z_peek_val = float(z_peek.group(1))
                if abs(z_peek_val - ink_z) < 0.01:
                    at_ink_layer = True

        # silverink_only: skip lines between preamble and ink layer
        if silverink_only and past_preamble and not at_ink_layer:
            i += 1
            continue

        # Track nozzle position from G0/G1 moves
        m_pos = _MOVE_RE.match(line)
        if m_pos:
            if m_pos.group("x"):
                track_x = float(m_pos.group("x"))
            if m_pos.group("y"):
                track_y = float(m_pos.group("y"))

        # Detect layer change
        z_match = _Z_RE.match(line)
        if z_match:
            z_val = float(z_match.group(1))
            total_layers += 1

            current_z = z_val

            # ── Ironing at floor level ────────────────────────
            # Trigger one layer ABOVE ink_z so the floor layer
            # (including ironing) prints first.
            if not ironing_injected and z_val > ink_z + 0.01:
                ironing_injected = True
                ink_layer_num = total_layers

                out.extend(_ironing_block(ink_z))
                stages.append(f"Ironing at Z={ink_z:.2f}")

            # ── Ink layer pause at trace roof ────────────────
            # Trigger after trace channel walls are printed so
            # that ink is deposited into the open channels right
            # before they are closed off.
            if not ink_injected and z_val > trace_roof_z + 0.01:
                ink_injected = True

                out.extend(_ink_pause_block(
                    "DEPOSIT CONDUCTIVE INK",
                    trace_roof_z,
                    [
                        "Trace channels have been printed.",
                        "Deposit conductive ink into the channels.",
                        "Press the knob when done to resume printing.",
                    ],
                    display_msg="connect silver ink",
                ))

                stages.append(f"Ink pause at Z={trace_roof_z:.2f}")
                stages.append(f"Ink layer: {ink_layer_num}")

                if silverink_only:
                    stages.append("Silver ink debug mode — stopping after ink pause")
                    z_offset = trace_roof_z - 0.2
                    _Z_PARAM = re.compile(r'(?<=Z)([\d.]+)')
                    shifted_out = []
                    for ol in out:
                        s_ol = ol.strip()
                        if s_ol.startswith(';Z:'):
                            old_z = float(s_ol[3:])
                            shifted_out.append(f";Z:{max(old_z - z_offset, 0.0):.3f}")
                        elif re.match(r'^G[01]\s', s_ol) and 'Z' in ol:
                            shifted_out.append(_Z_PARAM.sub(
                                lambda m: f"{max(float(m.group(1)) - z_offset, 0.0):.3f}", ol
                            ))
                        else:
                            shifted_out.append(ol)
                    out = shifted_out
                    stages.append(f"Z-offset: shifted down by {z_offset:.2f} mm")
                    for j in range(len(raw_lines) - 1, -1, -1):
                        if raw_lines[j].strip() == "; prusaslicer_config = begin":
                            out.extend(raw_lines[j:])
                            break
                    break

            # ── Component insertion pauses (N stages) ──────────
            while comp_pause_idx < len(pending_comp_pauses):
                cp_z, cp_label, cp_ids = pending_comp_pauses[comp_pause_idx]
                if z_val < cp_z - 0.001:
                    break
                comp_layer_nums.append(total_layers)

                if cp_label == "jumpers":
                    instructions = [
                        "Insert jumper wires into their channels.",
                        "Press each wire endpoint into its pinhole.",
                        "Press the knob when done to resume printing.",
                    ]
                elif cp_ids:
                    comp_lines = [
                        f"  - {cid}" for cid in cp_ids
                    ]
                    instructions = [
                        "Insert the following components into their pockets:",
                        *comp_lines,
                        "Ensure all pins seat fully into their pin holes.",
                        "Press the knob when done to resume printing.",
                    ]
                else:
                    instructions = [
                        "Insert components into their pockets.",
                        "Ensure all pins seat fully into their pin holes.",
                        "Press the knob when done to resume printing.",
                    ]
                out.extend(_pause_block(
                    f"INSERT COMPONENTS ({cp_label})",
                    cp_z,
                    instructions,
                ))
                stages.append(f"Component pause '{cp_label}' at Z={cp_z:.2f}")
                comp_pause_idx += 1

        # ── Strip ironing from non-ink layers ─────────────────
        # PrusaSlicer irons all top surfaces; we only want ironing
        # on the ink layer (traces) and the pin hole floor layer.
        # All other layers are stripped to save time.
        stripped_line = line.strip()
        strip_infill = (
            silverink_only
            and stripped_line == ';TYPE:Internal infill'
        )
        if strip_infill:
            # Collect all lines in the infill section
            section_inf: list[str] = []
            i += 1
            while i < len(raw_lines):
                nxt = raw_lines[i].strip()
                if nxt.startswith(';TYPE:') or nxt.startswith(';LAYER_CHANGE'):
                    break
                section_inf.append(raw_lines[i])
                i += 1

            # Remove preamble (retract / G92 E0) already appended to `out`
            preamble_start_inf = None
            for k in range(len(out) - 1, max(0, len(out) - 20), -1):
                if out[k].strip() == 'G92 E0':
                    preamble_start_inf = k
                    if k > 0 and re.match(
                        r'^G1\s+E[\d.]+\s+F\d+', out[k - 1].strip()
                    ):
                        preamble_start_inf = k - 1
                    break
            if preamble_start_inf is None:
                for k in range(len(out) - 1, max(0, len(out) - 15), -1):
                    s = out[k].strip()
                    if re.match(r'^G1\s+E-[\d.]+\s+F\d+$', s):
                        preamble_start_inf = k
                        break
                    if re.match(r'^G1\s+.*[XY].*E[\d.]', s):
                        break
            if preamble_start_inf is not None:
                del out[preamble_start_inf:]

            # ── Preserve postamble travel to the next section ──
            # Without this, the nozzle jumps between pads with no
            # retraction, causing stringing.
            next_is_print = (
                i < len(raw_lines)
                and raw_lines[i].strip().startswith(';TYPE:')
                and not raw_lines[i].strip().startswith(';TYPE:Custom')
            )
            if next_is_print and section_inf:
                # Method 1: G92 E0 (MK3S absolute-E mode)
                g92_idx = None
                for k in range(len(section_inf) - 1, -1, -1):
                    if section_inf[k].strip() == 'G92 E0':
                        g92_idx = k
                        break

                if g92_idx is not None:
                    kept = section_inf[g92_idx:]
                    for kl in kept:
                        out.append(kl)
                        m_k = _MOVE_RE.match(kl)
                        if m_k:
                            if m_k.group("x"):
                                track_x = float(m_k.group("x"))
                            if m_k.group("y"):
                                track_y = float(m_k.group("y"))
                else:
                    # Method 2: Core One M83 — emit corrective travel
                    target_x, target_y, target_z = track_x, track_y, current_z
                    for line_s in section_inf:
                        m_k = _MOVE_RE.match(line_s)
                        if m_k:
                            if m_k.group("x"):
                                target_x = float(m_k.group("x"))
                            if m_k.group("y"):
                                target_y = float(m_k.group("y"))
                        z_m = re.match(r'^G[01]\s+Z([\d.]+)', line_s.strip())
                        if z_m:
                            target_z = float(z_m.group(1))

                    dist = math.hypot(target_x - track_x, target_y - track_y)
                    if dist > 0.5:
                        out.append(f"G1 E-0.8 F2700 ; retract (infill stripped)")
                        out.append(f"G0 Z{target_z + 0.6:.3f} F720 ; Z-hop")
                        out.append(f"G0 X{target_x:.3f} Y{target_y:.3f} F21000 ; travel (infill stripped)")
                        out.append(f"G0 Z{target_z:.3f} F720 ; lower")
                        out.append(f"G1 E0.8 F1500 ; unretract")
                    track_x, track_y = target_x, target_y

            log.debug(
                "Stripped infill section '%s' at Z=%.2f (%d lines)",
                stripped_line, current_z, len(section_inf),
            )
            continue  # don't append the ;TYPE:…infill line itself

        keep_ironing = (
            abs(current_z - ink_z) <= 0.05
            or abs(current_z - pin_floor_z) <= 0.05
        )
        strip_ironing = (
            stripped_line == ';TYPE:Ironing'
            and not keep_ironing
        )
        if strip_ironing:
            # Collect all lines in the ironing section
            section: list[str] = []
            i += 1
            while i < len(raw_lines):
                nxt = raw_lines[i].strip()
                if nxt.startswith(';TYPE:') or nxt.startswith(';LAYER_CHANGE'):
                    break
                section.append(raw_lines[i])
                i += 1

            # ── Remove preamble from `out` ──
            # Walk backwards to find the retract / G92 E0 that
            # precedes the travel → unretract leading into ironing.
            preamble_start = None
            # Method 1: G92 E0 (MK3S absolute-E mode)
            for k in range(len(out) - 1, max(0, len(out) - 20), -1):
                if out[k].strip() == 'G92 E0':
                    preamble_start = k
                    if k > 0 and re.match(
                        r'^G1\s+E[\d.]+\s+F\d+', out[k - 1].strip()
                    ):
                        preamble_start = k - 1
                    break
            # Method 2: Core One M83 — find last retract (G1 E-… F…)
            if preamble_start is None:
                for k in range(len(out) - 1, max(0, len(out) - 15), -1):
                    s = out[k].strip()
                    if re.match(r'^G1\s+E-[\d.]+\s+F\d+$', s):
                        preamble_start = k
                        break
                    # Stop at the previous extrusion move
                    if re.match(r'^G1\s+.*[XY].*E[\d.]', s):
                        break

            preamble_removed = 0
            if preamble_start is not None:
                preamble_removed = len(out) - preamble_start
                del out[preamble_start:]

            # ── Determine what follows ──
            next_is_print_type = (
                i < len(raw_lines)
                and raw_lines[i].strip().startswith(';TYPE:')
                and not raw_lines[i].strip().startswith(';TYPE:Custom')
            )

            skipped = len(section)
            if next_is_print_type and section:
                # Another print section follows — keep the travel
                # from the ironing postamble to the next section.

                # Method 1: G92 E0 (MK3S)
                g92_idx = None
                for k in range(len(section) - 1, -1, -1):
                    if section[k].strip() == 'G92 E0':
                        g92_idx = k
                        break

                if g92_idx is not None:
                    # Keep from G92 E0 onward (travel + unretract).
                    kept = section[g92_idx:]
                    skipped = g92_idx
                    for kl in kept:
                        out.append(kl)
                        m_k = _MOVE_RE.match(kl)
                        if m_k:
                            if m_k.group("x"):
                                track_x = float(m_k.group("x"))
                            if m_k.group("y"):
                                track_y = float(m_k.group("y"))
                else:
                    # Method 2: Core One M83 — no G92 E0 markers.
                    # Parse the section to find where the postamble
                    # would position the nozzle, then emit a clean
                    # retract → travel → unretract sequence.
                    target_x, target_y, target_z = track_x, track_y, current_z
                    last_m204 = None
                    for line_s in section:
                        m_k = _MOVE_RE.match(line_s)
                        if m_k:
                            if m_k.group("x"):
                                target_x = float(m_k.group("x"))
                            if m_k.group("y"):
                                target_y = float(m_k.group("y"))
                        z_m = re.match(r'^G[01]\s+Z([\d.]+)', line_s.strip())
                        if z_m:
                            target_z = float(z_m.group(1))
                        if line_s.strip().startswith('M204'):
                            last_m204 = line_s

                    # Emit corrective travel to the position the
                    # ironing postamble would have reached.
                    dist = math.hypot(target_x - track_x, target_y - track_y)
                    if dist > 0.5:
                        out.append(f"G1 E-0.8 F2700 ; retract (ironing stripped)")
                        out.append(f"G0 Z{target_z + 0.6:.3f} F720 ; Z-hop")
                        out.append(f"G0 X{target_x:.3f} Y{target_y:.3f} F21000 ; travel (ironing stripped)")
                        out.append(f"G0 Z{target_z:.3f} F720 ; lower")
                        out.append(f"G1 E0.8 F1500 ; unretract")
                    if last_m204:
                        out.append(last_m204)
                    track_x, track_y = target_x, target_y
                    skipped = len(section)

            ironing_layers_stripped += 1
            ironing_lines_stripped += skipped + preamble_removed
            log.debug(
                "Stripped ironing at Z=%.2f (%d ironing + %d preamble stripped, %d kept)",
                current_z, skipped, preamble_removed,
                len(section) - skipped,
            )
            continue  # don't append the ;TYPE:Ironing line itself

        # Append the current line
        out.append(line)

        i += 1

    # ── Recalculate M73 progress after ironing was stripped ────
    if ironing_lines_stripped:
        out = _recalculate_m73(out)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    log.info(
        "Post-processed G-code: %d layers, ink@L%d (Z=%.2f), %d component pauses → %s",
        total_layers, ink_layer_num, ink_z, len(comp_layer_nums), output_path,
    )
    if ironing_layers_stripped:
        log.info(
            "  Stripped ironing from %d non-ink layers (%d lines removed)",
            ironing_layers_stripped, ironing_lines_stripped,
        )
        stages.append(
            f"Stripped ironing from {ironing_layers_stripped} non-ink layers "
            f"({ironing_lines_stripped} G-code lines removed)"
        )
    return PostProcessResult(
        output_path=output_path,
        total_layers=total_layers,
        ink_layer=ink_layer_num,
        component_layer=comp_layer_nums[0] if comp_layer_nums else -1,
        stages=stages,
    )
