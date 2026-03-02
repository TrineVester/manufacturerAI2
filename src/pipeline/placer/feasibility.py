"""
Lightweight placement feasibility check.

Runs a fast coarse-grid scan on each auto-placed component individually,
treating all UI-placed components as fixed obstacles.  Returns a text report
the design agent can act on *before* calling submit_design.

Differences from the real placer:
  - Grid step: 3 mm instead of 1 mm  (9x fewer cells → fast enough for a tool)
  - No inter-component overlap check  (each component scanned in isolation)
  - No pin-clearance check             (body + keepout envelope is enough)
  - No routing-channel reservation     (layout-phase detail irrelevant here)
  - Only outline-containment + UI-obstacle clearance are tested
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Polygon, box as shapely_box
from shapely.prepared import prep as shapely_prep

from src.catalog.models import CatalogResult, Component
from .geometry import footprint_envelope_halfdims, footprint_area
from .models import VALID_ROTATIONS, MIN_EDGE_CLEARANCE_MM


FAST_GRID_STEP = 3.0   # mm — coarse enough to be quick, fine enough to be reliable


# ── Internal data structures ───────────────────────────────────────


@dataclass
class _UIObstacle:
    instance_id: str
    x: float
    y: float
    ehw: float          # envelope half-width at rotation 0
    ehh: float          # envelope half-height at rotation 0
    keepout: float


@dataclass
class _RotResult:
    rotation: int
    valid_cells: int
    top_blockers: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class ComponentFeasibility:
    instance_id: str
    catalog_id: str
    body_w: float
    body_h: float
    keepout: float
    rotations: list[_RotResult]

    @property
    def feasible(self) -> bool:
        return any(r.valid_cells > 0 for r in self.rotations)

    @property
    def best_rotation(self) -> _RotResult | None:
        valid = [r for r in self.rotations if r.valid_cells > 0]
        return max(valid, key=lambda r: r.valid_cells) if valid else None


# ── Core scan ──────────────────────────────────────────────────────


def _scan_component(
    cat: Component,
    instance_id: str,
    prep_poly,
    poly_bounds: tuple[float, float, float, float],
    ui_obstacles: list[_UIObstacle],
    edge_clr: float,
) -> ComponentFeasibility:
    body_w = cat.body.width_mm or cat.body.diameter_mm or 1.0
    body_h = cat.body.length_mm or cat.body.diameter_mm or 1.0
    keepout = cat.mounting.keepout_margin_mm
    xmin, ymin, xmax, ymax = poly_bounds

    rot_results: list[_RotResult] = []

    for rot in VALID_ROTATIONS:
        ehw, ehh = footprint_envelope_halfdims(cat, rot)
        ihw = ehw + edge_clr
        ihh = ehh + edge_clr

        xs, xe = xmin + ihw, xmax - ihw
        ys, ye = ymin + ihh, ymax - ihh

        if xs > xe or ys > ye:
            rot_results.append(_RotResult(rot, 0, [("[scan_range_empty]", 1)]))
            continue

        valid = 0
        reasons: dict[str, int] = {}

        cx = xs
        while cx <= xe + 1e-6:
            cy = ys
            while cy <= ye + 1e-6:
                # Hard constraint 1: inflated footprint inside outline
                if not prep_poly.contains(
                    shapely_box(cx - ihw, cy - ihh, cx + ihw, cy + ihh)
                ):
                    reasons["[outline]"] = reasons.get("[outline]", 0) + 1
                    cy += FAST_GRID_STEP
                    continue

                # Hard constraint 2: clearance from UI obstacles
                blocked_by: str | None = None
                for obs in ui_obstacles:
                    gx = abs(cx - obs.x) - ehw - obs.ehw
                    gy = abs(cy - obs.y) - ehh - obs.ehh
                    actual_gap = max(gx, gy)
                    required = max(keepout, obs.keepout, 1.0)
                    if actual_gap < required:
                        blocked_by = obs.instance_id
                        break

                if blocked_by:
                    reasons[blocked_by] = reasons.get(blocked_by, 0) + 1
                    cy += FAST_GRID_STEP
                    continue

                valid += 1
                cy += FAST_GRID_STEP
            cx += FAST_GRID_STEP

        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:4]
        rot_results.append(_RotResult(rot, valid, top))

    return ComponentFeasibility(
        instance_id=instance_id,
        catalog_id=cat.id,
        body_w=body_w,
        body_h=body_h,
        keepout=keepout,
        rotations=rot_results,
    )


# ── Public entry point ─────────────────────────────────────────────


def run_feasibility_check(
    catalog: CatalogResult,
    components: list[dict],        # [{catalog_id, instance_id, ...}]
    outline_raw: list[dict],       # [{x, y, ...}]
    ui_placements_raw: list[dict], # [{instance_id, x_mm, y_mm}]
) -> str:
    """Return a plain-text feasibility report suitable for feeding back to the LLM."""

    cat_map: dict[str, Component] = {c.id: c for c in catalog.components}

    # Parse outline vertices
    try:
        verts: list[tuple[float, float]] = [
            (float(v["x"]), float(v["y"])) for v in outline_raw
        ]
    except (KeyError, TypeError, ValueError) as exc:
        return f"ERROR: could not parse outline — {exc}"

    if len(verts) < 3:
        return "ERROR: outline must have at least 3 vertices."

    poly = Polygon(verts)
    if not poly.is_valid:
        try:
            from shapely import make_valid
            poly = make_valid(poly)
        except Exception:
            pass
    if poly.area < 1.0:
        return "ERROR: outline polygon has near-zero area."

    prep_poly = shapely_prep(poly)
    xmin, ymin, xmax, ymax = poly.bounds

    # UI placement position lookup
    ui_pos: dict[str, tuple[float, float]] = {
        p["instance_id"]: (float(p["x_mm"]), float(p["y_mm"]))
        for p in ui_placements_raw
    }

    # Build UI obstacle list
    ui_obstacles: list[_UIObstacle] = []
    for comp_def in components:
        inst_id = comp_def.get("instance_id", "")
        cat_id = comp_def.get("catalog_id", "")
        cat_comp = cat_map.get(cat_id)
        if cat_comp is None or not cat_comp.ui_placement:
            continue
        if inst_id not in ui_pos:
            continue
        x, y = ui_pos[inst_id]
        ehw, ehh = footprint_envelope_halfdims(cat_comp, 0)
        ui_obstacles.append(_UIObstacle(
            instance_id=inst_id,
            x=x, y=y,
            ehw=ehw, ehh=ehh,
            keepout=cat_comp.mounting.keepout_margin_mm,
        ))

    # Collect auto-placed components, sorted largest-first (matches real placer order)
    auto_comps: list[tuple[Component, str]] = []
    for comp_def in components:
        inst_id = comp_def.get("instance_id", "")
        cat_id = comp_def.get("catalog_id", "")
        cat_comp = cat_map.get(cat_id)
        if cat_comp is None:
            continue
        if cat_comp.ui_placement:
            continue
        if cat_comp.mounting.style == "side":
            continue   # side-mount components don't go through area scan
        auto_comps.append((cat_comp, inst_id))

    auto_comps.sort(key=lambda t: footprint_area(t[0]), reverse=True)

    if not auto_comps:
        return (
            "No auto-placed components found in this design "
            "(all components are UI-placed or side-mounted). "
            "Nothing to check — proceed to submit_design."
        )

    # Run per-component scans
    feasibility: list[ComponentFeasibility] = []
    for cat_comp, inst_id in auto_comps:
        feasibility.append(
            _scan_component(
                cat_comp, inst_id,
                prep_poly, (xmin, ymin, xmax, ymax),
                ui_obstacles,
                edge_clr=MIN_EDGE_CLEARANCE_MM,
            )
        )

    # ── Format report ──────────────────────────────────────────────
    lines: list[str] = [
        f"=== Placement Feasibility Report ===",
        f"Outline: {xmax-xmin:.0f}x{ymax-ymin:.0f}mm bounding box, "
        f"{poly.area:.0f}mm2 area",
        f"UI obstacles: {len(ui_obstacles)}  |  Auto-placed: {len(auto_comps)}",
        f"(Grid step {FAST_GRID_STEP:.0f}mm — this is a coarse check, "
        f"not a guarantee)",
        "",
    ]

    for r in feasibility:
        if r.feasible:
            best = r.best_rotation
            lines.append(
                f"[OK]   {r.instance_id} ({r.catalog_id}, "
                f"{r.body_w:.0f}x{r.body_h:.0f}mm, {r.keepout:.0f}mm keepout) "
                f"-> best rotation {best.rotation}deg with {best.valid_cells} candidate cells"
            )
        else:
            lines.append(
                f"[FAIL] {r.instance_id} ({r.catalog_id}, "
                f"{r.body_w:.0f}x{r.body_h:.0f}mm, {r.keepout:.0f}mm keepout)"
            )
            for rr in r.rotations:
                blocker_str = ", ".join(f"{k}({v})" for k, v in rr.top_blockers)
                lines.append(f"       rot={rr.rotation}deg: {rr.valid_cells} cells  blocked by: {blocker_str}")

            # Identify the most-blamed UI components across all rotations
            ui_blame: dict[str, int] = {}
            for rr in r.rotations:
                for k, v in rr.top_blockers:
                    if not k.startswith("["):
                        ui_blame[k] = ui_blame.get(k, 0) + v

            if ui_blame:
                culprits = [k for k, _ in sorted(ui_blame.items(), key=lambda kv: -kv[1])[:3]]
                lines.append(
                    f"       -> Fix: move UI component(s) {', '.join(culprits)} "
                    f"to free up a {r.body_w:.0f}x{r.body_h:.0f}mm clear zone "
                    f"(+{r.keepout:.0f}mm keepout on all sides)."
                )
            else:
                lines.append(
                    f"       -> Fix: the outline is too small or narrow for this "
                    f"component at any rotation. Widen the outline or use a smaller component."
                )

    lines.append("")
    fails = [r for r in feasibility if not r.feasible]
    if not fails:
        lines.append(
            "All auto-placed components have candidate positions. "
            "The full placer may still reject due to inter-component overlap, "
            "but the layout looks viable. You may call submit_design."
        )
    else:
        fail_ids = ", ".join(r.instance_id for r in fails)
        lines.append(
            f"CANNOT SUBMIT: {len(fails)} component(s) have no valid position — "
            f"{fail_ids}. Adjust the ui_placements or outline before retrying."
        )

    return "\n".join(lines)
