"""Pipeline stages — design, placer, router, scad, gcode, firmware.

Each stage reads the previous stage's artifact from the session and
writes its own.  The stages in order:

  design         — LLM agent selects components, nets, outline, UI placements
  placer         — position all components inside the outline
  router         — route conductive traces between pads
  scad           — generate OpenSCAD enclosure model
  gcode          — slice STL, inject pauses, generate conductive-ink toolpaths
  firmware       — update ATmega328 sketch with routed pin assignments
"""

from __future__ import annotations

import sys
from pathlib import Path


def safe_path(p: Path | str) -> str:
    """Return a Windows 8.3 short path to avoid non-ASCII issues in C++ tools.

    GetShortPathNameW only works on paths that already exist.  For output
    files that don't exist yet, we convert the nearest existing ancestor
    and re-append the remaining components.
    """
    s = str(p)
    if sys.platform != "win32":
        return s
    try:
        import ctypes

        def _short(path_str: str) -> str | None:
            buf = ctypes.create_unicode_buffer(512)
            if ctypes.windll.kernel32.GetShortPathNameW(path_str, buf, 512):  # type: ignore[union-attr]
                return buf.value
            return None

        # Fast path: file/dir already exists
        result = _short(s)
        if result:
            return result

        # Walk up until we find an existing ancestor, then re-append tails
        path = Path(s)
        tail_parts: list[str] = []
        current = path
        while not current.exists():
            tail_parts.append(current.name)
            current = current.parent
            if current == current.parent:
                break  # reached root without finding existing path
        short_ancestor = _short(str(current))
        if short_ancestor and tail_parts:
            return str(Path(short_ancestor, *reversed(tail_parts)))
    except Exception:
        pass
    return s
