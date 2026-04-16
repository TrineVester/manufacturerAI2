"""Shared state and helpers used by route modules."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from fastapi import HTTPException

from src.catalog import load_catalog, CatalogResult
from src.session import load_session, Session
from src.web.tasks import _lock as _tasks_lock, _pipeline_subscribers


# ── Thread-safe background-task state ──

_compile_lock = threading.Lock()
_gcode_lock = threading.Lock()

_stl_compile: dict[str, dict[str, Any]] = {}
_gcode_state: dict[str, dict[str, Any]] = {}


def _notify_subscribers(sid: str) -> None:
    with _tasks_lock:
        for ev in _pipeline_subscribers.get(sid, []):
            ev.set()


def get_compile_state(sid: str) -> dict[str, Any] | None:
    with _compile_lock:
        return _stl_compile.get(sid)


def set_compile_state(sid: str, state: dict[str, Any]) -> None:
    with _compile_lock:
        _stl_compile[sid] = state
    _notify_subscribers(sid)


def get_gcode_state(sid: str) -> dict[str, Any] | None:
    with _gcode_lock:
        return _gcode_state.get(sid)


def set_gcode_state(sid: str, state: dict[str, Any]) -> None:
    with _gcode_lock:
        _gcode_state[sid] = state
    _notify_subscribers(sid)


# ── Catalog (auto-reloads when any catalog/*.json changes on disk) ──

_catalog_result: CatalogResult | None = None
_catalog_mtime: float = 0.0


def _catalog_dir_mtime() -> float:
    from src.catalog.loader import CATALOG_DIR
    try:
        return max((p.stat().st_mtime for p in CATALOG_DIR.glob("*.json")), default=0.0)
    except OSError:
        return 0.0


def get_catalog() -> CatalogResult:
    global _catalog_result, _catalog_mtime
    mtime = _catalog_dir_mtime()
    if _catalog_result is None or mtime > _catalog_mtime:
        _catalog_result = load_catalog()
        _catalog_mtime = mtime
    return _catalog_result


def reload_catalog() -> CatalogResult:
    global _catalog_result, _catalog_mtime
    _catalog_result = load_catalog()
    _catalog_mtime = _catalog_dir_mtime()
    return _catalog_result


# ── Session helpers ──

def load_session_or_404(sid: str) -> Session:
    s = load_session(sid)
    if s is None:
        raise HTTPException(404, f"Session '{sid}' not found")
    return s


def invalidate_downstream(session: Session, current_step: str) -> list[str]:
    return session.invalidate_downstream(current_step)


# ── Session artifact readers (raw dicts) ──

def _read_outline(session: Session) -> list:
    """Return the outline vertex list from outline.json, falling back to design.json."""
    data = session.read_artifact("outline.json")
    if data is not None:
        return data.get("outline", [])
    # Fallback: read outline from design.json
    design = session.read_artifact("design.json")
    if design is not None:
        return design.get("outline", [])
    return []


def _read_outline_full(session: Session) -> list:
    """Return outline points for API responses.

    Falls back to design.json when outline.json is absent.
    Returns the raw point list (frontend expects [{x, y, ...}, ...]).
    """
    data = session.read_artifact("outline.json")
    if data is not None:
        return data.get("outline", [])
    # Fallback: read outline from design.json
    design = session.read_artifact("design.json")
    if design is not None:
        return design.get("outline", [])
    return []


def require_design(session: Session) -> dict:
    data = session.read_artifact("design.json")
    if data is None:
        raise HTTPException(400, "No design.json — run the design agent first")
    return data


def require_circuit(session: Session) -> dict:
    data = session.read_artifact("circuit.json")
    if data is None:
        raise HTTPException(400, "No circuit.json — run the circuit agent first")
    return data


def require_placement(session: Session) -> dict:
    data = session.read_artifact("placement.json")
    if data is None:
        raise HTTPException(400, "No placement.json — run the placer first")
    return data


def require_routing(session: Session) -> dict:
    data = session.read_artifact("routing.json")
    if data is None:
        raise HTTPException(400, "No routing.json — run the router first")
    return data


# ── Enrichment ──

def enrich_components(components: list, cat) -> None:
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
            "height_mm": c.body.height_mm,
        }
        pp = comp.get("pin_positions", {})
        comp["pins"] = [
            {
                "id": p.id,
                "label": p.label,
                "position_mm": list(p.position_mm),
                "direction": p.direction,
                "hole_diameter_mm": p.hole_diameter_mm,
                "description": p.description,
                "voltage_v": p.voltage_v,
                "current_max_ma": p.current_max_ma,
                **({"shape": {"type": p.shape.type, "width_mm": p.shape.width_mm, "length_mm": p.shape.length_mm}} if p.shape else {}),
                **({"world_mm": list(pp[p.id])} if p.id in pp else {}),
            }
            for p in c.pins
        ]
        comp["ui_placement"] = c.ui_placement
        if c.mounting and c.mounting.cap:
            comp["cap_diameter_mm"] = c.mounting.cap.diameter_mm
            comp["cap_clearance_mm"] = c.mounting.cap.hole_clearance_mm


_shape_cache: dict[str, dict] = {}


def _shape_cache_key(outline_data, enclosure_data) -> str:
    raw = json.dumps({"o": outline_data, "e": enclosure_data}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _get_shape_fields(outline_data, enclosure_data) -> dict | None:
    """Compute or retrieve cached height grids and PCB contour."""
    if not outline_data:
        return None

    key = _shape_cache_key(outline_data, enclosure_data)
    cached = _shape_cache.get(key)
    if cached is not None:
        return cached

    from src.pipeline.design.parsing import _parse_outline, _parse_enclosure
    from src.pipeline.design.height_field import (
        sample_height_grid, sample_bottom_height_grid,
        pcb_contour_from_bottom_grid,
    )
    from src.pipeline.config import FLOOR_MM

    try:
        outline = _parse_outline(outline_data)
        enclosure = _parse_enclosure(enclosure_data)
    except Exception:
        return None

    result: dict = {
        "height_grid": sample_height_grid(outline, enclosure, resolution_mm=1.0),
    }
    bottom_grid = sample_bottom_height_grid(outline, enclosure, resolution_mm=1.0)
    if bottom_grid is not None:
        result["bottom_height_grid"] = bottom_grid
        contour = pcb_contour_from_bottom_grid(bottom_grid, outline, threshold_mm=FLOOR_MM)
        if contour is not None:
            result["pcb_contour"] = contour

    _shape_cache[key] = result
    return result


def _add_shape_fields(data: dict, outline_data, enclosure_data) -> None:
    """Add cached height grids and pcb_contour to a response dict."""
    fields = _get_shape_fields(outline_data, enclosure_data)
    if fields:
        for k in ("height_grid", "bottom_height_grid", "pcb_contour"):
            if k in fields:
                data[k] = fields[k]


def _tessellate_button_shapes(placements: list[dict]) -> None:
    from src.pipeline.design.shape2d import tessellate_shape
    for up in placements:
        shape = up.get("button_shape")
        if shape is None:
            continue
        if up.get("button_outline"):
            continue
        try:
            outline = tessellate_shape(shape)
            up["button_outline"] = [[round(v.x, 2), round(v.y, 2)] for v in outline.points]
        except Exception:
            pass


def enrich_design(data: dict, cat, session: Session | None = None) -> None:
    """Enrich a design dict: component bodies/pins + 3D height fields + surface data."""
    enrich_components(data.get("ui_placements", []), cat)
    _tessellate_button_shapes(data.get("ui_placements", []))
    outline_data = _read_outline(session) if session else data.get("outline", [])
    enclosure_data = data.get("enclosure", {})
    if outline_data:
        data["outline"] = _read_outline_full(session) if session else outline_data
    _add_shape_fields(data, outline_data, enclosure_data)

    fields = _get_shape_fields(outline_data, enclosure_data)
    if fields is None:
        return

    from src.pipeline.design.parsing import _parse_outline, _parse_enclosure
    from src.pipeline.design.height_field import blended_height, surface_normal_at
    try:
        outline = _parse_outline(outline_data)
        enclosure = _parse_enclosure(enclosure_data)
    except Exception:
        return

    grid = fields["height_grid"]
    for up in data.get("ui_placements", []):
        x, y = up.get("x_mm", 0), up.get("y_mm", 0)
        try:
            up["z_at_position"] = round(blended_height(x, y, outline, enclosure), 3)
            up["surface_normal"] = [round(n, 4) for n in surface_normal_at(x, y, grid)]
        except Exception:
            pass


# ── Response assembly ──
#
# Each artifact stores only what its pipeline step produces.
# These functions assemble the full picture for API responses
# by combining upstream artifacts.

_ENRICHED_PLACEMENT_FIELDS = {
    "body", "pins", "ui_placement", "cap_diameter_mm", "cap_clearance_mm",
    "z_at_position", "surface_normal", "pin_positions",
}


def strip_enriched_fields(design: dict) -> dict:
    """Return a copy of the design dict with only agent-authored fields.

    Removes catalog-derived metadata (body, pins, etc.) from ui_placements
    and top-level computed fields (outline, height_grid, etc.) so that
    design.json stores only what the agent produced.
    """
    clean = {
        "device_description": design.get("device_description", ""),
        "name": design.get("name", ""),
        "shape": design.get("shape"),
        "enclosure": design.get("enclosure"),
        "ui_placements": [],
    }
    for p in design.get("ui_placements", []):
        clean_p = {k: v for k, v in p.items() if k not in _ENRICHED_PLACEMENT_FIELDS}
        clean["ui_placements"].append(clean_p)
    return clean


def build_placement_response(session: Session, cat) -> dict:
    """Assemble a full placement response from design + circuit + placement artifacts."""
    design = require_design(session)
    placement = require_placement(session)
    circuit = require_circuit(session)
    outline_data = _read_outline(session)

    response = {
        "outline": _read_outline_full(session),
        "enclosure": design.get("enclosure", {}),
        "components": placement["components"],
        "nets": circuit.get("nets", []),
    }
    enrich_components(response["components"], cat)
    _add_shape_fields(response, outline_data, response["enclosure"])
    return response


def build_routing_response(session: Session, cat) -> dict:
    """Assemble a full routing response from design + placement + routing artifacts."""
    design = require_design(session)
    placement = require_placement(session)
    routing = require_routing(session)
    outline_data = _read_outline(session)

    from src.pipeline.config import TRACE_RULES
    response = {
        "outline": _read_outline_full(session),
        "enclosure": design.get("enclosure", {}),
        "components": placement["components"],
        "traces": routing.get("traces", []),
        "pin_assignments": routing.get("pin_assignments", {}),
        "failed_nets": routing.get("failed_nets", []),
        "jumpers": routing.get("jumpers", []),
        "trace_width_mm": TRACE_RULES.trace_width_mm,
        "pin_clearance_mm": TRACE_RULES.pin_clearance_mm,
    }
    enrich_components(response["components"], cat)
    _add_shape_fields(response, outline_data, response["enclosure"])
    return response
