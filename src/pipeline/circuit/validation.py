"""Circuit validation — check a CircuitDesign against the catalog and design context."""

from __future__ import annotations

from src.catalog import CatalogResult
from src.pipeline.design.models import CircuitDesign


def validate_circuit(
    circuit: CircuitDesign,
    catalog: CatalogResult,
    ui_instance_ids: set[str] | None = None,
) -> list[str]:
    """Validate a CircuitDesign against the catalog.

    Parameters
    ----------
    circuit          : The parsed circuit to validate.
    catalog          : The component catalog.
    ui_instance_ids  : Instance IDs from design.json ui_placements.
                       If provided, checks that all UI components are present.

    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []
    catalog_map = {c.id: c for c in catalog.components}

    # All UI components must be present
    circuit_instance_ids = {c.instance_id for c in circuit.components}
    if ui_instance_ids:
        missing_ui = ui_instance_ids - circuit_instance_ids
        if missing_ui:
            errors.append(
                f"Missing UI components from design: {', '.join(sorted(missing_ui))}. "
                f"You must include all placed UI components."
            )

    # All catalog_ids must exist
    for comp in circuit.components:
        if comp.catalog_id not in catalog_map:
            errors.append(
                f"Component '{comp.instance_id}' references unknown "
                f"catalog_id '{comp.catalog_id}'"
            )

    # Instance IDs must be unique
    seen_ids: set[str] = set()
    for comp in circuit.components:
        if comp.instance_id in seen_ids:
            errors.append(f"Duplicate instance_id '{comp.instance_id}'")
        seen_ids.add(comp.instance_id)

    # Nets must have at least 2 pins
    for net in circuit.nets:
        if len(net.pins) < 2:
            errors.append(f"Net '{net.id}' has fewer than 2 pins")

    # Pin references must point to existing instances and valid pins
    comp_catalog_map = {}
    for comp in circuit.components:
        if comp.catalog_id in catalog_map:
            comp_catalog_map[comp.instance_id] = catalog_map[comp.catalog_id]

    for net in circuit.nets:
        for pin_ref in net.pins:
            if ":" not in pin_ref:
                errors.append(
                    f"Net '{net.id}': invalid pin reference '{pin_ref}' "
                    f"(expected 'instance_id:pin_id')"
                )
                continue
            instance, pin_id = pin_ref.split(":", 1)
            if instance not in circuit_instance_ids:
                errors.append(
                    f"Net '{net.id}' references unknown instance "
                    f"'{instance}' in pin '{pin_ref}'"
                )
                continue
            cat = comp_catalog_map.get(instance)
            if cat:
                pin_ids = {p.id for p in cat.pins}
                group_ids = {g.id for g in cat.pin_groups} if cat.pin_groups else set()
                if pin_id not in pin_ids and pin_id not in group_ids:
                    errors.append(
                        f"Net '{net.id}': unknown pin '{pin_id}' on "
                        f"'{instance}' (catalog: {cat.id}). "
                        f"Valid pins: {', '.join(sorted(pin_ids))}"
                    )

    # Each pin in at most one net (allocatable groups allow multiple)
    allocatable_groups: dict[tuple[str, str], list[str]] = {}
    for comp in circuit.components:
        cat = comp_catalog_map.get(comp.instance_id)
        if cat and cat.pin_groups:
            for g in cat.pin_groups:
                if g.allocatable:
                    allocatable_groups[(comp.instance_id, g.id)] = g.pin_ids

    pin_to_net: dict[str, str] = {}
    group_alloc_count: dict[tuple[str, str], list[str]] = {}
    for net in circuit.nets:
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
                        f"Pin '{pin_ref}' is connected to both net "
                        f"'{pin_to_net[pin_ref]}' and net '{net.id}' "
                        f"— each pin can only belong to one net"
                    )
                else:
                    pin_to_net[pin_ref] = net.id

    for (iid, gid), net_ids in group_alloc_count.items():
        pool = allocatable_groups[(iid, gid)]
        if len(net_ids) > len(pool):
            errors.append(
                f"Group '{iid}:{gid}' used in {len(net_ids)} nets "
                f"but only has {len(pool)} pins available "
                f"(nets: {', '.join(net_ids)})"
            )

    return errors
