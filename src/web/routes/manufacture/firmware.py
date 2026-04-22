from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from src.catalog import load_catalog
from src.web.routes._deps import get_catalog, load_session_or_404, require_routing
from src.web.tasks import AgentTask, get_agent_task, set_agent_task

log = logging.getLogger(__name__)

router = APIRouter()


# ── Firmware agent background runner ─────────────────────────────

async def _run_firmware_agent_background(sid: str, prompt: str, task: AgentTask) -> None:
    """Run FirmwareAgent in the background, accumulating SSE events in *task*."""
    try:
        from src.pipeline.firmware.agent import FirmwareAgent

        sess = load_session_or_404(sid)
        cat = get_catalog()

        agent = FirmwareAgent(cat, sess)
        async for event in agent.run(prompt, cancel_event=task.cancel_event):
            if event.type == "checkpoint":
                task.last_save_cursor = len(task.events)
                continue
            task.append_event(event.type, event.data or {})

        task.finish("done")
    except asyncio.CancelledError:
        task.finish("done", error="Cancelled")
    except Exception as e:
        log.exception("Firmware agent background error")
        task.append_event("error", {"message": str(e)})
        task.finish("error", error=str(e))


@router.post("/sessions/{sid}/manufacture/firmware")
async def run_firmware(sid: str):
    s = load_session_or_404(sid)
    routing_data = require_routing(s)

    circuit_data = s.read_artifact("circuit.json")
    if not circuit_data:
        raise HTTPException(400, "No circuit.json — run circuit agent first")

    from src.pipeline.firmware.generator import generate_firmware

    # Merge circuit components + nets into the design dict expected by generator
    design_data = s.read_artifact("design.json") or {}
    merged = dict(design_data)
    merged["components"] = circuit_data.get("components", [])
    merged["nets"] = circuit_data.get("nets", [])

    cat = load_catalog()
    result = generate_firmware(merged, routing_data, cat)

    # Persist the sketch as an artifact
    sketch: str = result.get("sketch", "")
    s.write_artifact_text("firmware.ino", sketch)
    s.pipeline_state["firmware"] = "complete"
    s.clear_step_error("firmware")
    s.save()

    return result


@router.get("/sessions/{sid}/manufacture/firmware")
async def get_firmware(sid: str):
    s = load_session_or_404(sid)
    if not s.has_artifact("firmware.ino"):
        raise HTTPException(404, "No firmware.ino yet — run firmware generation first")

    sketch = s.artifact_path("firmware.ino").read_text(encoding="utf-8")

    # Try to re-derive pin_map / report from routing + circuit
    routing_data = s.read_artifact("routing.json")
    circuit_data = s.read_artifact("circuit.json")
    design_data = s.read_artifact("design.json") or {}

    if routing_data and circuit_data:
        from src.pipeline.firmware.generator import generate_firmware
        from src.catalog import load_catalog as _lc
        merged = dict(design_data)
        merged["components"] = circuit_data.get("components", [])
        merged["nets"] = circuit_data.get("nets", [])
        cat = _lc()
        result = generate_firmware(merged, routing_data, cat)
        return result

    return {"sketch": sketch, "pin_map": [], "pin_report": "", "warnings": []}


@router.get("/sessions/{sid}/manufacture/firmware/download")
async def download_firmware(sid: str):
    s = load_session_or_404(sid)
    path = s.artifact_path("firmware.ino")
    if not path.exists():
        raise HTTPException(404, "No firmware.ino yet")
    return PlainTextResponse(
        path.read_text(encoding="utf-8"),
        headers={"Content-Disposition": 'attachment; filename="firmware.ino"'},
    )


# ── Firmware agent endpoints ──────────────────────────────────────

@router.post("/sessions/{sid}/manufacture/firmware/agent")
async def run_firmware_agent(sid: str, request: Request):
    """Start (or continue) the firmware agent for *sid*."""
    s = load_session_or_404(sid)

    if not s.read_artifact("routing.json"):
        raise HTTPException(400, "No routing.json — run routing first")
    if not s.read_artifact("circuit.json"):
        raise HTTPException(400, "No circuit.json — run circuit agent first")

    existing = get_agent_task(sid, "firmware")
    if existing and existing.status == "running":
        raise HTTPException(409, "Firmware agent is already running")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    user_prompt = (body.get("prompt") or "").strip()
    if not user_prompt:
        from src.pipeline.firmware.agent import build_firmware_user_prompt
        design_data = s.read_artifact("design.json") or {}
        user_prompt = build_firmware_user_prompt(design_data)

    task = AgentTask()
    set_agent_task(sid, "firmware", task)
    task.asyncio_task = asyncio.create_task(
        _run_firmware_agent_background(sid, user_prompt, task)
    )
    return {"status": "running"}


@router.post("/sessions/{sid}/manufacture/firmware/agent/stop")
async def stop_firmware_agent(sid: str):
    task = get_agent_task(sid, "firmware")
    if not task or task.status != "running":
        raise HTTPException(404, "No running firmware agent")
    task.cancel_event.set()
    return {"status": "stopping"}


@router.get("/sessions/{sid}/manufacture/firmware/agent/stream")
async def stream_firmware_events(sid: str, after: int = Query(0)):
    """SSE endpoint: yields buffered events starting at *after*, then waits for new ones."""
    task = get_agent_task(sid, "firmware")
    if not task:
        raise HTTPException(404, "No firmware agent task")

    async def event_stream():
        cursor = after
        while True:
            while cursor < len(task.events):
                ev = task.events[cursor]
                data = json.dumps(ev["data"]) if ev["data"] else "{}"
                yield f"event: {ev['type']}\ndata: {data}\n\n"
                cursor += 1
            if task.status != "running":
                break
            await asyncio.sleep(0.05)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{sid}/manufacture/firmware/agent/status")
async def firmware_agent_status(sid: str):
    task = get_agent_task(sid, "firmware")
    if not task:
        return {"status": "idle", "event_count": 0}
    return {
        "status": task.status,
        "event_count": len(task.events),
        "last_save_cursor": task.last_save_cursor,
        "error": task.error,
    }


@router.get("/sessions/{sid}/manufacture/firmware/conversation")
async def get_firmware_conversation(sid: str):
    s = load_session_or_404(sid)
    data = s.read_artifact("firmware_conversation.json")
    if not data or not isinstance(data, list):
        return []
    return data
