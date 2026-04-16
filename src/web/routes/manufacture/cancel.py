from __future__ import annotations

from fastapi import APIRouter

from src.web.routes._deps import (
    load_session_or_404, get_compile_state, set_compile_state,
    get_gcode_state, set_gcode_state,
)
from src.web.tasks import get_all_pipeline_tasks, set_pipeline_task, PipelineTask

router = APIRouter()

PIPELINE_STEPS = ("placement", "routing", "bitmap", "scad", "compile", "gcode")


@router.post("/sessions/{sid}/manufacture/pipeline/cancel")
async def cancel_pipeline(sid: str):
    load_session_or_404(sid)

    cancelled: list[str] = []
    tasks = get_all_pipeline_tasks(sid)

    for step in PIPELINE_STEPS:
        task = tasks.get(step)
        if task and task.status == "running":
            task.cancel_event.set()
            set_pipeline_task(sid, step, PipelineTask(
                status="error", error="Cancelled", detail={"reason": "Cancelled"},
            ))
            cancelled.append(step)

    cs = get_compile_state(sid)
    if cs and cs.get("status") == "compiling" and cs.get("cancel"):
        cs["cancel"].set()
        set_compile_state(sid, {"status": "error", "message": "Cancelled"})
        if "compile" not in cancelled:
            cancelled.append("compile")

    gs = get_gcode_state(sid)
    if gs and gs.get("status") == "running":
        set_gcode_state(sid, {"status": "error", "message": "Cancelled", "stages": gs.get("stages", [])})
        if "gcode" not in cancelled:
            cancelled.append("gcode")

    return {"status": "ok", "cancelled": cancelled}
