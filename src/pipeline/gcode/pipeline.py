"""GCode pipeline orchestrator — runs the full slice → pause → post-process flow.

Stages
------
1. Resolve printer + filament definitions
2. Compute pause Z-heights from enclosure geometry + components
3. Locate the STL file (prefer enclosure.stl)
4. Compute bed offset (center model on usable bed area)
5. Run PrusaSlicer CLI
6. Post-process G-code (inject pauses, strip non-critical ironing)
7. Write manufacturing manifest
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

from src.session import Session
from src.catalog.loader import load_catalog
from src.pipeline.design.parsing import parse_design
from src.pipeline.placer.serialization import parse_placement

from .profiles import get_printer, get_filament, PrinterDef, FilamentDef
from .pause_points import (
    compute_pause_points, ComponentPauseInfo, PausePoints,
    FLOOR_MM,
)
from .slicer import slice_stl
from .postprocessor import postprocess_gcode

log = logging.getLogger(__name__)


@dataclass
class GcodePipelineResult:
    """Result of the full G-code pipeline."""

    success: bool
    message: str
    gcode_path: Path | None = None
    pause_points: PausePoints | None = None
    stages: list[str] = field(default_factory=list)


def _bed_center_offset(
    outline_verts: list[tuple[float, float]],
    printer: PrinterDef,
) -> tuple[float, float]:
    """Compute bed X,Y to center the enclosure outline on the usable area.

    The model is centred on (0,0) in model space.  PrusaSlicer's --center
    flag places the model centre at the given bed coordinate.

    We account for keepout margins so the model lands in the printable zone.
    """
    if not outline_verts:
        return printer.usable_center

    xs = [v[0] for v in outline_verts]
    ys = [v[1] for v in outline_verts]
    model_cx = (min(xs) + max(xs)) / 2
    model_cy = (min(ys) + max(ys)) / 2

    bed_cx, bed_cy = printer.usable_center

    # Model origin is at (0,0) — offset to bed centre
    # Y is mirrored in STL export (openscad uses right-hand coords)
    dx = bed_cx - model_cx
    dy = bed_cy + model_cy  # +cy because Y-mirror in STL → bed coords

    return round(dx, 2), round(dy, 2)


def _build_component_pause_infos(
    placement_raw: dict,
    catalog,
) -> list[ComponentPauseInfo]:
    """Extract component pause info from placement + catalog."""
    cat_map = {c.id: c for c in catalog.components}
    infos: list[ComponentPauseInfo] = []

    for comp in placement_raw.get("components", []):
        cat_id = comp.get("catalog_id", "")
        cat_comp = cat_map.get(cat_id)
        if not cat_comp:
            continue

        body_h = cat_comp.body.height_mm
        pin_len = 0.0
        if cat_comp.pins:
            # Use longest pin for Z calculation
            pin_len = max(
                (p.length_mm for p in cat_comp.pins if hasattr(p, "length_mm")),
                default=0.0,
            )

        mounting = "internal"
        if cat_comp.mounting:
            mounting = cat_comp.mounting.style

        infos.append(ComponentPauseInfo(
            instance_id=comp.get("instance_id", cat_id),
            body_height_mm=body_h,
            pin_length_mm=pin_len,
            mounting_style=mounting,
        ))

    return infos


def _write_manifest(
    session: Session,
    result: GcodePipelineResult,
    printer: PrinterDef,
    filament: FilamentDef,
    center: tuple[float, float],
) -> None:
    """Write a manufacturing manifest JSON to the session folder."""
    manifest = {
        "printer": {"id": printer.id, "label": printer.label},
        "filament": {"id": filament.id, "label": filament.label},
        "bed_center": list(center),
        "stages": result.stages,
        "success": result.success,
        "message": result.message,
    }

    if result.pause_points:
        manifest["pause_points"] = [
            {
                "z": p.z,
                "layer_number": p.layer_number,
                "label": p.label,
                "components": p.components,
            }
            for p in result.pause_points.pauses
        ]

    if result.gcode_path and result.gcode_path.exists():
        manifest["gcode_bytes"] = result.gcode_path.stat().st_size

    mfg_dir = session.path / "manufacturing"
    mfg_dir.mkdir(exist_ok=True)
    manifest_path = mfg_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    log.info("Wrote manifest → %s", manifest_path.name)


def run_gcode_pipeline(
    session: Session,
    printer_id: str = "mk3s",
    filament_id: str = "pla",
    cancel=None,
) -> GcodePipelineResult:
    """Run the full G-code pipeline for a session.

    Parameters
    ----------
    session     : Session with placement.json, routing.json, enclosure.stl
    printer_id  : Printer definition ID.
    filament_id : Filament definition ID.
    cancel      : Optional threading.Event for cancellation.

    Returns GcodePipelineResult with gcode_path on success.
    """
    stages: list[str] = []

    # ── 1. Resolve printer + filament ──
    try:
        printer = get_printer(printer_id)
        filament = get_filament(filament_id)
    except ValueError as e:
        return GcodePipelineResult(
            success=False, message=str(e), stages=stages,
        )
    stages.append(f"Printer: {printer.label}, Filament: {filament.label}")

    # ── 2. Load session artifacts ──
    placement_raw = session.read_artifact("placement.json")
    design_raw = session.read_artifact("design.json")
    if placement_raw is None or design_raw is None:
        return GcodePipelineResult(
            success=False,
            message="Missing placement.json or design.json",
            stages=stages,
        )

    catalog = load_catalog()
    design = parse_design(design_raw)
    enclosure = design.enclosure
    shell_height = enclosure.height_mm
    stages.append(f"Shell height: {shell_height:.1f} mm")

    # ── 3. Compute pause points ──
    comp_infos = _build_component_pause_infos(placement_raw, catalog)
    pauses = compute_pause_points(
        shell_height=shell_height,
        layer_height=0.2,
        components=comp_infos,
    )
    stages.append(
        f"Pauses: {len(pauses.pauses)} "
        f"(ink @ Z={pauses.ink_layer_z:.1f}mm, "
        f"{len(pauses.component_pauses)} component)"
    )

    # ── 4. Find STL ──
    stl_path = session.path / "enclosure.stl"
    if not stl_path.exists():
        return GcodePipelineResult(
            success=False,
            message="enclosure.stl not found — compile SCAD first.",
            stages=stages,
            pause_points=pauses,
        )
    stages.append(f"STL: {stl_path.name} ({stl_path.stat().st_size / 1024:.1f} kB)")

    # ── 5. Compute bed offset ──
    outline_verts = [
        (v["x"], v["y"]) if isinstance(v, dict) else tuple(v[:2])
        for v in placement_raw.get("outline", [])
    ]
    center = _bed_center_offset(outline_verts, printer)
    stages.append(f"Bed center: ({center[0]:.1f}, {center[1]:.1f}) mm")

    # ── 6. Slice ──
    mfg_dir = session.path / "manufacturing"
    mfg_dir.mkdir(exist_ok=True)
    gcode_path = mfg_dir / "enclosure.gcode"

    ok, msg = slice_stl(
        stl_path=stl_path,
        gcode_path=gcode_path,
        printer=printer,
        filament=filament,
        center_x=center[0],
        center_y=center[1],
        cancel=cancel,
    )

    if not ok:
        return GcodePipelineResult(
            success=False,
            message=f"Slicing failed: {msg}",
            gcode_path=None,
            pause_points=pauses,
            stages=stages,
        )
    stages.append(f"Sliced: {gcode_path.stat().st_size / 1024:.1f} kB raw G-code")

    # ── 7. Post-process ──
    raw_gcode = gcode_path.read_text(encoding="utf-8", errors="replace")
    processed, pp_result = postprocess_gcode(
        raw_gcode, pauses, pin_floor_z=FLOOR_MM,
    )
    gcode_path.write_text(processed, encoding="utf-8")
    stages.append(
        f"Post-processed: ink={'yes' if pp_result.ink_pause_injected else 'no'}, "
        f"comp_pauses={pp_result.component_pauses_injected}, "
        f"ironing_stripped={pp_result.ironing_layers_stripped}"
    )

    # ── 8. Write manifest ──
    result = GcodePipelineResult(
        success=True,
        message="G-code pipeline complete.",
        gcode_path=gcode_path,
        pause_points=pauses,
        stages=stages,
    )
    _write_manifest(session, result, printer, filament, center)

    return result
