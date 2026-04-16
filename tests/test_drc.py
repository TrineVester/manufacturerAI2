"""Tests for the post-route Design Rule Check (DRC).

Uses the flashlight fixture for a lightweight integration test and
the large fixture for a comprehensive stress test.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from src.catalog.loader import load_catalog
from src.pipeline.placer import place_components
from src.pipeline.router import route_traces
from src.pipeline.router.drc import run_drc, DRCReport, Violation
from src.pipeline.router.engine import _collect_pin_positions
from src.pipeline.router.models import RouterConfig, RoutingResult, Trace
from tests.flashlight_fixture import make_flashlight_design

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class TestDRCReport(unittest.TestCase):
    """Unit tests for the DRCReport dataclass."""

    def test_empty_report_is_ok(self):
        report = DRCReport()
        self.assertTrue(report.ok)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_warning_does_not_fail(self):
        report = DRCReport(violations=[
            Violation(rule="test", severity="warning", net_id="X", message="warn"),
        ])
        self.assertTrue(report.ok)
        self.assertEqual(len(report.warnings), 1)

    def test_error_fails(self):
        report = DRCReport(violations=[
            Violation(rule="test", severity="error", net_id="X", message="bad"),
        ])
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)

    def test_summary_contains_counts(self):
        report = DRCReport(violations=[
            Violation(rule="a", severity="error", net_id="N1", message="e"),
            Violation(rule="b", severity="warning", net_id="N2", message="w"),
        ])
        s = report.summary()
        self.assertIn("1 errors", s)
        self.assertIn("1 warnings", s)


class TestFlashlightDRC(unittest.TestCase):
    """DRC on the flashlight fixture — all checks should pass."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog()
        cls.design = make_flashlight_design()
        cls.placement = place_components(cls.design, cls.catalog)
        cls.result = route_traces(cls.placement, cls.catalog)
        cls.pin_positions = _collect_pin_positions(cls.placement, cls.catalog)
        cls.config = RouterConfig()

    def test_routing_succeeds(self):
        self.assertTrue(self.result.ok, f"Failed nets: {self.result.failed_nets}")

    def test_drc_no_errors(self):
        report = run_drc(self.result, self.pin_positions, None, self.config)
        self.assertTrue(report.ok, report.summary())

    def test_drc_no_pin_conflicts(self):
        report = run_drc(self.result, self.pin_positions, None, self.config)
        pin_conflicts = [v for v in report.violations if v.rule == "pin_conflict"]
        self.assertEqual(pin_conflicts, [])

    def test_drc_with_outline(self):
        outline_poly = Polygon(self.placement.outline.vertices)
        report = run_drc(self.result, self.pin_positions, outline_poly, self.config)
        edge_errors = [v for v in report.errors if v.rule == "edge_clearance"]
        self.assertEqual(edge_errors, [], f"Edge violations: {[v.message for v in edge_errors]}")


class TestDRCDetectsViolations(unittest.TestCase):
    """Verify DRC catches synthetic violations."""

    def test_pin_conflict_detected(self):
        result = RoutingResult(
            traces=[],
            failed_nets=[],
            pin_assignments={
                "NET_A|comp:pin": "comp:pin",
                "NET_B|comp:pin": "comp:pin",
            },
        )
        report = run_drc(result, {}, None, RouterConfig())
        conflicts = [v for v in report.errors if v.rule == "pin_conflict"]
        self.assertGreater(len(conflicts), 0)


@unittest.skipUnless(FIXTURE_DIR.exists(), "Large fixture data not available")
@pytest.mark.slow
class TestLargeDesignDRC(unittest.TestCase):
    """DRC on the 27-component button matrix design."""

    @classmethod
    def setUpClass(cls):
        from tests.test_router_profile import _load_large_fixture
        cls.catalog = load_catalog()
        cls.placement = _load_large_fixture()
        cls.config = RouterConfig()
        cls.result = route_traces(cls.placement, cls.catalog, config=cls.config)
        cls.pin_positions = _collect_pin_positions(cls.placement, cls.catalog)

    def test_routing_succeeds(self):
        self.assertTrue(self.result.ok, f"Failed nets: {self.result.failed_nets}")

    def test_drc_no_errors(self):
        report = run_drc(self.result, self.pin_positions, None, self.config)
        self.assertTrue(report.ok, report.summary())

    def test_no_trace_trace_violations(self):
        report = run_drc(self.result, self.pin_positions, None, self.config)
        tt = [v for v in report.errors if v.rule == "trace_trace_clearance"]
        self.assertEqual(tt, [], f"Trace-trace violations: {[v.message for v in tt]}")

    def test_no_trace_pin_violations(self):
        report = run_drc(self.result, self.pin_positions, None, self.config)
        tp = [v for v in report.errors if v.rule == "trace_pin_clearance"]
        self.assertEqual(tp, [], f"Trace-pin violations: {[v.message for v in tp]}")
