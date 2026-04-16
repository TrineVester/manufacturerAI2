"""Re-route all sessions and run DRC to verify clearance fixes."""
import json
import math
import logging
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO)

from src.session import load_session
from src.pipeline.design import parse_design, parse_physical_design
from src.pipeline.circuit import parse_circuit
from src.pipeline.placer import place_components, assemble_full_placement
from src.pipeline.router import route_traces, routing_to_dict
from src.pipeline.router.drc import run_drc
from src.pipeline.router.models import RouterConfig, RoutingResult, Trace
from src.catalog import load_catalog
from src.pipeline.trace_geometry import point_seg_dist
from shapely.geometry import Polygon

cat = load_catalog(Path("catalog"))
config = RouterConfig()
cat_map = {c.id: c for c in cat.components}

for sid in ["20260413_100000", "20260413_110000", "20260413_120000", "20260413_130000"]:
    print(f"\n{'='*60}")
    print(f"Session: {sid}")
    print(f"{'='*60}")
    s = load_session(sid)

    # Parse design and circuit
    design_raw = s.read_artifact("design.json")
    circuit_raw = s.read_artifact("circuit.json")
    placement_raw = s.read_artifact("placement.json")
    if design_raw is None or circuit_raw is None or placement_raw is None:
        print(f"  Missing artifacts, skipping")
        continue

    try:
        physical = parse_physical_design(design_raw)
        circuit = parse_circuit(circuit_raw)

        pd2 = json.loads(placement_raw) if isinstance(placement_raw, str) else placement_raw

        full_placement = assemble_full_placement(
            pd2, physical.outline, circuit.nets, physical.enclosure,
        )

        # Route!
        result = route_traces(full_placement, cat)

        # Save
        routing_dict = routing_to_dict(result)
        s.write_artifact("routing.json", routing_dict)
        s.save()

        # DRC
        outline_pts = [(p.x, p.y) for p in physical.outline.points]
        outline_poly = Polygon(outline_pts)

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
        print(f"\n  {drc.summary()}")

        if drc.violations:
            for v in drc.violations:
                print(f"    {v.rule}: {v.message}")

        # Raw distance check
        net_ids = list({t.net_id for t in result.traces})
        print(f"\n  Trace-trace min distances:")
        for i, na in enumerate(net_ids):
            for j in range(i + 1, len(net_ids)):
                nb = net_ids[j]
                paths_a = [t.path for t in result.traces if t.net_id == na]
                paths_b = [t.path for t in result.traces if t.net_id == nb]
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
                status = "OK" if best >= required - 0.25 else f"VIOL ({best:.2f} < {required:.2f})"
                if best < required + 0.5:
                    print(f"    {na} <-> {nb}: {best:.3f}mm {status}")

        print(f"\n  Traces: {len(result.traces)}, Failed: {result.failed_nets}")

    except Exception as e:
        traceback.print_exc()
        print(f"  ERROR: {e}")
