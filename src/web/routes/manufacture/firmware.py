from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from src.catalog import load_catalog
from src.web.routes._deps import load_session_or_404, require_routing

router = APIRouter()


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
