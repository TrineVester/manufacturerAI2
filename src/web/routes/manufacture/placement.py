from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from src.pipeline.design import (
    parse_physical_design, parse_circuit, build_design_spec, validate_design,
)
from src.pipeline.placer import place_components, placement_to_dict, PlacementError
from src.pipeline.config import get_printer
from src.web.routes._deps import (
    get_catalog, load_session_or_404, invalidate_downstream,
    require_design, require_circuit,
    build_placement_response,
)
from src.web.tasks import PipelineTask, get_pipeline_task, set_pipeline_task

router = APIRouter()


@router.post("/sessions/{sid}/manufacture/placement")
async def run_placement(sid: str):
    s = load_session_or_404(sid)

    existing = get_pipeline_task(sid, "placement")
    if existing and existing.status == "running":
        return {"status": "running"}

    task = PipelineTask(status="running")
    set_pipeline_task(sid, "placement", task)

    def _do():
        try:
            if task.cancel_event.is_set():
                return

            s.clear_stage_artifacts("placement")
            cat = get_catalog()
            physical = parse_physical_design(require_design(s))
            circuit = parse_circuit(require_circuit(s))
            design = build_design_spec(physical, circuit)

            errors = validate_design(design, cat, printer=get_printer(s.printer_id))
            if errors:
                detail = {
                    "error": "design_validation_failed",
                    "reason": "; ".join(errors),
                    "responsible_agent": "design",
                }
                s.set_step_error("placement", detail)
                set_pipeline_task(sid, "placement", PipelineTask(status="error", error=detail["reason"], detail=detail))
                return

            result = place_components(design, cat)
            s.clear_step_error("placement")
            s.write_artifact("placement.json", placement_to_dict(result))
            s.pipeline_state["placement"] = "complete"
            invalidate_downstream(s, "placement")
            s.save()
            set_pipeline_task(sid, "placement", PipelineTask(status="done"))
        except PlacementError as e:
            detail = {
                "error": "placement_failed",
                "instance_id": e.instance_id,
                "catalog_id": e.catalog_id,
                "reason": e.reason,
                "responsible_agent": "design",
            }
            s.set_step_error("placement", detail)
            set_pipeline_task(sid, "placement", PipelineTask(status="error", error=e.reason, detail=detail))
        except Exception as e:
            set_pipeline_task(sid, "placement", PipelineTask(status="error", error=str(e)))

    threading.Thread(target=_do, daemon=True).start()
    return {"status": "running"}


@router.get("/sessions/{sid}/manufacture/placement/status")
async def poll_placement(sid: str):
    task = get_pipeline_task(sid, "placement")
    if task:
        resp = {"status": task.status, "message": task.error or ""}
        if task.detail:
            resp["detail"] = task.detail
        return resp
    s = load_session_or_404(sid)
    if s.read_artifact("placement.json") is not None:
        return {"status": "done"}
    return {"status": "idle"}


@router.get("/sessions/{sid}/manufacture/placement")
async def get_placement(sid: str):
    s = load_session_or_404(sid)
    if s.read_artifact("placement.json") is None:
        raise HTTPException(404, "No placement yet")
    return build_placement_response(s, get_catalog())
