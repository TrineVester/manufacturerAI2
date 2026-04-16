from __future__ import annotations

import shutil

from fastapi import APIRouter, Query
from pydantic import BaseModel

from src.catalog import catalog_to_dict
from src.session import create_session
from src.pipeline.config import get_printer
from src.pipeline.gcode.filaments import get_filament
from src.agent.config import get_model, MODELS
from src.web.routes._deps import (
    get_catalog, load_session_or_404, invalidate_downstream,
)


router = APIRouter(tags=["sessions"])


@router.get("/sessions")
async def list_sessions():
    from src.session import list_sessions as _ls
    return {"sessions": _ls()}


@router.post("/sessions")
async def create_new_session(description: str = ""):
    session = create_session(description)
    session.save()
    return {"session_id": session.id, "created": session.created}


@router.get("/sessions/{sid}")
async def get_session(sid: str):
    s = load_session_or_404(sid)
    return {
        "id": s.id,
        "created": s.created,
        "last_modified": s.last_modified,
        "description": s.description,
        "name": s.name,
        "printer_id": s.printer_id,
        "filament_id": s.filament_id,
        "model_id": s.model_id,
        "pipeline_state": s.pipeline_state,
        "pipeline_errors": s.pipeline_errors,
        "artifacts": s.artifacts,
    }


@router.put("/sessions/{sid}/printer")
async def set_printer(sid: str, printer_id: str = Query(...)):
    s = load_session_or_404(sid)
    pdef = get_printer(printer_id)
    old_id = s.printer_id
    s.printer_id = pdef.id
    invalidated: list[str] = []
    if old_id != pdef.id:
        invalidated = invalidate_downstream(s, "placement")
    s.save()
    return {
        "printer_id": pdef.id,
        "label": pdef.label,
        "invalidated_steps": invalidated,
        "artifacts": s.artifacts,
        "pipeline_errors": s.pipeline_errors,
    }


@router.put("/sessions/{sid}/filament")
async def set_filament(sid: str, filament_id: str = Query(...)):
    s = load_session_or_404(sid)
    fdef = get_filament(filament_id)
    old_id = s.filament_id
    s.filament_id = fdef.id
    invalidated: list[str] = []
    if old_id != fdef.id:
        invalidated = invalidate_downstream(s, "scad")
    s.save()
    return {
        "filament_id": fdef.id,
        "label": fdef.label,
        "invalidated_steps": invalidated,
        "artifacts": s.artifacts,
        "pipeline_errors": s.pipeline_errors,
    }


@router.put("/sessions/{sid}/model")
async def set_model(sid: str, model_id: str = Query(...)):
    s = load_session_or_404(sid)
    mdef = get_model(model_id)
    s.model_id = mdef.id
    s.save()
    return {
        "model_id": mdef.id,
        "label": mdef.label,
    }


class RenameBody(BaseModel):
    name: str


@router.patch("/sessions/{sid}")
async def rename_session(sid: str, body: RenameBody):
    s = load_session_or_404(sid)
    s.name = body.name
    design = s.read_artifact("design.json")
    if design is not None:
        design["name"] = body.name
        s.write_artifact("design.json", design)
    s.save()
    return {"id": s.id, "name": s.name}


@router.delete("/sessions/{sid}")
async def delete_session(sid: str):
    s = load_session_or_404(sid)
    shutil.rmtree(s.path, ignore_errors=True)
    return {"deleted": True}


@router.get("/sessions/{sid}/catalog")
async def get_session_catalog(sid: str):
    load_session_or_404(sid)
    cat = get_catalog()
    return catalog_to_dict(cat)
