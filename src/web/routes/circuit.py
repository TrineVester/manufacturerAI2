from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import anthropic

from src.agent import CircuitAgent, build_circuit_user_prompt, build_circuit_prompt, CIRCUIT_TOOLS, MODEL, TOKEN_BUDGET, prune_messages, sanitize_messages, get_model
from src.pipeline.config import get_printer
from src.pipeline.design import (
    parse_physical_design, parse_circuit, build_design_spec, validate_design,
)
from src.web.routes._deps import (
    get_catalog, load_session_or_404, invalidate_downstream,
    enrich_components,
)
from src.web.tasks import AgentTask, get_agent_task, set_agent_task

log = logging.getLogger(__name__)

router = APIRouter(tags=["circuit"])


async def _run_circuit_background(sid: str, prompt: str, task: AgentTask, invalidated: list[str]):
    """Run the circuit agent in the background, accumulating events in *task*."""
    try:
        sess = load_session_or_404(sid)
        cat = get_catalog()

        if invalidated:
            task.append_event("invalidated", {
                "invalidated_steps": invalidated,
                "artifacts": sess.artifacts,
                "pipeline_errors": sess.pipeline_errors,
            })

        agent = CircuitAgent(cat, sess)
        async for event in agent.run(prompt, cancel_event=task.cancel_event):
            if event.type == "checkpoint":
                task.last_save_cursor = len(task.events)
                continue
            if event.type == "circuit" and event.data:
                circuit = event.data.get("circuit")
                if circuit:
                    enrich_components(circuit.get("components", []), cat)
            task.append_event(event.type, event.data or {})
        task.finish("done")
    except asyncio.CancelledError:
        task.finish("done", error="Cancelled")
    except Exception as e:
        log.exception("Circuit agent background error")
        task.append_event("error", {"message": str(e)})
        task.finish("error", error=str(e))


@router.post("/sessions/{sid}/circuit")
async def run_circuit(sid: str, request: Request):
    sess = load_session_or_404(sid)
    design_data = sess.read_artifact("design.json")
    if design_data is None:
        raise HTTPException(400, "No design.json — run the design agent first")

    existing = get_agent_task(sid, "circuit")
    if existing and existing.status == "running":
        raise HTTPException(409, "Circuit agent is already running")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    feedback = body.get("feedback")
    outline = body.get("outline")

    if feedback:
        prompt = (
            "The manufacturing pipeline failed after your circuit was submitted. "
            "Here is the error:\n\n"
            f"{feedback}\n\n"
            "Please fix the issue and resubmit the circuit."
        )
        invalidated = invalidate_downstream(sess, "circuit")
    elif outline:
        prompt = outline
        invalidated = invalidate_downstream(sess, "design")
    else:
        prompt = build_circuit_user_prompt(design_data, catalog=get_catalog())
        invalidated = invalidate_downstream(sess, "design")
    sess.save()

    task = AgentTask()
    set_agent_task(sid, "circuit", task)
    task.asyncio_task = asyncio.create_task(
        _run_circuit_background(sid, prompt, task, invalidated)
    )
    return {"status": "running"}


@router.post("/sessions/{sid}/circuit/stop")
async def stop_circuit(sid: str):
    task = get_agent_task(sid, "circuit")
    if not task or task.status != "running":
        raise HTTPException(404, "No running circuit agent")
    task.cancel_event.set()
    return {"status": "stopping"}


@router.post("/sessions/{sid}/circuit/revalidate")
async def revalidate_circuit(sid: str):
    """Re-run the cross-check of a pending circuit against current design.json.

    No LLM is involved — this just re-validates and marks the circuit complete
    if the design now satisfies the constraints.
    """
    sess = load_session_or_404(sid)
    cat = get_catalog()

    design_data = sess.read_artifact("design.json")
    if not design_data:
        raise HTTPException(400, "No design.json")

    circuit_data = sess.read_artifact("circuit_pending.json")
    if not circuit_data:
        raise HTTPException(400, "No pending circuit to revalidate")

    try:
        physical = parse_physical_design(design_data)
        circuit = parse_circuit(circuit_data)
        full_spec = build_design_spec(physical, circuit)
        printer = get_printer(sess.printer_id)
        errors = validate_design(full_spec, cat, printer=printer)
    except Exception as e:
        raise HTTPException(500, f"Validation error: {e}")

    if errors:
        error_list = "\n".join(f"  - {e}" for e in errors)
        return {
            "valid": False,
            "errors": error_list,
        }

    sess.write_artifact("circuit.json", circuit_data)
    sess.delete_artifact("circuit_pending.json")
    sess.pipeline_state["circuit"] = "complete"
    invalidated = sess.invalidate_downstream("circuit")
    sess.save()

    enrich_components(circuit_data.get("components", []), cat)
    return {
        "valid": True,
        "circuit": circuit_data,
        "invalidated_steps": invalidated,
        "artifacts": sess.artifacts,
        "pipeline_errors": sess.pipeline_errors,
    }


@router.get("/sessions/{sid}/circuit/stream")
async def stream_circuit_events(sid: str, after: int = Query(0)):
    """SSE endpoint: yields buffered events starting at *after*, then waits for new ones."""
    task = get_agent_task(sid, "circuit")
    if not task:
        raise HTTPException(404, "No circuit agent task")

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


@router.get("/sessions/{sid}/circuit/status")
async def circuit_agent_status(sid: str):
    task = get_agent_task(sid, "circuit")
    if not task:
        return {"status": "idle", "event_count": 0}
    return {
        "status": task.status,
        "event_count": len(task.events),
        "last_save_cursor": task.last_save_cursor,
        "error": task.error,
    }


@router.get("/sessions/{sid}/circuit")
async def get_circuit(sid: str):
    s = load_session_or_404(sid)
    data = s.read_artifact("circuit.json")
    if data is None:
        raise HTTPException(404, "No circuit yet")
    cat = get_catalog()
    enrich_components(data.get("components", []), cat)
    return data


@router.get("/sessions/{sid}/circuit/conversation")
async def get_circuit_conversation(sid: str):
    s = load_session_or_404(sid)
    data = s.read_artifact("circuit_conversation.json")
    return data if isinstance(data, list) else []


async def _replay_circuit_background(sid: str, task: AgentTask):
    """Replay a saved circuit conversation as SSE events with realistic delays."""
    try:
        sess = load_session_or_404(sid)
        convo = sess.read_artifact("circuit_conversation.json")
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
                    task.append_event("user_message", {"text": block.get("text", "")})
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
                            circuit_data = json.loads(json_str)
                            if "components" in circuit_data and "nets" in circuit_data:
                                enrich_components(circuit_data.get("components", []), cat)
                                task.append_event("circuit", {"circuit": circuit_data})
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
        log.exception("Circuit replay background error")
        task.append_event("error", {"message": str(e)})
        task.finish("error", error=str(e))


@router.post("/sessions/{sid}/circuit/replay")
async def replay_circuit(sid: str):
    load_session_or_404(sid)

    existing = get_agent_task(sid, "circuit")
    if existing and existing.status == "running":
        raise HTTPException(409, "Circuit agent is already running")

    task = AgentTask()
    set_agent_task(sid, "circuit", task)
    task.asyncio_task = asyncio.create_task(_replay_circuit_background(sid, task))
    return {"status": "running"}


@router.get("/sessions/{sid}/circuit/tokens")
def get_circuit_tokens(sid: str):
    s = load_session_or_404(sid)
    conversation = s.read_artifact("circuit_conversation.json")
    if not conversation or not isinstance(conversation, list):
        return {"input_tokens": 0, "budget": TOKEN_BUDGET}

    cat = get_catalog()
    system = build_circuit_prompt(cat)
    pruned = prune_messages(sanitize_messages(conversation))
    client = anthropic.Anthropic()
    try:
        result = client.messages.count_tokens(
            model=get_model(s.model_id).api_model,
            messages=pruned,
            system=system,
            tools=CIRCUIT_TOOLS,
        )
        return {"input_tokens": result.input_tokens, "budget": TOKEN_BUDGET}
    except Exception:
        return {"input_tokens": 0, "budget": TOKEN_BUDGET}
