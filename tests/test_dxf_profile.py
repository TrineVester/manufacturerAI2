"""Tests for the DXF profile loader."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

import pytest

from src.pipeline.scad.dxf_profile import (
    DxfEntities,
    load_dxf,
    _tokenise,
    _bulge_to_arc_points,
)

ROOT = Path(__file__).resolve().parent.parent
CLICK_LIP_DXF = ROOT / "click_lip_cut.dxf"


class TestTokenise:
    def test_basic_pairs(self):
        text = "  0\nSECTION\n  2\nHEADER\n"
        pairs = _tokenise(text)
        assert pairs == [(0, "SECTION"), (2, "HEADER")]

    def test_skips_blank_lines(self):
        text = "\n0\nFOO\n\n0\nBAR\n"
        pairs = _tokenise(text)
        assert len(pairs) == 2

    def test_crlf(self):
        text = "0\r\nSECTION\r\n2\r\nENTITIES\r\n"
        pairs = _tokenise(text)
        assert pairs == [(0, "SECTION"), (2, "ENTITIES")]


class TestBulgeToArc:
    def test_zero_bulge_returns_empty(self):
        assert _bulge_to_arc_points((0, 0), (1, 0), 0.0) == []

    def test_semicircle(self):
        pts = _bulge_to_arc_points((0, 0), (2, 0), 1.0, segments=8)
        assert len(pts) == 7
        for x, y in pts:
            dist = math.hypot(x - 1.0, y)
            assert abs(dist - 1.0) < 0.01

    def test_negative_bulge_flips(self):
        pts_pos = _bulge_to_arc_points((0, 0), (2, 0), 0.5, segments=4)
        pts_neg = _bulge_to_arc_points((0, 0), (2, 0), -0.5, segments=4)
        assert len(pts_pos) == len(pts_neg)
        for (_, y_pos), (_, y_neg) in zip(pts_pos, pts_neg):
            assert y_pos * y_neg < 0 or (abs(y_pos) < 1e-9 and abs(y_neg) < 1e-9)


class TestLoadClickLipDxf:
    @pytest.fixture()
    def entities(self) -> DxfEntities:
        assert CLICK_LIP_DXF.exists(), f"Missing {CLICK_LIP_DXF}"
        return load_dxf(CLICK_LIP_DXF)

    def test_units_mm(self, entities: DxfEntities):
        assert entities.units_scale == 1.0

    def test_polyline_count(self, entities: DxfEntities):
        assert len(entities.polylines) == 2

    def test_line_count(self, entities: DxfEntities):
        assert len(entities.lines) == 3

    def test_point_count(self, entities: DxfEntities):
        assert len(entities.points) == 2

    def test_polylines_closed(self, entities: DxfEntities):
        for pl in entities.polylines:
            assert pl.is_closed

    def test_polyline_vertex_count(self, entities: DxfEntities):
        for pl in entities.polylines:
            assert len(pl.points) == 7

    def test_first_polyline_vertices(self, entities: DxfEntities):
        pl = entities.polylines[0]
        expected = [
            (0.0, 0.0),
            (3.0, 0.0),
            (3.0, -3.0),
            (3.0, -8.0),
            (0.8686, -5.6740),
            (1.7850, -4.9729),
            (0.0, -3.0),
        ]
        assert len(pl.points) == len(expected)
        for (ax, ay), (ex, ey) in zip(pl.points, expected):
            assert abs(ax - ex) < 0.01, f"X mismatch: {ax} != {ex}"
            assert abs(ay - ey) < 0.01, f"Y mismatch: {ay} != {ey}"

    def test_second_polyline_vertices(self, entities: DxfEntities):
        pl = entities.polylines[1]
        expected = [
            (10.0, -3.0),
            (10.0, -8.0),
            (8.4154, -6.2708),
            (9.3277, -5.5727),
            (7.0, -3.0),
            (7.0, 0.0),
            (10.0, 0.0),
        ]
        assert len(pl.points) == len(expected)
        for (ax, ay), (ex, ey) in zip(pl.points, expected):
            assert abs(ax - ex) < 0.01, f"X mismatch: {ax} != {ex}"
            assert abs(ay - ey) < 0.01, f"Y mismatch: {ay} != {ey}"

    def test_line_coordinates(self, entities: DxfEntities):
        ln = entities.lines[0]
        assert abs(ln.x1 - 3.0) < 0.01
        assert abs(ln.y1 - (-3.0)) < 0.01
        assert abs(ln.x2 - 0.0) < 0.01
        assert abs(ln.y2 - (-3.0)) < 0.01


class TestSyntheticDxf:
    def test_empty_dxf(self, tmp_path: Path):
        dxf = tmp_path / "empty.dxf"
        dxf.write_text(textwrap.dedent("""\
            0
            SECTION
            2
            HEADER
            0
            ENDSEC
            0
            SECTION
            2
            ENTITIES
            0
            ENDSEC
            0
            EOF
        """))
        ent = load_dxf(dxf)
        assert len(ent.polylines) == 0
        assert len(ent.lines) == 0

    def test_open_polyline(self, tmp_path: Path):
        dxf = tmp_path / "open.dxf"
        dxf.write_text(textwrap.dedent("""\
            0
            SECTION
            2
            ENTITIES
            0
            LWPOLYLINE
            100
            AcDbEntity
            8
            MyLayer
            100
            AcDbPolyline
            90
            3
            70
            0
            43
            0.0
            10
            0.0
            20
            0.0
            10
            5.0
            20
            0.0
            10
            5.0
            20
            5.0
            0
            ENDSEC
            0
            EOF
        """))
        ent = load_dxf(dxf)
        assert len(ent.polylines) == 1
        pl = ent.polylines[0]
        assert not pl.is_closed
        assert pl.layer == "MyLayer"
        assert len(pl.points) == 3

    def test_circle_and_arc(self, tmp_path: Path):
        dxf = tmp_path / "circ.dxf"
        dxf.write_text(textwrap.dedent("""\
            0
            SECTION
            2
            ENTITIES
            0
            CIRCLE
            100
            AcDbEntity
            8
            0
            100
            AcDbCircle
            10
            5.0
            20
            5.0
            40
            2.5
            0
            ARC
            100
            AcDbEntity
            8
            0
            100
            AcDbArc
            10
            3.0
            20
            4.0
            40
            1.0
            50
            0.0
            51
            180.0
            0
            ENDSEC
            0
            EOF
        """))
        ent = load_dxf(dxf)
        assert len(ent.circles) == 1
        assert abs(ent.circles[0].cx - 5.0) < 1e-9
        assert abs(ent.circles[0].radius - 2.5) < 1e-9
        assert len(ent.arcs) == 1
        assert abs(ent.arcs[0].start_angle_deg) < 1e-9
        assert abs(ent.arcs[0].end_angle_deg - 180.0) < 1e-9

    def test_inch_units_scale(self, tmp_path: Path):
        dxf = tmp_path / "inch.dxf"
        dxf.write_text(textwrap.dedent("""\
            0
            SECTION
            2
            HEADER
            9
            $INSUNITS
            70
            1
            0
            ENDSEC
            0
            SECTION
            2
            ENTITIES
            0
            POINT
            100
            AcDbEntity
            8
            0
            100
            AcDbPoint
            10
            1.0
            20
            2.0
            0
            ENDSEC
            0
            EOF
        """))
        ent = load_dxf(dxf)
        assert ent.units_scale == 25.4
        assert abs(ent.points[0].x - 25.4) < 1e-9
        assert abs(ent.points[0].y - 50.8) < 1e-9

    def test_bulge_polyline(self, tmp_path: Path):
        dxf = tmp_path / "bulge.dxf"
        dxf.write_text(textwrap.dedent("""\
            0
            SECTION
            2
            ENTITIES
            0
            LWPOLYLINE
            100
            AcDbEntity
            8
            0
            100
            AcDbPolyline
            90
            4
            70
            1
            43
            0.0
            10
            0.0
            20
            0.0
            42
            1.0
            10
            2.0
            20
            0.0
            10
            2.0
            20
            2.0
            10
            0.0
            20
            2.0
            0
            ENDSEC
            0
            EOF
        """))
        ent = load_dxf(dxf)
        assert len(ent.polylines) == 1
        pl = ent.polylines[0]
        assert pl.is_closed
        assert len(pl.points) > 4
