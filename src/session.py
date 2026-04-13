"""
Session management — each session is a folder on disk holding all pipeline
artifacts (design spec, placement, routing, SCAD, G-code, etc.).

Sessions are identified by a short ID (timestamp-based) and stored under
  outputs/sessions/<session_id>/

A session folder contains:
  session.json   — metadata (created, last_modified, description, pipeline_state)
  catalog.json   — snapshot of the catalog result at session creation time
  design.json    — agent's DesignSpec (once created)
  placement.json — placer output
  routing.json   — router output
  enclosure.scad / enclosure.stl
  manufacturing/ — G-code + ink SVG

This module manages creation, loading, listing, and updating of sessions.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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
    pipeline_state: dict = field(default_factory=dict)  # stage -> status
    pipeline_errors: dict = field(default_factory=dict)  # stage -> {error, reason}
    version: int = 0                     # increments on every save()
    _batch_depth: int = field(default=0, repr=False)  # nesting depth for batch_update

    def save(self) -> None:
        """Persist session metadata to session.json.

        Inside a ``batch_update()`` context, this is deferred until the
        outermost context exits.  Outside of one it writes immediately.
        """
        if self._batch_depth > 0:
            return  # deferred — batch_update __exit__ will call _do_save
        self._do_save()

    def _do_save(self) -> None:
        """Unconditionally write session.json to disk."""
        self.version += 1
        self.last_modified = datetime.now(timezone.utc).isoformat()
        self.path.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": self.id,
            "created": self.created,
            "last_modified": self.last_modified,
            "description": self.description,
            "name": self.name,
            "pipeline_state": self.pipeline_state,
            "pipeline_errors": self.pipeline_errors,
            "version": self.version,
        }
        (self.path / "session.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8")

    def batch_update(self):
        """Context manager that defers ``save()`` calls until exit.

        Usage::

            with session.batch_update():
                session.pipeline_state["placement"] = "complete"
                session.write_artifact("placement.json", data)
                session.save()  # no-op inside batch
            # single save() happens here
        """
        return _BatchUpdate(self)

    def write_artifact(self, filename: str, data: Any) -> Path:
        """Write a JSON artifact to the session folder.

        NOTE: Does NOT auto-save session.json.  Callers must call
        ``save()`` explicitly after all artifact writes are complete
        to keep session metadata in sync with artifact files.
        """
        p = self.path / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def read_artifact(self, filename: str) -> Any | None:
        """Read a JSON artifact from the session folder. Returns None if missing."""
        p = self.path / filename
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def has_artifact(self, filename: str) -> bool:
        return (self.path / filename).exists()

    def delete_artifact(self, filename: str) -> bool:
        """Delete a JSON artifact. Returns True if it existed."""
        p = self.path / filename
        if p.exists():
            p.unlink()
            return True
        return False

    def record_error(self, stage: str, error: str, reason: str = "") -> None:
        """Record a structured pipeline error for *stage*."""
        self.pipeline_errors[stage] = {"error": error, "reason": reason}

    def clear_error(self, stage: str) -> None:
        """Remove a recorded error for *stage*."""
        self.pipeline_errors.pop(stage, None)


class _BatchUpdate:
    """Context manager returned by :meth:`Session.batch_update`."""

    def __init__(self, session: Session):
        self._session = session

    def __enter__(self):
        self._session._batch_depth += 1
        return self._session

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._session._batch_depth -= 1
        if self._session._batch_depth == 0:
            self._session._do_save()
        return False  # don't suppress exceptions


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
    try:
        return Session(
            id=meta["id"],
            path=path,
            created=meta["created"],
            last_modified=meta["last_modified"],
            description=meta.get("description", ""),
            name=meta.get("name", ""),
            pipeline_state=meta.get("pipeline_state", {}),
            pipeline_errors=meta.get("pipeline_errors", {}),
            version=meta.get("version", 0),
        )
    except (KeyError, TypeError) as e:
        log.warning("Corrupted session.json for %s: %s", session_id, e)
        return None


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
                "pipeline_state": meta.get("pipeline_state", {}),
                "pipeline_errors": meta.get("pipeline_errors", {}),
                "version": meta.get("version", 0),
            })
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            log.warning("Skipping corrupted session %s: %s", d.name, e)
            continue
    return sessions
