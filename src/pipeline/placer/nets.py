"""Net connectivity graph for placement scoring."""

from __future__ import annotations

from dataclasses import dataclass

from src.catalog.models import Component
from src.pipeline.design.models import Net


@dataclass
class NetEdge:
    """An edge in the net-connectivity graph between two instances."""
    net_id: str
    other_iid: str
    my_pins: list[str]
    other_pins: list[str]
    fanout: int = 2   # number of unique instances on this net


def build_net_graph(nets: list[Net]) -> dict[str, list[NetEdge]]:
    """Build net connectivity: instance_id -> [NetEdge, ...].

    For each net, creates edges between every pair of participating
    component instances.  Each edge carries the net's *fanout* — the
    number of distinct instances on the net — so scoring can give
    high-fanout nets (e.g. GND, VCC) stronger proximity weight.
    """
    graph: dict[str, list[NetEdge]] = {}

    for net in nets:
        # Group pins by instance
        by_inst: dict[str, list[str]] = {}
        for ref in net.pins:
            if ":" not in ref:
                continue
            iid, pid = ref.split(":", 1)
            by_inst.setdefault(iid, []).append(pid)

        fanout = len(by_inst)
        iids = list(by_inst.keys())
        for i, a in enumerate(iids):
            for b in iids[i + 1:]:
                graph.setdefault(a, []).append(
                    NetEdge(net.id, b, by_inst[a], by_inst[b], fanout))
                graph.setdefault(b, []).append(
                    NetEdge(net.id, a, by_inst[b], by_inst[a], fanout))

    return graph


def net_fanout_map(nets: list[Net]) -> dict[str, int]:
    """Return a mapping net_id -> number of unique component instances.

    Nets with fanout >= 3 are considered "high-fanout" — their
    connected components should be kept especially close together
    to avoid long-distance routing that blocks other traces.
    """
    result: dict[str, int] = {}
    for net in nets:
        instances: set[str] = set()
        for ref in net.pins:
            if ":" in ref:
                instances.add(ref.split(":", 1)[0])
        result[net.id] = len(instances)
    return result


def count_shared_nets(
    iid_a: str, iid_b: str,
    net_graph: dict[str, list[NetEdge]],
) -> int:
    """Count distinct nets connecting two component instances.

    This tells the placer how many trace channels must fit in the
    gap between two components.
    """
    nets: set[str] = set()
    for edge in net_graph.get(iid_a, []):
        if edge.other_iid == iid_b:
            nets.add(edge.net_id)
    return len(nets)


def component_degree(
    net_graph: dict[str, list[NetEdge]],
) -> dict[str, int]:
    """Count the number of unique component neighbors for each instance.

    Higher degree means the component is a "hub" — connected to many
    others.  Used to determine placement order: hubs are placed first
    so their satellites can cluster around them.
    """
    degrees: dict[str, int] = {}
    for iid, edges in net_graph.items():
        degrees[iid] = len({e.other_iid for e in edges})
    return degrees


def build_placement_groups(
    instance_ids: list[str],
    net_graph: dict[str, list[NetEdge]],
    area_map: dict[str, float],
) -> list[list[str]]:
    """Partition and order instances for group-aware placement.

    1. Connected-component detection (union-find on the net graph).
    2. Within each group, BFS from the highest-degree node to create
       a placement order that keeps tightly-connected components
       adjacent.  Ties are broken by footprint area (largest first)
       so large components are placed before small ones.
    3. Groups are sorted so the group containing the largest single
       component is placed first.

    Parameters
    ----------
    instance_ids : list[str]
        Auto-placed instance IDs (UI-placed components excluded).
    net_graph : dict
        Net connectivity graph from ``build_net_graph``.
    area_map : dict
        ``instance_id -> footprint area`` for tie-breaking.

    Returns
    -------
    list[list[str]]
        Ordered groups.  Place groups sequentially; within each group,
        place instances in the returned order.
    """
    if not instance_ids:
        return []

    degrees = component_degree(net_graph)
    id_set = set(instance_ids)

    # ── Connected components via full-graph BFS ─────────────────────
    # Trace connectivity through ALL components in the net graph
    # (including UI-placed ones), then filter each connected
    # component down to the requested instance_ids.  This ensures
    # that two auto-placed components linked transitively through
    # UI-placed intermediaries end up in the same group.
    visited_global: set[str] = set()
    raw_groups: dict[int, list[str]] = {}
    group_idx = 0

    for seed in instance_ids:
        if seed in visited_global:
            continue
        # BFS through the full net graph
        bfs_queue = [seed]
        reached: set[str] = {seed}
        while bfs_queue:
            current = bfs_queue.pop(0)
            for edge in net_graph.get(current, []):
                if edge.other_iid not in reached:
                    reached.add(edge.other_iid)
                    bfs_queue.append(edge.other_iid)
        # Keep only the requested (auto-placed) instance IDs
        members = [i for i in instance_ids if i in reached]
        visited_global.update(members)
        raw_groups[group_idx] = members
        group_idx += 1

    # ── BFS-order within each group ───────────────────────────────
    def _bfs_order(members: list[str]) -> list[str]:
        member_set = set(members)
        # Start BFS from the largest-footprint member.  This ensures
        # the biggest component claims space first; smaller parts
        # then fit into the remaining area.  Degree breaks ties so
        # highly-connected same-size components are still hubs.
        seed = max(members, key=lambda i: (area_map.get(i, 0), degrees.get(i, 0)))
        visited: list[str] = []
        queue = [seed]
        seen = {seed}
        while queue:
            # Within the current frontier, prioritise large area
            # first, then high degree, so the ordering is stable.
            queue.sort(
                key=lambda i: (area_map.get(i, 0), degrees.get(i, 0)),
                reverse=True,
            )
            current = queue.pop(0)
            visited.append(current)
            for edge in net_graph.get(current, []):
                if edge.other_iid in member_set and edge.other_iid not in seen:
                    seen.add(edge.other_iid)
                    queue.append(edge.other_iid)
        # Append any members not reachable (shouldn't happen, but
        # safety net) in descending area order.
        for m in sorted(members, key=lambda i: area_map.get(i, 0), reverse=True):
            if m not in seen:
                visited.append(m)
        return visited

    ordered_groups = [_bfs_order(members) for members in raw_groups.values()]

    # ── Sort groups: biggest max-component-area first ─────────────
    ordered_groups.sort(
        key=lambda g: max(area_map.get(i, 0) for i in g),
        reverse=True,
    )

    return ordered_groups


def resolve_pin_positions(
    pin_ids: list[str],
    cat: Component,
) -> list[tuple[float, float]]:
    """Get local positions for a list of pin IDs or group IDs.

    For group IDs (MCU gpio, etc.) returns the centroid of all pins
    in that group.  The router will later resolve the exact pin.
    """
    pin_map = {p.id: p.position_mm for p in cat.pins}
    group_map: dict[str, tuple[float, float]] = {}
    if cat.pin_groups:
        for g in cat.pin_groups:
            g_pins = [pin_map[p] for p in g.pin_ids if p in pin_map]
            if g_pins:
                group_map[g.id] = (
                    sum(p[0] for p in g_pins) / len(g_pins),
                    sum(p[1] for p in g_pins) / len(g_pins),
                )

    positions: list[tuple[float, float]] = []
    for pid in pin_ids:
        if pid in pin_map:
            positions.append(pin_map[pid])
        elif pid in group_map:
            positions.append(group_map[pid])
    return positions
