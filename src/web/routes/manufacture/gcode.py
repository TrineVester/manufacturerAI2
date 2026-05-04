from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.pipeline.design import parse_physical_design
from src.web.routes._deps import (
    get_gcode_state, set_gcode_state,
    get_compile_state,
    load_session_or_404, require_routing,
)

router = APIRouter()


@router.post("/sessions/{sid}/manufacture/gcode")
async def start_gcode(
    sid: str,
    force: bool = Query(False),
    silverink_only: bool = Query(False),
):
    s = load_session_or_404(sid)
    c_state = get_compile_state(sid)
    if c_state and c_state.get("status") == "compiling":
        raise HTTPException(400, "STL is still compiling — please wait for the 3D model to finish building before running manufacturing")

    two_part = s.has_artifact("enclosure_bottom.stl")
    stl_path = s.artifact_path("enclosure_bottom.stl" if two_part else "enclosure.stl")
    if not stl_path.exists():
        name = "enclosure_bottom.stl" if two_part else "enclosure.stl"
        raise HTTPException(400, f"No {name} — compile SCAD first")

    routing_data = require_routing(s)

    cur = get_gcode_state(sid)
    if not force and cur and cur["status"] == "running":
        return {"status": "running"}
    if not force and cur and cur["status"] in ("done", "error"):
        return cur

    set_gcode_state(sid, {"status": "running", "message": "Starting G-code pipeline…", "stages": []})

    def _do_gcode():
        from src.pipeline.gcode.pipeline import run_gcode_pipeline
        from src.pipeline.gcode.pause_points import ComponentPauseInfo
        from src.catalog.loader import load_catalog
        try:
            s.clear_stage_artifacts("gcode")
            shell_height = None
            design_data = s.read_artifact("design.json")
            if design_data:
                try:
                    shell_height = parse_physical_design(design_data).enclosure.height_mm
                except Exception:
                    pass

            # Build component pause info from placement + catalog
            comp_infos: list[ComponentPauseInfo] | None = None
            cat_idx: dict = {}
            placement_data = s.read_artifact("placement.json")
            if placement_data:
                cat = load_catalog()
                cat_idx = {c.id: c for c in cat.components}
                infos: list[ComponentPauseInfo] = []
                for comp in placement_data.get("components", []):
                    cat_entry = cat_idx.get(comp.get("catalog_id", ""))
                    if cat_entry:
                        infos.append(ComponentPauseInfo(
                            instance_id=comp.get("instance_id", ""),
                            body_height_mm=cat_entry.protrusion_height_mm,
                            mounting_style=comp.get("mounting_style") or cat_entry.mounting.style,
                            pin_length_mm=cat_entry.pin_length_mm,
                        ))
                if infos:
                    comp_infos = infos

            result = run_gcode_pipeline(
                stl_path=stl_path,
                output_dir=s.artifact_path("enclosure.gcode").parent,
                routing_result=routing_data,
                shell_height=shell_height,
                printer=s.printer_id,
                filament=s.filament_id,
                silverink_only=silverink_only,
                component_infos=comp_infos,
            )
            if result.success:
                s.pipeline_state["gcode"] = "complete"
                s.save()

                # Slice extras (buttons, battery hatch) if extras.stl exists
                extras_gcode_bytes = 0
                extras_stl = s.artifact_path("extras.stl")
                if extras_stl.exists():
                    from src.pipeline.gcode.slicer import slice_stl
                    extras_gcode = s.artifact_path("extras.gcode")
                    ok, msg, _ = slice_stl(
                        extras_stl, extras_gcode,
                        printer=s.printer_id,
                        filament=s.filament_id,
                    )
                    if ok and extras_gcode.exists():
                        extras_gcode_bytes = extras_gcode.stat().st_size
                        logging.info("Extras sliced: %d bytes", extras_gcode_bytes)
                    else:
                        logging.warning("Extras slicing failed: %s", msg)

                set_gcode_state(sid, {
                    "status": "done",
                    "message": result.message,
                    "stages": result.stages,
                    "gcode_bytes": (
                        Path(result.gcode_path).stat().st_size
                        if result.gcode_path and Path(result.gcode_path).exists()
                        else 0
                    ),
                    "extras_gcode_bytes": extras_gcode_bytes,
                })
            else:
                set_gcode_state(sid, {"status": "error", "message": result.message, "stages": result.stages})
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            logging.exception("G-code pipeline error")
            set_gcode_state(sid, {"status": "error", "message": f"{type(exc).__name__}: {exc}", "stages": [tb]})

    threading.Thread(target=_do_gcode, daemon=True).start()
    return {"status": "running"}


@router.get("/sessions/{sid}/manufacture/gcode")
async def poll_gcode(sid: str):
    s = load_session_or_404(sid)
    cur = get_gcode_state(sid)
    if cur:
        return cur
    gcode = s.artifact_path("enclosure.gcode")
    if gcode.exists():
        extras = s.artifact_path("extras.gcode")
        return {
            "status": "done",
            "message": "G-code pipeline completed successfully.",
            "stages": [],
            "gcode_bytes": gcode.stat().st_size,
            "extras_gcode_bytes": extras.stat().st_size if extras.exists() else 0,
        }
    return {"status": "pending"}


@router.get("/sessions/{sid}/manufacture/gcode/download")
async def download_gcode(sid: str):
    s = load_session_or_404(sid)
    path = s.artifact_path("enclosure.gcode")
    if not path.exists():
        raise HTTPException(404, "No enclosure.gcode — run the G-code pipeline first")
    return FileResponse(path, media_type="text/plain", filename="enclosure.gcode",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/sessions/{sid}/manufacture/extras-gcode/download")
async def download_extras_gcode(sid: str):
    s = load_session_or_404(sid)
    path = s.artifact_path("extras.gcode")
    if not path.exists():
        raise HTTPException(404, "No extras.gcode — run the G-code pipeline first")
    return FileResponse(path, media_type="text/plain", filename="extras.gcode",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
