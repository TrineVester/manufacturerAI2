"""SCAD pipeline stage — generate enclosure.scad from placement + routing.

Public entry point
------------------
    from src.pipeline.scad import run_scad_step
    scad_path = run_scad_step(session)
"""

from .generator import run_scad_step  # noqa: F401

__all__ = ["run_scad_step"]
