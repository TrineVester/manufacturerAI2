"""Shared profiling infrastructure for sunburst visualizations.

Provides HTimer (hierarchical timer), TimerNode, and HTML generation.
Both the router and placer profilers import from here.
"""

from __future__ import annotations

import functools
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class TimerNode:
    name: str
    elapsed: float = 0.0
    children: list[TimerNode] = field(default_factory=list)
    _child_map: dict[str, TimerNode] = field(default_factory=dict, repr=False)
    call_count: int = 0

    def child(self, name: str) -> TimerNode:
        if name not in self._child_map:
            node = TimerNode(name=name)
            self._child_map[name] = node
            self.children.append(node)
        return self._child_map[name]

    def add_child_node(self, node: TimerNode) -> None:
        existing = self._child_map.get(node.name)
        if existing:
            existing.elapsed += node.elapsed
            existing.call_count += node.call_count
            for c in node.children:
                existing.add_child_node(c)
        else:
            self._child_map[node.name] = node
            self.children.append(node)

    @property
    def self_time(self) -> float:
        return max(0.0, self.elapsed - sum(c.elapsed for c in self.children))


class HTimer:
    """Hierarchical timer with context-manager and decorator support."""

    def __init__(self):
        self.root = TimerNode(name="total")
        self._stack: list[TimerNode] = [self.root]
        self._t_stack: list[float] = []

    @contextmanager
    def section(self, name: str):
        parent = self._stack[-1]
        node = parent.child(name)
        node.call_count += 1
        self._stack.append(node)
        t0 = time.perf_counter()
        try:
            yield node
        finally:
            dt = time.perf_counter() - t0
            node.elapsed += dt
            self._stack.pop()

    def wrap(self, name: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                with self.section(name):
                    return fn(*a, **kw)
            return wrapper
        return decorator

    def current(self) -> TimerNode:
        return self._stack[-1]


def _flatten_for_sunburst(
    node: TimerNode,
    parent_id: str = "",
    ids: list | None = None,
    labels: list | None = None,
    parents: list | None = None,
    values: list | None = None,
    texts: list | None = None,
    colors: list | None = None,
    depth: int = 0,
):
    if ids is None:
        ids, labels, parents, values, texts, colors = [], [], [], [], [], []

    node_id = f"{parent_id}/{node.name}" if parent_id else node.name

    ids.append(node_id)
    labels.append(node.name)
    parents.append(parent_id)
    values.append(round(node.elapsed * 1000, 1))

    pct = ""
    if parent_id:
        pct_val = node.elapsed / max(1e-9, _find_node(ids, parents, values, parent_id))
        pct = f" ({pct_val*100:.1f}%)"

    calls_str = f"  [{node.call_count}x]" if node.call_count > 1 else ""
    texts.append(f"{node.elapsed*1000:.1f}ms{pct}{calls_str}")

    colors.append(_name_color_map.get(node.name, "#bab0ac") if parent_id else "#ffffff")

    for child in node.children:
        _flatten_for_sunburst(
            child, node_id, ids, labels, parents, values, texts, colors, depth + 1,
        )

    if node.children:
        node_idx = ids.index(node_id)
        parent_val = values[node_idx]
        children_sum = round(sum(
            values[i] for i, p in enumerate(parents) if p == node_id
        ), 1)
        remainder = round(parent_val - children_sum, 1)
        if remainder > 0:
            uid = f"{node_id}/(other)"
            ids.append(uid)
            labels.append("(other)")
            parents.append(node_id)
            values.append(remainder)
            u_pct = remainder / max(1e-9, parent_val) * 100
            texts.append(f"{remainder:.1f}ms ({u_pct:.1f}%)")
            colors.append("#dddddd")
        elif remainder < 0:
            values[node_idx] = children_sum

    return ids, labels, parents, values, texts, colors


def _find_node(ids, parents, values, target_id):
    for i, id_ in enumerate(ids):
        if id_ == target_id:
            return values[i] / 1000.0
    return 1.0


def _tree_to_dict(node: TimerNode) -> dict:
    return {
        "name": node.name,
        "elapsed": node.elapsed,
        "call_count": node.call_count,
        "children": [_tree_to_dict(c) for c in node.children],
    }


def _collect_all_names(node: TimerNode, out: set[str] | None = None) -> set[str]:
    if out is None:
        out = set()
    out.add(node.name)
    for c in node.children:
        _collect_all_names(c, out)
    return out


def _build_name_color_map(root: TimerNode) -> dict[str, str]:
    names = _collect_all_names(root) - {root.name, "(other)", "(self)"}
    sorted_names = sorted(names)
    n = len(sorted_names)
    color_map = {}
    for i, name in enumerate(sorted_names):
        hue = (i / n * 360) if n else 0
        color_map[name] = f"hsl({hue:.0f}, 65%, 55%)"
    color_map["(other)"] = "#dddddd"
    color_map["(self)"] = "#cccccc"
    return color_map


_name_color_map: dict[str, str] = {}


def build_sunburst_html(root: TimerNode, title: str = "Profile") -> str:
    global _name_color_map
    _name_color_map = _build_name_color_map(root)
    import json as _json
    tree_json = _json.dumps(_tree_to_dict(root))

    all_nodes: list[tuple[str, float, int]] = []
    _collect_leaf_times(root, all_nodes)
    merged: dict[str, tuple[float, int]] = {}
    for name, elapsed, calls in all_nodes:
        t, c = merged.get(name, (0.0, 0))
        merged[name] = (t + elapsed, c + calls)
    rows = sorted(merged.items(), key=lambda x: -x[1][0])
    table_rows = "".join(
        f"<tr><td>{name}</td><td>{t*1000:.1f}</td><td>{c}</td>"
        f"<td>{t*1000/c:.2f}</td></tr>"
        for name, (t, c) in rows if c > 0
    )

    return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #fafafa; }}
.container {{ display: flex; gap: 20px; max-width: 1400px; margin: 0 auto; }}
.sidebar {{ width: 280px; flex-shrink: 0; }}
.sidebar h3 {{ margin: 0 0 8px; font-size: 14px; color: #555; }}
.node-list {{ max-height: 700px; overflow-y: auto; border: 1px solid #ddd; border-radius: 6px; background: #fff; padding: 6px; }}
.node-item {{ display: flex; align-items: center; gap: 6px; padding: 4px 6px; border-radius: 4px; cursor: pointer; font-size: 13px; user-select: none; }}
.node-item:hover {{ background: #f0f4ff; }}
.node-item.selected {{ background: #e0e8ff; font-weight: 600; }}
.node-item .time {{ color: #888; margin-left: auto; font-size: 11px; white-space: nowrap; }}
.chart {{ flex: 1; min-width: 0; }}
.controls {{ display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }}
.controls button {{ padding: 5px 14px; border: 1px solid #ccc; border-radius: 4px; background: #fff; cursor: pointer; font-size: 13px; }}
.controls button:hover {{ background: #f0f0f0; }}
.controls .depth-label {{ font-size: 13px; color: #555; }}
.controls input[type=range] {{ width: 100px; }}
h1 {{ font-size: 20px; margin: 0 0 16px; text-align: center; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ padding: 4px 8px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }}
th {{ background: #f0f0f0; }}
td:nth-child(2), td:nth-child(3), td:nth-child(4),
th:nth-child(2), th:nth-child(3), th:nth-child(4) {{ text-align: right; }}
</style>
</head><body>
<h1>{title}</h1>
<div class="container">
  <div class="sidebar">
    <h3>Select root node(s)</h3>
    <div class="controls">
      <button id="btn-reset">Reset</button>
      <button id="btn-all">Select all</button>
    </div>
    <div class="node-list" id="node-list"></div>
  </div>
  <div class="chart">
    <div class="controls">
      <span class="depth-label">Depth:</span>
      <input type="range" id="depth-slider" min="2" max="10" value="3">
      <span id="depth-val">3</span>
    </div>
    <div id="sunburst"></div>
  </div>
</div>
<div style="max-width:800px;margin:30px auto">
  <h2 style="font-size:16px">All categories sorted by total time</h2>
  <table>
    <tr><th>Category</th><th>Total (ms)</th><th>Calls</th><th>Avg (ms)</th></tr>
    {table_rows}
  </table>
</div>
<script>
const TREE = {tree_json};
function buildColorMap(node, names) {{
  if (node.name !== "(other)" && node.name !== "(self)") names.add(node.name);
  for (const c of node.children) buildColorMap(c, names);
}}
const _allNames = new Set();
buildColorMap(TREE, _allNames);
_allNames.delete(TREE.name);
const _sortedNames = [..._allNames].sort();
const NAME_COLOR = {{}};
for (let i = 0; i < _sortedNames.length; i++) {{
  const hue = (_sortedNames.length > 0) ? (i / _sortedNames.length * 360) : 0;
  NAME_COLOR[_sortedNames[i]] = "hsl(" + hue.toFixed(0) + ", 65%, 55%)";
}}
NAME_COLOR["(other)"] = "#dddddd";
NAME_COLOR["(self)"] = "#cccccc";

function collectNodes(node, path, depth, out) {{
  const id = path ? path + "/" + node.name : node.name;
  out.push({{ id, name: node.name, elapsed: node.elapsed, calls: node.call_count, depth, node }});
  for (const c of node.children) collectNodes(c, id, depth + 1, out);
}}

function findByName(node, name, results) {{
  if (node.name === name) results.push(node);
  for (const c of node.children) findByName(c, name, results);
}}

function flattenNode(node, parentId, depth, ids, labels, parents, values, texts, colors) {{
  const nodeId = parentId ? parentId + "/" + node.name : node.name;
  const ms = Math.round(node.elapsed * 1000 * 10) / 10;
  const callStr = node.call_count > 1 ? "  [" + node.call_count + "x]" : "";
  const nodeIdx = ids.length;
  ids.push(nodeId); labels.push(node.name); parents.push(parentId);
  values.push(ms); texts.push(ms.toFixed(1) + "ms" + callStr);
  colors.push(parentId ? (NAME_COLOR[node.name] || "#bab0ac") : "#ffffff");
  let childSum = 0;
  for (const c of node.children) {{
    childSum += flattenNode(c, nodeId, depth + 1, ids, labels, parents, values, texts, colors);
  }}
  if (node.children.length > 0) {{
    const remainder = Math.round((ms - childSum) * 10) / 10;
    if (remainder > 0) {{
      const uid = nodeId + "/(other)";
      ids.push(uid); labels.push("(other)"); parents.push(nodeId);
      values.push(remainder); texts.push(remainder.toFixed(1) + "ms");
      colors.push("#dddddd");
      return ms;
    }} else if (remainder < 0) {{
      values[nodeIdx] = Math.round(childSum * 10) / 10;
      return values[nodeIdx];
    }}
  }}
  return ms;
}}

function mergeNodes(nodes) {{
  const merged = {{ name: "selected", elapsed: 0, call_count: 0, children: [] }};
  const childMap = {{}};
  for (const n of nodes) {{
    merged.elapsed += n.elapsed;
    merged.call_count += n.call_count;
    for (const c of n.children) {{
      if (childMap[c.name]) {{
        childMap[c.name] = mergeTwo(childMap[c.name], c);
      }} else {{
        childMap[c.name] = deepCopy(c);
      }}
    }}
    const selfTime = n.elapsed - n.children.reduce((s, c) => s + c.elapsed, 0);
    if (selfTime > 0.0005 && n.children.length > 0) {{
      if (childMap["(self)"]) {{
        childMap["(self)"].elapsed += selfTime;
        childMap["(self)"].call_count += Math.max(n.call_count, 1);
      }} else {{
        childMap["(self)"] = {{ name: "(self)", elapsed: selfTime, call_count: Math.max(n.call_count, 1), children: [] }};
      }}
    }}
  }}
  merged.children = Object.values(childMap).sort((a, b) => b.elapsed - a.elapsed);
  return merged;
}}

function mergeTwo(a, b) {{
  const merged = {{ name: a.name, elapsed: a.elapsed + b.elapsed, call_count: a.call_count + b.call_count, children: [] }};
  const childMap = {{}};
  for (const c of a.children) childMap[c.name] = deepCopy(c);
  for (const c of b.children) {{
    if (childMap[c.name]) childMap[c.name] = mergeTwo(childMap[c.name], c);
    else childMap[c.name] = deepCopy(c);
  }}
  merged.children = Object.values(childMap).sort((a, b) => b.elapsed - a.elapsed);
  return merged;
}}

function deepCopy(n) {{
  return {{ name: n.name, elapsed: n.elapsed, call_count: n.call_count, children: n.children.map(deepCopy) }};
}}

const allFlat = [];
collectNodes(TREE, "", 0, allFlat);
const nameMap = {{}};
for (const n of allFlat) {{
  if (n.node.children.length === 0) continue;
  if (!nameMap[n.name]) nameMap[n.name] = {{ elapsed: 0, calls: 0 }};
  nameMap[n.name].elapsed += n.elapsed;
  nameMap[n.name].calls += n.calls;
}}
const nameList = Object.entries(nameMap).sort((a, b) => b[1].elapsed - a[1].elapsed);

const listEl = document.getElementById("node-list");
const selected = new Set();

for (const [name, info] of nameList) {{
  const div = document.createElement("div");
  div.className = "node-item";
  div.dataset.name = name;
  div.innerHTML = '<span>' + name + '</span><span class="time">' + (info.elapsed * 1000).toFixed(1) + 'ms</span>';
  div.addEventListener("click", () => {{
    if (selected.has(name)) selected.delete(name); else selected.add(name);
    div.classList.toggle("selected");
    rebuild();
  }});
  listEl.appendChild(div);
}}

let maxDepth = 3;
const depthSlider = document.getElementById("depth-slider");
const depthVal = document.getElementById("depth-val");
depthSlider.addEventListener("input", () => {{
  maxDepth = parseInt(depthSlider.value);
  depthVal.textContent = maxDepth;
  rebuild();
}});

document.getElementById("btn-reset").addEventListener("click", () => {{
  selected.clear();
  listEl.querySelectorAll(".node-item").forEach(el => el.classList.remove("selected"));
  rebuild();
}});
document.getElementById("btn-all").addEventListener("click", () => {{
  listEl.querySelectorAll(".node-item").forEach(el => {{
    selected.add(el.dataset.name);
    el.classList.add("selected");
  }});
  rebuild();
}});

function rebuild() {{
  let root;
  if (selected.size === 0) {{
    root = TREE;
  }} else {{
    const matches = [];
    for (const name of selected) {{
      findByName(TREE, name, matches);
    }}
    if (matches.length === 0) {{ root = TREE; }}
    else if (matches.length === 1) {{ root = deepCopy(matches[0]); }}
    else {{ root = mergeNodes(matches); }}
  }}
  const ids = [], labels = [], parents = [], values = [], texts = [], colors = [];
  flattenNode(root, "", 0, ids, labels, parents, values, texts, colors);
  Plotly.react("sunburst", [{{
    type: "sunburst", ids, labels, parents, values, text: texts,
    branchvalues: "total",
    hovertemplate: "<b>%{{label}}</b><br>%{{text}}<extra></extra>",
    textinfo: "label+text", insidetextorientation: "radial",
    marker: {{ colors, line: {{ width: 1, color: "white" }} }},
    maxdepth: maxDepth,
  }}], {{
    margin: {{ t: 10, l: 10, r: 10, b: 10 }},
    width: 900, height: 800,
  }});
}}
rebuild();
</script>
</body></html>'''


def print_tree(node: TimerNode, indent: int = 0, parent_elapsed: float = 0):
    pct = f" ({node.elapsed / parent_elapsed * 100:5.1f}%)" if parent_elapsed > 0 else ""
    calls = f" [{node.call_count}x]" if node.call_count > 1 else ""
    print(f"{'  ' * indent}{node.name}: {node.elapsed * 1000:.1f}ms{pct}{calls}")
    for child in sorted(node.children, key=lambda c: -c.elapsed):
        print_tree(child, indent + 1, node.elapsed)
    if node.children:
        st = node.self_time
        if st > 0.5e-3:
            pct2 = f" ({st / node.elapsed * 100:5.1f}%)" if node.elapsed > 0 else ""
            print(f"{'  ' * (indent + 1)}(other): {st * 1000:.1f}ms{pct2}")


def _collect_leaf_times(node: TimerNode, out: list[tuple[str, float, int]]):
    if not node.children:
        out.append((node.name, node.elapsed, max(node.call_count, 1)))
    else:
        for child in node.children:
            _collect_leaf_times(child, out)
        st = node.self_time
        if st > 0:
            out.append((f"{node.name} (self)", st, max(node.call_count, 1)))


def _collect_all_nodes(
    node: TimerNode,
    parent_name: str,
    path: str,
    out: list[dict],
):
    node_path = f"{path}/{node.name}" if path else node.name
    out.append({
        "name": node.name,
        "parent": parent_name,
        "path": node_path,
        "elapsed": node.elapsed,
        "self_time": node.self_time,
        "calls": max(node.call_count, 1),
        "is_leaf": len(node.children) == 0,
    })
    for c in node.children:
        _collect_all_nodes(c, node.name, node_path, out)


def build_insights_html(root: TimerNode, title: str = "Profile Insights") -> str:
    import json as _json

    all_nodes: list[dict] = []
    _collect_all_nodes(root, "", "", all_nodes)

    color_map = _build_name_color_map(root)

    cat_agg: dict[str, dict] = {}
    for n in all_nodes:
        name = n["name"]
        if name == root.name:
            continue
        if name not in cat_agg:
            cat_agg[name] = {"total": 0.0, "self": 0.0, "calls": 0, "callers": {}}
        cat_agg[name]["total"] += n["elapsed"]
        cat_agg[name]["self"] += n["self_time"]
        cat_agg[name]["calls"] += n["calls"]
        p = n["parent"]
        if p and p != root.name:
            if p not in cat_agg[name]["callers"]:
                cat_agg[name]["callers"][p] = {"total": 0.0, "calls": 0}
            cat_agg[name]["callers"][p]["total"] += n["elapsed"]
            cat_agg[name]["callers"][p]["calls"] += n["calls"]

    sorted_cats = sorted(cat_agg.items(), key=lambda x: -x[1]["total"])

    bar_data = []
    for name, info in sorted_cats[:40]:
        bar_data.append({
            "name": name,
            "total_ms": round(info["total"] * 1000, 1),
            "self_ms": round(info["self"] * 1000, 1),
            "calls": info["calls"],
            "avg_ms": round(info["total"] / max(info["calls"], 1) * 1000, 3),
            "color": color_map.get(name, "#bab0ac"),
        })

    scatter_data = []
    for name, info in cat_agg.items():
        if info["calls"] == 0:
            continue
        scatter_data.append({
            "name": name,
            "calls": info["calls"],
            "avg_ms": round(info["total"] / info["calls"] * 1000, 4),
            "total_ms": round(info["total"] * 1000, 1),
            "self_ms": round(info["self"] * 1000, 1),
            "color": color_map.get(name, "#bab0ac"),
        })

    caller_data: dict[str, list] = {}
    for name, info in sorted_cats[:30]:
        callers = sorted(info["callers"].items(), key=lambda x: -x[1]["total"])[:5]
        if callers:
            caller_data[name] = [
                {"parent": p, "ms": round(v["total"] * 1000, 1), "calls": v["calls"]}
                for p, v in callers
            ]

    total_ms = round(root.elapsed * 1000, 1)

    return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #fafafa; color: #333; }}
h1 {{ font-size: 22px; text-align: center; margin: 0 0 4px; }}
.subtitle {{ text-align: center; color: #888; font-size: 14px; margin-bottom: 20px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; max-width: 1600px; margin: 0 auto; }}
.panel {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; }}
.panel h2 {{ font-size: 15px; margin: 0 0 4px; color: #444; }}
.panel .desc {{ font-size: 12px; color: #999; margin: 0 0 12px; }}
.full-width {{ grid-column: 1 / -1; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ padding: 5px 10px; text-align: left; border-bottom: 1px solid #f0f0f0; }}
th {{ background: #f8f8f8; font-weight: 600; position: sticky; top: 0; }}
td:nth-child(n+2), th:nth-child(n+2) {{ text-align: right; }}
tr:hover {{ background: #f5f8ff; }}
.swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: middle; }}
.caller-section {{ margin-bottom: 12px; }}
.caller-section h3 {{ font-size: 13px; margin: 0 0 4px; }}
.caller-section .bar {{ height: 6px; border-radius: 3px; margin-top: 2px; }}
.caller-row {{ display: flex; align-items: center; gap: 8px; font-size: 12px; padding: 2px 0; }}
.caller-row .label {{ min-width: 120px; }}
.caller-row .value {{ color: #888; white-space: nowrap; }}
.scroll-box {{ max-height: 600px; overflow-y: auto; }}
</style>
</head><body>
<h1>{title}</h1>
<p class="subtitle">Total: {total_ms:.1f}ms</p>
<div class="grid">
<div class="panel">
  <h2>Time by Category (aggregated)</h2>
  <p class="desc">All instances of each operation summed. Self-time = time not in children.</p>
  <div id="bar-chart"></div>
</div>
<div class="panel">
  <h2>Optimization Quadrant</h2>
  <p class="desc">X = call count, Y = avg ms/call, bubble size = total time. Top-right = worst offenders.</p>
  <div id="scatter-chart"></div>
</div>
<div class="panel full-width">
  <h2>Top Callers per Category</h2>
  <p class="desc">Which parent operations contribute the most time to each category.</p>
  <div class="scroll-box" id="caller-breakdown"></div>
</div>
<div class="panel full-width">
  <h2>Full Breakdown Table</h2>
  <p class="desc">All categories sorted by total time. "Self %" = fraction of time not delegated to children.</p>
  <div class="scroll-box">
    <table id="full-table">
      <tr>
        <th>Category</th><th>Total (ms)</th><th>Self (ms)</th><th>Self %</th>
        <th>Calls</th><th>Avg (ms)</th><th>% of Total</th>
      </tr>
    </table>
  </div>
</div>
</div>
<script>
const BAR = {_json.dumps(bar_data)};
const SCATTER = {_json.dumps(scatter_data)};
const CALLERS = {_json.dumps(caller_data)};
const TOTAL_MS = {total_ms};

// --- Bar chart ---
Plotly.newPlot("bar-chart", [
  {{
    type: "bar", orientation: "h",
    y: BAR.map(d => d.name).reverse(),
    x: BAR.map(d => d.self_ms).reverse(),
    name: "Self time",
    marker: {{ color: BAR.map(d => d.color).reverse(), opacity: 0.9 }},
    hovertemplate: "<b>%{{y}}</b><br>Self: %{{x:.1f}}ms<extra></extra>",
  }},
  {{
    type: "bar", orientation: "h",
    y: BAR.map(d => d.name).reverse(),
    x: BAR.map(d => Math.max(0, d.total_ms - d.self_ms)).reverse(),
    name: "Child time",
    marker: {{ color: BAR.map(d => d.color).reverse(), opacity: 0.35 }},
    hovertemplate: "<b>%{{y}}</b><br>Child: %{{x:.1f}}ms<extra></extra>",
  }}
], {{
  barmode: "stack",
  margin: {{ l: 140, r: 20, t: 10, b: 40 }},
  height: Math.max(400, BAR.length * 22),
  xaxis: {{ title: "ms" }},
  yaxis: {{ automargin: true, tickfont: {{ size: 12 }} }},
  legend: {{ orientation: "h", y: -0.08 }},
  hoverlabel: {{ font: {{ size: 12 }} }},
}});

// --- Scatter chart ---
const scFiltered = SCATTER.filter(d => d.total_ms > 0.1);
Plotly.newPlot("scatter-chart", [{{
  type: "scatter", mode: "markers+text",
  x: scFiltered.map(d => d.calls),
  y: scFiltered.map(d => d.avg_ms),
  text: scFiltered.map(d => d.name),
  textposition: "top center",
  textfont: {{ size: 10 }},
  marker: {{
    size: scFiltered.map(d => Math.max(8, Math.min(60, Math.sqrt(d.total_ms) * 2))),
    color: scFiltered.map(d => d.color),
    opacity: 0.8,
    line: {{ width: 1, color: "#fff" }},
  }},
  hovertemplate: "<b>%{{text}}</b><br>Calls: %{{x}}<br>Avg: %{{y:.3f}}ms<br>Total: %{{customdata:.1f}}ms<extra></extra>",
  customdata: scFiltered.map(d => d.total_ms),
}}], {{
  margin: {{ l: 60, r: 20, t: 20, b: 50 }},
  height: 500,
  xaxis: {{ title: "Call count", type: "log" }},
  yaxis: {{ title: "Avg time per call (ms)", type: "log" }},
  hoverlabel: {{ font: {{ size: 12 }} }},
  showlegend: false,
}});

// --- Caller breakdown ---
const callerEl = document.getElementById("caller-breakdown");
const catNames = Object.keys(CALLERS);
for (const cat of catNames) {{
  const entries = CALLERS[cat];
  const maxMs = entries[0]?.ms || 1;
  let html = '<div class="caller-section"><h3><span class="swatch" style="background:' +
    (BAR.find(b => b.name === cat)?.color || "#bab0ac") + '"></span>' + cat + '</h3>';
  for (const e of entries) {{
    const pct = (e.ms / TOTAL_MS * 100).toFixed(1);
    const w = (e.ms / maxMs * 100).toFixed(0);
    html += '<div class="caller-row">' +
      '<span class="label">&larr; ' + e.parent + '</span>' +
      '<span class="value">' + e.ms.toFixed(1) + 'ms (' + pct + '%) [' + e.calls + 'x]</span>' +
      '</div>' +
      '<div class="bar" style="width:' + w + '%;background:' +
      (BAR.find(b => b.name === cat)?.color || "#bab0ac") + ';opacity:0.5"></div>';
  }}
  html += '</div>';
  callerEl.innerHTML += html;
}}

// --- Full table ---
const tbl = document.getElementById("full-table");
const allCats = BAR.slice();
for (const d of SCATTER) {{
  if (!allCats.find(b => b.name === d.name)) allCats.push(d);
}}
allCats.sort((a, b) => b.total_ms - a.total_ms);
for (const d of allCats) {{
  const selfPct = d.total_ms > 0 ? (d.self_ms / d.total_ms * 100).toFixed(0) : "—";
  const totalPct = (d.total_ms / TOTAL_MS * 100).toFixed(1);
  const tr = document.createElement("tr");
  tr.innerHTML = '<td><span class="swatch" style="background:' + d.color + '"></span>' + d.name + '</td>' +
    '<td>' + d.total_ms.toFixed(1) + '</td>' +
    '<td>' + d.self_ms.toFixed(1) + '</td>' +
    '<td>' + selfPct + '%</td>' +
    '<td>' + d.calls.toLocaleString() + '</td>' +
    '<td>' + d.avg_ms.toFixed(3) + '</td>' +
    '<td>' + totalPct + '%</td>';
  tbl.appendChild(tr);
}}
</script>
</body></html>'''
