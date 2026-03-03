"""outline.py — tessellate the design outline into a flat 2-D polygon.

Reuses the Bézier-corner expansion already implemented in height_field so
the SCAD footprint exactly matches the 3-D viewport and height-field
sampling polygon.
"""

from __future__ import annotations

from src.pipeline.design.models import Outline
from src.pipeline.design.height_field import _bezier_expand_outline

# Number of Bézier sub-samples per eased corner.
# 2 gives 84 vertices for a 28-point outline — smooth enough for 3D printing
# and 3× fewer CSG vertices than the previous value of 8 (252 vertices).
DEFAULT_SEGMENTS = 2


def tessellate_outline(
    outline: Outline,
    segments: int = DEFAULT_SEGMENTS,
) -> list[list[float]]:
    """Return a flat 2-D polygon [[x, y], ...] from the design outline.

    Corner easing is expanded via Bézier curves using the same math as the
    JS 3-D viewport.  The result is suitable for use as an OpenSCAD
    ``polygon()`` call.
    """
    pts = _bezier_expand_outline(outline, segments=segments)
    return [[x, y] for x, y in pts]
