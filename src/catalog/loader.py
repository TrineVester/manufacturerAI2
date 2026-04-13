"""Catalog loader — reads catalog/*.json files, parses and validates them."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    Body, BodyChannels, Cap, Hatch, SoundHoles, Mounting, Pin, PinGroup, Component,
    ExtraPart, ValidationError, CatalogResult,
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
    return Cap(
        diameter_mm=data["diameter_mm"],
        height_mm=data["height_mm"],
        hole_clearance_mm=data["hole_clearance_mm"],
    )


def _parse_hatch(data: dict | None) -> Hatch | None:
    if data is None:
        return None
    return Hatch(
        enabled=data["enabled"],
        clearance_mm=data["clearance_mm"],
        thickness_mm=data["thickness_mm"],
    )


def _parse_sound_holes(data: dict | None) -> SoundHoles | None:
    if data is None:
        return None
    return SoundHoles(
        enabled=data.get("enabled", False),
        pattern=data.get("pattern", "grid"),
        hole_diameter_mm=data.get("hole_diameter_mm", 1.5),
        hole_spacing_mm=data.get("hole_spacing_mm", 3.0),
    )


def _parse_mounting(data: dict) -> Mounting:
    return Mounting(
        style=data["style"],
        allowed_styles=data["allowed_styles"],
        blocks_routing=data["blocks_routing"],
        keepout_margin_mm=data["keepout_margin_mm"],
        cap=_parse_cap(data.get("cap")),
        hatch=_parse_hatch(data.get("hatch")),
        sound_holes=_parse_sound_holes(data.get("sound_holes")),
    )


def _parse_pin(data: dict) -> Pin:
    pos = data.get("position_mm", [0, 0])
    return Pin(
        id=data["id"],
        label=data.get("label", data["id"]),
        position_mm=(pos[0], pos[1]),
        direction=data["direction"],
        voltage_v=data.get("voltage_v"),
        current_max_ma=data.get("current_max_ma"),
        hole_diameter_mm=data.get("hole_diameter_mm", 0.8),
        description=data.get("description", ""),
        shape=data.get("shape", "round"),
        shape_width_mm=data.get("shape_width_mm"),
        shape_length_mm=data.get("shape_length_mm"),
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


def _parse_component(data: dict, source_file: str = "") -> Component:
    extra_parts = []
    for ep in data.get("extra_parts", []):
        extra_parts.append(ExtraPart(
            id=ep["id"],
            name=ep["name"],
            description=ep.get("description", ""),
            scad_module=ep.get("scad_module", ""),
        ))
    return Component(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        ui_placement=data["ui_placement"],
        body=_parse_body(data["body"]),
        mounting=_parse_mounting(data["mounting"]),
        pins=[_parse_pin(p) for p in data["pins"]],
        internal_nets=data.get("internal_nets", []),
        pin_groups=[_parse_pin_group(g) for g in data["pin_groups"]] if data.get("pin_groups") else None,
        configurable=data.get("configurable"),
        extra_parts=extra_parts,
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
