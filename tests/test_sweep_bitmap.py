"""Tests for the bed bitmap geometry and bitmap generation.

Covers:
  - PrintheadConfig derived properties
  - bed_bitmap() dimensions for every printer definition
  - BedBitmap.bed_to_pixel() coordinate transform
  - get_printer() fallback behaviour
  - generate_trace_bitmap() rasterization correctness
"""

from __future__ import annotations

import math
import unittest

from src.pipeline.config import (
    PIXEL_SIZE_MM,
    PrinterDef,
    PRINTERS,
    BedBitmap,
    bed_bitmap,
    get_printer,
    DEFAULT_PRINTER,
)
from shapely.geometry import LineString

from src.pipeline.router.models import Trace, InflatedTrace, RoutingResult
from src.pipeline.router.bitmap import generate_trace_bitmap


def _inflated_from_trace(trace: Trace, width: float) -> InflatedTrace:
    """Buffer a trace centreline into an InflatedTrace for testing."""
    line = LineString(trace.path)
    return InflatedTrace(
        net_id=trace.net_id,
        centreline=list(trace.path),
        polygon=line.buffer(width / 2, cap_style="flat"),
    )


# ── Pixel resolution constant ──────────────────────────────────────

class TestPixelSize(unittest.TestCase):

    def test_pixel_size(self):
        self.assertAlmostEqual(PIXEL_SIZE_MM, 0.1371)


# ── bed_bitmap() dimensions ───────────────────────────────────────

class TestBedBitmapDimensions(unittest.TestCase):
    """Verify bed_bitmap() produces correct dimensions for every printer."""

    def test_mk3s_dimensions(self):
        pdef = PRINTERS["mk3s"]
        grid = bed_bitmap(pdef)
        expected_cols = math.ceil(250.0 / 0.1371)
        expected_rows = math.ceil(210.0 / 0.1371)
        self.assertEqual(grid.cols, expected_cols)
        self.assertEqual(grid.rows, expected_rows)

    def test_mk3s_plus_dimensions(self):
        pdef = PRINTERS["mk3s_plus"]
        grid = bed_bitmap(pdef)
        expected_cols = math.ceil(250.0 / 0.1371)
        expected_rows = math.ceil(210.0 / 0.1371)
        self.assertEqual(grid.cols, expected_cols)
        self.assertEqual(grid.rows, expected_rows)

    def test_coreone_dimensions(self):
        pdef = PRINTERS["coreone"]
        grid = bed_bitmap(pdef)
        expected_cols = math.ceil(250.0 / 0.1371)
        expected_rows = math.ceil(250.0 / 0.1371)
        self.assertEqual(grid.cols, expected_cols)
        self.assertEqual(grid.rows, expected_rows)

    def test_coreone_deeper_than_mk3s(self):
        mk3s = bed_bitmap(PRINTERS["mk3s"])
        core = bed_bitmap(PRINTERS["coreone"])
        self.assertEqual(mk3s.cols, core.cols)
        self.assertGreater(core.rows, mk3s.rows)

    def test_pixel_size_matches_constant(self):
        for pid, pdef in PRINTERS.items():
            with self.subTest(printer=pid):
                grid = bed_bitmap(pdef)
                self.assertAlmostEqual(grid.pixel_size_mm, PIXEL_SIZE_MM)


# ── BedBitmap.bed_to_pixel() ──────────────────────────────────────

class TestBedToPixel(unittest.TestCase):

    def setUp(self):
        self.pdef = PRINTERS["mk3s_plus"]
        self.grid = bed_bitmap(self.pdef)

    def test_origin_maps_to_zero(self):
        px, py = self.grid.bed_to_pixel(0.0, 0.0)
        self.assertAlmostEqual(px, 0.0, places=6)
        self.assertAlmostEqual(py, 0.0, places=6)

    def test_known_point(self):
        px, py = self.grid.bed_to_pixel(100.0, 100.0)
        self.assertAlmostEqual(px, 100.0 / 0.1371, places=3)
        self.assertAlmostEqual(py, 100.0 / 0.1371, places=3)

    def test_transform_is_pure_scaling(self):
        px1, py1 = self.grid.bed_to_pixel(50.0, 50.0)
        px2, py2 = self.grid.bed_to_pixel(60.0, 70.0)
        self.assertAlmostEqual((px2 - px1) * 0.1371, 10.0, places=6)
        self.assertAlmostEqual((py2 - py1) * 0.1371, 20.0, places=6)

    def test_all_printers_same_transform(self):
        grids = {pid: bed_bitmap(pdef) for pid, pdef in PRINTERS.items()}
        ref_px, _ = grids["mk3s"].bed_to_pixel(100.0, 100.0)
        for pid, g in grids.items():
            px, _ = g.bed_to_pixel(100.0, 100.0)
            self.assertAlmostEqual(px, ref_px, places=6, msg=pid)


# ── get_printer() ─────────────────────────────────────────────────

class TestGetPrinter(unittest.TestCase):

    def test_known_printer(self):
        p = get_printer("mk3s")
        self.assertEqual(p.id, "mk3s")

    def test_default_printer(self):
        p = get_printer(None)
        self.assertEqual(p.id, DEFAULT_PRINTER)

    def test_unknown_falls_back(self):
        p = get_printer("nonexistent_printer")
        self.assertEqual(p.id, DEFAULT_PRINTER)

    def test_case_insensitive(self):
        p = get_printer("CoreOne")
        self.assertEqual(p.id, "coreone")


# ── PrinterDef keepout / usable area ──────────────────────────────

class TestPrinterDefUsableArea(unittest.TestCase):

    def test_usable_width(self):
        pdef = PRINTERS["coreone"]
        expected = pdef.nominal_bed_width - pdef.keepout_left - pdef.keepout_right
        self.assertAlmostEqual(pdef.usable_width, expected)

    def test_usable_depth(self):
        pdef = PRINTERS["coreone"]
        expected = pdef.nominal_bed_depth - pdef.keepout_front - pdef.keepout_back
        self.assertAlmostEqual(pdef.usable_depth, expected)

    def test_bed_width_is_usable_width(self):
        for pid, pdef in PRINTERS.items():
            with self.subTest(printer=pid):
                self.assertEqual(pdef.bed_width, pdef.usable_width)
                self.assertEqual(pdef.bed_depth, pdef.usable_depth)


# ── generate_trace_bitmap() ───────────────────────────────────────

class TestGenerateTraceBitmap(unittest.TestCase):

    def setUp(self):
        self.pdef = PRINTERS["coreone"]
        self.grid = bed_bitmap(self.pdef)

    def test_empty_routing_produces_blank_bitmap(self):
        result = RoutingResult(traces=[], pin_assignments={}, failed_nets=[])
        lines = generate_trace_bitmap(result, 0.5, grid=self.grid)
        self.assertEqual(len(lines), self.grid.rows)
        self.assertTrue(all(len(l) == self.grid.cols for l in lines))
        self.assertTrue(all(c == '0' for line in lines for c in line))

    def test_bitmap_only_contains_0_and_1(self):
        trace = Trace(
            net_id="test",
            path=[(5.0, 5.0), (15.0, 5.0)],
        )
        it = _inflated_from_trace(trace, 0.5)
        result = RoutingResult(traces=[trace], pin_assignments={}, failed_nets=[], inflated_traces=[it])
        model_to_bed = (80.0, 80.0)
        lines = generate_trace_bitmap(result, 0.5, grid=self.grid, model_to_bed=model_to_bed)
        all_chars = set(c for line in lines for c in line)
        self.assertTrue(all_chars.issubset({'0', '1'}))

    def test_trace_produces_ink(self):
        trace = Trace(
            net_id="signal",
            path=[(0.0, 5.0), (20.0, 5.0)],
        )
        it = _inflated_from_trace(trace, 0.5)
        result = RoutingResult(traces=[trace], pin_assignments={}, failed_nets=[], inflated_traces=[it])
        model_to_bed = (100.0, 100.0)
        lines = generate_trace_bitmap(result, 0.5, grid=self.grid, model_to_bed=model_to_bed)
        total_ink = sum(line.count('1') for line in lines)
        self.assertGreater(total_ink, 0)

    def test_out_of_bounds_trace_produces_no_ink(self):
        trace = Trace(
            net_id="offscreen",
            path=[(-1000.0, -1000.0), (-900.0, -1000.0)],
        )
        it = _inflated_from_trace(trace, 0.5)
        result = RoutingResult(traces=[trace], pin_assignments={}, failed_nets=[], inflated_traces=[it])
        lines = generate_trace_bitmap(result, 0.5, grid=self.grid)
        total_ink = sum(line.count('1') for line in lines)
        self.assertEqual(total_ink, 0)

    def test_horizontal_trace_width(self):
        trace_w = 0.5
        trace = Trace(
            net_id="hline",
            path=[(5.0, 10.0), (15.0, 10.0)],
        )
        it = _inflated_from_trace(trace, trace_w)
        result = RoutingResult(traces=[trace], pin_assignments={}, failed_nets=[], inflated_traces=[it])
        model_to_bed = (100.0, 100.0)
        lines = generate_trace_bitmap(result, trace_w, grid=self.grid, model_to_bed=model_to_bed)

        inked_rows = [i for i, line in enumerate(lines) if '1' in line]
        self.assertGreater(len(inked_rows), 0)
        expected_rows = max(1, round(trace_w / self.grid.pixel_size_mm))
        self.assertAlmostEqual(len(inked_rows), expected_rows, delta=2)

    def test_diagonal_trace_continuous(self):
        trace = Trace(
            net_id="diag",
            path=[(5.0, 5.0), (15.0, 15.0)],
        )
        it = _inflated_from_trace(trace, 0.5)
        result = RoutingResult(traces=[trace], pin_assignments={}, failed_nets=[], inflated_traces=[it])
        model_to_bed = (100.0, 100.0)
        lines = generate_trace_bitmap(result, 0.5, grid=self.grid, model_to_bed=model_to_bed)

        inked_rows = [i for i, line in enumerate(lines) if '1' in line]
        self.assertGreater(len(inked_rows), 0)
        row_min, row_max = min(inked_rows), max(inked_rows)
        for r in range(row_min, row_max + 1):
            self.assertIn('1', lines[r],
                          f"Gap at row {r} — diagonal trace is fragmented")


# ── Y-mirror consistency: gcode vs bitmap ─────────────────────────

class TestYMirrorConsistency(unittest.TestCase):
    """The SCAD emitter wraps models in mirror([0,1,0]), negating all Y
    in the STL.  The gcode pipeline reads the mirrored STL's bbox
    centre and applies ``-y + dy``.  The bitmap pipeline must produce
    the same bed-Y for the same model-local trace point.

    Both paths now use ``-y + dy`` where ``dy = ucy + model_cy``
    (i.e. ``ucy - (-model_cy)``), so they agree everywhere.
    """

    def setUp(self):
        self.pdef = PRINTERS["mk3s"]
        self.grid = bed_bitmap(self.pdef)
        self.model_w = 20.0
        self.model_h = 10.0
        self.unmirored_cy = self.model_h / 2   # 5
        self.mirrored_cy = -self.unmirored_cy   # −5
        self.ucx, self.ucy = self.pdef.usable_center

    def _gcode_bed_y(self, model_y: float) -> float:
        """Gcode transform: compute_bed_offset from mirrored STL, then -y + dy."""
        dy = self.ucy - self.mirrored_cy
        return -model_y + dy

    def _bitmap_bed_y(self, model_y: float) -> float:
        """Bitmap transform: model_to_bed with mirrored dy, then -y + dy."""
        dy = self.ucy + self.unmirored_cy        # ucy - (-model_cy) = ucy + model_cy
        return -model_y + dy

    def test_center_point_agrees(self):
        g = self._gcode_bed_y(self.unmirored_cy)
        b = self._bitmap_bed_y(self.unmirored_cy)
        self.assertAlmostEqual(g, self.ucy)
        self.assertAlmostEqual(b, self.ucy)

    def test_off_center_point_agrees(self):
        for y in (0.0, 1.0, 5.0, 9.0, 10.0):
            with self.subTest(y=y):
                g = self._gcode_bed_y(y)
                b = self._bitmap_bed_y(y)
                self.assertAlmostEqual(g, b)

    def test_bitmap_pixel_row_matches(self):
        y = 1.0
        g_bed_y = self._gcode_bed_y(y)
        b_bed_y = self._bitmap_bed_y(y)
        px = self.grid.pixel_size_mm
        self.assertEqual(int(g_bed_y / px), int(b_bed_y / px))


if __name__ == "__main__":
    unittest.main()
