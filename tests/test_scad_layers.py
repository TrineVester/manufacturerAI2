"""Tests for src/pipeline/scad/layers.py — shell body polyhedron generation.

Coverage
--------
* ``_polygon_signed_area``         — correct sign for CCW and CW polygons.
* ``_inset_polygon_pts``           — vertex count preserved; inset moves inward.
* ``_earclip``                     — correct triangulation for convex and non-convex polygons.
* ``_build_rings``                 — correct ring count for all edge-profile combinations.
* ``shell_body_lines``             — polyhedron with ear-clipped caps.
* polyhedron output structure      — correct point/face counts, valid index range.
* Edge profiles in polyhedron path — extra rings are emitted for chamfer/fillet.
* Winding sanity                   — bottom-cap and top-cap face indices are reversed
                                     from each other (required for correct outward normals).
* OpenSCAD syntax check            — if the ``openscad`` binary is available, verifies
                                     that the emitted SCAD file parses without errors.
"""

from __future__ import annotations

import math
import re
import tempfile
import unittest
from pathlib import Path

from src.pipeline.design.models import (
    Enclosure,
    EdgeProfile,
    Outline,
    OutlineVertex,
    TopSurface,
)
from src.pipeline.scad.layers import (
    _CURVE_STEPS,
    _build_rings,
    _earclip,
    _inset_polygon_pts,
    _polygon_signed_area,
    shell_body_lines,
)


# ── Shared polygon fixtures ────────────────────────────────────────────────────


def _ccw_square(side: float = 20.0) -> list[list[float]]:
    """CCW (math-convention) square: (0,0),(side,0),(side,side),(0,side).

    Signed area > 0 in standard math coords (Y up).
    """
    s = side
    return [[0.0, 0.0], [s, 0.0], [s, s], [0.0, s]]


def _cw_square(side: float = 20.0) -> list[list[float]]:
    """CW (math-convention) square — reverse of the CCW version.

    Signed area < 0 (same shape, opposite orientation).
    """
    return _ccw_square(side)[::-1]


def _rect_pts(w: float = 30.0, h: float = 50.0) -> list[list[float]]:
    """CCW rectangle centred at origin."""
    hw, hh = w / 2, h / 2
    return [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]]


def _plain_enclosure(height: float = 25.0) -> Enclosure:
    return Enclosure(height_mm=height)


def _fillet_enclosure(height: float = 25.0, size: float = 2.0) -> Enclosure:
    profile = EdgeProfile(type="fillet", size_mm=size)
    return Enclosure(height_mm=height, edge_top=profile, edge_bottom=profile)


def _chamfer_enclosure(height: float = 25.0, size: float = 2.0) -> Enclosure:
    profile = EdgeProfile(type="chamfer", size_mm=size)
    return Enclosure(height_mm=height, edge_top=profile, edge_bottom=profile)


def _outline_from_pts(pts: list[list[float]]) -> Outline:
    return Outline(points=[OutlineVertex(x=x, y=y) for x, y in pts])


# ── _polygon_signed_area ───────────────────────────────────────────────────────


class TestPolygonSignedArea(unittest.TestCase):
    """Verify the signed-area helper correctly identifies orientation."""

    def test_ccw_square_positive(self):
        area = _polygon_signed_area(_ccw_square(10.0))
        self.assertGreater(area, 0)
        self.assertAlmostEqual(area, 100.0, places=6)

    def test_cw_square_negative(self):
        area = _polygon_signed_area(_cw_square(10.0))
        self.assertLess(area, 0)
        self.assertAlmostEqual(area, -100.0, places=6)

    def test_degenerate_triangle(self):
        pts = [[0.0, 0.0], [5.0, 0.0], [0.0, 5.0]]
        area = _polygon_signed_area(pts)
        self.assertAlmostEqual(abs(area), 12.5, places=6)


# ── _inset_polygon_pts ────────────────────────────────────────────────────────


class TestInsetPolygonPts(unittest.TestCase):
    """Validate the per-vertex miter inset preserves vertex count and moves inward."""

    def _centroid(self, pts):
        n = len(pts)
        return sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n

    def test_zero_inset_returns_copy(self):
        pts = _ccw_square(20.0)
        result = _inset_polygon_pts(pts, 0.0)
        self.assertEqual(len(result), len(pts))
        for r, o in zip(result, pts):
            self.assertAlmostEqual(r[0], o[0])
            self.assertAlmostEqual(r[1], o[1])

    def test_vertex_count_preserved_ccw(self):
        pts = _rect_pts(30, 50)
        for inset in (0.5, 1.0, 2.0, 3.0):
            result = _inset_polygon_pts(pts, inset)
            self.assertEqual(len(result), len(pts),
                             f"vertex count changed at inset={inset}")

    def test_vertex_count_preserved_cw(self):
        pts = _cw_square(20.0)
        for inset in (0.5, 1.5):
            result = _inset_polygon_pts(pts, inset)
            self.assertEqual(len(result), len(pts))

    def test_inset_moves_toward_centroid_ccw(self):
        """Each inseted vertex should be closer to the centroid."""
        pts = _ccw_square(20.0)
        cx, cy = self._centroid(pts)
        result = _inset_polygon_pts(pts, 2.0)
        for orig, ins in zip(pts, result):
            d_orig = math.hypot(orig[0] - cx, orig[1] - cy)
            d_ins  = math.hypot(ins[0]  - cx, ins[1]  - cy)
            self.assertLess(d_ins, d_orig,
                            "inset vertex should be closer to centroid")

    def test_inset_moves_toward_centroid_cw(self):
        """CW polygon: inset should still shrink toward the centroid."""
        pts = _cw_square(20.0)
        cx, cy = self._centroid(pts)
        result = _inset_polygon_pts(pts, 2.0)
        for orig, ins in zip(pts, result):
            d_orig = math.hypot(orig[0] - cx, orig[1] - cy)
            d_ins  = math.hypot(ins[0]  - cx, ins[1]  - cy)
            self.assertLess(d_ins, d_orig)

    def test_right_angle_miter_is_45_degrees(self):
        """For a 90° right-angle corner the bisector sits at 45°."""
        pts = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
        result = _inset_polygon_pts(pts, 1.0)
        # Bottom-left corner (0,0): inset should move to roughly (1, 1)
        self.assertAlmostEqual(result[0][0], 1.0, delta=0.05)
        self.assertAlmostEqual(result[0][1], 1.0, delta=0.05)


# ── _build_rings ──────────────────────────────────────────────────────────────


class TestBuildRings(unittest.TestCase):
    """Check ring counts for all edge-profile combinations."""

    def setUp(self):
        self.pts = _rect_pts(30, 50)
        self.top_zs = [25.0] * 4   # uniform; shape logic is independent

    def _ring_count_for(self, enc: Enclosure) -> int:
        rings = _build_rings(self.pts, self.top_zs, enc)
        return len(rings)

    def test_no_profiles_gives_two_rings(self):
        # bottom ring (z=0) + top ring (z=top_zs)
        self.assertEqual(self._ring_count_for(_plain_enclosure()), 2)

    def test_bottom_only_fillet(self):
        enc = Enclosure(
            height_mm=25.0,
            edge_bottom=EdgeProfile(type="fillet", size_mm=2.0),
        )
        # _CURVE_STEPS+1 bottom rings + 1 top ring
        expected = _CURVE_STEPS + 1 + 1
        self.assertEqual(self._ring_count_for(enc), expected)

    def test_top_only_fillet(self):
        enc = Enclosure(
            height_mm=25.0,
            edge_top=EdgeProfile(type="fillet", size_mm=2.0),
        )
        # 1 bottom ring + _CURVE_STEPS+1 top rings
        expected = 1 + _CURVE_STEPS + 1
        self.assertEqual(self._ring_count_for(enc), expected)

    def test_both_profiles_fillet(self):
        expected = (_CURVE_STEPS + 1) + (_CURVE_STEPS + 1)
        self.assertEqual(self._ring_count_for(_fillet_enclosure(25.0, 2.0)), expected)

    def test_both_profiles_chamfer(self):
        expected = (_CURVE_STEPS + 1) + (_CURVE_STEPS + 1)
        self.assertEqual(self._ring_count_for(_chamfer_enclosure(25.0, 2.0)), expected)

    def test_each_ring_has_n_vertices(self):
        rings = _build_rings(self.pts, self.top_zs, _fillet_enclosure())
        n = len(self.pts)
        for i, ring in enumerate(rings):
            self.assertEqual(len(ring), n, f"ring {i} has wrong vertex count")

    def test_bottom_ring_is_at_z_zero(self):
        """First ring must start at z=0 (no bottom profile case)."""
        rings = _build_rings(self.pts, self.top_zs, _plain_enclosure())
        for pt in rings[0]:
            self.assertAlmostEqual(pt[2], 0.0)

    def test_top_ring_follows_top_zs(self):
        """Last ring z-values must match top_zs (no top profile)."""
        top_zs = [20.0, 22.0, 25.0, 21.0]
        pts = _rect_pts(30, 50)
        rings = _build_rings(pts, top_zs, _plain_enclosure())
        last = rings[-1]
        for pt, tz in zip(last, top_zs):
            self.assertAlmostEqual(pt[2], tz, places=4)

    def test_variable_top_zs_propagate_through_fillet(self):
        """With a top fillet, the last ring z-values should equal the per-vertex
        top_zs (at frac=1 the z_offset equals top_size)."""
        top_zs = [20.0, 22.0, 25.0, 21.0]
        pts = _rect_pts(30, 50)
        top_size = 2.0
        enc = Enclosure(
            height_mm=min(top_zs),
            edge_top=EdgeProfile(type="fillet", size_mm=top_size),
        )
        rings = _build_rings(pts, top_zs, enc)
        last = rings[-1]
        for pt, tz in zip(last, top_zs):
            self.assertAlmostEqual(pt[2], tz, places=4,
                                   msg=f"last fillet ring z should equal top_zs")


# ── shell_body_lines: uniform path ────────────────────────────────────────────


class TestEarclip(unittest.TestCase):
    """Validate the ear-clipping polygon triangulation."""

    def test_triangle(self):
        pts = [[0.0, 0.0], [10.0, 0.0], [5.0, 10.0]]
        tris = _earclip(pts)
        self.assertEqual(len(tris), 1)
        self.assertEqual(tris[0], (0, 1, 2))

    def test_square(self):
        pts = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
        tris = _earclip(pts)
        self.assertEqual(len(tris), 2)

    def test_l_shape_nonconvex(self):
        """Non-convex L-shape should produce N-2=4 triangles."""
        pts = [[0, 0], [10, 0], [10, 5], [5, 5], [5, 10], [0, 10]]
        tris = _earclip(pts)
        self.assertEqual(len(tris), 4)
        for a, b, c in tris:
            self.assertGreaterEqual(min(a, b, c), 0)
            self.assertLess(max(a, b, c), 6)

    def test_cw_polygon(self):
        """CW winding should also triangulate correctly."""
        pts = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0]]
        tris = _earclip(pts)
        self.assertEqual(len(tris), 2)

    def test_all_indices_unique_per_triangle(self):
        pts = [[0, 0], [20, 0], [20, 10], [15, 10], [15, 20], [0, 20]]
        tris = _earclip(pts)
        for a, b, c in tris:
            self.assertEqual(len({a, b, c}), 3)


# ── shell_body_lines: uniform path ────────────────────────────────────────────


class TestShellBodyUniformHeight(unittest.TestCase):
    """Shell body uses polyhedron for all height configurations."""

    def setUp(self):
        self.pts = _rect_pts(30, 50)
        self.outline = _outline_from_pts(self.pts)

    def _joined(self, lines):
        return "\n".join(lines)

    def test_none_top_zs_is_polyhedron_path(self):
        lines = shell_body_lines(self.outline, _plain_enclosure(25.0), self.pts)
        scad = self._joined(lines)
        self.assertIn("polyhedron", scad)

    def test_uniform_top_zs_uses_polyhedron(self):
        top_zs = [25.0] * len(self.pts)
        lines = shell_body_lines(self.outline, _plain_enclosure(25.0), self.pts, top_zs=top_zs)
        scad = self._joined(lines)
        self.assertIn("polyhedron", scad)

    def test_near_threshold_variation_still_polyhedron(self):
        """Small height variation still uses the polyhedron path."""
        top_zs = [25.0, 25.09, 25.0, 25.0]
        lines = shell_body_lines(self.outline, _plain_enclosure(25.0), self.pts, top_zs=top_zs)
        scad = self._joined(lines)
        self.assertIn("polyhedron", scad)

    def test_uniform_height_from_top_zs_overrides_enclosure_height(self):
        """If all top_zs are equal but differ from enclosure.height_mm,
        the emitted height should match top_zs[0], not enclosure.height_mm."""
        top_zs = [30.0] * len(self.pts)   # all = 30, but enclosure says 25
        lines = shell_body_lines(self.outline, _plain_enclosure(25.0), self.pts, top_zs=top_zs)
        scad = self._joined(lines)
        self.assertIn("30.000", scad)

    def test_with_fillet_uniform_height_uses_polyhedron(self):
        top_zs = [25.0] * len(self.pts)
        lines = shell_body_lines(self.outline, _fillet_enclosure(25.0), self.pts, top_zs=top_zs)
        scad = self._joined(lines)
        self.assertIn("polyhedron", scad)


# ── shell_body_lines: variable heights ──────────────────────────────────────────────


class TestShellBodyPolyhedronPath(unittest.TestCase):
    """shell_body_lines uses ear-clipped caps for all height configurations."""

    def setUp(self):
        # Simple 4-vertex rectangle with variable ceiling heights
        self.pts = _rect_pts(30, 50)
        self.outline = _outline_from_pts(self.pts)
        self.top_zs     = [20.0, 25.0, 30.0, 22.0]   # 10 mm variation
        self.enclosure  = _plain_enclosure(20.0)

    def _get_scad(self, enc=None, top_zs=None):
        enc = enc or self.enclosure
        tz  = top_zs if top_zs is not None else self.top_zs
        return "\n".join(shell_body_lines(self.outline, enc, self.pts, top_zs=tz))

    # ── Output structure ────────────────────────────────────────────────────────

    def test_polyhedron_emitted(self):
        self.assertIn("polyhedron(", self._get_scad())

    def test_no_linear_extrude(self):
        self.assertNotIn("linear_extrude", self._get_scad())

    def test_variable_height_uses_polyhedron(self):
        top_zs = [25.0, 25.2, 25.0, 25.0]
        self.assertIn("polyhedron(", self._get_scad(top_zs=top_zs))

    # ── points= field ──────────────────────────────────────────────────────────

    def _parse_points(self, scad: str) -> list[list[float]]:
        """Extract all [x,y,z] triples from points=[...] in the SCAD."""
        m = re.search(r"points\s*=\s*\[(.+?)\](?=,\s*faces)", scad, re.DOTALL)
        self.assertIsNotNone(m, "could not find points=[...] in SCAD output")
        triples = re.findall(r"\[([^]]+)\]", m.group(1))
        result = []
        for t in triples:
            vals = [float(v.strip()) for v in t.split(",")]
            self.assertEqual(len(vals), 3, "each point must have 3 coordinates")
            result.append(vals)
        return result

    def _parse_faces(self, scad: str) -> list[list[int]]:
        """Extract all face index lists from faces=[...] in the SCAD."""
        m = re.search(r"faces\s*=\s*\[(.+?)\](?=,\s*convexity)", scad, re.DOTALL)
        self.assertIsNotNone(m, "could not find faces=[...] in SCAD output")
        face_strs = re.findall(r"\[([^\]]+)\]", m.group(1))
        faces = []
        for fs in face_strs:
            faces.append([int(i.strip()) for i in fs.split(",") if i.strip()])
        return faces

    def test_point_count_no_profiles(self):
        """No profiles → R*N ring points (no cap rings or centroids)."""
        N = len(self.pts)
        R = 2  # 1 bottom ring + 1 top ring
        scad = self._get_scad()
        pts = self._parse_points(scad)
        self.assertEqual(len(pts), R * N)

    def test_point_count_with_both_fillets(self):
        """Both profiles → 2*(CS+1) main rings × N points."""
        N = len(self.pts)
        R = 2 * (_CURVE_STEPS + 1)
        expected_pts = R * N
        scad = self._get_scad(enc=_fillet_enclosure(20.0, 2.0))
        pts = self._parse_points(scad)
        self.assertEqual(len(pts), expected_pts)

    def test_all_point_z_values_are_finite(self):
        scad = self._get_scad()
        pts = self._parse_points(scad)
        for pt in pts:
            self.assertTrue(math.isfinite(pt[2]), f"z={pt[2]} is not finite")

    def test_bottom_ring_z_is_zero(self):
        """First ring (indices 0..N-1) must all have z=0."""
        N = len(self.pts)
        scad = self._get_scad()
        pts = self._parse_points(scad)
        for i in range(N):
            self.assertAlmostEqual(pts[i][2], 0.0, places=3,
                                   msg=f"bottom ring pt {i} z should be 0")

    def test_top_ring_z_matches_top_zs(self):
        """Last ring (indices (R-1)*N .. R*N-1) must match top_zs.

        With a top fillet the fillet zone base heights are smoothed for visual
        quality, but the final ring (frac=1 lerp) always lands exactly at
        top_zs[i] so component clearances and cutout depths are preserved.
        """
        N = len(self.pts)
        scad = self._get_scad()
        pts = self._parse_points(scad)
        # 2 rings, no profiles
        top_ring_start = N
        for i, tz in enumerate(self.top_zs):
            self.assertAlmostEqual(pts[top_ring_start + i][2], tz, places=3)

    # ── faces= field ───────────────────────────────────────────────────────────

    def test_face_count_no_profiles(self):
        """2 main rings → (N-2) bottom + (N-2) top ear-clip tris + (R-1)*N*2 side tris."""
        N = len(self.pts)
        R = 2
        expected_faces = 2 * (N - 2) + (R - 1) * N * 2
        scad = self._get_scad()
        faces = self._parse_faces(scad)
        self.assertEqual(len(faces), expected_faces)

    def test_face_count_with_both_fillets(self):
        """Both fillets → (N-2) bot + (N-2) top + (R-1)*N*2 sides."""
        N = len(self.pts)
        R = 2 * (_CURVE_STEPS + 1)
        expected_faces = 2 * (N - 2) + (R - 1) * N * 2
        scad = self._get_scad(enc=_fillet_enclosure(20.0, 2.0))
        faces = self._parse_faces(scad)
        self.assertEqual(len(faces), expected_faces)

    def test_all_face_indices_in_range(self):
        """Every face index must refer to a valid point."""
        N = len(self.pts)
        scad = self._get_scad()
        pts   = self._parse_points(scad)
        faces = self._parse_faces(scad)
        total_pts = len(pts)
        for face in faces:
            for idx in face:
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, total_pts,
                                f"face index {idx} out of range (max {total_pts-1})")

    def test_bottom_and_top_caps_cover_all_ring_vertices(self):
        """Bottom cap triangles reference all first-ring vertices; top cap
        triangles reference all last-ring vertices."""
        N = len(self.pts)
        R = 2
        scad = self._get_scad()
        pts   = self._parse_points(scad)
        faces = self._parse_faces(scad)

        bot_indices = set(range(N))
        top_indices = set(range((R - 1) * N, R * N))

        bot_tris = [f for f in faces if all(i in bot_indices for i in f)]
        bot_verts = {v for tri in bot_tris for v in tri}
        self.assertEqual(bot_verts, bot_indices)
        self.assertEqual(len(bot_tris), N - 2)

        top_tris = [f for f in faces if all(i in top_indices for i in f)]
        top_verts = {v for tri in top_tris for v in tri}
        self.assertEqual(top_verts, top_indices)
        self.assertEqual(len(top_tris), N - 2)

    def test_bottom_and_top_caps_are_reversed_from_each_other(self):
        """Bottom and top cap triangles must have opposite 2-D signed areas,
        ensuring outward normals point in opposite Z directions."""
        N = len(self.pts)
        R = 2
        scad = self._get_scad()
        pts   = self._parse_points(scad)
        faces = self._parse_faces(scad)

        bot_indices = set(range(N))
        top_indices = set(range((R - 1) * N, R * N))

        bot_tris = [f for f in faces if all(i in bot_indices for i in f)]
        top_tris = [f for f in faces if all(i in top_indices for i in f)]

        def _signed_area_face(face_indices, all_pts):
            n = len(face_indices)
            return 0.5 * sum(
                all_pts[face_indices[i]][0] * all_pts[face_indices[(i + 1) % n]][1]
                - all_pts[face_indices[(i + 1) % n]][0] * all_pts[face_indices[i]][1]
                for i in range(n)
            )

        bot_area = sum(_signed_area_face(t, pts) for t in bot_tris)
        top_area = sum(_signed_area_face(t, pts) for t in top_tris)

        self.assertNotEqual(
            bot_area > 0, top_area > 0,
            f"Bottom and top caps must have opposite winding "
            f"(bot_area={bot_area:.2f}, top_area={top_area:.2f})"
        )

    # ── Edge profiles in polyhedron path ───────────────────────────────────────

    def test_chamfer_produces_more_rings_than_plain(self):
        scad_plain   = self._get_scad(enc=_plain_enclosure(20.0))
        scad_chamfer = self._get_scad(enc=_chamfer_enclosure(20.0, 2.0))
        pts_plain   = self._parse_points(scad_plain)
        pts_chamfer = self._parse_points(scad_chamfer)
        self.assertGreater(len(pts_chamfer), len(pts_plain))

    def test_fillet_and_chamfer_same_ring_count(self):
        """Fillet and chamfer both use _CURVE_STEPS+1 rings per zone."""
        scad_f = self._get_scad(enc=_fillet_enclosure(20.0, 2.0))
        scad_c = self._get_scad(enc=_chamfer_enclosure(20.0, 2.0))
        pts_f = self._parse_points(scad_f)
        pts_c = self._parse_points(scad_c)
        self.assertEqual(len(pts_f), len(pts_c))

    def test_top_zs_are_reflected_in_top_profile_ring(self):
        """With a top fillet, the last ring z must exactly match top_zs."""
        enc = _fillet_enclosure(20.0, 2.0)
        N = len(self.pts)
        scad = self._get_scad(enc=enc)
        pts = self._parse_points(scad)
        R = len(pts) // N
        top_ring_start = (R - 1) * N
        top_ring = pts[top_ring_start:top_ring_start + N]
        for i, tz in enumerate(self.top_zs):
            self.assertAlmostEqual(top_ring[i][2], tz, places=3,
                                   msg=f"vertex {i}: fillet last ring z != top_zs")


# ── More polygons: 8-vertex octagon and 3-vertex triangle ─────────────────────


class TestPolyhedronWithVariousPolygons(unittest.TestCase):
    """Ensure the polyhedron path works for polygons of different sizes."""

    def _octagon(self, r: float = 15.0) -> list[list[float]]:
        n = 8
        return [
            [r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)]
            for i in range(n)
        ]

    def _variable_top_zs(self, pts: list[list[float]]) -> list[float]:
        """Assign heights that vary by more than the threshold."""
        n = len(pts)
        return [20.0 + 5.0 * (i / max(n - 1, 1)) for i in range(n)]

    def _run_checks(self, pts, top_zs, enc):
        """Run the basic structural checks for any polygon."""
        outline = _outline_from_pts(pts)
        lines = shell_body_lines(outline, enc, pts, top_zs=top_zs)
        scad = "\n".join(lines)
        self.assertIn("polyhedron(", scad)

        # Extract and validate
        m_pts = re.search(r"points\s*=\s*\[(.+?)\](?=,\s*faces)", scad, re.DOTALL)
        self.assertIsNotNone(m_pts)
        raw_pts = re.findall(r"\[([^]]+)\]", m_pts.group(1))
        total_pts = len(raw_pts)

        m_faces = re.search(r"faces\s*=\s*\[(.+?)\](?=,\s*convexity)", scad, re.DOTALL)
        self.assertIsNotNone(m_faces)
        face_strs = re.findall(r"\[([^\]]+)\]", m_faces.group(1))
        faces = [[int(i) for i in fs.split(",") if i.strip()] for fs in face_strs]

        for face in faces:
            for idx in face:
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, total_pts)

    def test_octagon_no_profiles(self):
        pts = self._octagon()
        top_zs = self._variable_top_zs(pts)
        self._run_checks(pts, top_zs, _plain_enclosure(22.0))

    def test_octagon_with_fillet(self):
        pts = self._octagon()
        top_zs = self._variable_top_zs(pts)
        self._run_checks(pts, top_zs, _fillet_enclosure(22.0, 1.5))

    def test_triangle_no_profiles(self):
        pts = [[0.0, 0.0], [20.0, 0.0], [10.0, 20.0]]
        top_zs = [20.0, 28.0, 24.0]
        self._run_checks(pts, top_zs, _plain_enclosure(20.0))


# ── OpenSCAD syntax check (optional, requires openscad binary) ─────────────────


class TestOpenSCADSyntax(unittest.TestCase):
    """If the openscad binary is available, verify the generated SCAD parses."""

    def _openscad_available(self) -> bool:
        from src.pipeline.scad.compiler import _find_openscad
        return _find_openscad() is not None

    def _write_and_check(self, scad_lines: list[str]) -> tuple[bool, str]:
        from src.pipeline.scad.compiler import check_scad
        scad = "$fn = 16;\n" + "\n".join(scad_lines) + "\n"
        with tempfile.NamedTemporaryFile(
            suffix=".scad", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(scad)
            tmp = Path(f.name)
        try:
            return check_scad(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_polyhedron_plain_parses(self):
        if not self._openscad_available():
            self.skipTest("openscad not found")
        pts = _rect_pts(30, 50)
        outline = _outline_from_pts(pts)
        top_zs = [20.0, 25.0, 30.0, 22.0]
        lines = shell_body_lines(outline, _plain_enclosure(20.0), pts, top_zs=top_zs)
        ok, msg = self._write_and_check(lines)
        self.assertTrue(ok, f"OpenSCAD syntax error: {msg}")

    def test_polyhedron_fillet_parses(self):
        if not self._openscad_available():
            self.skipTest("openscad not found")
        pts = _rect_pts(30, 50)
        outline = _outline_from_pts(pts)
        top_zs = [20.0, 25.0, 30.0, 22.0]
        lines = shell_body_lines(outline, _fillet_enclosure(20.0, 2.0), pts, top_zs=top_zs)
        ok, msg = self._write_and_check(lines)
        self.assertTrue(ok, f"OpenSCAD syntax error: {msg}")

    def test_uniform_plain_parses(self):
        if not self._openscad_available():
            self.skipTest("openscad not found")
        pts = _rect_pts(30, 50)
        outline = _outline_from_pts(pts)
        lines = shell_body_lines(outline, _plain_enclosure(25.0), pts)
        ok, msg = self._write_and_check(lines)
        self.assertTrue(ok, f"OpenSCAD syntax error: {msg}")

    def test_polyhedron_variable_bottom_parses(self):
        if not self._openscad_available():
            self.skipTest("openscad not found")
        pts = _rect_pts(30, 50)
        outline = _outline_from_pts(pts)
        top_zs    = [25.0, 25.0, 25.0, 25.0]
        bottom_zs = [0.0, 3.0, 5.0, 2.0]
        lines = shell_body_lines(outline, _plain_enclosure(25.0), pts,
                                 top_zs=top_zs, bottom_zs=bottom_zs)
        ok, msg = self._write_and_check(lines)
        self.assertTrue(ok, f"OpenSCAD syntax error: {msg}")

    def test_polyhedron_variable_both_parses(self):
        if not self._openscad_available():
            self.skipTest("openscad not found")
        pts = _rect_pts(30, 50)
        outline = _outline_from_pts(pts)
        top_zs    = [20.0, 25.0, 30.0, 22.0]
        bottom_zs = [0.0, 3.0, 5.0, 1.0]
        lines = shell_body_lines(outline, _fillet_enclosure(20.0, 2.0), pts,
                                 top_zs=top_zs, bottom_zs=bottom_zs)
        ok, msg = self._write_and_check(lines)
        self.assertTrue(ok, f"OpenSCAD syntax error: {msg}")


# ── Variable bottom surface tests ──────────────────────────────────────────────


class TestVariableBottomSurface(unittest.TestCase):
    """Tests for the custom bottom surface feature (bottom_zs)."""

    def setUp(self):
        self.pts = _rect_pts(30, 50)
        self.outline = _outline_from_pts(self.pts)
        self.top_zs = [25.0] * 4
        self.bottom_zs = [0.0, 3.0, 5.0, 2.0]  # variable floor

    def _joined(self, lines):
        return "\n".join(lines)

    def _parse_points(self, scad: str) -> list[list[float]]:
        m = re.search(r"points\s*=\s*\[(.+?)\](?=,\s*faces)", scad, re.DOTALL)
        self.assertIsNotNone(m)
        triples = re.findall(r"\[([^]]+)\]", m.group(1))
        return [[float(v.strip()) for v in t.split(",")] for t in triples]

    def _parse_faces(self, scad: str) -> list[list[int]]:
        m = re.search(r"faces\s*=\s*\[(.+?)\](?=,\s*convexity)", scad, re.DOTALL)
        self.assertIsNotNone(m)
        return [[int(i) for i in fs.split(",") if i.strip()]
                for fs in re.findall(r"\[([^\]]+)\]", m.group(1))]

    def test_variable_bottom_activates_polyhedron(self):
        """Variable bottom_zs should produce a polyhedron even with uniform
        top_zs."""
        lines = shell_body_lines(
            self.outline, _plain_enclosure(25.0), self.pts,
            top_zs=self.top_zs, bottom_zs=self.bottom_zs)
        scad = self._joined(lines)
        self.assertIn("polyhedron(", scad)
        self.assertNotIn("linear_extrude", scad)

    def test_bottom_ring_follows_bottom_zs(self):
        """First ring z-values must match bottom_zs."""
        N = len(self.pts)
        lines = shell_body_lines(
            self.outline, _plain_enclosure(25.0), self.pts,
            top_zs=self.top_zs, bottom_zs=self.bottom_zs)
        scad = self._joined(lines)
        pts = self._parse_points(scad)
        for i, bz in enumerate(self.bottom_zs):
            self.assertAlmostEqual(pts[i][2], bz, places=3,
                                   msg=f"bottom ring pt {i} z should be {bz}")

    def test_all_face_indices_valid(self):
        """Every face index must refer to a valid point."""
        lines = shell_body_lines(
            self.outline, _plain_enclosure(25.0), self.pts,
            top_zs=self.top_zs, bottom_zs=self.bottom_zs)
        scad = self._joined(lines)
        pts = self._parse_points(scad)
        faces = self._parse_faces(scad)
        for face in faces:
            for idx in face:
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, len(pts))

    def test_variable_vs_flat_bottom_same_point_count(self):
        """Variable and flat bottom use the same ring structure (R*N points)."""
        flat_lines = shell_body_lines(
            self.outline, _plain_enclosure(25.0), self.pts,
            top_zs=[20.0, 25.0, 30.0, 22.0])
        flat_pts = self._parse_points(self._joined(flat_lines))

        var_lines = shell_body_lines(
            self.outline, _plain_enclosure(25.0), self.pts,
            top_zs=[20.0, 25.0, 30.0, 22.0], bottom_zs=self.bottom_zs)
        var_pts = self._parse_points(self._joined(var_lines))

        self.assertEqual(len(var_pts), len(flat_pts))

    def test_uniform_bottom_nonzero_uses_polyhedron(self):
        """Uniform but non-zero bottom_zs should still trigger polyhedron."""
        bottom_zs = [3.0] * 4
        lines = shell_body_lines(
            self.outline, _plain_enclosure(25.0), self.pts,
            top_zs=self.top_zs, bottom_zs=bottom_zs)
        scad = self._joined(lines)
        # Uniform non-zero bottom → polyhedron (bot_z_max >= threshold)
        self.assertIn("polyhedron(", scad)

    def test_both_variable_top_and_bottom(self):
        """Variable top and bottom should produce valid polyhedron."""
        top_zs = [20.0, 25.0, 30.0, 22.0]
        bottom_zs = [0.0, 3.0, 5.0, 1.0]
        lines = shell_body_lines(
            self.outline, _plain_enclosure(20.0), self.pts,
            top_zs=top_zs, bottom_zs=bottom_zs)
        scad = self._joined(lines)
        self.assertIn("polyhedron(", scad)
        pts = self._parse_points(scad)
        faces = self._parse_faces(scad)
        for face in faces:
            for idx in face:
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, len(pts))

    def test_variable_bottom_with_fillet(self):
        """Variable bottom + fillet profiles should produce valid geometry."""
        bottom_zs = [0.0, 2.0, 4.0, 1.0]
        enc = _fillet_enclosure(25.0, 2.0)
        lines = shell_body_lines(
            self.outline, enc, self.pts,
            top_zs=self.top_zs, bottom_zs=bottom_zs)
        scad = self._joined(lines)
        self.assertIn("polyhedron(", scad)
        pts = self._parse_points(scad)
        faces = self._parse_faces(scad)
        for face in faces:
            for idx in face:
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, len(pts))

    def test_build_rings_with_bottom_zs(self):
        """_build_rings should use bottom_zs for the first ring."""
        bottom_zs = [1.0, 3.0, 5.0, 2.0]
        top_zs = [25.0] * 4
        rings = _build_rings(self.pts, top_zs, _plain_enclosure(), bottom_zs=bottom_zs)
        for i, bz in enumerate(bottom_zs):
            self.assertAlmostEqual(rings[0][i][2], bz, places=4)
        for i, tz in enumerate(top_zs):
            self.assertAlmostEqual(rings[-1][i][2], tz, places=4)

    def test_flat_bottom_zs_no_change(self):
        """All-zero bottom_zs should behave the same as None."""
        enc = _plain_enclosure(25.0)
        top_zs = [20.0, 25.0, 30.0, 22.0]
        lines_none = shell_body_lines(
            self.outline, enc, self.pts, top_zs=top_zs)
        lines_zero = shell_body_lines(
            self.outline, enc, self.pts, top_zs=top_zs,
            bottom_zs=[0.0] * 4)
        # Both should produce polyhedron (variable top), same content
        self.assertEqual(lines_none, lines_zero)


class TestPCBContour(unittest.TestCase):
    """Tests for pcb_contour_from_bottom_grid."""

    def test_all_flat_returns_none(self):
        """When all grid cells are below threshold, returns None."""
        from src.pipeline.design.height_field import (
            pcb_contour_from_bottom_grid, sample_bottom_height_grid,
        )

        outline = Outline(points=[
            OutlineVertex(0, 0, z_bottom=0.5),
            OutlineVertex(40, 0, z_bottom=0.5),
            OutlineVertex(40, 40, z_bottom=0.5),
            OutlineVertex(0, 40, z_bottom=0.5),
        ])
        enc = Enclosure(height_mm=25)
        grid = sample_bottom_height_grid(outline, enc, resolution_mm=2.0)
        self.assertIsNotNone(grid)
        result = pcb_contour_from_bottom_grid(grid, outline, threshold_mm=2.0)
        # z_bottom=0.5 everywhere — well below threshold, so no contour needed
        self.assertIsNone(result)

    def test_partial_raised_returns_polygon(self):
        """When some vertices exceed the threshold, returns a non-empty contour."""
        from src.pipeline.design.height_field import (
            pcb_contour_from_bottom_grid, sample_bottom_height_grid,
        )

        outline = Outline(points=[
            OutlineVertex(0, 0),
            OutlineVertex(60, 0),
            OutlineVertex(60, 60),
            OutlineVertex(0, 60, z_bottom=10),  # raised corner
        ])
        enc = Enclosure(height_mm=25)
        grid = sample_bottom_height_grid(outline, enc, resolution_mm=2.0)
        self.assertIsNotNone(grid)
        result = pcb_contour_from_bottom_grid(grid, outline, threshold_mm=2.0)
        # Should return a polygon (list of [x, y] pairs)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 2)
        # Each element is [x, y]
        for pt in result:
            self.assertEqual(len(pt), 2)

    def test_all_raised_returns_empty(self):
        """When the entire floor exceeds the threshold, returns []."""
        from src.pipeline.design.height_field import (
            pcb_contour_from_bottom_grid, sample_bottom_height_grid,
        )

        outline = Outline(points=[
            OutlineVertex(0, 0, z_bottom=10),
            OutlineVertex(40, 0, z_bottom=10),
            OutlineVertex(40, 40, z_bottom=10),
            OutlineVertex(0, 40, z_bottom=10),
        ])
        enc = Enclosure(height_mm=25)
        grid = sample_bottom_height_grid(outline, enc, resolution_mm=2.0)
        self.assertIsNotNone(grid)
        result = pcb_contour_from_bottom_grid(grid, outline, threshold_mm=2.0)
        # All raised — no flat region
        self.assertEqual(result, [])

    def test_contour_area_smaller_than_outline(self):
        """The contour polygon area should be smaller than the full outline."""
        from shapely.geometry import Polygon as ShapelyPolygon
        from src.pipeline.design.height_field import (
            pcb_contour_from_bottom_grid, sample_bottom_height_grid,
        )

        outline = Outline(points=[
            OutlineVertex(0, 0),
            OutlineVertex(60, 0),
            OutlineVertex(60, 60),
            OutlineVertex(0, 60, z_bottom=10),
        ])
        enc = Enclosure(height_mm=25)
        grid = sample_bottom_height_grid(outline, enc, resolution_mm=1.0)
        contour = pcb_contour_from_bottom_grid(grid, outline, threshold_mm=2.0)
        self.assertIsNotNone(contour)
        self.assertGreater(len(contour), 2)

        contour_poly = ShapelyPolygon(contour)
        outline_poly = ShapelyPolygon(outline.vertices)
        self.assertLess(contour_poly.area, outline_poly.area)


if __name__ == "__main__":
    unittest.main()
