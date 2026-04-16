"""Design spec validation — check a DesignSpec against the catalog."""

from __future__ import annotations

from src.catalog import CatalogResult
from src.pipeline.config import PrinterDef
from .models import DesignSpec, PhysicalDesign


def _fmt_indices(idxs: list[int]) -> str:
    if len(idxs) == 1:
        return f"Vertex {idxs[0]}"
    # Compress into contiguous ranges
    ranges: list[str] = []
    start = idxs[0]
    prev = start
    for v in idxs[1:]:
        if v == prev + 1:
            prev = v
        else:
            ranges.append(f"{start}–{prev}" if prev != start else str(start))
            start = prev = v
    ranges.append(f"{start}–{prev}" if prev != start else str(start))
    return f"Vertices {', '.join(ranges)} ({len(idxs)} vertices)"


def validate_physical_design(
    physical: PhysicalDesign,
    catalog: CatalogResult,
    printer: PrinterDef | None = None,
) -> list[str]:
    """Validate a PhysicalDesign (outline, enclosure, ui_placements) without components/nets.

    This is what the design agent calls — it can validate the physical shape
    before any circuit data exists. Height checks use only the UI-placed
    components (the only ones known at design time).
    """
    from src.pipeline.config import get_printer, FLOOR_MM, CEILING_MM
    pdef = printer or get_printer()
    errors: list[str] = []
    catalog_map = {c.id: c for c in catalog.components}

    # ── Outline validation ──
    if len(physical.outline.points) < 3:
        errors.append("Outline must have at least 3 vertices")

    for i, pt in enumerate(physical.outline.points):
        if pt.ease_in < 0:
            errors.append(f"Vertex {i}: ease_in must be >= 0")
        if pt.ease_out < 0:
            errors.append(f"Vertex {i}: ease_out must be >= 0")

    # ── Outline must fit within the printer bed ──
    if len(physical.outline.points) >= 3:
        xs = [pt.x for pt in physical.outline.points]
        ys = [pt.y for pt in physical.outline.points]
        outline_w = max(xs) - min(xs)
        outline_h = max(ys) - min(ys)
        if outline_w > pdef.bed_width or outline_h > pdef.bed_depth:
            errors.append(
                f"Outline bounding box ({outline_w:.1f}×{outline_h:.1f} mm) "
                f"exceeds printer bed ({pdef.bed_width:.0f}×{pdef.bed_depth:.0f} mm)"
            )

    # ── Enclosure height validation (using UI-placed components) ──
    MIN_CAVITY_MM = 4.0
    tallest_mm = MIN_CAVITY_MM
    for up in physical.ui_placements:
        if up.catalog_id and up.catalog_id in catalog_map:
            cat = catalog_map[up.catalog_id]
            comp_height = cat.body.height_mm if cat.body.height_mm else 0.0
            if comp_height > tallest_mm:
                tallest_mm = comp_height

    min_required_z = FLOOR_MM + tallest_mm + CEILING_MM

    if physical.enclosure.height_mm < min_required_z:
        errors.append(
            f"Enclosure height_mm ({physical.enclosure.height_mm:.1f}mm) is too short — "
            f"needs at least {min_required_z:.1f}mm "
            f"(floor {FLOOR_MM}mm + tallest component {tallest_mm:.1f}mm + ceiling {CEILING_MM}mm)"
        )

    too_short: dict[float, list[int]] = {}
    too_tall: dict[float, list[int]] = {}
    for i, pt in enumerate(physical.outline.points):
        eff_z = pt.z_top if pt.z_top is not None else physical.enclosure.height_mm
        if eff_z < min_required_z:
            too_short.setdefault(eff_z, []).append(i)
        if eff_z > pdef.max_z_mm:
            too_tall.setdefault(eff_z, []).append(i)
    for eff_z, idxs in too_short.items():
        verts = _fmt_indices(idxs)
        errors.append(
            f"{verts} z_top ({eff_z:.1f}mm) is too short — "
            f"needs at least {min_required_z:.1f}mm"
        )
    for eff_z, idxs in too_tall.items():
        verts = _fmt_indices(idxs)
        errors.append(
            f"{verts} z_top ({eff_z:.1f}mm) exceeds printer max Z "
            f"({pdef.max_z_mm:.0f}mm)"
        )

    if physical.enclosure.height_mm > pdef.max_z_mm:
        errors.append(
            f"Enclosure height_mm ({physical.enclosure.height_mm:.1f}mm) exceeds "
            f"printer max Z ({pdef.max_z_mm:.0f}mm)"
        )

    # ── top_surface validation ──
    ts = physical.enclosure.top_surface
    if ts is not None and ts.type != "flat":
        if ts.type == "dome":
            missing = [f for f in ("peak_x_mm", "peak_y_mm", "peak_height_mm", "base_height_mm")
                       if getattr(ts, f) is None]
            if missing:
                errors.append(f"top_surface dome is missing required fields: {', '.join(missing)}")
            else:
                if ts.peak_height_mm < ts.base_height_mm:
                    errors.append(
                        f"top_surface dome peak_height_mm ({ts.peak_height_mm}) must be >= "
                        f"base_height_mm ({ts.base_height_mm})"
                    )
                if ts.peak_height_mm > pdef.max_z_mm:
                    errors.append(
                        f"top_surface dome peak_height_mm ({ts.peak_height_mm}mm) exceeds "
                        f"printer max Z ({pdef.max_z_mm:.0f}mm)"
                    )
        elif ts.type == "ridge":
            missing = [f for f in ("x1", "y1", "x2", "y2", "crest_height_mm", "base_height_mm", "falloff_mm")
                       if getattr(ts, f) is None]
            if missing:
                errors.append(f"top_surface ridge is missing required fields: {', '.join(missing)}")
            else:
                if ts.crest_height_mm < ts.base_height_mm:
                    errors.append(
                        f"top_surface ridge crest_height_mm ({ts.crest_height_mm}) must be >= "
                        f"base_height_mm ({ts.base_height_mm})"
                    )
                if ts.crest_height_mm > pdef.max_z_mm:
                    errors.append(
                        f"top_surface ridge crest_height_mm ({ts.crest_height_mm}mm) exceeds "
                        f"printer max Z ({pdef.max_z_mm:.0f}mm)"
                    )
        else:
            errors.append(f"top_surface type '{ts.type}' is unknown (expected: flat, dome, ridge)")

    # ── Outline polygon validity & UI placement checks ──
    if len(physical.outline.vertices) >= 3:
        try:
            from shapely.geometry import Polygon, Point
            poly = Polygon(
                physical.outline.vertices,
                physical.outline.hole_vertices or None,
            )
            if not poly.is_valid:
                errors.append("Outline polygon is self-intersecting or invalid")
            elif poly.area <= 0:
                errors.append("Outline polygon has zero or negative area")
            else:
                for up in physical.ui_placements:
                    if up.edge_index is not None:
                        continue
                    pt = Point(up.x_mm, up.y_mm)
                    if not poly.contains(pt):
                        errors.append(
                            f"UI placement '{up.instance_id}' at "
                            f"({up.x_mm}, {up.y_mm}) is outside the outline"
                        )
        except Exception:
            pass

    # ── UI placement validation ──
    for up in physical.ui_placements:
        cat = catalog_map.get(up.catalog_id) if up.catalog_id else None

        if cat:
            if not cat.ui_placement:
                errors.append(
                    f"UI placement: '{up.instance_id}' ({cat.id}) has ui_placement=false"
                )
            if up.mounting_style and up.mounting_style not in cat.mounting.allowed_styles:
                errors.append(
                    f"UI placement '{up.instance_id}': mounting_style '{up.mounting_style}' "
                    f"not in allowed_styles {cat.mounting.allowed_styles}"
                )

            eff_style = up.mounting_style or cat.mounting.style
            if eff_style == "side":
                if up.edge_index is None:
                    errors.append(
                        f"UI placement '{up.instance_id}': side-mount components "
                        f"require edge_index (which outline edge to mount on)"
                    )
            elif up.edge_index is not None:
                errors.append(
                    f"UI placement '{up.instance_id}': edge_index is only for "
                    f"side-mount components (mounting style is '{eff_style}')"
                )

        if up.edge_index is not None:
            if up.edge_index < 0 or up.edge_index >= len(physical.outline.points):
                errors.append(
                    f"UI placement '{up.instance_id}': edge_index {up.edge_index} "
                    f"out of range (0–{len(physical.outline.points) - 1})"
                )

    return errors


def _check_board_capacity(
    poly,  # Shapely Polygon
    spec: DesignSpec,
    catalog_map: dict,
    instance_to_catalog: dict,
    errors: list[str],
) -> None:
    """Check whether the outline is large enough for all components.

    Adds errors if the board area is too small or the narrowest
    dimension can't fit the largest component.
    """
    import math
    from src.pipeline.config import TRACE_RULES
    from src.pipeline.placer.geometry import footprint_envelope_halfdims

    edge_clr = TRACE_RULES.min_edge_clearance_mm

    # Erode outline by edge clearance to get usable placement area
    eroded = poly.buffer(-edge_clr)
    if eroded.is_empty or eroded.area <= 0:
        errors.append(
            f"Outline is too narrow — after {edge_clr:.1f}mm edge clearance "
            f"there is no usable area for components"
        )
        return

    usable_area = eroded.area

    # Compute the narrowest usable dimension via minimum rotated rectangle
    min_dim = 0.0
    try:
        min_rect = eroded.minimum_rotated_rectangle
        if min_rect and not min_rect.is_empty:
            coords = list(min_rect.exterior.coords)
            side1 = math.hypot(
                coords[1][0] - coords[0][0], coords[1][1] - coords[0][1])
            side2 = math.hypot(
                coords[2][0] - coords[1][0], coords[2][1] - coords[1][1])
            min_dim = min(side1, side2)
    except Exception:
        pass

    # Resolve effective mounting style for each component
    up_style_map = {
        up.instance_id: up.mounting_style
        for up in spec.ui_placements if up.mounting_style
    }

    total_footprint_area = 0.0
    largest_min_span = 0.0
    largest_min_span_id = ""

    for ci in spec.components:
        if ci.catalog_id not in catalog_map:
            continue
        cat = catalog_map[ci.catalog_id]

        # Resolve effective mounting style
        eff_style = (
            up_style_map.get(ci.instance_id)
            or ci.mounting_style
            or cat.mounting.style
        )
        if eff_style == "side":
            continue  # side-mount components live on the edge

        keepout = cat.mounting.keepout_margin_mm

        # Compute footprint area and minimum axis span across all rotations
        best_min_span = float("inf")
        best_area = float("inf")
        for rot in (0, 90, 180, 270):
            ehw, ehh = footprint_envelope_halfdims(cat, rot)
            span_x = 2 * ehw + 2 * keepout
            span_y = 2 * ehh + 2 * keepout
            this_min = min(span_x, span_y)
            this_area = span_x * span_y
            if this_min < best_min_span:
                best_min_span = this_min
            if this_area < best_area:
                best_area = this_area

        total_footprint_area += best_area

        if best_min_span > largest_min_span:
            largest_min_span = best_min_span
            largest_min_span_id = ci.instance_id

    # ── Minimum dimension check ──
    if min_dim > 0 and largest_min_span > 0 and largest_min_span > min_dim:
        errors.append(
            f"Outline is too narrow for component '{largest_min_span_id}': "
            f"narrowest usable width is {min_dim:.1f}mm but the component "
            f"needs at least {largest_min_span:.1f}mm (envelope + keepout). "
            f"Widen the outline."
        )

    # ── Total area check (packing factor accounts for routing channels) ──
    PACKING_FACTOR = 2.0
    required_area = total_footprint_area * PACKING_FACTOR
    if required_area > usable_area:
        errors.append(
            f"Outline is likely too small for all {len(spec.components)} "
            f"components: footprint area is {total_footprint_area:.0f}mm² "
            f"and with routing space needs ~{required_area:.0f}mm², "
            f"but usable outline area is only {usable_area:.0f}mm². "
            f"Enlarge the outline or simplify the circuit."
        )


def validate_design(
    spec: DesignSpec,
    catalog: CatalogResult,
    printer: PrinterDef | None = None,
) -> list[str]:
    """Validate a DesignSpec against the catalog. Returns error messages (empty = valid)."""
    from src.pipeline.config import get_printer
    pdef = printer or get_printer()
    errors: list[str] = []
    catalog_map = {c.id: c for c in catalog.components}

    # ── All catalog_ids must exist ──
    for ci in spec.components:
        if ci.catalog_id not in catalog_map:
            errors.append(f"Component '{ci.instance_id}': unknown catalog_id '{ci.catalog_id}'")

    # ── Instance IDs must be unique ──
    seen_ids: set[str] = set()
    for ci in spec.components:
        if ci.instance_id in seen_ids:
            errors.append(f"Duplicate instance_id '{ci.instance_id}'")
        seen_ids.add(ci.instance_id)

    # Build lookup: instance_id -> catalog Component (only for known catalog_ids)
    instance_to_catalog = {}
    for ci in spec.components:
        if ci.catalog_id in catalog_map:
            instance_to_catalog[ci.instance_id] = catalog_map[ci.catalog_id]

    # ── Mounting style overrides ──
    for ci in spec.components:
        if ci.mounting_style and ci.catalog_id in catalog_map:
            cat = catalog_map[ci.catalog_id]
            if ci.mounting_style not in cat.mounting.allowed_styles:
                errors.append(
                    f"Component '{ci.instance_id}': mounting_style '{ci.mounting_style}' "
                    f"not in allowed_styles {cat.mounting.allowed_styles}"
                )

    # ── Configurable fields ──
    for ci in spec.components:
        if ci.config and ci.catalog_id in catalog_map:
            cat = catalog_map[ci.catalog_id]
            if not cat.configurable:
                errors.append(
                    f"Component '{ci.instance_id}': has config but "
                    f"'{ci.catalog_id}' has no configurable fields"
                )
            else:
                for key, value in ci.config.items():
                    if key not in cat.configurable:
                        errors.append(
                            f"Component '{ci.instance_id}': unknown config key '{key}'"
                        )
                        continue
                    field_def = cat.configurable[key]
                    if isinstance(field_def, dict) and field_def.get("type") == "enum":
                        options = field_def.get("options", {})
                        if value not in options:
                            errors.append(
                                f"Component '{ci.instance_id}': config '{key}' "
                                f"value '{value}' not in options: {list(options.keys())}"
                            )

    # ── Net pin references ──
    for net in spec.nets:
        if len(net.pins) < 2:
            errors.append(f"Net '{net.id}': must have at least 2 pins")
        for pin_ref in net.pins:
            if ":" not in pin_ref:
                errors.append(
                    f"Net '{net.id}': invalid pin reference '{pin_ref}' "
                    f"(expected 'instance_id:pin_id')"
                )
                continue
            iid, pid = pin_ref.split(":", 1)
            if iid not in seen_ids:
                errors.append(f"Net '{net.id}': unknown instance '{iid}' in '{pin_ref}'")
                continue
            if iid not in instance_to_catalog:
                continue  # catalog_id was unknown, already reported
            cat = instance_to_catalog[iid]
            pin_ids = {p.id for p in cat.pins}
            group_ids = {g.id for g in cat.pin_groups} if cat.pin_groups else set()
            if pid not in pin_ids and pid not in group_ids:
                errors.append(
                    f"Net '{net.id}': unknown pin/group '{pid}' on "
                    f"'{iid}' (catalog: {cat.id})"
                )

    # ── Each pin in at most one net (group refs are dynamic allocations) ──
    allocatable_groups: dict[tuple[str, str], list[str]] = {}
    for ci in spec.components:
        if ci.instance_id not in instance_to_catalog:
            continue
        cat = instance_to_catalog[ci.instance_id]
        if cat.pin_groups:
            for g in cat.pin_groups:
                if g.allocatable:
                    allocatable_groups[(ci.instance_id, g.id)] = g.pin_ids

    pin_to_net: dict[str, str] = {}
    group_alloc_count: dict[tuple[str, str], list[str]] = {}
    for net in spec.nets:
        for pin_ref in net.pins:
            if ":" not in pin_ref:
                continue
            iid, pid = pin_ref.split(":", 1)
            key = (iid, pid)
            if key in allocatable_groups:
                group_alloc_count.setdefault(key, []).append(net.id)
            else:
                if pin_ref in pin_to_net:
                    errors.append(
                        f"Pin '{pin_ref}' in both net '{pin_to_net[pin_ref]}' "
                        f"and net '{net.id}'"
                    )
                else:
                    pin_to_net[pin_ref] = net.id

    # ── Validate group allocation counts don't exceed pool size ──
    for (iid, gid), net_ids in group_alloc_count.items():
        pool = allocatable_groups[(iid, gid)]
        if len(net_ids) > len(pool):
            errors.append(
                f"Group '{iid}:{gid}' used in {len(net_ids)} nets "
                f"but only has {len(pool)} pins available "
                f"(nets: {', '.join(net_ids)})"
            )

    # ── UI placements must reference ui_placement=true components ──
    for up in spec.ui_placements:
        if up.instance_id not in instance_to_catalog:
            if up.instance_id not in seen_ids:
                errors.append(f"UI placement: unknown instance '{up.instance_id}'")
            continue
        cat = instance_to_catalog[up.instance_id]
        if not cat.ui_placement:
            errors.append(
                f"UI placement: '{up.instance_id}' ({cat.id}) has ui_placement=false"
            )

        # Validate mounting_style override on ui_placement
        if up.mounting_style and up.mounting_style not in cat.mounting.allowed_styles:
            errors.append(
                f"UI placement '{up.instance_id}': mounting_style '{up.mounting_style}' "
                f"not in allowed_styles {cat.mounting.allowed_styles}"
            )

        # Resolve effective mounting style (ui_placement > component > catalog default)
        ci_match = next((ci for ci in spec.components if ci.instance_id == up.instance_id), None)
        eff_style = (
            up.mounting_style
            or (ci_match.mounting_style if ci_match and ci_match.mounting_style else None)
            or cat.mounting.style
        )

        if eff_style == "side":
            if up.edge_index is None:
                errors.append(
                    f"UI placement '{up.instance_id}': side-mount components "
                    f"require edge_index (which outline edge to mount on)"
                )
            elif up.edge_index < 0 or up.edge_index >= len(spec.outline.points):
                errors.append(
                    f"UI placement '{up.instance_id}': edge_index {up.edge_index} "
                    f"out of range (0–{len(spec.outline.points) - 1})"
                )
        elif up.edge_index is not None:
            errors.append(
                f"UI placement '{up.instance_id}': edge_index is only for "
                f"side-mount components (mounting style is '{eff_style}')"
            )

    # ── All ui_placement=true components must have a placement ──
    ui_placed = {up.instance_id for up in spec.ui_placements}
    for ci in spec.components:
        if ci.catalog_id in catalog_map:
            cat = catalog_map[ci.catalog_id]
            if cat.ui_placement and ci.instance_id not in ui_placed:
                errors.append(
                    f"Component '{ci.instance_id}' ({cat.id}) has "
                    f"ui_placement=true but no UIPlacement defined"
                )

    # ── Outline validation ──
    if len(spec.outline.points) < 3:
        errors.append("Outline must have at least 3 vertices")

    for i, pt in enumerate(spec.outline.points):
        if pt.ease_in < 0:
            errors.append(f"Vertex {i}: ease_in must be >= 0")
        if pt.ease_out < 0:
            errors.append(f"Vertex {i}: ease_out must be >= 0")

    # ── Enclosure height validation ──
    from src.pipeline.config import FLOOR_MM, CEILING_MM
    MIN_CAVITY_MM = 4.0  # bare minimum clearance even with no components

    # ── Outline must fit within the printer bed ──
    if len(spec.outline.points) >= 3:
        xs = [pt.x for pt in spec.outline.points]
        ys = [pt.y for pt in spec.outline.points]
        outline_w = max(xs) - min(xs)
        outline_h = max(ys) - min(ys)
        if outline_w > pdef.bed_width or outline_h > pdef.bed_depth:
            errors.append(
                f"Outline bounding box ({outline_w:.1f}×{outline_h:.1f} mm) "
                f"exceeds printer bed ({pdef.bed_width:.0f}×{pdef.bed_depth:.0f} mm)"
            )

    # Tallest component across ALL instances (for overall enclosure height)
    tallest_mm = MIN_CAVITY_MM
    for ci in spec.components:
        if ci.catalog_id in catalog_map:
            cat = catalog_map[ci.catalog_id]
            comp_height = cat.body.height_mm if cat.body.height_mm else 0.0
            if comp_height > tallest_mm:
                tallest_mm = comp_height

    # Tallest UI-placed component only (for per-vertex checks — internal
    # components are auto-placed and the placer picks locations with enough
    # headroom, so thin extremities like ears/tails don't need to fit a battery)
    ui_ids = {up.instance_id for up in spec.ui_placements}
    tallest_ui_mm = MIN_CAVITY_MM
    for ci in spec.components:
        if ci.instance_id in ui_ids and ci.catalog_id in catalog_map:
            cat = catalog_map[ci.catalog_id]
            comp_height = cat.body.height_mm if cat.body.height_mm else 0.0
            if comp_height > tallest_ui_mm:
                tallest_ui_mm = comp_height

    min_required_z = FLOOR_MM + tallest_mm + CEILING_MM
    min_required_z_vertex = FLOOR_MM + tallest_ui_mm + CEILING_MM

    if spec.enclosure.height_mm < min_required_z:
        errors.append(
            f"Enclosure height_mm ({spec.enclosure.height_mm:.1f}mm) is too short — "
            f"needs at least {min_required_z:.1f}mm "
            f"(floor {FLOOR_MM}mm + tallest component {tallest_mm:.1f}mm + ceiling {CEILING_MM}mm)"
        )

    too_short: dict[float, list[int]] = {}
    too_tall: dict[float, list[int]] = {}
    for i, pt in enumerate(spec.outline.points):
        eff_z = pt.z_top if pt.z_top is not None else spec.enclosure.height_mm
        if eff_z < min_required_z_vertex:
            too_short.setdefault(eff_z, []).append(i)
        if eff_z > pdef.max_z_mm:
            too_tall.setdefault(eff_z, []).append(i)
    for eff_z, idxs in too_short.items():
        verts = _fmt_indices(idxs)
        errors.append(
            f"{verts} z_top ({eff_z:.1f}mm) is too short — "
            f"needs at least {min_required_z_vertex:.1f}mm to fit the tallest component"
        )
    for eff_z, idxs in too_tall.items():
        verts = _fmt_indices(idxs)
        errors.append(
            f"{verts} z_top ({eff_z:.1f}mm) exceeds printer max Z "
            f"({pdef.max_z_mm:.0f}mm)"
        )

    if spec.enclosure.height_mm > pdef.max_z_mm:
        errors.append(
            f"Enclosure height_mm ({spec.enclosure.height_mm:.1f}mm) exceeds "
            f"printer max Z ({pdef.max_z_mm:.0f}mm)"
        )

    # ── top_surface validation ──
    ts = spec.enclosure.top_surface
    if ts is not None and ts.type != "flat":
        if ts.type == "dome":
            missing = [f for f in ("peak_x_mm", "peak_y_mm", "peak_height_mm", "base_height_mm")
                       if getattr(ts, f) is None]
            if missing:
                errors.append(f"top_surface dome is missing required fields: {', '.join(missing)}")
            else:
                if ts.peak_height_mm < ts.base_height_mm:
                    errors.append(
                        f"top_surface dome peak_height_mm ({ts.peak_height_mm}) must be >= "
                        f"base_height_mm ({ts.base_height_mm})"
                    )
                if ts.peak_height_mm > pdef.max_z_mm:
                    errors.append(
                        f"top_surface dome peak_height_mm ({ts.peak_height_mm}mm) exceeds "
                        f"printer max Z ({pdef.max_z_mm:.0f}mm)"
                    )
        elif ts.type == "ridge":
            missing = [f for f in ("x1", "y1", "x2", "y2", "crest_height_mm", "base_height_mm", "falloff_mm")
                       if getattr(ts, f) is None]
            if missing:
                errors.append(f"top_surface ridge is missing required fields: {', '.join(missing)}")
            else:
                if ts.crest_height_mm < ts.base_height_mm:
                    errors.append(
                        f"top_surface ridge crest_height_mm ({ts.crest_height_mm}) must be >= "
                        f"base_height_mm ({ts.base_height_mm})"
                    )
                if ts.crest_height_mm > pdef.max_z_mm:
                    errors.append(
                        f"top_surface ridge crest_height_mm ({ts.crest_height_mm}mm) exceeds "
                        f"printer max Z ({pdef.max_z_mm:.0f}mm)"
                    )
        else:
            errors.append(f"top_surface type '{ts.type}' is unknown (expected: flat, dome, ridge)")

    # ── Outline polygon validity (Shapely) ──
    if len(spec.outline.vertices) >= 3:
        try:
            from shapely.geometry import Polygon, Point
            poly = Polygon(
                spec.outline.vertices,
                spec.outline.hole_vertices or None,
            )
            if not poly.is_valid:
                errors.append("Outline polygon is self-intersecting or invalid")
            elif poly.area <= 0:
                errors.append("Outline polygon has zero or negative area")
            else:
                for up in spec.ui_placements:
                    if up.edge_index is not None:
                        continue  # side-mount: on the edge, not interior
                    pt = Point(up.x_mm, up.y_mm)
                    if not poly.contains(pt):
                        errors.append(
                            f"UI placement '{up.instance_id}' at "
                            f"({up.x_mm}, {up.y_mm}) is outside the outline"
                        )
                        continue
                    # ── Edge clearance check ──
                    if up.instance_id in instance_to_catalog:
                        cat = instance_to_catalog[up.instance_id]
                        body = cat.body
                        half_size = max(
                            body.width_mm or 0,
                            body.length_mm or 0,
                            body.diameter_mm or 0,
                        ) / 2
                        required_clearance = half_size + cat.mounting.keepout_margin_mm
                        dist_to_edge = poly.boundary.distance(pt)
                        if dist_to_edge < required_clearance:
                            errors.append(
                                f"UI placement '{up.instance_id}' at "
                                f"({up.x_mm}, {up.y_mm}) is {dist_to_edge:.1f}mm from "
                                f"the outline edge — needs at least "
                                f"{required_clearance:.1f}mm "
                                f"(body half-size {half_size:.1f}mm + "
                                f"keepout {cat.mounting.keepout_margin_mm:.1f}mm)"
                            )

                # ── Board area / minimum dimension check ──
                _check_board_capacity(
                    poly, spec, catalog_map, instance_to_catalog, errors,
                )
        except ImportError:
            pass  # Shapely optional for polygon checks

    return errors
