from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.web.routes._deps import (
    load_session_or_404, get_compile_state, get_gcode_state,
)
from src.web.tasks import (
    get_all_pipeline_tasks, subscribe_pipeline, unsubscribe_pipeline,
)

router = APIRouter()

PIPELINE_STEPS = ("placement", "routing", "bitmap", "scad", "compile", "gcode")


def _build_snapshot(sid: str) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    tasks = get_all_pipeline_tasks(sid)
    for step in PIPELINE_STEPS:
        task = tasks.get(step)
        if task:
            entry: dict[str, Any] = {"status": task.status, "message": task.message or task.error or ""}
            if task.detail:
                entry["detail"] = task.detail
            snapshot[step] = entry
            continue

        if step == "compile":
            cs = get_compile_state(sid)
            if cs:
                snapshot[step] = {"status": cs["status"], "message": cs.get("message", "")}
                continue

        if step == "gcode":
            gs = get_gcode_state(sid)
            if gs:
                snapshot[step] = {"status": gs["status"], "message": gs.get("message", "")}
                continue

    return snapshot


@router.get("/sessions/{sid}/manufacture/pipeline/events")
async def pipeline_events(sid: str):
    load_session_or_404(sid)

    notify = asyncio.Event()
    subscribe_pipeline(sid, notify)

    async def event_stream():
        try:
            prev_snapshot: dict[str, dict[str, Any]] = {}
            while True:
                snapshot = _build_snapshot(sid)
                if snapshot != prev_snapshot:
                    yield f"event: pipeline_status\ndata: {json.dumps(snapshot)}\n\n"
                    prev_snapshot = snapshot

                    all_terminal = all(
                        snapshot.get(s, {}).get("status") in ("done", "error")
                        for s in PIPELINE_STEPS
                        if s in snapshot
                    )
                    has_running = any(
                        snapshot.get(s, {}).get("status") in ("running", "compiling")
                        for s in PIPELINE_STEPS
                    )
                    if snapshot and all_terminal and not has_running:
                        break

                notify.clear()
                try:
                    await asyncio.wait_for(notify.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe_pipeline(sid, notify)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
