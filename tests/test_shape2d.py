"""Tests for src/pipeline/design/shape2d.py — 2D CSG tessellation & validation.

Coverage
--------
* Rectangle: basic, corner_radius, tapered (size_end+axis), triangle (size_end=0), rotated.
* Ellipse: circle, oval, rotated oval, capsule (end_center+radius_end).
* Boolean ops: union, difference, intersection, nested.
* Node-level transforms: rotate on ops, scale (uniform & per-axis), mirror (x/y/xy).
* Combined transforms: scale + mirror + rotate on a single node.
* Per-primitive z_top / z_bottom attribution.
* Negative tests: bad types, bad scale/mirror/rotate values, depth limit.
"""

from __future__ import annotations

import unittest

from src.pipeline.design.shape2d import validate_shape, tessellate_shape


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bbox(outline):
    """Return (min_x, min_y, max_x, max_y) for an Outline."""
    xs = [p.x for p in outline.points]
    ys = [p.y for p in outline.points]
    return min(xs), min(ys), max(xs), max(ys)


def _width(outline):
    bx = _bbox(outline)
    return bx[2] - bx[0]


def _height(outline):
    bx = _bbox(outline)
    return bx[3] - bx[1]


# ── Rectangle Validation ──────────────────────────────────────────────────────

class TestValidateRectangle(unittest.TestCase):

    def test_basic(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100]}
        self.assertEqual(validate_shape(node), [])

    def test_corner_radius(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100], "corner_radius": 8}
        self.assertEqual(validate_shape(node), [])

    def test_tapered(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [30, 80], "size_end": [10, 80], "axis": "y"}
        self.assertEqual(validate_shape(node), [])

    def test_triangle(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [30, 80], "size_end": [0, 80], "axis": "y"}
        self.assertEqual(validate_shape(node), [])

    def test_rotated(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [20, 60], "rotate": 45}
        self.assertEqual(validate_shape(node), [])

    def test_missing_center(self):
        node = {"type": "rectangle", "size": [10, 20]}
        errs = validate_shape(node)
        self.assertTrue(any("center" in e for e in errs))

    def test_missing_size(self):
        node = {"type": "rectangle", "center": [0, 0]}
        errs = validate_shape(node)
        self.assertTrue(any("size" in e for e in errs))

    def test_negative_size(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [-5, 10]}
        errs = validate_shape(node)
        self.assertTrue(any("positive" in e for e in errs))

    def test_bad_size_end_length(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "size_end": [5]}
        errs = validate_shape(node)
        self.assertTrue(any("size_end" in e for e in errs))

    def test_bad_axis(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "axis": "z"}
        errs = validate_shape(node)
        self.assertTrue(any("axis" in e for e in errs))

    def test_negative_corner_radius(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "corner_radius": -1}
        errs = validate_shape(node)
        self.assertTrue(any("corner_radius" in e for e in errs))


# ── Ellipse Validation ────────────────────────────────────────────────────────

class TestValidateEllipse(unittest.TestCase):

    def test_circle(self):
        node = {"type": "ellipse", "center": [25, 25], "radius": 20}
        self.assertEqual(validate_shape(node), [])

    def test_oval(self):
        node = {"type": "ellipse", "center": [25, 25], "radius": [20, 30]}
        self.assertEqual(validate_shape(node), [])

    def test_rotated_oval(self):
        node = {"type": "ellipse", "center": [25, 25], "radius": [20, 10], "rotate": 45}
        self.assertEqual(validate_shape(node), [])

    def test_capsule(self):
        node = {"type": "ellipse", "center": [50, 90], "radius": 8, "end_center": [20, 55], "radius_end": 3}
        self.assertEqual(validate_shape(node), [])

    def test_missing_radius(self):
        node = {"type": "ellipse", "center": [0, 0]}
        errs = validate_shape(node)
        self.assertTrue(any("radius" in e for e in errs))

    def test_negative_radius(self):
        node = {"type": "ellipse", "center": [0, 0], "radius": -5}
        errs = validate_shape(node)
        self.assertTrue(any("radius" in e for e in errs))

    def test_bad_radius_array(self):
        node = {"type": "ellipse", "center": [0, 0], "radius": [10, -5]}
        errs = validate_shape(node)
        self.assertTrue(any("radius" in e for e in errs))

    def test_bad_end_center(self):
        node = {"type": "ellipse", "center": [0, 0], "radius": 5, "end_center": [10]}
        errs = validate_shape(node)
        self.assertTrue(any("end_center" in e for e in errs))


# ── Boolean Operations ────────────────────────────────────────────────────────

class TestValidateOperations(unittest.TestCase):

    def test_union(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 10], "size": [20, 20]},
            {"type": "ellipse", "center": [25, 10], "radius": 10},
        ]}
        self.assertEqual(validate_shape(node), [])

    def test_difference(self):
        node = {"op": "difference", "children": [
            {"type": "rectangle", "center": [25, 50], "size": [50, 100]},
            {"type": "ellipse", "center": [0, 50], "radius": [8, 15]},
        ]}
        self.assertEqual(validate_shape(node), [])

    def test_intersection(self):
        node = {"op": "intersection", "children": [
            {"type": "rectangle", "center": [25, 40], "size": [50, 80]},
            {"type": "ellipse", "center": [25, 40], "radius": [30, 45]},
        ]}
        self.assertEqual(validate_shape(node), [])

    def test_unknown_op(self):
        node = {"op": "xor", "children": [
            {"type": "rectangle", "center": [0, 0], "size": [10, 10]},
            {"type": "rectangle", "center": [5, 5], "size": [10, 10]},
        ]}
        errs = validate_shape(node)
        self.assertTrue(any("xor" in e for e in errs))

    def test_too_few_children(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [0, 0], "size": [10, 10]},
        ]}
        errs = validate_shape(node)
        self.assertTrue(any("2 children" in e for e in errs))

    def test_nested_ops(self):
        node = {"op": "union", "children": [
            {"op": "difference", "children": [
                {"type": "rectangle", "center": [25, 50], "size": [50, 100]},
                {"type": "ellipse", "center": [0, 50], "radius": 10},
            ]},
            {"type": "ellipse", "center": [25, 10], "radius": 15},
        ]}
        self.assertEqual(validate_shape(node), [])

    def test_depth_limit(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10]}
        for _ in range(25):
            node = {"op": "union", "children": [
                node,
                {"type": "rectangle", "center": [0, 0], "size": [10, 10]},
            ]}
        errs = validate_shape(node)
        self.assertTrue(any("depth" in e for e in errs))

    def test_missing_type_and_op(self):
        node = {"center": [0, 0]}
        errs = validate_shape(node)
        self.assertTrue(any("type" in e or "op" in e for e in errs))

    def test_unknown_primitive_type(self):
        node = {"type": "hexagon", "center": [0, 0]}
        errs = validate_shape(node)
        self.assertTrue(any("hexagon" in e for e in errs))


# ── Node-Level Transform Validation ──────────────────────────────────────────

class TestValidateTransforms(unittest.TestCase):

    def test_rotate_on_primitive(self):
        node = {"type": "rectangle", "center": [10, 10], "size": [20, 40], "rotate": 30}
        self.assertEqual(validate_shape(node), [])

    def test_rotate_on_op(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 20], "size": [8, 30]},
            {"type": "ellipse", "center": [10, 5], "radius": 6},
        ], "rotate": 45}
        self.assertEqual(validate_shape(node), [])

    def test_rotate_bad_type(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "rotate": "abc"}
        errs = validate_shape(node)
        self.assertTrue(any("rotate" in e for e in errs))

    def test_scale_uniform(self):
        node = {"type": "rectangle", "center": [10, 10], "size": [20, 40], "scale": 1.5}
        self.assertEqual(validate_shape(node), [])

    def test_scale_per_axis(self):
        node = {"type": "ellipse", "center": [25, 25], "radius": 15, "scale": [1.0, 0.5]}
        self.assertEqual(validate_shape(node), [])

    def test_scale_on_op(self):
        node = {"op": "difference", "children": [
            {"type": "rectangle", "center": [25, 50], "size": [50, 100]},
            {"type": "ellipse", "center": [0, 50], "radius": [8, 15]},
        ], "scale": [1.2, 0.8]}
        self.assertEqual(validate_shape(node), [])

    def test_scale_zero_rejected(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "scale": 0}
        errs = validate_shape(node)
        self.assertTrue(any("scale" in e for e in errs))

    def test_scale_zero_in_array_rejected(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "scale": [1, 0]}
        errs = validate_shape(node)
        self.assertTrue(any("scale" in e for e in errs))

    def test_scale_bad_array_len(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "scale": [1, 2, 3]}
        errs = validate_shape(node)
        self.assertTrue(any("scale" in e for e in errs))

    def test_scale_bad_type(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "scale": "big"}
        errs = validate_shape(node)
        self.assertTrue(any("scale" in e for e in errs))

    def test_mirror_x(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "mirror": "x"}
        self.assertEqual(validate_shape(node), [])

    def test_mirror_y(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "mirror": "y"}
        self.assertEqual(validate_shape(node), [])

    def test_mirror_xy(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "mirror": "xy"}
        self.assertEqual(validate_shape(node), [])

    def test_mirror_on_op(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 20], "size": [10, 30]},
            {"type": "ellipse", "center": [10, 5], "radius": 8},
        ], "mirror": "y"}
        self.assertEqual(validate_shape(node), [])

    def test_mirror_bad_axis(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10], "mirror": "z"}
        errs = validate_shape(node)
        self.assertTrue(any("mirror" in e for e in errs))

    def test_combined_transforms(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 15], "size": [6, 20], "size_end": [2, 20], "axis": "y"},
            {"type": "ellipse", "center": [10, 5], "radius": 5},
        ], "rotate": 45, "scale": 0.8}
        self.assertEqual(validate_shape(node), [])

    def test_all_transforms_together(self):
        node = {"type": "rectangle", "center": [20, 30], "size": [10, 20],
                "rotate": 15, "scale": [1.5, 0.7], "mirror": "x"}
        self.assertEqual(validate_shape(node), [])


# ── Origin Validation ─────────────────────────────────────────────────────────

class TestValidateOrigin(unittest.TestCase):

    def test_origin_on_op(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 20], "size": [8, 30]},
            {"type": "ellipse", "center": [10, 5], "radius": 6},
        ], "rotate": 45, "origin": [10, 25]}
        self.assertEqual(validate_shape(node), [])

    def test_origin_bad_length(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 20], "size": [8, 30]},
            {"type": "ellipse", "center": [10, 5], "radius": 6},
        ], "origin": [10]}
        errs = validate_shape(node)
        self.assertTrue(any("origin" in e for e in errs))

    def test_origin_bad_type(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 20], "size": [8, 30]},
            {"type": "ellipse", "center": [10, 5], "radius": 6},
        ], "origin": "center"}
        errs = validate_shape(node)
        self.assertTrue(any("origin" in e for e in errs))


# ── Translate Validation ──────────────────────────────────────────────────────

class TestValidateTranslate(unittest.TestCase):

    def test_translate_on_primitive(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 20],
                "translate": [50, 30]}
        self.assertEqual(validate_shape(node), [])

    def test_translate_on_op(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 20], "size": [8, 30]},
            {"type": "ellipse", "center": [10, 5], "radius": 6},
        ], "translate": [100, 0]}
        self.assertEqual(validate_shape(node), [])

    def test_translate_bad_length(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10],
                "translate": [5]}
        errs = validate_shape(node)
        self.assertTrue(any("translate" in e for e in errs))

    def test_translate_bad_type(self):
        node = {"type": "rectangle", "center": [0, 0], "size": [10, 10],
                "translate": "up"}
        errs = validate_shape(node)
        self.assertTrue(any("translate" in e for e in errs))


# ── Tessellation Basic ────────────────────────────────────────────────────────

class TestTessellateBasic(unittest.TestCase):

    def test_rectangle_4_vertices(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100]}
        out = tessellate_shape(node)
        self.assertEqual(len(out.points), 4)

    def test_rectangle_bbox(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100]}
        out = tessellate_shape(node)
        bx = _bbox(out)
        self.assertAlmostEqual(bx[0], 0, places=1)
        self.assertAlmostEqual(bx[1], 0, places=1)
        self.assertAlmostEqual(bx[2], 50, places=1)
        self.assertAlmostEqual(bx[3], 100, places=1)

    def test_rounded_rectangle_more_vertices(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100], "corner_radius": 8}
        out = tessellate_shape(node)
        self.assertGreater(len(out.points), 4)

    def test_circle_many_vertices(self):
        node = {"type": "ellipse", "center": [25, 25], "radius": 20}
        out = tessellate_shape(node)
        self.assertGreater(len(out.points), 10)

    def test_tapered_rectangle(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [30, 80], "size_end": [10, 80], "axis": "y"}
        out = tessellate_shape(node)
        self.assertGreaterEqual(len(out.points), 4)

    def test_capsule(self):
        node = {"type": "ellipse", "center": [50, 90], "radius": 8, "end_center": [20, 55], "radius_end": 3}
        out = tessellate_shape(node)
        self.assertGreater(len(out.points), 4)

    def test_union(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [25, 50], "size": [50, 100]},
            {"type": "ellipse", "center": [25, 10], "radius": 20},
        ]}
        out = tessellate_shape(node)
        self.assertGreater(len(out.points), 4)

    def test_difference(self):
        node = {"op": "difference", "children": [
            {"type": "rectangle", "center": [25, 50], "size": [50, 100], "corner_radius": 10},
            {"type": "ellipse", "center": [0, 50], "radius": [10, 20]},
        ]}
        out = tessellate_shape(node)
        self.assertGreater(len(out.points), 4)

    def test_empty_result_raises(self):
        node = {"op": "difference", "children": [
            {"type": "rectangle", "center": [0, 0], "size": [10, 10]},
            {"type": "rectangle", "center": [0, 0], "size": [100, 100]},
        ]}
        with self.assertRaises(ValueError):
            tessellate_shape(node)


# ── Tessellation Transforms ──────────────────────────────────────────────────

class TestTessellateTransforms(unittest.TestCase):

    def test_scale_doubles_width(self):
        base = {"type": "rectangle", "center": [25, 50], "size": [20, 40]}
        scaled = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "scale": 2.0}
        w_base = _width(tessellate_shape(base))
        w_scaled = _width(tessellate_shape(scaled))
        self.assertAlmostEqual(w_scaled, w_base * 2, delta=0.2)

    def test_scale_per_axis(self):
        base = {"type": "rectangle", "center": [25, 50], "size": [20, 40]}
        scaled = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "scale": [2.0, 1.0]}
        w_base = _width(tessellate_shape(base))
        h_base = _height(tessellate_shape(base))
        out = tessellate_shape(scaled)
        self.assertAlmostEqual(_width(out), w_base * 2, delta=0.2)
        self.assertAlmostEqual(_height(out), h_base, delta=0.2)

    def test_mirror_x_symmetric_rect(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "mirror": "x"}
        out = tessellate_shape(node)
        self.assertEqual(len(out.points), 4)

    def test_mirror_y_preserves_shape(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [20, 40], "mirror": "y"}
        out = tessellate_shape(node)
        w = _width(out)
        self.assertAlmostEqual(w, 20, delta=0.2)

    def test_rotate_on_op_changes_vertices(self):
        children = [
            {"type": "rectangle", "center": [25, 50], "size": [50, 100]},
            {"type": "ellipse", "center": [25, 10], "radius": 20},
        ]
        out_no_rot = tessellate_shape({"op": "union", "children": children})
        out_rot = tessellate_shape({"op": "union", "children": children, "rotate": 45})
        xs_nr = [p.x for p in out_no_rot.points]
        xs_r = [p.x for p in out_rot.points]
        self.assertNotAlmostEqual(max(xs_nr), max(xs_r), delta=1.0)

    def test_scale_on_op(self):
        children = [
            {"type": "rectangle", "center": [25, 50], "size": [50, 100]},
            {"type": "ellipse", "center": [25, 10], "radius": 20},
        ]
        out_base = tessellate_shape({"op": "union", "children": children})
        out_scaled = tessellate_shape({"op": "union", "children": children, "scale": 0.5})
        self.assertAlmostEqual(_width(out_scaled), _width(out_base) * 0.5, delta=1.0)

    def test_combined_transforms_on_op(self):
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 15], "size": [6, 20], "size_end": [2, 20], "axis": "y"},
            {"type": "ellipse", "center": [10, 5], "radius": 5},
        ], "rotate": 45, "scale": 0.8}
        out = tessellate_shape(node)
        self.assertGreater(len(out.points), 4)

    def test_origin_changes_rotation_pivot(self):
        """Rotation with origin should pivot around the specified point, not centroid."""
        children = [
            {"type": "rectangle", "center": [50, 50], "size": [20, 20]},
            {"type": "rectangle", "center": [50, 80], "size": [20, 20]},
        ]
        # Rotate 90° around centroid (default)
        out_centroid = tessellate_shape({"op": "union", "children": children, "rotate": 90})
        # Rotate 90° around [50, 50] (top-left of the group)
        out_origin = tessellate_shape({"op": "union", "children": children, "rotate": 90, "origin": [50, 50]})
        # Different pivot should produce different bounding boxes
        bb_c = _bbox(out_centroid)
        bb_o = _bbox(out_origin)
        self.assertNotAlmostEqual(bb_c[0], bb_o[0], delta=1.0)

    def test_origin_at_junction_carries_children(self):
        """Rotating a parent group around a junction should move all children together."""
        # A branch starting at [50, 60] going to [30, 35] with a sub-branch
        branch = {"op": "union", "origin": [50, 60], "children": [
            {"type": "ellipse", "center": [50, 60], "radius": 5,
             "end_center": [30, 35], "radius_end": 2},
            {"type": "ellipse", "center": [30, 35], "radius": 2,
             "end_center": [20, 18], "radius_end": 1},
        ]}
        # Without rotation
        out_0 = tessellate_shape({**branch, "rotate": 0} if False else branch)
        bb_0 = _bbox(out_0)
        # With rotation — the junction point [50, 60] should stay fixed
        out_rot = tessellate_shape({**branch, "rotate": 30})
        bb_rot = _bbox(out_rot)
        # Bounding box should change (the group swung around the junction)
        self.assertNotAlmostEqual(bb_0[0], bb_rot[0], delta=1.0)

    def test_positive_rotation_is_ccw(self):
        """Positive rotate = CCW: rotate 90 should move top-right corner to top-left."""
        # A thin horizontal bar centered at origin
        node = {"type": "rectangle", "center": [50, 50], "size": [40, 10], "rotate": 90}
        out = tessellate_shape(node)
        # 90° CCW turns a wide-short rect into a narrow-tall rect
        w, h = _width(out), _height(out)
        self.assertAlmostEqual(w, 10, delta=0.5)
        self.assertAlmostEqual(h, 40, delta=0.5)

    def test_scale_before_rotate_on_primitive(self):
        """Transform order: scale then rotate. Scaling a rect 2x wide then rotating 90
        should produce a tall rect (height = 2*original width)."""
        node = {"type": "rectangle", "center": [50, 50], "size": [20, 10],
                "scale": [2.0, 1.0], "rotate": 90}
        out = tessellate_shape(node)
        # scale [2,1] makes it 40×10, then rotate 90 CCW makes it 10×40
        w, h = _width(out), _height(out)
        self.assertAlmostEqual(w, 10, delta=0.5)
        self.assertAlmostEqual(h, 40, delta=0.5)

    def test_translate_moves_primitive(self):
        """translate on a primitive should shift its position."""
        base = {"type": "rectangle", "center": [25, 50], "size": [20, 40]}
        shifted = {"type": "rectangle", "center": [25, 50], "size": [20, 40],
                   "translate": [100, 0]}
        bb_base = _bbox(tessellate_shape(base))
        bb_shifted = _bbox(tessellate_shape(shifted))
        self.assertAlmostEqual(bb_shifted[0], bb_base[0] + 100, delta=0.2)
        self.assertAlmostEqual(bb_shifted[2], bb_base[2] + 100, delta=0.2)
        # Y should be unchanged
        self.assertAlmostEqual(bb_shifted[1], bb_base[1], delta=0.2)

    def test_translate_moves_op(self):
        """translate on an operation should shift the combined geometry."""
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [10, 10], "size": [20, 20]},
            {"type": "ellipse", "center": [25, 10], "radius": 10},
        ], "translate": [0, 50]}
        out = tessellate_shape(node)
        bb = _bbox(out)
        # Everything should be shifted down by 50mm
        self.assertGreater(bb[1], 45)

    def test_translate_after_rotate(self):
        """translate is applied after rotation — rotate then move."""
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [0, 0], "size": [20, 60]},
            {"type": "rectangle", "center": [0, 0], "size": [60, 20]},
        ], "rotate": 45, "translate": [100, 100]}
        out = tessellate_shape(node)
        bb = _bbox(out)
        # After translate, all coords should be near [100, 100]
        self.assertGreater(bb[0], 50)
        self.assertGreater(bb[1], 50)

    def test_hierarchical_tree_structure(self):
        """A nested tree structure with origin at branch junctions should tessellate."""
        tree = {"op": "union", "children": [
            # Trunk
            {"type": "ellipse", "center": [50, 100], "radius": 8,
             "end_center": [50, 60], "radius_end": 5},
            # Left branch group
            {"op": "union", "rotate": -15, "origin": [50, 60], "children": [
                {"type": "ellipse", "center": [50, 60], "radius": 5,
                 "end_center": [30, 35], "radius_end": 2},
                # Sub-branch
                {"type": "ellipse", "center": [30, 35], "radius": 2,
                 "end_center": [20, 18], "radius_end": 1},
            ]},
            # Right branch group
            {"op": "union", "rotate": 15, "origin": [50, 60], "children": [
                {"type": "ellipse", "center": [50, 60], "radius": 5,
                 "end_center": [70, 35], "radius_end": 2},
                {"type": "ellipse", "center": [70, 35], "radius": 2,
                 "end_center": [80, 18], "radius_end": 1},
            ]},
        ]}
        self.assertEqual(validate_shape(tree), [])
        out = tessellate_shape(tree)
        self.assertGreater(len(out.points), 10)


# ── Z-Height Attribution ─────────────────────────────────────────────────────

class TestZAttribution(unittest.TestCase):

    def test_z_top_inherited(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100], "z_top": 30}
        out = tessellate_shape(node)
        for p in out.points:
            self.assertEqual(p.z_top, 30)

    def test_z_bottom_inherited(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100], "z_bottom": 5}
        out = tessellate_shape(node)
        for p in out.points:
            self.assertEqual(p.z_bottom, 5)

    def test_default_z_propagated(self):
        node = {"type": "rectangle", "center": [25, 50], "size": [50, 100]}
        out = tessellate_shape(node, default_z_top=20, default_z_bottom=2)
        for p in out.points:
            self.assertEqual(p.z_top, 20)
            self.assertEqual(p.z_bottom, 2)

    def test_z_follows_op_rotation(self):
        """z_top must follow vertices through op-level rotation."""
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [25, 25], "size": [50, 50], "z_top": 20},
            {"type": "rectangle", "center": [25, 75], "size": [50, 50], "z_top": 35},
        ], "rotate": 90}
        out = tessellate_shape(node)
        # rotate: 90 = 90° CCW — top (y=25) swings left, bottom (y=75) swings right
        for p in out.points:
            if p.x > 50:
                self.assertEqual(p.z_top, 35,
                    f"Right-side vertex ({p.x:.0f},{p.y:.0f}) should be z_top=35")
            elif p.x < 0:
                self.assertEqual(p.z_top, 20,
                    f"Left-side vertex ({p.x:.0f},{p.y:.0f}) should be z_top=20")

    def test_z_follows_op_scale(self):
        """z_top regions must scale with the geometry."""
        node = {"op": "union", "children": [
            {"type": "rectangle", "center": [25, 25], "size": [50, 50], "z_top": 15},
            {"type": "rectangle", "center": [25, 75], "size": [50, 50], "z_top": 30},
        ], "scale": 0.5}
        out = tessellate_shape(node)
        for p in out.points:
            if p.y < 50:
                self.assertEqual(p.z_top, 15,
                    f"Top vertex ({p.x:.0f},{p.y:.0f}) should be z_top=15")

    def test_z_with_primitive_scale(self):
        """Scaled primitive z_top must cover all output vertices."""
        node = {"type": "rectangle", "center": [25, 50], "size": [10, 10],
                "scale": 3.0, "z_top": 30}
        out = tessellate_shape(node)
        for p in out.points:
            self.assertEqual(p.z_top, 30)


# ── Complex Tree ──────────────────────────────────────────────────────────────

class TestComplexTree(unittest.TestCase):

    def test_mixed_primitives_and_transforms(self):
        tree = {
            "op": "union",
            "children": [
                {"type": "rectangle", "center": [25, 50], "size": [50, 100], "corner_radius": 5},
                {"type": "rectangle", "center": [50, 30], "size": [12, 40],
                 "size_end": [4, 40], "axis": "y", "rotate": -30},
                {"type": "ellipse", "center": [10, 20], "radius": 8,
                 "end_center": [40, 10], "radius_end": 3},
            ],
        }
        self.assertEqual(validate_shape(tree), [])
        out = tessellate_shape(tree)
        self.assertGreater(len(out.points), 10)

    def test_nested_with_transforms(self):
        tree = {
            "op": "difference",
            "children": [
                {"op": "union", "children": [
                    {"type": "rectangle", "center": [25, 50], "size": [50, 100], "corner_radius": 8},
                    {"type": "ellipse", "center": [25, 5], "radius": 20},
                ], "scale": 1.1},
                {"type": "ellipse", "center": [0, 50], "radius": [10, 18]},
                {"type": "ellipse", "center": [50, 50], "radius": [10, 18], "mirror": "x"},
            ],
            "rotate": 5,
        }
        self.assertEqual(validate_shape(tree), [])
        out = tessellate_shape(tree)
        self.assertGreater(len(out.points), 10)


if __name__ == "__main__":
    unittest.main()
