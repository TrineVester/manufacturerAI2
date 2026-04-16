"""
Arduino CLI wrapper — compiles .ino sketches and returns results.

Handles the quirks of arduino-cli's directory expectations:
the sketch must be in a folder with the same name as the .ino file.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.pipeline import safe_path as _safe_path

log = logging.getLogger(__name__)

# Board FQBN for ATmega328P (Arduino Uno compatible)
FQBN = "arduino:avr:uno"


@dataclass
class CompileResult:
    success: bool
    stdout: str
    stderr: str
    hex_path: Path | None = None
    elf_path: Path | None = None


def find_arduino_cli() -> str | None:
    """Return the path to arduino-cli, or None if not found."""
    return shutil.which("arduino-cli")


def compile_sketch(
    ino_content: str,
    output_dir: Path,
    *,
    sketch_name: str = "firmware",
    fqbn: str = FQBN,
    timeout_seconds: int = 120,
) -> CompileResult:
    """Compile an Arduino sketch and return the result.

    Creates a temporary sketch directory structure that arduino-cli expects:
      output_dir/sketch_name/sketch_name.ino

    On success, copies .hex and .elf to output_dir/firmware_build/.

    Parameters
    ----------
    ino_content : str
        The complete .ino file contents.
    output_dir : Path
        Session directory where artifacts are saved.
    sketch_name : str
        Name for the sketch folder and file.
    fqbn : str
        Fully Qualified Board Name.
    timeout_seconds : int
        Maximum compilation time.

    Returns
    -------
    CompileResult
        Success/failure, compiler output, and paths to built artifacts.
    """
    cli = find_arduino_cli()
    if cli is None:
        return CompileResult(
            success=False,
            stdout="",
            stderr="arduino-cli not found on PATH. Install it with: "
                   "https://arduino.github.io/arduino-cli/installation/",
        )

    # Create sketch directory structure
    sketch_dir = output_dir / sketch_name
    sketch_dir.mkdir(parents=True, exist_ok=True)
    ino_path = sketch_dir / f"{sketch_name}.ino"
    ino_path.write_text(ino_content, encoding="utf-8")

    # Build output directory
    build_dir = output_dir / "firmware_build"
    build_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        cli,
        "compile",
        "--fqbn", fqbn,
        "--output-dir", _safe_path(build_dir),
        _safe_path(sketch_dir),
    ]

    log.info("Compiling sketch: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=_safe_path(output_dir),
        )
    except subprocess.TimeoutExpired:
        return CompileResult(
            success=False,
            stdout="",
            stderr=f"Compilation timed out after {timeout_seconds} seconds.",
        )
    except FileNotFoundError:
        return CompileResult(
            success=False,
            stdout="",
            stderr="arduino-cli executable not found.",
        )

    hex_path = build_dir / f"{sketch_name}.ino.hex"
    elf_path = build_dir / f"{sketch_name}.ino.elf"

    if result.returncode == 0:
        log.info("Compilation successful")
        return CompileResult(
            success=True,
            stdout=result.stdout,
            stderr=result.stderr,
            hex_path=hex_path if hex_path.exists() else None,
            elf_path=elf_path if elf_path.exists() else None,
        )
    else:
        log.warning("Compilation failed:\n%s", result.stderr)
        return CompileResult(
            success=False,
            stdout=result.stdout,
            stderr=result.stderr,
        )
