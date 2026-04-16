"""
G-code pipeline orchestrator — runs the full slice → post-process flow.

This is the single entry point that the main manufacturing pipeline
and web server call.  It:

1. Slices the enclosure STL via PrusaSlicer CLI
2. Computes pause Z-heights from the enclosure geometry
3. Generates conductive-ink toolpath G-code from routing data
4. Post-processes the slicer G-code to inject pauses and ink paths
5. Returns the final staged G-code path and metadata
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.pipeline.config import get_printer
from src.pipeline.gcode.slicer import slice_stl
from src.pipeline.gcode.pause_points import compute_pause_points, PausePoints, ComponentPauseInfo

from src.pipeline.gcode.postprocessor import postprocess_gcode, PostProcessResult
from src.pipeline.gcode.filaments import get_filament, write_filament_overrides

log = logging.getLogger(__name__)


@dataclass
class GcodePipelineResult:
    """Full result of the G-code generation pipeline."""

    success: bool
    message: str
    gcode_path: Path | None = None
    pause_points: PausePoints | None = None
    postprocess: PostProcessResult | None = None
    stages: list[str] = field(default_factory=list)


def run_gcode_pipeline(
    stl_path: Path,
    output_dir: Path,
    routing_result: dict,
    *,
    shell_height: float | None = None,
    layer_height: float = 0.2,
    slicer_profile: Path | None = None,
    printer: str | None = None,
    filament: str = "",
    silverink_only: bool = False,
    component_infos: list[ComponentPauseInfo] | None = None,
    extra_overrides: list[Path] | None = None,
) -> GcodePipelineResult:
    """Run the full G-code pipeline: slice → inject pauses → output.

    Parameters
    ----------
    stl_path : Path
        The enclosure STL to slice (typically ``enclosure.stl``).
    output_dir : Path
        Directory for all output files.
    routing_result : dict
        The ``routing.json`` data (traces with ``[x_mm, y_mm]`` paths).
    shell_height : float, optional
        Total enclosure height.  If *None*, uses the default.
    layer_height : float
        Slicer layer height in mm.
    slicer_profile : Path, optional
        Custom PrusaSlicer ``.ini`` profile.
    printer : str, optional
        Printer id (``"mk3s"`` or ``"coreone"``).
    filament : str, optional
        Filament id (``"prusament_pla"`` or ``"overture_rockpla"``).

    Returns
    -------
    GcodePipelineResult
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stages: list[str] = []

    pdef = get_printer(printer)
    stages.append(f"Printer: {pdef.label} (bed {pdef.bed_width:.0f}×{pdef.bed_depth:.0f} mm)")

    # Resolve filament and write override .ini
    fdef = get_filament(filament)
    filament_ini = write_filament_overrides(filament, output_dir)
    stages.append(f"Filament: {fdef.label}")

    # ── 1. Compute pause points ────────────────────────────────────
    log.info("Computing pause points...")
    jumper_count = len(routing_result.get("jumpers", []))
    pauses = compute_pause_points(
        shell_height=shell_height,
        layer_height=layer_height,
        components=component_infos,
        jumper_count=jumper_count,
    )

    pause_summary = ", ".join(
        f"{p.label} @ Z={p.z:.2f} (L{p.layer_number})" for p in pauses.pauses
    )
    stages.append(f"Pause points: {pause_summary}")
    log.info("Pause points: %s", pause_summary)

    # ── 1b. Prefer print_plate.stl (enclosure + battery hatch) ──
    print_plate = stl_path.parent / "print_plate.stl"
    if print_plate.exists():
        log.info("Found print_plate.stl — slicing combined model")
        stl_path = print_plate
        stages.append("Using print_plate.stl (enclosure + battery hatch)")

    # ── 2. Slice STL ──────────────────────────────────────────────
    slicer_output = output_dir / "_slicer_output.gcode"
    log.info("Slicing %s → %s", stl_path, slicer_output)
    stages.append(f"Slicing {stl_path.name} with PrusaSlicer...")

    ok, msg, slicer_gcode_path = slice_stl(
        stl_path,
        output_gcode=slicer_output,
        profile_path=slicer_profile,
        printer=printer,
        filament_override_path=filament_ini,
        center=pdef.usable_center,
        extra_overrides=extra_overrides,
    )

    # Clean up filament override .ini (no longer needed after slicing)
    if filament_ini and filament_ini.exists():
        filament_ini.unlink()

    if not ok:
        log.error("Slicing failed: %s", msg)
        return GcodePipelineResult(
            success=False,
            message=f"Slicing failed: {msg}",
            pause_points=pauses,
            stages=stages,
        )
    stages.append(f"Slicing succeeded: {slicer_gcode_path}")

    # ── 3. Post-process ───────────────────────────────────────────
    final_gcode = output_dir / "enclosure.gcode"
    log.info("Post-processing G-code...")

    pp_result = postprocess_gcode(
        gcode_path=slicer_gcode_path,
        output_path=final_gcode,
        ink_z=pauses.ink_layer_z,
        component_pauses=[
            (p.z, p.label, p.components) for p in pauses.pauses if p.label != "ink"
        ],
        silverink_only=silverink_only,
    )
    stages.extend(pp_result.stages)
    stages.append(f"G-code written: {final_gcode}")

    # Clean up slicer temp file
    if slicer_gcode_path.exists():
        slicer_gcode_path.unlink()

    log.info("G-code pipeline complete: %s", final_gcode)

    return GcodePipelineResult(
        success=True,
        message="G-code pipeline completed successfully.",
        gcode_path=final_gcode,
        pause_points=pauses,
        postprocess=pp_result,
        stages=stages,
    )
