"""generator.py — top-level SCAD generation step.

Reads session artifacts, runs the full pipeline, and writes
``enclosure.scad`` (and optionally ``enclosure.stl``) to the session folder.

Public entry point
------------------
    from src.pipeline.scad import run_scad_step
    scad_path = run_scad_step(session)        # always writes .scad
    scad_path = run_scad_step(session, compile_stl=True)  # also renders STL
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.catalog.loader import load_catalog
from src.pipeline.design.parsing import parse_design
from src.pipeline.design.height_field import sample_height_grid
from src.pipeline.placer.serialization import parse_placement
from src.pipeline.router.serialization import parse_routing
from src.session import Session

from .outline import tessellate_outline
from .layers import shell_body_lines
from .cutouts import build_cutouts
from .emit import generate_scad
from .compiler import compile_scad

log = logging.getLogger(__name__)


def run_scad_step(
    session: Session,
    compile_stl: bool = False,
) -> Path:
    """Generate ``enclosure.scad`` for the session.

    Parameters
    ----------
    session     : Session  The active session (must have placement + routing).
    compile_stl : bool     If True, also invoke OpenSCAD to render the STL.

    Returns the path to the written ``enclosure.scad``.

    Raises
    ------
    RuntimeError  If required upstream artifacts are missing.
    """

    # ── 1. Load artifacts ──────────────────────────────────────────
    placement_raw = session.read_artifact("placement.json")
    routing_raw   = session.read_artifact("routing.json")
    design_raw    = session.read_artifact("design.json")

    if placement_raw is None:
        raise RuntimeError("placement.json not found — run the placer step first.")
    if routing_raw is None:
        raise RuntimeError("routing.json not found — run the router step first.")
    if design_raw is None:
        raise RuntimeError("design.json not found — run the design step first.")

    placement = parse_placement(placement_raw)
    routing   = parse_routing(routing_raw)
    design    = parse_design(design_raw)  # noqa: F841 — kept for forward use
    catalog   = load_catalog()

    if not catalog.ok:
        for err in catalog.errors:
            log.warning("Catalog validation: %s", err)

    # Prefer placement's outline/enclosure (already includes z_top per vertex)
    outline   = placement.outline
    enclosure = placement.enclosure

    log.info(
        "SCAD step: %d components  %d nets  base_height=%.1f mm",
        len(placement.components), len(placement.nets), enclosure.height_mm,
    )

    # ── 2. Tessellate footprint polygon ───────────────────────────
    flat_pts = tessellate_outline(outline)
    log.info("Footprint: %d vertices", len(flat_pts))

    # ── 3. Compute shell body layers ──────────────────────────────
    body_lines = shell_body_lines(outline, enclosure, flat_pts)
    log.info("Shell body: %d SCAD lines", len(body_lines))

    # ── 4. Compute cutouts ────────────────────────────────────────
    cuts = build_cutouts(placement, routing, catalog, outline, enclosure)
    log.info("Cutouts: %d total", len(cuts))

    # ── 5. Compute metadata for header comment ────────────────────
    height_grid = sample_height_grid(outline, enclosure, resolution_mm=2.0)
    max_h = enclosure.height_mm
    for row in height_grid["grid"]:
        for h in row:
            if h is not None and h > max_h:
                max_h = h

    metadata = {
        "components":       len(placement.components),
        "traces":           len(routing.traces),
        "cutouts":          len(cuts),
        "base_height_mm":   enclosure.height_mm,
        "max_height_mm":    round(max_h, 1),
        "footprint_verts":  len(flat_pts),
    }

    # ── 6. Emit SCAD string ───────────────────────────────────────
    scad_str = generate_scad(
        body_lines, cuts,
        session_id=session.id,
        metadata=metadata,
        outline_pts=flat_pts,
    )

    # ── 7. Write to session folder ────────────────────────────────
    scad_path: Path = session.path / "enclosure.scad"
    scad_path.write_text(scad_str, encoding="utf-8")

    log.info(
        "Wrote %s (%.1f kB, %d lines)",
        scad_path.name,
        len(scad_str.encode()) / 1024,
        scad_str.count("\n"),
    )

    session.pipeline_state["scad"] = "done"
    session.save()

    # ── 8. Optional: compile to STL ───────────────────────────────
    if compile_stl:
        stl_path = session.path / "enclosure.stl"
        ok, msg, out = compile_scad(scad_path, stl_path)
        if ok:
            log.info("STL rendered: %s", stl_path.name)
            session.pipeline_state["stl"] = "done"
        else:
            log.error("STL render failed: %s", msg)
            session.pipeline_state["stl"] = "error"
        session.save()

    return scad_path
