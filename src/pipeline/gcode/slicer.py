"""
PrusaSlicer CLI bridge — slices STL models into G-code.

Finds the ``prusa-slicer-console`` executable automatically and invokes
it with a printer profile.  Supports multiple printers (MK3S, MK3S+, CORE One).
Profile ``.ini`` files live in the ``profiles/`` directory next to this module.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from src.pipeline import safe_path as _safe_path
from src.pipeline.config import get_printer

log = logging.getLogger(__name__)

# ── Locate PrusaSlicer ─────────────────────────────────────────────

_CANDIDATES = [
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
    r"C:\Program Files (x86)\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
]


def find_prusaslicer() -> str | None:
    """Return the path to ``prusa-slicer-console``, or *None*."""
    path = shutil.which("prusa-slicer-console")
    if path:
        return path
    for c in _CANDIDATES:
        if Path(c).exists():
            return c
    return None


_GUI_CANDIDATES = [
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer.exe",
    r"C:\Program Files (x86)\Prusa3D\PrusaSlicer\prusa-slicer.exe",
]


def find_prusaslicer_gui() -> str | None:
    """Return the path to the *GUI* ``prusa-slicer`` executable.

    The ``--gcodeviewer`` flag requires the GUI binary, not the
    headless ``prusa-slicer-console``.
    """
    path = shutil.which("prusa-slicer")
    if path:
        return path
    for c in _GUI_CANDIDATES:
        if Path(c).exists():
            return c
    return None


# ── Profile resolution ─────────────────────────────────────────────

_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

# Recognised directives in profile .ini files (# @key: value).
# Maps directive name → PrusaSlicer CLI flag.
_DIRECTIVE_FLAGS: dict[str, str] = {
    "printer-profile":  "--printer-profile",
    "print-profile":    "--print-profile",
    "material-profile": "--material-profile",
    "thumbnails":       "--thumbnails",
}


def _parse_profile(profile_path: Path) -> tuple[Path, list[str]]:
    """Read a profile ``.ini`` and extract ``# @directive`` CLI flags.

    Returns ``(profile_path, extra_cli_args)`` where *extra_cli_args*
    are the CLI flags derived from directives found in the file.
    """
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Slicer profile not found: {profile_path}\n"
            f"Expected profiles live in {_PROFILES_DIR}"
        )
    extra: list[str] = []
    for line in profile_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("# @"):
            continue
        # Format: # @key: value
        rest = line[3:]  # strip "# @"
        if ":" not in rest:
            continue
        key, _, value = rest.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in _DIRECTIVE_FLAGS and value:
            extra += [_DIRECTIVE_FLAGS[key], value]
    return profile_path, extra


# ── Slice ──────────────────────────────────────────────────────────

def slice_stl(
    stl_path: Path,
    output_gcode: Path | None = None,
    profile_path: Path | None = None,
    *,
    printer: str | None = None,
    filament: str | None = None,
    filament_override_path: Path | None = None,
    center: tuple[float, float] | None = None,
    extra_overrides: list[Path] | None = None,
    timeout_s: int = 300,
) -> tuple[bool, str, Path | None]:
    """Slice *stl_path* and write G-code.

    Parameters
    ----------
    stl_path : Path
        Input STL file.
    output_gcode : Path, optional
        Where to write the ``.gcode`` file.  Defaults to
        ``stl_path.with_suffix('.gcode')``.
    profile_path : Path, optional
        ``--load`` ini file.  When *None* the printer-specific
        profile from ``profiles/`` is used.
    printer : str, optional
        Printer id (``"mk3s"``, ``"mk3s_plus"``, or ``"coreone"``).
        Determines which default profile to use when *profile_path* is None.
    filament : str, optional
        Filament id (e.g. ``"prusament_pla"``, ``"overture_rockpla"``).
        Used only if *filament_override_path* is not provided.
    filament_override_path : Path, optional
        Pre-written filament override ``.ini``.
    center : tuple[float, float], optional
        ``(X, Y)`` bed coordinate for the model centre.  Passed to
        PrusaSlicer as ``--center X,Y``.  When *None*, PrusaSlicer
        auto-centres on the build plate.
    extra_overrides : list[Path], optional
        Additional ``--load`` ini files applied after the main profile
        and filament overrides.
    timeout_s : int
        CLI timeout in seconds.

    Returns
    -------
    (ok, message, gcode_path_or_none)
    """
    exe = find_prusaslicer()
    if not exe:
        return False, "PrusaSlicer not found on this system.", None

    pdef = get_printer(printer)

    if profile_path is None:
        profile_path = _PROFILES_DIR / pdef.profile_filename

    profile_path, profile_cli_args = _parse_profile(profile_path)

    if output_gcode is None:
        output_gcode = stl_path.with_suffix(".gcode")

    cmd = [exe, "--export-gcode"]

    if center is not None:
        cmd += ["--center", f"{center[0]:.3f},{center[1]:.3f}"]

    # Directives from the profile (e.g. --printer-profile, --thumbnails)
    # are applied first so the profile's --load overrides sit on top.
    cmd += profile_cli_args
    cmd += ["--load", _safe_path(profile_path)]

    # Filament overrides (temperature, bed, cooling) are loaded *after*
    # the printer profile so they take final precedence.
    if filament_override_path is None and filament:
        from src.pipeline.gcode.filaments import write_filament_overrides
        filament_override_path = write_filament_overrides(
            filament, stl_path.parent,
        )
    if filament_override_path and filament_override_path.exists():
        cmd += ["--load", _safe_path(filament_override_path)]
        log.info("Filament overrides loaded: %s", filament_override_path)

    for p in (extra_overrides or []):
        if p.exists():
            cmd += ["--load", _safe_path(p)]

    cmd += ["--output", _safe_path(output_gcode), _safe_path(stl_path)]

    log.info("Slicing: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0 and output_gcode.exists():
            log.info("Slicing succeeded: %s", output_gcode)
            return True, stderr or "OK", output_gcode
        return False, stderr or f"PrusaSlicer exited with code {result.returncode}", None
    except subprocess.TimeoutExpired:
        return False, f"PrusaSlicer timed out ({timeout_s}s).", None
    except Exception as e:
        return False, str(e), None
