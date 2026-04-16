"""Print trace waypoints to analyze where clearance violations occur."""
import json
from src.session import load_session
from src.pipeline.trace_geometry import point_seg_dist

s = load_session("20260413_100000")
rd = s.read_artifact("routing.json")
d = json.loads(rd) if isinstance(rd, str) else rd

traces = d["traces"]
for t in traces:
    print(f"Net {t['net_id']}:")
    for i, p in enumerate(t["path"]):
        print(f"  [{i}] ({p[0]:.3f}, {p[1]:.3f})")

# Find the violating pair (GND <-> LED_IN)
gnd_path = None
led_in_path = None
for t in traces:
    if t["net_id"] == "GND":
        gnd_path = [tuple(p) for p in t["path"]]
    elif t["net_id"] == "LED_IN":
        led_in_path = [tuple(p) for p in t["path"]]

if gnd_path and led_in_path:
    print("\nGND vs LED_IN segment distances:")
    for i in range(len(gnd_path) - 1):
        for j in range(len(led_in_path) - 1):
            d = min(
                point_seg_dist(gnd_path[i][0], gnd_path[i][1],
                               led_in_path[j][0], led_in_path[j][1],
                               led_in_path[j+1][0], led_in_path[j+1][1]),
                point_seg_dist(gnd_path[i+1][0], gnd_path[i+1][1],
                               led_in_path[j][0], led_in_path[j][1],
                               led_in_path[j+1][0], led_in_path[j+1][1]),
                point_seg_dist(led_in_path[j][0], led_in_path[j][1],
                               gnd_path[i][0], gnd_path[i][1],
                               gnd_path[i+1][0], gnd_path[i+1][1]),
                point_seg_dist(led_in_path[j+1][0], led_in_path[j+1][1],
                               gnd_path[i][0], gnd_path[i][1],
                               gnd_path[i+1][0], gnd_path[i+1][1]),
            )
            if d < 2.0:
                print(f"  GND seg[{i}] ({gnd_path[i]}-{gnd_path[i+1]}) vs "
                      f"LED_IN seg[{j}] ({led_in_path[j]}-{led_in_path[j+1]}): {d:.3f}mm")
