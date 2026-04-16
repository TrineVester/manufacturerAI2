"""
Session management — each session is a folder on disk holding all pipeline
artifacts (design spec, placement, routing, SCAD, G-code, etc.).

Sessions are identified by a short ID (timestamp-based) and stored under
  outputs/sessions/<session_id>/

A session folder contains:
  session.json   — metadata (created, last_modified, description, pipeline_state)
  design.json    — agent's DesignSpec (once created)
  placement.json — placer output
  routing.json   — router output
  enclosure.scad / enclosure.stl
  manufacturing/ — G-code + ink SVG

This module manages creation, loading, listing, and updating of sessions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from src.pipeline.config import DEFAULT_PRINTER

DEFAULT_FILAMENT = "pla"

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "outputs" / "sessions"


@dataclass
class Session:
    id: str
    path: Path
    created: str                         # ISO 8601
    last_modified: str                   # ISO 8601
    description: str = ""
    name: str = ""                       # LLM-generated friendly name
    printer_id: str = DEFAULT_PRINTER
    filament_id: str = DEFAULT_FILAMENT
    model_id: str = "medium"
    pipeline_state: dict = field(default_factory=dict)  # stage -> status
    pipeline_errors: dict = field(default_factory=dict)  # stage -> {error, reason, responsible_agent}

    def set_step_error(self, step: str, detail: dict) -> None:
        """Persist an error for a pipeline step."""
        self.pipeline_errors[step] = detail
        self.save()

    def clear_step_error(self, step: str) -> None:
        """Remove a persisted error for a pipeline step."""
        self.pipeline_errors.pop(step, None)

    def save(self) -> None:
        """Persist session metadata to session.json."""
        self.last_modified = datetime.now(timezone.utc).isoformat()
        self.path.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": self.id,
            "created": self.created,
            "last_modified": self.last_modified,
            "description": self.description,
            "name": self.name,
            "printer_id": self.printer_id,
            "filament_id": self.filament_id,
            "model_id": self.model_id,
            "pipeline_state": self.pipeline_state,
            "pipeline_errors": self.pipeline_errors,
        }
        (self.path / "session.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8")

    def artifact_path(self, filename: str) -> Path:
        """Return the full path for an artifact, namespaced by pipeline stage."""
        stage = self._ARTIFACT_STAGE.get(filename)
        if stage:
            return self.path / stage / filename
        return self.path / filename

    def write_artifact(self, filename: str, data: Any) -> Path:
        """Write a JSON artifact to the session folder."""
        p = self.artifact_path(filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.save()
        return p

    def write_artifact_text(self, filename: str, text: str) -> Path:
        """Write a raw text artifact to the session folder."""
        p = self.artifact_path(filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.save()
        return p

    def read_artifact(self, filename: str) -> Any | None:
        """Read a JSON artifact from the session folder. Returns None if missing."""
        p = self.artifact_path(filename)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def has_artifact(self, filename: str) -> bool:
        return self.artifact_path(filename).exists()

    def delete_artifact(self, filename: str) -> bool:
        """Delete a JSON artifact. Returns True if it existed."""
        p = self.artifact_path(filename)
        if p.exists():
            p.unlink()
            return True
        return False

    @property
    def artifacts(self) -> dict[str, bool]:
        return {
            "catalog": True,
            "design": self.has_artifact("design.json"),
            "circuit": self.has_artifact("circuit.json"),
            "circuit_pending": self.has_artifact("circuit_pending.json"),
            "placement": self.has_artifact("placement.json"),
            "routing": self.has_artifact("routing.json"),
            "bitmap": self.has_artifact("trace_bitmap.txt"),
            "scad": self.has_artifact("enclosure.scad") or self.has_artifact("enclosure_bottom.scad"),
            "compile": self.has_artifact("enclosure.stl") or self.has_artifact("enclosure_bottom.stl"),
            "gcode": self.has_artifact("enclosure.gcode"),
            "firmware": self.has_artifact("firmware.ino"),
        }

    _PIPELINE_ORDER: ClassVar[list[str]] = ["design", "circuit", "placement", "routing", "bitmap", "scad", "gcode", "firmware"]
    _STAGE_ARTIFACTS: ClassVar[dict[str, list[str]]] = {
        "design": ["design.json", "outline.json", "design_conversation.json"],
        "circuit": ["circuit.json", "circuit_conversation.json"],
        "placement": ["placement.json"],
        "routing": ["routing.json", "routing_debug.json"],
        "bitmap": ["trace_bitmap.txt"],
        "scad": ["enclosure.scad", "enclosure.stl", "extras.scad", "extras.stl",
                "enclosure_bottom.scad", "enclosure_bottom.stl",
                "enclosure_top.scad", "enclosure_top.stl"],
        "gcode": ["enclosure.gcode", "print_job.json"],
        "firmware": ["firmware.ino", "sim_config.json"],
    }

    _ARTIFACT_STAGE: ClassVar[dict[str, str]] = {
        "design.json": "design",
        "outline.json": "design",
        "design_conversation.json": "design",
        "circuit.json": "circuit",
        "circuit_pending.json": "circuit",
        "circuit_conversation.json": "circuit",
        "placement.json": "placement",
        "routing.json": "routing",
        "routing_debug.json": "routing",
        "trace_bitmap.txt": "bitmap",
        "enclosure.scad": "scad",
        "enclosure.stl": "scad",
        "enclosure_bottom.scad": "scad",
        "enclosure_bottom.stl": "scad",
        "enclosure_top.scad": "scad",
        "enclosure_top.stl": "scad",
        "extras.scad": "scad",
        "extras.stl": "scad",
        "enclosure.gcode": "gcode",
        "firmware.ino": "firmware",
        "print_job.json": "gcode",
        "sim_config.json": "firmware",
    }

    def clear_stage_artifacts(self, stage: str) -> None:
        """Delete all artifacts for a pipeline stage before rerunning it."""
        for artifact in self._STAGE_ARTIFACTS.get(stage, []):
            self.delete_artifact(artifact)

    def invalidate_downstream(self, current_step: str) -> list[str]:
        """Delete artifacts and pipeline_state for all stages after *current_step*."""
        idx = self._PIPELINE_ORDER.index(current_step) if current_step in self._PIPELINE_ORDER else -1
        invalidated: list[str] = []
        for later in self._PIPELINE_ORDER[idx + 1:]:
            for artifact in self._STAGE_ARTIFACTS.get(later, [f"{later}.json"]):
                self.delete_artifact(artifact)
            if later in self.pipeline_state:
                del self.pipeline_state[later]
                invalidated.append(later)
            self.pipeline_errors.pop(later, None)
        return invalidated

    def invalidate_design_smart(self, new_design: dict) -> list[str]:
        """Invalidate downstream of design, but skip circuit if components unchanged.

        Only invalidates circuit when ui_placements differ in identity or
        configuration (added, removed, catalog_id changed, config changed,
        mounting_style changed). Pure position changes (x_mm, y_mm) and
        outline/enclosure changes do NOT invalidate circuit.

        When circuit IS invalidated due to component changes, the existing
        circuit.json is preserved as circuit_pending.json so it can be
        re-validated against the updated design without re-running the LLM.
        """
        old_design = self.read_artifact("design.json")
        if _components_changed(old_design, new_design):
            circuit_data = self.read_artifact("circuit.json")
            if circuit_data and not self.has_artifact("circuit_pending.json"):
                self.write_artifact("circuit_pending.json", circuit_data)
            return self.invalidate_downstream("design")
        return self.invalidate_downstream("circuit")


def _component_signature(design: dict | None) -> set[tuple]:
    """Extract a hashable set of (instance_id, catalog_id, config, mounting_style)
    from ui_placements, ignoring positional fields."""
    if not design:
        return set()
    sigs = set()
    for p in design.get("ui_placements", []):
        config = p.get("config")
        config_key = tuple(sorted(config.items())) if isinstance(config, dict) else config
        sigs.add((
            p.get("instance_id"),
            p.get("catalog_id"),
            config_key,
            p.get("mounting_style"),
        ))
    return sigs


def _components_changed(old_design: dict | None, new_design: dict | None) -> bool:
    """Return True if components were added, removed, or changed values."""
    return _component_signature(old_design) != _component_signature(new_design)


def _generate_session_id() -> str:
    """Generate a short, unique, human-readable session ID."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def create_session(description: str = "") -> Session:
    """Create a new session with a fresh folder on disk."""
    sid = _generate_session_id()
    path = SESSIONS_DIR / sid

    # Avoid collision (rare but possible if called twice in same second)
    while path.exists():
        time.sleep(0.1)
        sid = _generate_session_id()
        path = SESSIONS_DIR / sid

    now = datetime.now(timezone.utc).isoformat()
    session = Session(
        id=sid,
        path=path,
        created=now,
        last_modified=now,
        description=description,
    )
    session.save()
    return session


def load_session(session_id: str) -> Session | None:
    """Load an existing session by ID. Returns None if not found."""
    path = SESSIONS_DIR / session_id
    meta_path = path / "session.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return Session(
        id=meta["id"],
        path=path,
        created=meta["created"],
        last_modified=meta["last_modified"],
        description=meta.get("description", ""),
        name=meta.get("name", ""),
        printer_id=meta.get("printer_id", DEFAULT_PRINTER),
        filament_id=meta.get("filament_id", DEFAULT_FILAMENT),
        model_id=meta.get("model_id", "medium"),
        pipeline_state=meta.get("pipeline_state", {}),
        pipeline_errors=meta.get("pipeline_errors", {}),
    )


def list_sessions() -> list[dict]:
    """List all sessions, newest first. Returns lightweight metadata dicts."""
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for d in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "session.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            sessions.append({
                "id": meta["id"],
                "created": meta["created"],
                "last_modified": meta["last_modified"],
                "description": meta.get("description", ""),
                "name": meta.get("name", ""),
                "printer_id": meta.get("printer_id", DEFAULT_PRINTER),
                "pipeline_state": meta.get("pipeline_state", {}),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return sessions
