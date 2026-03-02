"""Design spec validation — check a DesignSpec against the catalog."""

from __future__ import annotations

from src.catalog import CatalogResult
from .models import DesignSpec


def validate_design(spec: DesignSpec, catalog: CatalogResult) -> list[str]:
    """Validate a DesignSpec against the catalog. Returns error messages (empty = valid)."""
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
                for key in ci.config:
                    if key not in cat.configurable:
                        errors.append(
                            f"Component '{ci.instance_id}': unknown config key '{key}'"
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

        # Resolve effective mounting style
        ci_match = next((ci for ci in spec.components if ci.instance_id == up.instance_id), None)
        eff_style = (ci_match.mounting_style if ci_match and ci_match.mounting_style else cat.mounting.style)

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
    FLOOR_MM = 2.0
    CEILING_MM = 2.0
    MIN_CAVITY_MM = 4.0  # bare minimum clearance even with no components

    # Tallest internal component determines minimum required cavity height
    tallest_mm = MIN_CAVITY_MM
    for ci in spec.components:
        if ci.catalog_id in catalog_map:
            cat = catalog_map[ci.catalog_id]
            comp_height = cat.body.height_mm if cat.body.height_mm else 0.0
            if comp_height > tallest_mm:
                tallest_mm = comp_height

    min_required_z = FLOOR_MM + tallest_mm + CEILING_MM

    if spec.enclosure.height_mm < min_required_z:
        errors.append(
            f"Enclosure height_mm ({spec.enclosure.height_mm:.1f}mm) is too short — "
            f"needs at least {min_required_z:.1f}mm "
            f"(floor {FLOOR_MM}mm + tallest component {tallest_mm:.1f}mm + ceiling {CEILING_MM}mm)"
        )

    for i, pt in enumerate(spec.outline.points):
        eff_z = pt.z_top if pt.z_top is not None else spec.enclosure.height_mm
        if eff_z < min_required_z:
            errors.append(
                f"Vertex {i} z_top ({eff_z:.1f}mm) is too short — "
                f"needs at least {min_required_z:.1f}mm to fit the tallest component"
            )

    # ── top_surface validation ──
    ts = spec.enclosure.top_surface
    if ts is not None and ts.type != "flat":
        if ts.type == "dome":
            missing = [f for f in ("peak_x_mm", "peak_y_mm", "peak_height_mm", "base_height_mm")
                       if getattr(ts, f) is None]
            if missing:
                errors.append(f"top_surface dome is missing required fields: {', '.join(missing)}")
            elif ts.peak_height_mm < ts.base_height_mm:
                errors.append(
                    f"top_surface dome peak_height_mm ({ts.peak_height_mm}) must be >= "
                    f"base_height_mm ({ts.base_height_mm})"
                )
        elif ts.type == "ridge":
            missing = [f for f in ("x1", "y1", "x2", "y2", "crest_height_mm", "base_height_mm", "falloff_mm")
                       if getattr(ts, f) is None]
            if missing:
                errors.append(f"top_surface ridge is missing required fields: {', '.join(missing)}")
            elif ts.crest_height_mm < ts.base_height_mm:
                errors.append(
                    f"top_surface ridge crest_height_mm ({ts.crest_height_mm}) must be >= "
                    f"base_height_mm ({ts.base_height_mm})"
                )
        else:
            errors.append(f"top_surface type '{ts.type}' is unknown (expected: flat, dome, ridge)")

    # ── Outline polygon validity (Shapely) ──
    if len(spec.outline.vertices) >= 3:
        try:
            from shapely.geometry import Polygon, Point
            poly = Polygon(spec.outline.vertices)
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
        except ImportError:
            pass  # Shapely optional for polygon checks

    return errors
