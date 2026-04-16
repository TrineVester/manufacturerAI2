"""Catalog loader — reads catalog/*.json files, parses and validates them."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    Body, BodyChannels, Cap, ExtraPart, Mounting, SwitchActuator,
    Pin, PinShape, PinGroup, ScadPattern, ScadFeature, Component,
    ValidationError, CatalogResult,
)


CATALOG_DIR = Path(__file__).resolve().parent.parent.parent / "catalog"


# ── Validation ─────────────────────────────────────────────────────

def _validate_component(comp: Component) -> list[ValidationError]:
    """Run all validation checks on a single component."""
    errs: list[ValidationError] = []
    cid = comp.id

    # Body dimensions
    if comp.body.shape == "rect":
        if comp.body.width_mm is None or comp.body.width_mm <= 0:
            errs.append(ValidationError(cid, "body.width_mm", "Must be > 0 for rect shape"))
        if comp.body.length_mm is None or comp.body.length_mm <= 0:
            errs.append(ValidationError(cid, "body.length_mm", "Must be > 0 for rect shape"))
    elif comp.body.shape == "circle":
        if comp.body.diameter_mm is None or comp.body.diameter_mm <= 0:
            errs.append(ValidationError(cid, "body.diameter_mm", "Must be > 0 for circle shape"))
    else:
        errs.append(ValidationError(cid, "body.shape", f"Unknown shape '{comp.body.shape}', expected 'rect' or 'circle'"))

    if comp.body.height_mm <= 0:
        errs.append(ValidationError(cid, "body.height_mm", "Must be > 0"))

    # Mounting style
    valid_styles = {"top", "side", "internal", "bottom"}
    if comp.mounting.style not in valid_styles:
        errs.append(ValidationError(cid, "mounting.style", f"Unknown style '{comp.mounting.style}'"))
    for s in comp.mounting.allowed_styles:
        if s not in valid_styles:
            errs.append(ValidationError(cid, "mounting.allowed_styles", f"Unknown style '{s}'"))
    if comp.mounting.style not in comp.mounting.allowed_styles:
        errs.append(ValidationError(cid, "mounting.style",
                                    f"Default style '{comp.mounting.style}' not in allowed_styles {comp.mounting.allowed_styles}"))

    # Pin IDs unique
    pin_ids = [p.id for p in comp.pins]
    seen: set[str] = set()
    for pid in pin_ids:
        if pid in seen:
            errs.append(ValidationError(cid, f"pins.{pid}", "Duplicate pin ID"))
        seen.add(pid)

    pin_id_set = set(pin_ids)

    # Pin direction
    valid_dirs = {"in", "out", "bidirectional"}
    for pin in comp.pins:
        if pin.direction not in valid_dirs:
            errs.append(ValidationError(cid, f"pins.{pin.id}.direction",
                                        f"Unknown direction '{pin.direction}'"))

    # internal_nets reference valid pins
    for i, net_group in enumerate(comp.internal_nets):
        for pid in net_group:
            if pid not in pin_id_set:
                errs.append(ValidationError(cid, f"internal_nets[{i}]",
                                            f"References unknown pin '{pid}'"))

    # pin_groups reference valid pins
    if comp.pin_groups:
        for group in comp.pin_groups:
            for pid in group.pin_ids:
                if pid not in pin_id_set:
                    errs.append(ValidationError(cid, f"pin_groups.{group.id}",
                                                f"References unknown pin '{pid}'"))

    return errs


# ── Parsing ────────────────────────────────────────────────────────

def _parse_body(data: dict) -> Body:
    ch_raw = data.get("channels")
    channels = None
    if ch_raw:
        channels = BodyChannels(
            axis=ch_raw["axis"],
            count=ch_raw["count"],
            diameter_mm=ch_raw["diameter_mm"],
            spacing_mm=ch_raw["spacing_mm"],
            length_mm=ch_raw["length_mm"],
            center_z_mm=ch_raw["center_z_mm"],
        )
    return Body(
        shape=data["shape"],
        height_mm=data["height_mm"],
        width_mm=data.get("width_mm"),
        length_mm=data.get("length_mm"),
        diameter_mm=data.get("diameter_mm"),
        channels=channels,
    )


def _parse_cap(data: dict | None) -> Cap | None:
    if data is None:
        return None
    actuator_raw = data.get("actuator")
    actuator = None
    if actuator_raw is not None:
        actuator = SwitchActuator(
            total_height_mm=actuator_raw["total_height_mm"],
            base_height_mm=actuator_raw["base_height_mm"],
            cylinder_height_mm=actuator_raw["cylinder_height_mm"],
            cylinder_diameter_mm=actuator_raw["cylinder_diameter_mm"],
        )
    return Cap(
        diameter_mm=data["diameter_mm"],
        height_mm=data["height_mm"],
        hole_clearance_mm=data["hole_clearance_mm"],
        actuator=actuator,
    )


def _parse_extra_part(data: dict) -> ExtraPart:
    return ExtraPart(
        label=data.get("label", data["shape"]),
        shape=data["shape"],
        width_mm=data.get("width_mm"),
        length_mm=data.get("length_mm"),
        thickness_mm=data.get("thickness_mm"),
        diameter_mm=data.get("diameter_mm"),
    )


def _parse_mounting(data: dict) -> Mounting:
    extras_raw = data.get("extras")
    extras = [_parse_extra_part(e) for e in extras_raw] if extras_raw else []

    cap = _parse_cap(data.get("cap"))
    body_raw = data.get("_body")

    return Mounting(
        style=data["style"],
        allowed_styles=data["allowed_styles"],
        blocks_routing=data["blocks_routing"],
        keepout_margin_mm=data["keepout_margin_mm"],
        cap=cap,
        installed_height_mm=data.get("installed_height_mm"),
        extras=extras,
    )


def _parse_pin(data: dict) -> Pin:
    pos = data.get("position_mm", [0, 0])
    shape_raw = data.get("shape")
    shape = None
    if shape_raw is not None:
        shape = PinShape(
            type=shape_raw.get("type", "circle"),
            width_mm=shape_raw.get("width_mm"),
            length_mm=shape_raw.get("length_mm"),
        )
    return Pin(
        id=data["id"],
        label=data.get("label", data["id"]),
        position_mm=(pos[0], pos[1]),
        direction=data["direction"],
        voltage_v=data.get("voltage_v"),
        current_max_ma=data.get("current_max_ma"),
        hole_diameter_mm=data.get("hole_diameter_mm", 1.0),
        description=data.get("description", ""),
        shape=shape,
    )


def _parse_pin_group(data: dict) -> PinGroup:
    return PinGroup(
        id=data["id"],
        pin_ids=data["pin_ids"],
        description=data.get("description", ""),
        fixed_net=data.get("fixed_net"),
        allocatable=data.get("allocatable", False),
        capabilities=data.get("capabilities"),
    )


def _parse_scad_feature(data: dict) -> ScadFeature:
    pos = data.get("position_mm", [0, 0])
    pattern_raw = data.get("pattern")
    pattern = None
    if pattern_raw is not None:
        pattern = ScadPattern(
            type=pattern_raw["type"],
            spacing_mm=pattern_raw["spacing_mm"],
            clip_to_body=pattern_raw.get("clip_to_body", True),
        )
    rot_raw = data.get("rotate")
    rotate = tuple(rot_raw) if rot_raw else None
    return ScadFeature(
        shape=data["shape"],
        label=data.get("label", ""),
        position_mm=(pos[0], pos[1]),
        width_mm=data.get("width_mm"),
        length_mm=data.get("length_mm"),
        diameter_mm=data.get("diameter_mm"),
        depth_mm=data.get("depth_mm"),
        z_anchor=data.get("z_anchor", "cavity_start"),
        z_center_mm=data.get("z_center_mm"),
        through_surface=data.get("through_surface", False),
        rotate=rotate,
        pattern=pattern,
    )


def _parse_component(data: dict, source_file: str = "") -> Component:
    scad_raw = data.get("scad", {})
    features_raw = scad_raw.get("features", []) if isinstance(scad_raw, dict) else []
    return Component(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        ui_placement=data["ui_placement"],
        body=_parse_body(data["body"]),
        mounting=_parse_mounting(data["mounting"]),
        pins=[_parse_pin(p) for p in data["pins"]],
        pin_length_mm=data.get("pin_length_mm"),
        internal_nets=data.get("internal_nets", []),
        pin_groups=[_parse_pin_group(g) for g in data["pin_groups"]] if data.get("pin_groups") else None,
        configurable=data.get("configurable"),
        scad_features=[_parse_scad_feature(f) for f in features_raw],
        source_file=source_file,
    )


# ── Public API ─────────────────────────────────────────────────────

def load_catalog(catalog_dir: Path | None = None) -> CatalogResult:
    """Load all catalog/*.json files, parse and validate.

    Returns a CatalogResult with components and any validation errors.
    Components that fail to parse are skipped (error recorded).
    Components that parse but have validation issues are still included.
    """
    d = catalog_dir or CATALOG_DIR
    components: list[Component] = []
    errors: list[ValidationError] = []

    json_files = sorted(d.glob("*.json"))
    if not json_files:
        errors.append(ValidationError("_catalog", "files", f"No .json files found in {d}"))
        return CatalogResult(components=components, errors=errors)

    for path in json_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(ValidationError(
                path.stem, "json", f"Parse error: {exc}"))
            continue
        except OSError as exc:
            errors.append(ValidationError(
                path.stem, "file", f"Read error: {exc}"))
            continue

        try:
            comp = _parse_component(raw, source_file=str(path))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(ValidationError(
                raw.get("id", path.stem), "parse", f"Missing/invalid field: {exc}"))
            continue

        # Validate
        comp_errors = _validate_component(comp)
        errors.extend(comp_errors)
        components.append(comp)

    # Check for duplicate IDs across files
    id_counts: dict[str, int] = {}
    for comp in components:
        id_counts[comp.id] = id_counts.get(comp.id, 0) + 1
    for cid, count in id_counts.items():
        if count > 1:
            errors.append(ValidationError(cid, "id", f"Duplicate component ID (appears {count} times)"))

    return CatalogResult(components=components, errors=errors)


def get_component(catalog: list[Component] | CatalogResult, component_id: str) -> Component | None:
    """Look up a component by ID. Returns None if not found."""
    comps = catalog.components if isinstance(catalog, CatalogResult) else catalog
    for c in comps:
        if c.id == component_id:
            return c
    return None
