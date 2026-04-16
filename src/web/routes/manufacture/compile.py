from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.web.routes._deps import (
    get_compile_state, set_compile_state,
    load_session_or_404,
)

router = APIRouter()


@router.post("/sessions/{sid}/manufacture/compile")
async def start_compile(sid: str, force: bool = Query(False)):
    s = load_session_or_404(sid)
    two_part = s.has_artifact("enclosure_bottom.scad")
    scad_path = s.artifact_path("enclosure.scad")
    if not scad_path.exists() and two_part:
        scad_path = s.artifact_path("enclosure_bottom.scad")
    if not scad_path.exists():
        raise HTTPException(400, "No enclosure.scad yet — run SCAD first")

    stl_path = s.artifact_path("enclosure_bottom.stl" if two_part else "enclosure.stl")
    cur = get_compile_state(sid)

    if not force and stl_path.exists() and cur is None:
        if not (scad_path.stat().st_mtime > stl_path.stat().st_mtime):
            return {"status": "done", "stl_bytes": stl_path.stat().st_size}

    if cur and cur["status"] == "compiling" and not force:
        return {"status": "compiling"}

    if not force and cur and cur["status"] in ("done", "error"):
        return {"status": cur["status"], "message": cur.get("message", ""),
                "stl_bytes": stl_path.stat().st_size if stl_path.exists() else 0}

    if force and cur and cur.get("cancel"):
        cur["cancel"].set()

    cancel = threading.Event()
    set_compile_state(sid, {"status": "compiling", "cancel": cancel, "message": ""})

    def _do_compile():
        from src.pipeline.scad.compiler import compile_scad
        for stl_name in ("enclosure.stl", "enclosure_bottom.stl", "enclosure_top.stl", "extras.stl"):
            s.delete_artifact(stl_name)
        if two_part:
            # Compile both halves
            bottom_scad = s.artifact_path("enclosure_bottom.scad")
            bottom_stl = s.artifact_path("enclosure_bottom.stl")
            ok, msg, out = compile_scad(bottom_scad, bottom_stl, cancel=cancel, timeout=600)
            if ok:
                top_scad = s.artifact_path("enclosure_top.scad")
                top_stl = s.artifact_path("enclosure_top.stl")
                ok2, msg2, out2 = compile_scad(top_scad, top_stl, cancel=cancel, timeout=600)
                if not ok2:
                    ok, msg = ok2, msg2
        else:
            ok, msg, out = compile_scad(scad_path, stl_path, cancel=cancel, timeout=600)
        if ok:
            extras_scad = s.artifact_path("extras.scad")
            if extras_scad.exists():
                extras_stl = s.artifact_path("extras.stl")
                compile_scad(extras_scad, extras_stl, cancel=cancel, timeout=600)
        set_compile_state(sid, {"status": "done" if ok else "error", "message": msg, "cancel": cancel})
        if ok:
            s.pipeline_state["scad"] = "complete"
            s.save()

    threading.Thread(target=_do_compile, daemon=True).start()
    return {"status": "compiling"}


@router.get("/sessions/{sid}/manufacture/compile")
async def poll_compile(sid: str):
    s = load_session_or_404(sid)
    stl_path = s.artifact_path("enclosure.stl")
    if not stl_path.exists():
        stl_path = s.artifact_path("enclosure_bottom.stl")
    state = get_compile_state(sid)
    if state:
        out = {"status": state["status"], "message": state.get("message", "")}
        if state["status"] == "done" and stl_path.exists():
            out["stl_bytes"] = stl_path.stat().st_size
        return out
    if stl_path.exists():
        return {"status": "done", "stl_bytes": stl_path.stat().st_size}
    return {"status": "pending"}


@router.get("/sessions/{sid}/manufacture/stl")
async def download_stl(sid: str):
    s = load_session_or_404(sid)
    # Two-part mode: prefer bottom STL, fallback to solid
    stl_path = s.artifact_path("enclosure.stl")
    if not stl_path.exists():
        stl_path = s.artifact_path("enclosure_bottom.stl")
    if not stl_path.exists():
        raise HTTPException(404, "No enclosure.stl yet \u2014 compile first")
    return FileResponse(
        stl_path,
        media_type="application/octet-stream",
        filename=stl_path.name,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/sessions/{sid}/manufacture/stl-bottom")
async def download_stl_bottom(sid: str):
    s = load_session_or_404(sid)
    stl_path = s.artifact_path("enclosure_bottom.stl")
    if not stl_path.exists():
        raise HTTPException(404, "No enclosure_bottom.stl \u2014 compile with two-part mode first")
    return FileResponse(
        stl_path,
        media_type="application/octet-stream",
        filename="enclosure_bottom.stl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/sessions/{sid}/manufacture/stl-top")
async def download_stl_top(sid: str):
    s = load_session_or_404(sid)
    stl_path = s.artifact_path("enclosure_top.stl")
    if not stl_path.exists():
        raise HTTPException(404, "No enclosure_top.stl \u2014 compile with two-part mode first")
    return FileResponse(
        stl_path,
        media_type="application/octet-stream",
        filename="enclosure_top.stl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/sessions/{sid}/manufacture/extras-stl")
async def download_extras_stl(sid: str):
    s = load_session_or_404(sid)
    stl_path = s.artifact_path("extras.stl")
    if not stl_path.exists():
        raise HTTPException(404, "No extras.stl — either no extra parts or compile not run yet")
    return FileResponse(
        stl_path,
        media_type="application/octet-stream",
        filename="extras.stl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )
