"""One-off script: enlarge session 110000 ellipse outline to fit all components."""
import json, math
from src.session import load_session

s = load_session("20260413_110000")
d = s.read_artifact("design.json")

# Current ellipse: center (40,58), radii (38,55)
# Scale up ~20%: radii (46, 66), new center (46, 69)
cx, cy = 46, 69
rx, ry = 46, 66

# Update shape
d["shape"]["children"][0]["center"] = [cx, cy]
d["shape"]["children"][0]["radius"] = [rx, ry]

# Update enclosure dome peak
d["enclosure"]["top_surface"]["peak_x_mm"] = cx
d["enclosure"]["top_surface"]["peak_y_mm"] = cy

# Reset UI placements to original values then scale gently
original_placements = [
    {"instance_id": "btn_power", "catalog_id": "tactile_button_6x6", "x_mm": 40, "y_mm": 35},
    {"instance_id": "btn_vol_up", "catalog_id": "tactile_button_6x6", "x_mm": 28, "y_mm": 55},
    {"instance_id": "btn_vol_down", "catalog_id": "tactile_button_6x6", "x_mm": 52, "y_mm": 55},
    {"instance_id": "ir_led", "catalog_id": "led_5mm", "x_mm": 40, "y_mm": 5,
     "mounting_style": "side", "edge_index": 16},
]
d["ui_placements"] = original_placements

# Scale UI placements — use a gentler scale so they stay well inside the outline
old_cx, old_cy = 40, 58
scale = 0.9  # keep placements closer to center than the full outline scale
for p in d["ui_placements"]:
    p["x_mm"] = round(cx + (p["x_mm"] - old_cx) * scale, 1)
    p["y_mm"] = round(cy + (p["y_mm"] - old_cy) * scale, 1)

# Regenerate outline polygon (32 points on ellipse)
n = 32
outline = []
for i in range(n):
    angle = 2 * math.pi * i / n
    x = round(cx + rx * math.cos(angle), 2)
    y = round(cy + ry * math.sin(angle), 2)
    outline.append({"x": x, "y": y})
d["outline"] = outline

# Verify area with shoelace formula
area = 0.0
for i in range(len(outline)):
    j = (i + 1) % len(outline)
    area += outline[i]["x"] * outline[j]["y"]
    area -= outline[j]["x"] * outline[i]["y"]
area = abs(area) / 2

xs = [v["x"] for v in outline]
ys = [v["y"] for v in outline]
print(f"New outline: {max(xs)-min(xs):.0f} x {max(ys)-min(ys):.0f} mm")
print(f"Outline area: {area:.0f} mm^2 (need ~7432)")
for p in d["ui_placements"]:
    print(f"  {p['instance_id']}: ({p['x_mm']}, {p['y_mm']})")

s.write_artifact("design.json", d)
s.save()
print("Saved updated design.json")
