"""Custom G-code pipeline for multi-stage 3D printing.

Slices STL via PrusaSlicer CLI, then post-processes the G-code to insert
pause points, ironing passes, and conductive-ink toolpaths so that
electronic components can be placed mid-print.
"""

from .pipeline import run_gcode_pipeline, GcodePipelineResult

__all__ = [
    "run_gcode_pipeline",
    "GcodePipelineResult",
]
