"""Post-route Design Rule Check (DRC) for routing results."""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field

from shapely.geometry import Polygon

from src.pipeline.config import TRACE_RULES
from src.pipeline.trace_geometry import point_seg_dist
from src.pipeline.router.models import RoutingResult, RouterConfig

log = logging.getLogger(__name__)


@dataclass
class Violation:
    rule: str
    severity: str
    net_id: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class DRCReport:
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        lines = [
            "DRC: {} errors, {} warnings".format(
                len(self.errors), len(self.warnings)
            )
        ]
        for v in self.violations:
            marker = "ERROR" if v.severity == "error" else "WARN "
            lines.append("  [{}] {} | {}: {}".format(marker, v.rule, v.net_id, v.message))
        return "\n".join(lines)


def run_drc(
    result: RoutingResult,
    pin_positions: dict[str, tuple[float, float]],
    outline_poly: Polygon | None = None,
    config: RouterConfig | None = None,
) -> DRCReport:
    if config is None:
        config = RouterConfig()

    report = DRCReport()
    _check_pin_conflicts(result, report)
    net_pins = _build_net_pin_set(result, pin_positions)
    _check_trace_to_pin_clearance(result, pin_positions, net_pins, config, report)
    _check_trace_to_trace_clearance(result, config, report)
    _check_path_efficiency(result, report)
    if outline_poly is not None:
        _check_edge_clearance(result, outline_poly, config, report)
    return report


def _build_net_pin_set(
    result: RoutingResult,
    pin_positions: dict[str, tuple[float, float]],
) -> dict[str, set[str]]:
    net_pins: dict[str, set[str]] = {}
    for key, val in result.pin_assignments.items():
        net_id = key.split("|")[0]
        net_pins.setdefault(net_id, set()).add(val)

    endpoint_tol = 0.6
    for trace in result.traces:
        nid = trace.net_id
        own = net_pins.setdefault(nid, set())
        if not trace.path:
            continue
        endpoints = [trace.path[0], trace.path[-1]]
        for pin_key, (px, py) in pin_positions.items():
            for ex, ey in endpoints:
                if math.hypot(px - ex, py - ey) <= endpoint_tol:
                    own.add(pin_key)
                    break
    return net_pins


def _check_pin_conflicts(result: RoutingResult, report: DRCReport) -> None:
    pin_to_nets: dict[str, list[str]] = {}
    for key, physical_pin in result.pin_assignments.items():
        net_id = key.split("|")[0]
        pin_to_nets.setdefault(physical_pin, []).append(net_id)

    for physical_pin, nets in pin_to_nets.items():
        unique_nets = set(nets)
        if len(unique_nets) > 1:
            report.violations.append(Violation(
                rule="pin_conflict",
                severity="error",
                net_id=", ".join(sorted(unique_nets)),
                message="Physical pin {} assigned to multiple nets: {}".format(
                    physical_pin, sorted(unique_nets)
                ),
                details={"pin": physical_pin, "nets": sorted(unique_nets)},
            ))


def _is_snap_seg(seg_idx: int, n_segs: int) -> bool:
    return seg_idx <= 1 or seg_idx >= n_segs - 2


def _check_trace_to_pin_clearance(
    result: RoutingResult,
    pin_positions: dict[str, tuple[float, float]],
    net_pins: dict[str, set[str]],
    config: RouterConfig,
    report: DRCReport,
) -> None:
    min_clearance = config.pin_clearance_mm + config.trace_width_mm / 2
    grid_tolerance = config.grid_resolution_mm * math.sqrt(2)

    for trace in result.traces:
        own_pins = net_pins.get(trace.net_id, set())
        path = trace.path
        n_seg = len(path) - 1
        if n_seg < 1:
            continue

        for pin_key, (px, py) in pin_positions.items():
            if pin_key in own_pins:
                continue

            best = float("inf")
            for i in range(n_seg):
                ax, ay = path[i]
                bx, by = path[i + 1]
                d = point_seg_dist(px, py, ax, ay, bx, by)
                if d < best:
                    best = d

            if best < min_clearance - grid_tolerance:
                report.violations.append(Violation(
                    rule="trace_pin_clearance",
                    severity="error",
                    net_id=trace.net_id,
                    message="Trace passes {:.2f}mm from pin {} (min {:.2f}mm required)".format(
                        best, pin_key, min_clearance
                    ),
                    details={
                        "pin": pin_key,
                        "distance_mm": round(best, 3),
                        "required_mm": round(min_clearance, 3),
                    },
                ))


def _check_trace_to_trace_clearance(
    result: RoutingResult,
    config: RouterConfig,
    report: DRCReport,
) -> None:
    min_clearance = config.trace_clearance_mm + config.trace_width_mm
    grid_tolerance = config.grid_resolution_mm * math.sqrt(2)

    traces_by_net: dict[str, list[list[tuple[float, float]]]] = {}
    for trace in result.traces:
        traces_by_net.setdefault(trace.net_id, []).append(trace.path)

    net_ids = list(traces_by_net.keys())
    checked: set[tuple[str, str]] = set()

    for i, net_a in enumerate(net_ids):
        for j in range(i + 1, len(net_ids)):
            net_b = net_ids[j]
            pair = (min(net_a, net_b), max(net_a, net_b))
            if pair in checked:
                continue
            checked.add(pair)

            dist = _min_trace_distance(
                traces_by_net[net_a], traces_by_net[net_b],
            )
            if dist < min_clearance - grid_tolerance:
                report.violations.append(Violation(
                    rule="trace_trace_clearance",
                    severity="error",
                    net_id="{} <-> {}".format(net_a, net_b),
                    message="Traces {} and {} are {:.2f}mm apart (min {:.2f}mm required)".format(
                        net_a, net_b, dist, min_clearance
                    ),
                    details={
                        "net_a": net_a, "net_b": net_b,
                        "distance_mm": round(dist, 3),
                        "required_mm": round(min_clearance, 3),
                    },
                ))


def _min_trace_distance(
    paths_a: list[list[tuple[float, float]]],
    paths_b: list[list[tuple[float, float]]],
) -> float:
    best = float("inf")
    for pa in paths_a:
        for pb in paths_b:
            for i in range(len(pa) - 1):
                for j in range(len(pb) - 1):
                    d = _seg_seg_dist(
                        pa[i][0], pa[i][1], pa[i+1][0], pa[i+1][1],
                        pb[j][0], pb[j][1], pb[j+1][0], pb[j+1][1],
                    )
                    if d < best:
                        best = d
                    if best < 1e-9:
                        return best
    return best


def _seg_seg_dist(
    ax1: float, ay1: float, ax2: float, ay2: float,
    bx1: float, by1: float, bx2: float, by2: float,
) -> float:
    return min(
        point_seg_dist(ax1, ay1, bx1, by1, bx2, by2),
        point_seg_dist(ax2, ay2, bx1, by1, bx2, by2),
        point_seg_dist(bx1, by1, ax1, ay1, ax2, ay2),
        point_seg_dist(bx2, by2, ax1, ay1, ax2, ay2),
    )


def _check_path_efficiency(result: RoutingResult, report: DRCReport) -> None:
    base_threshold = 3.0
    traces_by_net: dict[str, list[list[tuple[float, float]]]] = {}
    for trace in result.traces:
        traces_by_net.setdefault(trace.net_id, []).append(trace.path)

    for net_id, paths in traces_by_net.items():
        pin_proxy = len(paths) + 1
        threshold = base_threshold + 0.5 * math.log2(max(2, pin_proxy))

        all_pts: list[tuple[float, float]] = []
        total_len = 0.0
        for path in paths:
            all_pts.extend(path)
            for i in range(len(path) - 1):
                dx = path[i+1][0] - path[i][0]
                dy = path[i+1][1] - path[i][1]
                total_len += math.hypot(dx, dy)

        if not all_pts or total_len < 1e-6:
            continue
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        if hpwl < 1e-6:
            continue
        ratio = total_len / hpwl
        if ratio > threshold:
            report.violations.append(Violation(
                rule="path_efficiency",
                severity="warning",
                net_id=net_id,
                message="Trace length {:.1f}mm is {:.1f}x the HPWL {:.1f}mm (threshold {:.0f}x)".format(
                    total_len, ratio, hpwl, threshold
                ),
                details={
                    "trace_length_mm": round(total_len, 2),
                    "hpwl_mm": round(hpwl, 2),
                    "ratio": round(ratio, 2),
                },
            ))


def _check_edge_clearance(
    result: RoutingResult,
    outline_poly: Polygon,
    config: RouterConfig,
    report: DRCReport,
) -> None:
    min_clearance = config.edge_clearance_mm
    boundary = outline_poly.boundary

    for trace in result.traces:
        for point in trace.path:
            from shapely.geometry import Point as ShapelyPoint
            dist = boundary.distance(ShapelyPoint(point[0], point[1]))
            if dist < min_clearance:
                report.violations.append(Violation(
                    rule="edge_clearance",
                    severity="error",
                    net_id=trace.net_id,
                    message="Trace at ({:.2f}, {:.2f}) is {:.2f}mm from edge (min {:.2f}mm)".format(
                        point[0], point[1], dist, min_clearance
                    ),
                    details={
                        "point": list(point),
                        "distance_mm": round(dist, 3),
                        "required_mm": round(min_clearance, 3),
                    },
                ))
                break
