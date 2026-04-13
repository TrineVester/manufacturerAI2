"""Bitmap rasterization — trace polygons → 1-bit bitmap at Xaar 128 nozzle pitch.

Public entry point
------------------
    from src.pipeline.bitmap import rasterize_traces
    result = rasterize_traces(session)
"""

from .rasterizer import rasterize_traces  # noqa: F401

__all__ = ["rasterize_traces"]
