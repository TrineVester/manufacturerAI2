"""Background task registry — runs agents and pipeline steps detached from HTTP connections.

Each task is identified by a composite key (session_id, task_type) where task_type
is one of: "design", "circuit", "placement", "routing", "bitmap", "scad", "compile", "gcode".

Agent tasks accumulate SSE-style events in an in-memory buffer that clients can
subscribe to at any offset, enabling reconnect-and-resume.

Pipeline tasks just track status: "running" | "done" | "error".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── Task state ────────────────────────────────────────────────────

@dataclass
class AgentTask:
    """A running or completed agent (design / circuit) background task."""
    status: str = "running"                    # running | done | error
    events: list[dict[str, Any]] = field(default_factory=lambda: [])
    error: str | None = None
    asyncio_task: asyncio.Task[Any] | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_save_cursor: int = 0

    def append_event(self, etype: str, data: dict[str, Any]) -> None:
        self.events.append({"type": etype, "data": data})

    def finish(self, status: str = "done", error: str | None = None) -> None:
        self.status = status
        self.error = error


@dataclass
class PipelineTask:
    """A running or completed pipeline step (placement, routing, etc.)."""
    status: str = "running"
    message: str = ""
    error: str | None = None
    result: Any = None
    detail: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


# ── Registry ──────────────────────────────────────────────────────

_lock = threading.Lock()
_agent_tasks: dict[tuple[str, str], AgentTask] = {}       # (sid, "design"|"circuit") -> AgentTask
_pipeline_tasks: dict[tuple[str, str], PipelineTask] = {}  # (sid, step) -> PipelineTask


def get_agent_task(sid: str, agent: str) -> AgentTask | None:
    with _lock:
        return _agent_tasks.get((sid, agent))


def set_agent_task(sid: str, agent: str, task: AgentTask) -> None:
    with _lock:
        old = _agent_tasks.get((sid, agent))
        if old and old.asyncio_task and not old.asyncio_task.done():
            old.cancel_event.set()
        _agent_tasks[(sid, agent)] = task


def remove_agent_task(sid: str, agent: str) -> None:
    with _lock:
        _agent_tasks.pop((sid, agent), None)


_pipeline_subscribers: dict[str, list[asyncio.Event]] = {}  # sid -> [event, ...]


def get_pipeline_task(sid: str, step: str) -> PipelineTask | None:
    with _lock:
        return _pipeline_tasks.get((sid, step))


def set_pipeline_task(sid: str, step: str, task: PipelineTask) -> None:
    with _lock:
        _pipeline_tasks[(sid, step)] = task
        for ev in _pipeline_subscribers.get(sid, []):
            ev.set()


def remove_pipeline_task(sid: str, step: str) -> None:
    with _lock:
        _pipeline_tasks.pop((sid, step), None)


def subscribe_pipeline(sid: str, event: asyncio.Event) -> None:
    with _lock:
        _pipeline_subscribers.setdefault(sid, []).append(event)


def unsubscribe_pipeline(sid: str, event: asyncio.Event) -> None:
    with _lock:
        subs = _pipeline_subscribers.get(sid, [])
        if event in subs:
            subs.remove(event)
        if not subs:
            _pipeline_subscribers.pop(sid, None)


def get_all_pipeline_tasks(sid: str) -> dict[str, PipelineTask]:
    with _lock:
        return {
            step: task
            for (s, step), task in _pipeline_tasks.items()
            if s == sid
        }


def cancel_all_pipeline_tasks(sid: str) -> None:
    """Signal every running pipeline task for *sid* to abort."""
    with _lock:
        for (s, step), task in _pipeline_tasks.items():
            if s == sid and task.status == "running":
                task.cancel_event.set()
