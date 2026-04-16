from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.session import load_session
from src.agent import (
    DesignAgent, DESIGN_TOOLS,
    MODEL, TOKEN_BUDGET, get_model,
    build_design_prompt, prune_messages, sanitize_messages,
)
from src.pipeline.design import parse_design, validate_design, parse_physical_design, validate_physical_design
from src.pipeline.config import get_printer

import anthropic

from src.web.routes._deps import (
    get_catalog, load_session_or_404, invalidate_downstream,
    enrich_design, enrich_components, _read_outline, strip_enriched_fields,
)
from src.web.tasks import AgentTask, get_agent_task, set_agent_task

log = logging.getLogger(__name__)

router = APIRouter(tags=["design"])


async def _run_design_background(sid: str, prompt: str, task: AgentTask):
    """Run the design agent in the background, accumulating events in *task*."""
    try:
        from src.web.routes._deps import get_catalog, load_session_or_404, enrich_design
        sess = load_session_or_404(sid)
        cat = get_catalog()
        agent = DesignAgent(cat, sess)
        async for event in agent.run(prompt, cancel_event=task.cancel_event):
            if event.type == "checkpoint":
                task.last_save_cursor = len(task.events)
                continue
            if event.type == "design" and event.data:
                design = event.data.get("design")
                if design:
                    enrich_design(design, cat, session=sess)
            task.append_event(event.type, event.data or {})
            if event.type == "design":
                design = event.data.get("design") if event.data else None
                if design:
                    name = design.get("name") or ""
                    if name:
                        sess.name = name
                        sess.save()
                        task.append_event("session_named", {"name": name})
        task.finish("done")
    except asyncio.CancelledError:
        task.finish("done", error="Cancelled")
    except Exception as e:
        log.exception("Design agent background error")
        task.append_event("error", {"message": str(e)})
        task.finish("error", error=str(e))


@router.post("/sessions/{sid}/design")
async def run_design(sid: str, request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "Missing 'prompt' in request body")

    sess = load_session_or_404(sid)

    existing = get_agent_task(sid, "design")
    if existing and existing.status == "running":
        raise HTTPException(409, "Design agent is already running")

    task = AgentTask()
    set_agent_task(sid, "design", task)
    task.asyncio_task = asyncio.create_task(_run_design_background(sid, prompt, task))
    return {"status": "running"}


@router.post("/sessions/{sid}/design/stop")
async def stop_design(sid: str):
    task = get_agent_task(sid, "design")
    if not task or task.status != "running":
        raise HTTPException(404, "No running design agent")
    task.cancel_event.set()
    return {"status": "stopping"}


@router.get("/sessions/{sid}/design/stream")
async def stream_design_events(sid: str, after: int = Query(0)):
    """SSE endpoint: yields buffered events starting at *after*, then waits for new ones."""
    task = get_agent_task(sid, "design")
    if not task:
        raise HTTPException(404, "No design agent task")

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


@router.get("/sessions/{sid}/design/status")
async def design_agent_status(sid: str):
    task = get_agent_task(sid, "design")
    if not task:
        return {"status": "idle", "event_count": 0}
    return {
        "status": task.status,
        "event_count": len(task.events),
        "last_save_cursor": task.last_save_cursor,
        "error": task.error,
    }


@router.get("/sessions/{sid}/design")
async def get_design(sid: str):
    s = load_session_or_404(sid)
    data = s.read_artifact("design.json")
    if data is None:
        raise HTTPException(404, "No design yet")
    cat = get_catalog()
    enrich_design(data, cat, session=s)
    # Merge circuit data so the built-in frontend gets the full picture
    circuit = s.read_artifact("circuit.json")
    if circuit:
        data.setdefault("components", circuit.get("components", []))
        data.setdefault("nets", circuit.get("nets", []))
        enrich_components(data["components"], cat)
    return data


@router.put("/sessions/{sid}/design")
async def put_design(sid: str, request: Request):
    body = await request.json()
    s = load_session_or_404(sid)
    cat = get_catalog()

    try:
        physical = parse_physical_design(body)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise HTTPException(400, f"Design parsing error: {e}")

    errors = validate_physical_design(physical, cat, printer=get_printer(s.printer_id))
    if errors:
        raise HTTPException(422, detail={"errors": errors})

    invalidated = s.invalidate_design_smart(body)
    clean = strip_enriched_fields(body)
    s.write_artifact("design.json", clean)

    name = clean.get("name") or ""
    if name:
        s.name = name

    outline_data = [v.to_dict() for v in physical.outline.points]
    outline_json: dict = {"outline": outline_data}
    if physical.outline.holes:
        outline_json["holes"] = [
            [v.to_dict() for v in hole] for hole in physical.outline.holes
        ]
    s.write_artifact("outline.json", outline_json)
    s.pipeline_state["design"] = "complete"
    s.save()

    enrich_design(body, cat, session=s)
    body["invalidated_steps"] = invalidated
    body["artifacts"] = s.artifacts
    body["pipeline_errors"] = s.pipeline_errors
    return body


@router.get("/sessions/{sid}/design/conversation")
async def get_design_conversation(sid: str):
    s = load_session_or_404(sid)
    data = s.read_artifact("design_conversation.json")
    return data if isinstance(data, list) else []


async def _replay_design_background(sid: str, task: AgentTask):
    """Replay a saved conversation as SSE events with realistic delays."""
    try:
        sess = load_session_or_404(sid)
        convo = sess.read_artifact("design_conversation.json")
        if not convo or not isinstance(convo, list):
            task.finish("error", error="No conversation to replay")
            return

        cat = get_catalog()

        for msg in convo:
            if task.cancel_event.is_set():
                break

            role = msg.get("role", "")
            content = msg.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]

            for block in content:
                if task.cancel_event.is_set():
                    break

                btype = block.get("type", "")

                if role == "user" and btype == "text":
                    text = block.get("text", "")
                    if text.startswith("<!-- design-context -->"):
                        continue
                    task.append_event("user_message", {"text": text})
                    await asyncio.sleep(0.3)

                elif role == "user" and btype == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    result_content = block.get("content", "")
                    task.append_event("tool_result", {
                        "id": tool_use_id,
                        "content": result_content,
                        "name": block.get("name", ""),
                        "is_error": block.get("is_error", False),
                    })
                    if "```json" in result_content:
                        try:
                            json_str = result_content.split("```json\n", 1)[1].split("\n```", 1)[0]
                            design_data = json.loads(json_str)
                            if design_data.get("shape") is not None:
                                enrich_design(design_data, cat, session=sess)
                                task.append_event("design", {"design": design_data})
                        except (IndexError, json.JSONDecodeError):
                            pass
                    await asyncio.sleep(0.5)

                elif role == "assistant" and btype == "thinking":
                    text = block.get("thinking", "")
                    task.append_event("thinking_start", {})
                    chunk_size = 20
                    for i in range(0, len(text), chunk_size):
                        task.append_event("thinking_delta", {"text": text[i:i + chunk_size]})
                        await asyncio.sleep(0.005)
                    task.append_event("block_stop", {})
                    await asyncio.sleep(0.2)

                elif role == "assistant" and btype == "text":
                    text = block.get("text", "")
                    task.append_event("message_start", {})
                    words = text.split(" ")
                    for i, word in enumerate(words):
                        chunk = word if i == 0 else " " + word
                        task.append_event("message_delta", {"text": chunk})
                        await asyncio.sleep(0.03)
                    task.append_event("block_stop", {})
                    await asyncio.sleep(0.2)

                elif role == "assistant" and btype == "tool_use":
                    task.append_event("tool_call", {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    })
                    await asyncio.sleep(0.3)

            await asyncio.sleep(0.3)

        task.append_event("done", {})
        task.finish("done")
    except asyncio.CancelledError:
        task.finish("done", error="Cancelled")
    except Exception as e:
        log.exception("Design replay background error")
        task.append_event("error", {"message": str(e)})
        task.finish("error", error=str(e))


@router.post("/sessions/{sid}/design/replay")
async def replay_design(sid: str):
    load_session_or_404(sid)

    existing = get_agent_task(sid, "design")
    if existing and existing.status == "running":
        raise HTTPException(409, "Design agent is already running")

    task = AgentTask()
    set_agent_task(sid, "design", task)
    task.asyncio_task = asyncio.create_task(_replay_design_background(sid, task))
    return {"status": "running"}


@router.patch("/sessions/{sid}/design/enclosure")
async def patch_enclosure(sid: str, request: Request):
    body = await request.json()
    s = load_session_or_404(sid)
    data = s.read_artifact("design.json")
    if data is None:
        raise HTTPException(404, "No design yet")

    enc = data.setdefault("enclosure", {})
    for key in ("height_mm", "top_surface", "edge_top", "edge_bottom"):
        if key in body:
            enc[key] = body[key]

    s.write_artifact("design.json", data)
    cat = get_catalog()
    enrich_design(data, cat, session=s)
    return data


@router.post("/sessions/{sid}/design/validate-ui-placement")
async def validate_ui_placement(sid: str, request: Request):
    body = await request.json()
    s = load_session_or_404(sid)
    data = s.read_artifact("design.json")
    if data is None:
        raise HTTPException(404, "No design yet")

    cat = get_catalog()
    cat_map = {c.id: c for c in cat.components}
    errors: list[str] = []

    instance_id = body.get("instance_id", "")
    x_mm = body.get("x_mm", 0)
    y_mm = body.get("y_mm", 0)
    edge_index = body.get("edge_index")

    comp_entry = next(
        (c for c in data.get("ui_placements", []) if c["instance_id"] == instance_id),
        None,
    )
    cat_comp = cat_map.get(comp_entry["catalog_id"]) if comp_entry else None

    outline = _read_outline(s)
    if len(outline) < 3:
        return {"valid": False, "errors": ["Outline has fewer than 3 vertices"]}

    try:
        from shapely.geometry import Polygon, Point
        verts = [(p["x"], p["y"]) for p in outline]
        poly = Polygon(verts)

        if edge_index is not None:
            if edge_index < 0 or edge_index >= len(outline):
                errors.append(f"edge_index {edge_index} out of range")
        else:
            pt = Point(x_mm, y_mm)
            if not poly.contains(pt):
                errors.append("Position is outside the outline")
            elif cat_comp:
                body_c = cat_comp.body
                half_size = max(
                    body_c.width_mm or 0,
                    body_c.length_mm or 0,
                    body_c.diameter_mm or 0,
                ) / 2
                required_clearance = half_size + cat_comp.mounting.keepout_margin_mm
                dist_to_edge = poly.boundary.distance(pt)
                if dist_to_edge < required_clearance:
                    errors.append(
                        f"Too close to edge ({dist_to_edge:.1f}mm, "
                        f"needs {required_clearance:.1f}mm)"
                    )

        if cat_comp:
            body_c = cat_comp.body
            hw = max(body_c.width_mm or 0, body_c.diameter_mm or 0) / 2
            hh = max(body_c.length_mm or 0, body_c.diameter_mm or 0) / 2
            keepout = cat_comp.mounting.keepout_margin_mm

            for other_up in data.get("ui_placements", []):
                if other_up["instance_id"] == instance_id:
                    continue
                other_comp = next(
                    (c for c in data.get("ui_placements", [])
                     if c["instance_id"] == other_up["instance_id"]),
                    None,
                )
                if not other_comp:
                    continue
                other_cat = cat_map.get(other_comp["catalog_id"])
                if not other_cat:
                    continue
                o_body = other_cat.body
                o_hw = max(o_body.width_mm or 0, o_body.diameter_mm or 0) / 2
                o_hh = max(o_body.length_mm or 0, o_body.diameter_mm or 0) / 2
                o_keepout = other_cat.mounting.keepout_margin_mm
                gap_x = abs(x_mm - other_up["x_mm"]) - hw - o_hw
                gap_y = abs(y_mm - other_up["y_mm"]) - hh - o_hh
                gap = max(gap_x, gap_y)
                required_gap = max(keepout, o_keepout, 1.0)
                if gap < required_gap:
                    errors.append(
                        f"Overlaps with {other_up['instance_id']} "
                        f"(gap {gap:.1f}mm, needs {required_gap:.1f}mm)"
                    )
    except ImportError:
        pass

    return {"valid": len(errors) == 0, "errors": errors}


@router.patch("/sessions/{sid}/design/conversation/submit")
async def submit_design_to_conversation(sid: str, request: Request):
    body = await request.json()
    s = load_session_or_404(sid)
    conversation = s.read_artifact("design_conversation.json")
    if not isinstance(conversation, list):
        conversation = []

    conversation = sanitize_messages(conversation)

    design = body.get("design", {})

    new_msg = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": json.dumps({
                    "source": "interactive_designer",
                    "description": "User modified the design interactively in the UI designer. The design below reflects their changes.",
                    "design": design,
                }),
            }
        ],
    }

    if (conversation
            and conversation[-1].get("role") == "user"
            and isinstance(conversation[-1].get("content"), list)
            and any(
                b.get("type") == "text"
                and "interactive_designer" in (b.get("text") or "")
                for b in conversation[-1]["content"]
            )):
        conversation[-1] = new_msg
    else:
        conversation.append(new_msg)

    s.write_artifact("design_conversation.json", conversation)
    return {"ok": True}


@router.get("/sessions/{sid}/design/tokens")
def get_design_tokens(sid: str):
    s = load_session_or_404(sid)
    conversation = s.read_artifact("design_conversation.json")
    if not conversation or not isinstance(conversation, list):
        return {"input_tokens": 0, "budget": TOKEN_BUDGET}

    cat = get_catalog()
    design = s.read_artifact("design.json")
    import json as _json
    design_text = _json.dumps(design, indent=2) if design else None
    system = build_design_prompt(cat, printer=get_printer(s.printer_id))
    pruned = prune_messages(sanitize_messages(conversation))
    client = anthropic.Anthropic()
    try:
        result = client.messages.count_tokens(
            model=get_model(s.model_id).api_model,
            messages=pruned,
            system=system,
            tools=DESIGN_TOOLS,
        )
        return {"input_tokens": result.input_tokens, "budget": TOKEN_BUDGET}
    except Exception:
        return {"input_tokens": 0, "budget": TOKEN_BUDGET}
