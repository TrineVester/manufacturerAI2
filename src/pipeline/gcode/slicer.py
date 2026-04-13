"""PrusaSlicer CLI wrapper — slices STL to G-code.

Uses the same subprocess pattern as scad/compiler.py:
  - Popen with pipe capture
  - Cancellation via threading.Event
  - Full output logging
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .profiles import PrinterDef, FilamentDef

log = logging.getLogger(__name__)

# ── Profile directory (shipped alongside code) ────────────────────

PROFILE_DIR = Path(__file__).resolve().parent / "profiles_ini"


def _find_prusa_slicer() -> str | None:
    """Locate the PrusaSlicer console binary."""
    # Check PATH
    for name in ("prusa-slicer-console", "prusa-slicer"):
        path = shutil.which(name)
        if path:
            return path
    # Windows hard-coded locations
    if sys.platform == "win32":
        for candidate in [
            r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
            r"C:\Program Files (x86)\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
        ]:
            if Path(candidate).exists():
                return candidate
    return None


def _write_filament_ini(filament: FilamentDef, out_dir: Path) -> Path:
    """Write a temporary .ini file with filament overrides."""
    ini_path = out_dir / f"filament_{filament.id}.ini"
    lines = [f"# Auto-generated filament override: {filament.label}"]
    for key, val in filament.overrides.items():
        lines.append(f"{key} = {val}")
    ini_path.write_text("\n".join(lines), encoding="utf-8")
    return ini_path


def _parse_profile_directives(ini_path: Path) -> dict[str, str]:
    """Extract ``# @key: value`` directives from a profile .ini file.

    These custom markers control which PrusaSlicer CLI flags are used:
      # @printer-profile: Prusa i3 MK3S+
      # @print-profile: 0.20mm BALANCED
      # @material-profile: Prusament PLA
      # @thumbnails: 16x16/PNG,220x124/PNG
    """
    directives: dict[str, str] = {}
    if not ini_path.exists():
        return directives
    for line in ini_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("# @") and ":" in line:
            key, _, val = line[3:].partition(":")
            directives[key.strip()] = val.strip()
    return directives


def _kill_proc(pid: int) -> None:
    """Kill a process tree (Windows-safe)."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        import os
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def slice_stl(
    stl_path: Path,
    gcode_path: Path,
    printer: PrinterDef,
    filament: FilamentDef,
    center_x: float,
    center_y: float,
    cancel: threading.Event | None = None,
    timeout: float = 300,
) -> tuple[bool, str]:
    """Slice an STL file to G-code via PrusaSlicer CLI.

    Parameters
    ----------
    stl_path   : Path to the input STL.
    gcode_path : Path for the output G-code.
    printer    : PrinterDef with profile_filename.
    filament   : FilamentDef with overrides.
    center_x, center_y : Bed coordinates to center the model on.
    cancel     : Optional threading.Event  to abort.
    timeout    : Max seconds to wait.

    Returns (ok, message).
    """
    exe = _find_prusa_slicer()
    if not exe:
        return False, "PrusaSlicer not found. Install it or add to PATH."

    gcode_path.parent.mkdir(parents=True, exist_ok=True)

    # Write filament overrides to temp .ini
    filament_ini = _write_filament_ini(filament, gcode_path.parent)

    # Build command
    cmd = [
        exe,
        "--export-gcode",
        f"--center", f"{center_x:.1f},{center_y:.1f}",
    ]

    # Load printer profile if it exists
    profile_ini = PROFILE_DIR / printer.profile_filename
    directives: dict[str, str] = {}
    if profile_ini.exists():
        directives = _parse_profile_directives(profile_ini)
        cmd.extend(["--load", str(profile_ini)])

    # Apply directives as CLI flags
    for key in ("printer-profile", "print-profile", "material-profile", "thumbnails"):
        if key in directives:
            cmd.extend([f"--{key}", directives[key]])

    # Load filament overrides
    cmd.extend(["--load", str(filament_ini)])

    cmd.extend(["--output", str(gcode_path), str(stl_path)])

    log.info("PrusaSlicer: %s", " ".join(cmd))

    # ── Run subprocess ─────────────────────────────────────────
    log_path = gcode_path.with_suffix(".slicer.log")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        return False, f"Failed to start PrusaSlicer: {e}"

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _drain(pipe, chunks):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception:
            pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel and cancel.is_set():
                _kill_proc(proc.pid)
                proc.wait(timeout=5)
                return False, "Slicing cancelled."
            if time.monotonic() > deadline:
                _kill_proc(proc.pid)
                proc.wait(timeout=5)
                return False, f"PrusaSlicer timed out ({timeout:.0f}s)."
            time.sleep(0.25)
    except Exception as e:
        _kill_proc(proc.pid)
        return False, str(e)

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
    combined = "\n".join(filter(None, [stderr, stdout]))

    # Write log
    try:
        log_path.write_text(
            f"Command: {' '.join(cmd)}\nExit code: {proc.returncode}\n\n"
            f"--- stderr ---\n{stderr}\n\n--- stdout ---\n{stdout}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    if proc.returncode == 0 and gcode_path.exists():
        log.info("Sliced OK → %s (%.1f kB)", gcode_path.name,
                 gcode_path.stat().st_size / 1024)
        return True, combined or "OK"

    return False, combined or f"PrusaSlicer exited with code {proc.returncode}"
