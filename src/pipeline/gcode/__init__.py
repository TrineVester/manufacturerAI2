"""GCode pipeline — slice STL, inject pauses, post-process.

Public entry point
------------------
    from src.pipeline.gcode import run_gcode_pipeline
    result = run_gcode_pipeline(session)
"""

from .pipeline import run_gcode_pipeline  # noqa: F401

__all__ = ["run_gcode_pipeline"]
