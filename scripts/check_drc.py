"""Quick DRC check on all sessions with routing data."""
import json
import math
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

from src.session import load_session
from src.pipeline.router.drc import run_drc
from src.pipeline.router.models import RouterConfig, RoutingResult, Trace
from src.catalog import load_catalog
from src.pipeline.trace_geometry import point_seg_dist
from shapely.geometry import Polygon

cat = load_catalog(Path("catalog"))
config = RouterConfig()
cat_map = {c.id: c for c in cat.components}

for sid in ["20260413_100000", "20260413_110000", "20260413_120000", "20260413_130000"]:
    s = load_session(sid)
    routing_data = s.read_artifact("routing.json")
    if routing_data is None:
        print(f"{sid}: no routing data")
        continue
    rd = json.loads(routing_data) if isinstance(routing_data, str) else routing_data

    traces = [
        Trace(net_id=t["net_id"], path=[tuple(p) for p in t["path"]])
        for t in rd.get("traces", [])
    ]
    result = RoutingResult(
        traces=traces,
        pin_assignments=rd.get("pin_assignments", {}),
        failed_nets=rd.get("failed_nets", []),
    )

    design_data = s.read_artifact("design.json")
    dd = json.loads(design_data) if isinstance(design_data, str) else design_data
    outline_pts = [(p["x"], p["y"]) for p in dd["outline"]]
    outline_poly = Polygon(outline_pts)

    placement_data = s.read_artifact("placement.json")
    pd2 = json.loads(placement_data) if isinstance(placement_data, str) else placement_data

    pin_positions = {}
    for comp in pd2.get("components", []):
        cobj = cat_map.get(comp["catalog_id"])
        if cobj is None:
            continue
        cx, cy, rot = comp["x_mm"], comp["y_mm"], comp.get("rotation_deg", 0)
        rad = math.radians(rot)
        for pin in cobj.pins:
            lx, ly = pin.position_mm
            wx = cx + lx * math.cos(rad) - ly * math.sin(rad)
            wy = cy + lx * math.sin(rad) + ly * math.cos(rad)
            key = f"{comp['instance_id']}:{pin.id}"
            pin_positions[key] = (wx, wy)

    drc = run_drc(result, pin_positions, outline_poly, config)
    print(f"=== {sid} ===")
    print(drc.summary())
    for v in drc.violations:
        print(f"  {v.rule}: {v.message}")

    # Also compute raw minimum distances
    print("\n  Trace-to-trace distances:")
    net_ids = list({t.net_id for t in traces})
    for i, na in enumerate(net_ids):
        for j in range(i + 1, len(net_ids)):
            nb = net_ids[j]
            paths_a = [t.path for t in traces if t.net_id == na]
            paths_b = [t.path for t in traces if t.net_id == nb]
            best = float("inf")
            for pa in paths_a:
                for pb in paths_b:
                    for si in range(len(pa) - 1):
                        for sj in range(len(pb) - 1):
                            d = min(
                                point_seg_dist(pa[si][0], pa[si][1], pb[sj][0], pb[sj][1], pb[sj+1][0], pb[sj+1][1]),
                                point_seg_dist(pa[si+1][0], pa[si+1][1], pb[sj][0], pb[sj][1], pb[sj+1][0], pb[sj+1][1]),
                                point_seg_dist(pb[sj][0], pb[sj][1], pa[si][0], pa[si][1], pa[si+1][0], pa[si+1][1]),
                                point_seg_dist(pb[sj+1][0], pb[sj+1][1], pa[si][0], pa[si][1], pa[si+1][0], pa[si+1][1]),
                            )
                            if d < best:
                                best = d
            required = config.trace_clearance_mm + config.trace_width_mm
            status = "OK" if best >= required else f"VIOLATION (need {required:.2f}mm)"
            print(f"    {na} <-> {nb}: {best:.3f}mm {status}")

    print("\n  Trace-to-pin distances (foreign pins only):")
    net_pin_sets = {}
    for t in traces:
        net_pin_sets.setdefault(t.net_id, set())
    for key, val in result.pin_assignments.items():
        nid = key.split("|")[0]
        net_pin_sets.setdefault(nid, set()).add(val)
    # Also match endpoints
    for t in traces:
        if not t.path:
            continue
        own = net_pin_sets.setdefault(t.net_id, set())
        for pk, (px, py) in pin_positions.items():
            for ep in [t.path[0], t.path[-1]]:
                if math.hypot(px - ep[0], py - ep[1]) <= 0.6:
                    own.add(pk)

    for t in traces:
        own_pins = net_pin_sets.get(t.net_id, set())
        worst_pin = None
        worst_dist = float("inf")
        for pk, (px, py) in pin_positions.items():
            if pk in own_pins:
                continue
            for si in range(len(t.path) - 1):
                d = point_seg_dist(px, py, t.path[si][0], t.path[si][1], t.path[si+1][0], t.path[si+1][1])
                if d < worst_dist:
                    worst_dist = d
                    worst_pin = pk
        if worst_pin:
            required = config.pin_clearance_mm + config.trace_width_mm / 2
            status = "OK" if worst_dist >= required else f"VIOLATION (need {required:.2f}mm)"
            print(f"    {t.net_id} closest to {worst_pin}: {worst_dist:.3f}mm {status}")
    print()
