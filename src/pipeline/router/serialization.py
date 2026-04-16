"""Routing serialization — JSON conversion."""

from __future__ import annotations

from shapely.geometry import Polygon

from src.pipeline.design.models import Net

from .models import Trace, InflatedTrace, RoutingResult


def routing_to_dict(result: RoutingResult) -> dict:
    """Serialize a RoutingResult to a JSON-safe dict."""
    return {
        "traces": [
            {
                "net_id": t.net_id,
                "path": [list(p) for p in t.path],
            }
            for t in result.traces
        ],
        "pin_assignments": dict(result.pin_assignments),
        "failed_nets": list(result.failed_nets),
    }


def parse_routing(data: dict) -> RoutingResult:
    """Parse a routing.json dict back into a RoutingResult."""
    traces = [
        Trace(
            net_id=t["net_id"],
            path=[tuple(p) for p in t["path"]],
        )
        for t in data.get("traces", [])
    ]

    pin_assignments = dict(data.get("pin_assignments", {}))
    failed_nets = list(data.get("failed_nets", []))

    return RoutingResult(
        traces=traces,
        pin_assignments=pin_assignments,
        failed_nets=failed_nets,
    )
