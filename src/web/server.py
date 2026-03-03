"""
Web server — lightweight FastAPI app that dispatches pipeline stages
and serves a UI for inspecting each step.

Run:  python -m uvicorn src.web.server:app --reload --port 8000
  or: python -m src.web.server

Every request carries ?session=<id> to identify the working session.
The server dynamically loads/generates content for each pipeline step.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# compile state: session_id -> {status, message, cancel}
_stl_compile: dict[str, dict] = {}

from src.catalog import load_catalog, catalog_to_dict, CatalogResult
from src.session import create_session, load_session, list_sessions, Session
from src.agent import DesignAgent, TOOLS, MODEL, THINKING_BUDGET, TOKEN_BUDGET, _build_system_prompt, _prune_messages
from src.pipeline.design import parse_design, validate_design
from src.pipeline.placer import place_components, placement_to_dict, parse_placement, PlacementError
from src.pipeline.router import route_traces, routing_to_dict
from src.pipeline.scad import run_scad_step
from src.web.naming import generate_session_name

import anthropic

# ── .env loader ────────────────────────────────────────────────────

def _load_env():
    root = Path(__file__).resolve().parents[2]
    for name in (".env", ".env.local"):
        p = root / name
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v

_load_env()

# ── App ────────────────────────────────────────────────────────────

app = FastAPI(title="ManufacturerAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# ── Catalog (auto-reloads when any catalog/*.json changes on disk) ──

_catalog_result: CatalogResult | None = None
_catalog_mtime: float = 0.0


def _catalog_dir_mtime() -> float:
    """Return the newest mtime among all catalog/*.json files."""
    from src.catalog.loader import CATALOG_DIR
    try:
        return max((p.stat().st_mtime for p in CATALOG_DIR.glob("*.json")), default=0.0)
    except OSError:
        return 0.0


def _get_catalog() -> CatalogResult:
    global _catalog_result, _catalog_mtime
    mtime = _catalog_dir_mtime()
    if _catalog_result is None or mtime > _catalog_mtime:
        _catalog_result = load_catalog()
        _catalog_mtime = mtime
    return _catalog_result


def _reload_catalog() -> CatalogResult:
    global _catalog_result, _catalog_mtime
    _catalog_result = load_catalog()
    _catalog_mtime = _catalog_dir_mtime()
    return _catalog_result


# ── Session helpers ────────────────────────────────────────────────

def _resolve_session(session_id: str | None) -> Session:
    """Get or create a session from the query param."""
    if session_id:
        s = load_session(session_id)
        if s is None:
            raise HTTPException(404, f"Session '{session_id}' not found")
        return s
    # No session specified — create a new one
    return create_session()


# Pipeline ordering — each step depends on everything before it.
_PIPELINE_ORDER = ["design", "placement", "routing", "scad", "manufacturing"]


def _invalidate_downstream(session: Session, current_step: str) -> list[str]:
    """Delete artifacts and pipeline_state for steps after *current_step*.

    Returns the list of step names that were invalidated.
    """
    idx = _PIPELINE_ORDER.index(current_step) if current_step in _PIPELINE_ORDER else -1
    invalidated: list[str] = []
    for later in _PIPELINE_ORDER[idx + 1:]:
        artifact = f"{later}.json"
        if session.has_artifact(artifact):
            session.delete_artifact(artifact)
        if later in session.pipeline_state:
            del session.pipeline_state[later]
            invalidated.append(later)
    return invalidated


# ── Routes: Pages ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main HTML page."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>ManufacturerAI</h1><p>Static files not found.</p>")
    return FileResponse(html_path)


# ── Routes: Session API ───────────────────────────────────────────

@app.get("/api/sessions")
async def api_list_sessions():
    """List all available sessions."""
    return {"sessions": list_sessions()}


@app.post("/api/sessions")
async def api_create_session(description: str = ""):
    """Create a new session. Saves catalog snapshot."""
    session = create_session(description=description)
    cat = _get_catalog()
    session.write_artifact("catalog.json", catalog_to_dict(cat))
    session.pipeline_state["catalog"] = "loaded"
    session.save()
    return {"session_id": session.id, "created": session.created}


@app.get("/api/session")
async def api_get_session(session: str = Query(...)):
    """Get session metadata + pipeline state."""
    s = _resolve_session(session)
    return {
        "id": s.id,
        "created": s.created,
        "last_modified": s.last_modified,
        "description": s.description,
        "name": s.name,
        "pipeline_state": s.pipeline_state,
        "artifacts": {
            "catalog": s.has_artifact("catalog.json"),
            "design": s.has_artifact("design.json"),
            "placement": s.has_artifact("placement.json"),
            "routing": s.has_artifact("routing.json"),
            "scad": s.has_artifact("enclosure.scad"),
        },
    }


# ── Routes: Catalog API ───────────────────────────────────────────

@app.get("/api/catalog")
async def api_catalog():
    """Return the full loaded catalog with validation results."""
    cat = _get_catalog()
    return catalog_to_dict(cat)


@app.post("/api/catalog/reload")
async def api_catalog_reload():
    """Force-reload the catalog from disk."""
    cat = _reload_catalog()
    return catalog_to_dict(cat)


@app.get("/api/catalog/{component_id}")
async def api_catalog_component(component_id: str):
    """Get a single component by ID."""
    cat = _get_catalog()
    for c in cat.components:
        if c.id == component_id:
            from src.catalog import component_to_dict
            return component_to_dict(c)
    raise HTTPException(404, f"Component '{component_id}' not found")


# ── Routes: Session-scoped catalog ─────────────────────────────────

@app.get("/api/session/catalog")
async def api_session_catalog(session: str = Query(...)):
    """Get the catalog snapshot for a session."""
    s = _resolve_session(session)
    data = s.read_artifact("catalog.json")
    if data is None:
        # Generate it on the fly
        cat = _get_catalog()
        data = catalog_to_dict(cat)
        s.write_artifact("catalog.json", data)
        s.pipeline_state["catalog"] = "loaded"
        s.save()
    return data


# ── Routes: Placer API ────────────────────────────────────────────

@app.post("/api/session/placement")
async def api_run_placement(session: str = Query(...)):
    """Run the placer on the session's design. Saves placement.json."""
    s = _resolve_session(session)
    design_data = s.read_artifact("design.json")
    if design_data is None:
        raise HTTPException(400, "No design.json — run the design agent first")

    cat = _get_catalog()
    design = parse_design(design_data)

    errors = validate_design(design, cat)
    if errors:
        raise HTTPException(400, f"Design validation failed: {'; '.join(errors)}")

    try:
        result = place_components(design, cat)
    except PlacementError as e:
        raise HTTPException(
            422,
            detail={
                "error": "placement_failed",
                "instance_id": e.instance_id,
                "catalog_id": e.catalog_id,
                "reason": e.reason,
            },
        )

    data = placement_to_dict(result)
    s.write_artifact("placement.json", data)
    s.pipeline_state["placement"] = "complete"
    # Invalidate downstream: routing depends on placement
    _invalidate_downstream(s, "placement")
    s.save()
    return _enrich_placement(data, cat)


@app.get("/api/session/placement/result")
async def api_placement_result(session: str = Query(...)):
    """Return the saved placement for a session, if any."""
    s = _resolve_session(session)
    data = s.read_artifact("placement.json")
    if data is None:
        raise HTTPException(404, "No placement yet")
    cat = _get_catalog()
    return _enrich_placement(data, cat)


def _enrich_components(components: list, cat) -> None:
    """Add body dimensions (including height) and pin positions from the catalog."""
    cat_map = {c.id: c for c in cat.components}
    for comp in components:
        c = cat_map.get(comp.get("catalog_id"))
        if not c:
            continue
        comp["body"] = {
            "shape": c.body.shape,
            "width_mm": c.body.width_mm,
            "length_mm": c.body.length_mm,
            "diameter_mm": c.body.diameter_mm,
            "height_mm": c.body.height_mm,   # needed for 3D viewport component boxes
        }
        comp["pins"] = [
            {"id": p.id, "position_mm": list(p.position_mm)}
            for p in c.pins
        ]
        comp["ui_placement"] = c.ui_placement
        if c.mounting and c.mounting.cap:
            comp["cap_diameter_mm"] = c.mounting.cap.diameter_mm
            comp["cap_clearance_mm"] = c.mounting.cap.hole_clearance_mm


def _enrich_design_3d(data: dict) -> None:
    """Compute and attach height_grid + per-placement surface_normal to a design dict.

    Mutates *data* in place.  Safe to call multiple times (idempotent).
    The frontend reads these precomputed values directly — no geometry math in JS.
    """
    from src.pipeline.design.parsing import _parse_outline, _parse_enclosure
    from src.pipeline.design.height_field import (
        sample_height_grid, surface_normal_at, blended_height,
    )

    outline_data = data.get("outline", [])
    enclosure_data = data.get("enclosure", {})
    if not outline_data:
        return

    try:
        outline = _parse_outline(outline_data)
        enclosure = _parse_enclosure(enclosure_data)
    except Exception:
        return

    # Sample the height field on a 2mm grid
    grid = sample_height_grid(outline, enclosure, resolution_mm=1.0)
    data["height_grid"] = grid

    # Add surface_normal and z_at_position to each UI placement
    for up in data.get("ui_placements", []):
        x, y = up.get("x_mm", 0), up.get("y_mm", 0)
        try:
            z = blended_height(x, y, outline, enclosure)
            normal = surface_normal_at(x, y, grid)
            up["z_at_position"] = round(z, 3)
            up["surface_normal"] = [round(n, 4) for n in normal]
        except Exception:
            pass


def _enrich_placement(data: dict, cat) -> dict:
    """Add body dimensions and pin positions to each placed component."""
    _enrich_components(data.get("components", []), cat)
    return data


# ── Routes: Router API ────────────────────────────────────────────

@app.post("/api/session/routing")
async def api_run_routing(session: str = Query(...)):
    """Run the router on the session's placement. Saves routing.json."""
    s = _resolve_session(session)
    placement_data = s.read_artifact("placement.json")
    if placement_data is None:
        raise HTTPException(400, "No placement.json — run the placer first")

    cat = _get_catalog()
    placement = parse_placement(placement_data)

    try:
        result = route_traces(placement, cat)
    except Exception as e:
        raise HTTPException(
            422,
            detail={
                "error": "routing_failed",
                "reason": str(e),
            },
        )

    data = routing_to_dict(result)
    # Attach outline + components + nets for the viewport renderer
    data["outline"] = placement_data.get("outline", [])
    data["components"] = placement_data.get("components", [])
    data["nets"] = placement_data.get("nets", [])
    data["enclosure"] = placement_data.get("enclosure", {"height_mm": 25})

    # Enrich components with body + pin data for rendering
    _enrich_components(data.get("components", []), cat)

    s.write_artifact("routing.json", data)
    s.pipeline_state["routing"] = "complete"
    s.save()
    return data


@app.get("/api/session/routing/result")
async def api_routing_result(session: str = Query(...)):
    """Return the saved routing for a session, if any."""
    s = _resolve_session(session)
    data = s.read_artifact("routing.json")
    if data is None:
        raise HTTPException(404, "No routing yet")
    # Re-enrich components with body + pin data if missing
    cat = _get_catalog()
    for comp in data.get("components", []):
        if "body" not in comp or "pins" not in comp:
            _enrich_components([comp], cat)
    return data


# ── Routes: SCAD API ─────────────────────────────────────────────

@app.post("/api/session/scad")
async def api_run_scad(session: str = Query(...)):
    """Generate enclosure.scad from placement + routing.  Saves to session folder."""
    s = _resolve_session(session)
    if s.read_artifact("placement.json") is None:
        raise HTTPException(400, "No placement.json — run the placer first")
    if s.read_artifact("routing.json") is None:
        raise HTTPException(400, "No routing.json — run the router first")

    try:
        scad_path = run_scad_step(s)
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"error": "scad_failed", "reason": str(exc)},
        )

    scad_text = scad_path.read_text(encoding="utf-8")
    return {
        "status": "done",
        "scad_lines": scad_text.count("\n"),
        "scad_bytes": len(scad_text),
    }


@app.get("/api/session/scad/result")
async def api_scad_result(session: str = Query(...)):
    """Return the generated enclosure.scad text, if available."""
    s = _resolve_session(session)
    scad_path = s.path / "enclosure.scad"
    if not scad_path.exists():
        raise HTTPException(404, "No enclosure.scad yet -- run /api/session/scad first")
    scad_text = scad_path.read_text(encoding="utf-8")
    return {
        "status": "done",
        "scad": scad_text,
        "scad_lines": scad_text.count("\n"),
        "scad_bytes": len(scad_text),
    }


@app.post("/api/session/scad/compile")
async def api_compile_stl(session: str = Query(...), force: bool = Query(False)):
    """Start background STL compilation for the session's enclosure.scad.

    Pass ``force=true`` to restart compilation even if a previous attempt
    finished with an error (or succeeded).
    """
    s = _resolve_session(session)
    scad_path = s.path / "enclosure.scad"
    if not scad_path.exists():
        raise HTTPException(400, "No enclosure.scad yet -- run /api/session/scad first")

    stl_path = s.path / "enclosure.stl"

    # Already done (and not forcing a redo)
    if not force and stl_path.exists() and session not in _stl_compile:
        return {"status": "done", "stl_bytes": stl_path.stat().st_size}

    # Already compiling
    cur = _stl_compile.get(session)
    if cur and cur["status"] == "compiling" and not force:
        return {"status": "compiling"}

    # Return cached status if done/error (and not forcing)
    if not force and cur and cur["status"] in ("done", "error"):
        return {"status": cur["status"], "message": cur.get("message", ""),
                "stl_bytes": stl_path.stat().st_size if stl_path.exists() else 0}

    # Cancel any in-flight compile when forcing
    if force and cur and cur.get("cancel"):
        cur["cancel"].set()

    # Start a new compile
    cancel = threading.Event()
    _stl_compile[session] = {"status": "compiling", "cancel": cancel, "message": ""}

    def _do_compile():
        from src.pipeline.scad.compiler import compile_scad
        ok, msg, out = compile_scad(scad_path, stl_path, cancel=cancel, timeout=600)
        _stl_compile[session] = {
            "status": "done" if ok else "error",
            "message": msg,
            "cancel": cancel,
        }

    threading.Thread(target=_do_compile, daemon=True).start()
    return {"status": "compiling"}


@app.get("/api/session/scad/compile")
async def api_compile_stl_status(session: str = Query(...)):
    """Poll the STL compilation status for the session."""
    s = _resolve_session(session)
    stl_path = s.path / "enclosure.stl"
    state = _stl_compile.get(session)
    if state:
        out = {"status": state["status"], "message": state.get("message", "")}
        if state["status"] == "done" and stl_path.exists():
            out["stl_bytes"] = stl_path.stat().st_size
        return out
    # Not in state dict — check if file exists on disk
    if stl_path.exists():
        return {"status": "done", "stl_bytes": stl_path.stat().st_size}
    return {"status": "pending"}


@app.get("/api/session/scad/stl")
async def api_serve_stl(session: str = Query(...)):
    """Serve the compiled enclosure.stl as a binary download."""
    s = _resolve_session(session)
    stl_path = s.path / "enclosure.stl"
    if not stl_path.exists():
        raise HTTPException(404, "No enclosure.stl yet -- compile first")
    return FileResponse(
        stl_path,
        media_type="application/octet-stream",
        filename="enclosure.stl",
    )


# ── Routes: Design Agent API ──────────────────────────────────────

@app.get("/api/session/tokens")
def api_session_tokens(session: str = Query(...)):
    """Return the current input token count for the session's conversation."""
    s = _resolve_session(session)
    conversation = s.read_artifact("conversation.json")
    if not conversation or not isinstance(conversation, list):
        return {"input_tokens": 0, "budget": TOKEN_BUDGET}

    cat = _get_catalog()
    system = _build_system_prompt(cat)
    pruned = _prune_messages(conversation)
    client = anthropic.Anthropic()
    try:
        result = client.messages.count_tokens(
            model=MODEL,
            messages=pruned,
            system=system,
            tools=TOOLS,
            thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
        )
        return {"input_tokens": result.input_tokens, "budget": TOKEN_BUDGET}
    except Exception:
        return {"input_tokens": 0, "budget": TOKEN_BUDGET}


@app.get("/api/session/conversation")
async def api_conversation(session: str = Query(...)):
    """Return the saved conversation history for a session."""
    s = _resolve_session(session)
    data = s.read_artifact("conversation.json")
    return data if isinstance(data, list) else []


@app.get("/api/session/design/result")
async def api_design_result(session: str = Query(...)):
    """Return the saved design spec for a session, if any."""
    s = _resolve_session(session)
    data = s.read_artifact("design.json")
    if data is None:
        raise HTTPException(404, "No design yet")
    # Enrich components with body + pin data for rendering
    cat = _get_catalog()
    _enrich_components(data.get("components", []), cat)
    _enrich_design_3d(data)
    return data


@app.patch("/api/session/design/enclosure")
async def api_patch_enclosure(request: Request, session: str = Query(...)):
    """Patch enclosure fields (edge_top, edge_bottom, height_mm, top_surface).

    Accepts a partial enclosure JSON object.  Only keys present in the body
    are merged; all others are left unchanged.  Returns the full enriched
    design dict so the frontend can re-render immediately.
    """
    body = await request.json()
    s = _resolve_session(session)
    data = s.read_artifact("design.json")
    if data is None:
        raise HTTPException(404, "No design yet")

    enc = data.setdefault("enclosure", {})
    for key in ("height_mm", "top_surface", "edge_top", "edge_bottom"):
        if key in body:
            enc[key] = body[key]

    s.write_artifact("design.json", data)
    _enrich_design_3d(data)
    return data


@app.post("/api/session/design")
async def api_design(request: Request, session: str = Query(None)):
    """
    Run the design agent. Returns an SSE stream.

    If no session is provided, a new session is created automatically
    and its ID is sent as the first SSE event.

    Body: {"prompt": "Design a flashlight with..."}

    SSE event types:
      session_created — new session was auto-created (data: {"session_id": "..."})
      thinking_start  — new thinking block
      thinking_delta  — incremental thinking text (data: {"text": "..."})
      message_start   — new text block
      message_delta   — incremental text (data: {"text": "..."})
      block_stop      — current content block finished
      tool_call       — tool invocation
      tool_result     — tool call result
      design          — validated design spec
      error           — error message
      done            — agent finished
    """
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "Missing 'prompt' in request body")

    # Auto-create session if none specified
    created_new = False
    if session:
        sess = _resolve_session(session)
    else:
        sess = create_session()
        cat = _get_catalog()
        sess.write_artifact("catalog.json", catalog_to_dict(cat))
        sess.pipeline_state["catalog"] = "loaded"
        sess.save()
        created_new = True

    cat = _get_catalog()

    async def event_stream():
        try:
            # Notify the client of the new session ID
            if created_new:
                data = json.dumps({"session_id": sess.id})
                yield f"event: session_created\ndata: {data}\n\n"

            agent = DesignAgent(cat, sess)
            async for event in agent.run(prompt):
                # Enrich design components with body + pin data
                if event.type == "design" and event.data:
                    design = event.data.get("design")
                    if design:
                        _enrich_components(
                            design.get("components", []), cat,
                        )
                        _enrich_design_3d(design)
                data = json.dumps(event.data) if event.data else "{}"
                yield f"event: {event.type}\ndata: {data}\n\n"

                # After a successful design submission, generate a session name
                if event.type == "design":
                    name = generate_session_name(sess)
                    if name:
                        yield f"event: session_named\ndata: {json.dumps({'name': name})}\n\n"
        except Exception as e:
            data = json.dumps({"message": str(e)})
            yield f"event: error\ndata: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("Starting ManufacturerAI server on http://localhost:8000")
    uvicorn.run("src.web.server:app", host="127.0.0.1", port=8000, reload=True)
