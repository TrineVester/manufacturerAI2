from __future__ import annotations

import base64
import threading

from fastapi import APIRouter, HTTPException

from src.pipeline.design import parse_physical_design
from src.pipeline.router import generate_trace_bitmap, parse_routing
from src.pipeline.config import TRACE_RULES, bed_bitmap, get_printer
from src.web.routes._deps import (
    get_catalog, load_session_or_404,
    require_design, require_placement, require_routing,
    enrich_components,
)
from src.web.tasks import PipelineTask, get_pipeline_task, set_pipeline_task

router = APIRouter()


def _bed_center_offset(outline_verts: list, pdef) -> tuple[float, float]:
    """Offset from model-local coords to bed coords, accounting for Y-mirror.

    The SCAD emitter wraps the model in ``mirror([0,1,0])`` which negates all
    Y in the STL.  Callers apply ``bed = (-y + dy)`` so *dy* must be computed
    from the mirrored model centre: ``dy = usable_center_y - (-model_cy)``.
    """
    xs = [v[0] for v in outline_verts]
    ys = [v[1] for v in outline_verts]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    usable_center_x = pdef.keepout_left + pdef.usable_width / 2
    usable_center_y = pdef.keepout_front + pdef.usable_depth / 2
    return usable_center_x - cx, usable_center_y + cy


def _generate_and_save_bitmap(session) -> None:
    """Generate trace bitmap and write it to trace_bitmap.txt in the session folder."""
    routing_data = require_routing(session)
    physical = parse_physical_design(require_design(session))
    result = parse_routing(routing_data)

    pdef = get_printer(session.printer_id)
    grid = bed_bitmap(pdef)
    model_to_bed = _bed_center_offset(physical.outline.vertices, pdef)
    lines = generate_trace_bitmap(result, TRACE_RULES.trace_width_mm,
                                  grid=grid, model_to_bed=model_to_bed)
    path = session.artifact_path("trace_bitmap.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8')
    session.save()


def _read_bitmap_lines(session) -> list[str]:
    """Read the stored trace_bitmap.txt from the session folder."""
    path = session.artifact_path("trace_bitmap.txt")
    if not path.exists():
        raise HTTPException(404, "No trace_bitmap.txt — run the bitmap step first")
    return path.read_text(encoding='utf-8').split('\n')


@router.post("/sessions/{sid}/manufacture/bitmap")
async def run_bitmap(sid: str):
    s = load_session_or_404(sid)

    existing = get_pipeline_task(sid, "bitmap")
    if existing and existing.status == "running":
        return {"status": "running"}

    task = PipelineTask(status="running")
    set_pipeline_task(sid, "bitmap", task)

    def _do():
        try:
            if task.cancel_event.is_set():
                return

            s.clear_stage_artifacts("bitmap")
            require_placement(s)
            require_routing(s)
            _generate_and_save_bitmap(s)
            set_pipeline_task(sid, "bitmap", PipelineTask(status="done"))
        except Exception as e:
            set_pipeline_task(sid, "bitmap", PipelineTask(status="error", error=str(e)))

    threading.Thread(target=_do, daemon=True).start()
    return {"status": "running"}


@router.get("/sessions/{sid}/manufacture/bitmap/status")
async def poll_bitmap(sid: str):
    task = get_pipeline_task(sid, "bitmap")
    if task:
        return {"status": task.status, "message": task.error or ""}
    s = load_session_or_404(sid)
    if s.has_artifact("trace_bitmap.txt"):
        return {"status": "done"}
    return {"status": "idle"}


@router.get("/sessions/{sid}/manufacture/bitmap/download")
async def download_bitmap(sid: str):
    from fastapi.responses import PlainTextResponse
    s = load_session_or_404(sid)
    rows = _read_bitmap_lines(s)
    return PlainTextResponse(
        '\n'.join(rows),
        media_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="trace_bitmap.txt"'},
    )


@router.get("/sessions/{sid}/manufacture/bitmap")
async def get_bitmap(sid: str):
    s = load_session_or_404(sid)

    design_data = require_design(s)
    placement_data = require_placement(s)
    routing_data = require_routing(s)
    pdef = get_printer(s.printer_id)

    from src.web.routes._deps import _read_outline, _read_outline_full
    outline = _read_outline(s)
    outline_full = _read_outline_full(s)
    components = placement_data.get("components", [])
    traces = routing_data.get("traces", [])

    if components:
        enrich_components(components, get_catalog())

    outline_verts = [[p["x"], p["y"]] for p in outline] if outline else []
    bed_offset_x, bed_offset_y = _bed_center_offset(outline_verts, pdef) if outline_verts else (0.0, 0.0)

    rows = _read_bitmap_lines(s)
    num_rows = len(rows)
    cols = len(rows[0]) if rows else 0
    byte_cols = (cols + 7) // 8
    packed = bytearray(num_rows * byte_cols)
    for ri, line in enumerate(rows):
        offset = ri * byte_cols
        for ci in range(min(len(line), cols)):
            if line[ci] == '1':
                packed[offset + ci // 8] |= 1 << (7 - ci % 8)
    bitmap_b64 = base64.b64encode(bytes(packed)).decode('ascii')

    return {
        "bitmap_cols": cols,
        "bitmap_rows": num_rows,
        "bitmap_b64": bitmap_b64,
        "bed_width": pdef.bed_width,
        "bed_depth": pdef.bed_depth,
        "nominal_bed_width": pdef.nominal_bed_width,
        "nominal_bed_depth": pdef.nominal_bed_depth,
        "keepout_left": pdef.keepout_left,
        "keepout_right": pdef.keepout_right,
        "keepout_front": pdef.keepout_front,
        "keepout_back": pdef.keepout_back,
        "bed_offset_x": bed_offset_x,
        "bed_offset_y": bed_offset_y,
        "outline": outline_full,
        "components": components,
        "traces": traces,
        "trace_width_mm": TRACE_RULES.trace_width_mm,
        "pin_clearance_mm": TRACE_RULES.pin_clearance_mm,
    }
