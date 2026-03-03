"""
OpenSCAD compiler wrapper  runs openscad CLI for syntax checking and STL rendering.
"""

from __future__ import annotations
import logging
import os
import signal
import subprocess
import shutil
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _find_openscad() -> str | None:
    """Locate the openscad binary."""
    path = shutil.which("openscad")
    if path:
        return path
    for candidate in [
        r"C:\Program Files\OpenSCAD\openscad.exe",
        r"C:\Program Files (x86)\OpenSCAD\openscad.exe",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def _is_windows() -> bool:
    return sys.platform == "win32"


def check_scad(scad_path: Path) -> tuple[bool, str]:
    """Syntax-check an OpenSCAD file without rendering. Returns (ok, message)."""
    exe = _find_openscad()
    if not exe:
        return False, "OpenSCAD not found on PATH."

    try:
        result = subprocess.run(
            [exe, "-o", "NUL" if _is_windows() else "/dev/null", str(scad_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stderr = result.stderr.strip()
        if result.returncode == 0:
            return True, stderr or "OK"
        return False, stderr or f"OpenSCAD exited with code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "OpenSCAD timed out (30s)."
    except Exception as e:
        return False, str(e)


def _kill_proc_tree(pid: int) -> None:
    """Kill a process and all its children (Windows-safe)."""
    if _is_windows():
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def compile_scad(
    scad_path: Path,
    stl_path: Path | None = None,
    cancel: threading.Event | None = None,
    timeout: float = 600,
) -> tuple[bool, str, Path | None]:
    """Compile an OpenSCAD file to STL. Returns (ok, message, stl_path_or_none)."""
    exe = _find_openscad()
    if not exe:
        return False, "OpenSCAD not found on PATH.", None

    if stl_path is None:
        stl_path = scad_path.with_suffix(".stl")

    log_path = stl_path.with_suffix(".openscad.log")
    cmd = [exe, "-o", str(stl_path), str(scad_path)]
    log.info("Running: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        return False, str(e), None

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _drain_pipe(pipe, chunks):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception:
            pass

    t_out = threading.Thread(target=_drain_pipe, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_drain_pipe, args=(proc.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel and cancel.is_set():
                _kill_proc_tree(proc.pid)
                proc.wait(timeout=5)
                return False, "Cancelled.", None
            if time.monotonic() > deadline:
                _kill_proc_tree(proc.pid)
                proc.wait(timeout=5)
                return False, f"OpenSCAD timed out ({timeout:.0f}s).", None
            time.sleep(0.25)
    except Exception as e:
        _kill_proc_tree(proc.pid)
        return False, str(e), None

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
    # OpenSCAD writes most output to stderr; stdout is usually empty but capture it too
    combined = "\n".join(filter(None, [stderr, stdout]))

    # Always write a log file so the full output is inspectable
    try:
        log_path.write_text(
            f"Command: {' '.join(cmd)}\nExit code: {proc.returncode}\n\n"
            f"--- stderr ---\n{stderr}\n\n--- stdout ---\n{stdout}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    if proc.returncode == 0 and stl_path.exists():
        log.info("Compiled OK → %s (%.1f kB)", stl_path.name, stl_path.stat().st_size / 1024)
        return True, combined or "OK", stl_path

    # Build a diagnostic message when OpenSCAD gives us nothing
    if not combined:
        diag_parts = [f"OpenSCAD exited with code {proc.returncode} (no output)."]
        if not scad_path.exists():
            diag_parts.append(f"SCAD file not found: {scad_path}")
        try:
            stl_path.parent.mkdir(parents=True, exist_ok=True)
            test = stl_path.parent / ".write_test"
            test.touch(); test.unlink()
        except Exception as we:
            diag_parts.append(f"Output directory not writable: {we}")
        diag_parts.append(f"Log: {log_path}")
        combined = " ".join(diag_parts)

    return False, combined, None
