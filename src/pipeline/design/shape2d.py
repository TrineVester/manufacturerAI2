"""2D CSG tessellator — convert a shape tree into an Outline vertex list.

Primitives: rectangle, ellipse.
Operations: union, difference, intersection.

Primitives support optional modifiers:
  - rectangle: size_end + axis (taper), corner_radius
  - ellipse: end_center + radius_end (tapered capsule)

Node-level transforms (apply to any primitive or operation result):
  - rotate: degrees  — positive = counter-clockwise (CCW), standard CAD
                        convention; around center (primitives) or
                        origin/centroid (ops)
  - scale: number | [sx, sy]  — uniform or per-axis scaling
  - mirror: "x" | "y" | "xy"  — reflection across axis
  - translate: [dx, dy]  — offset applied after all other transforms
  - origin: [x, y]  — pivot point for rotate/scale/mirror on operations
                       (defaults to centroid if omitted)

Transform order: scale → mirror → rotate → translate.

Each primitive can carry optional z_top / z_bottom values.
"""

from __future__ import annotations

from shapely.affinity import (
    rotate as _shapely_rotate,
    scale as _shapely_scale,
    translate as _shapely_translate,
)
from shapely.geometry import Polygon, Point, box
from shapely.validation import make_valid

from .models import Outline, OutlineVertex

RESOLUTION = 16  # quarter-circle segments for curves


# -- Validation ----------------------------------------------------------------

def validate_shape(node: dict) -> list[str]:
    """Validate a CSG tree structure. Returns error messages (empty = valid)."""
    errors: list[str] = []
    _validate_node(node, errors, depth=0)
    return errors


def _validate_node(node: dict, errors: list[str], depth: int) -> None:
    if depth > 20:
        errors.append("CSG tree exceeds maximum nesting depth (20)")
        return

    _validate_transforms(node, errors)

    if "op" in node:
        op = node["op"]
        if op not in ("union", "difference", "intersection"):
            errors.append(f"Unknown operation '{op}' (expected: union, difference, intersection)")
        children = node.get("children")
        if not isinstance(children, list) or len(children) < 2:
            errors.append(f"Operation '{op}' requires at least 2 children")
        else:
            for child in children:
                if not isinstance(child, dict):
                    errors.append("Each child must be an object")
                else:
                    _validate_node(child, errors, depth + 1)
    elif "type" in node:
        ptype = node["type"]
        if ptype == "rectangle":
            _validate_rectangle(node, errors)
        elif ptype == "ellipse":
            _validate_ellipse(node, errors)
        else:
            errors.append(f"Unknown primitive type '{ptype}' (expected: rectangle, ellipse)")
    else:
        errors.append("Node must have either 'type' (primitive) or 'op' (operation)")


def _validate_transforms(node: dict, errors: list[str]) -> None:
    """Validate node-level transforms (rotate, scale, mirror, translate, origin)."""
    rotate = node.get("rotate")
    if rotate is not None and not isinstance(rotate, (int, float)):
        errors.append("'rotate' must be a number (degrees)")
    scale_val = node.get("scale")
    if scale_val is not None:
        if isinstance(scale_val, list):
            if len(scale_val) != 2:
                errors.append("'scale' as array must be [sx, sy]")
            elif any(v == 0 for v in scale_val):
                errors.append("'scale' values must be non-zero")
        elif isinstance(scale_val, (int, float)):
            if scale_val == 0:
                errors.append("'scale' must be non-zero")
        else:
            errors.append("'scale' must be a number or [sx, sy]")
    mirror = node.get("mirror")
    if mirror is not None and mirror not in ("x", "y", "xy"):
        errors.append("'mirror' must be 'x', 'y', or 'xy'")
    translate = node.get("translate")
    if translate is not None:
        if not isinstance(translate, list) or len(translate) != 2:
            errors.append("'translate' must be [dx, dy]")
        elif not all(isinstance(v, (int, float)) for v in translate):
            errors.append("'translate' values must be numbers")
    origin = node.get("origin")
    if origin is not None:
        if not isinstance(origin, list) or len(origin) != 2:
            errors.append("'origin' must be [x, y]")
        elif not all(isinstance(v, (int, float)) for v in origin):
            errors.append("'origin' values must be numbers")


def _validate_rectangle(node: dict, errors: list[str]) -> None:
    center = node.get("center")
    size = node.get("size")
    if not isinstance(center, list) or len(center) != 2:
        errors.append("Rectangle requires 'center' as [x, y]")
    if not isinstance(size, list) or len(size) != 2:
        errors.append("Rectangle requires 'size' as [width, height]")
    elif any(v <= 0 for v in size):
        errors.append("Rectangle size values must be positive")
    cr = node.get("corner_radius", 0)
    if cr < 0:
        errors.append("Rectangle corner_radius must be >= 0")
    size_end = node.get("size_end")
    if size_end is not None:
        if not isinstance(size_end, list) or len(size_end) != 2:
            errors.append("Rectangle size_end must be [width, height]")
        elif any(v < 0 for v in size_end):
            errors.append("Rectangle size_end values must be >= 0")
    axis = node.get("axis")
    if axis is not None and axis not in ("x", "y"):
        errors.append("Rectangle axis must be 'x' or 'y'")


def _validate_ellipse(node: dict, errors: list[str]) -> None:
    center = node.get("center")
    radius = node.get("radius")
    if not isinstance(center, list) or len(center) != 2:
        errors.append("Ellipse requires 'center' as [x, y]")
    if radius is None:
        errors.append("Ellipse requires 'radius' (number or [rx, ry])")
    elif isinstance(radius, list):
        if len(radius) != 2 or any(v <= 0 for v in radius):
            errors.append("Ellipse radius as [rx, ry] must have 2 positive values")
    elif not isinstance(radius, (int, float)) or radius <= 0:
        errors.append("Ellipse radius must be a positive number")
    end_center = node.get("end_center")
    if end_center is not None:
        if not isinstance(end_center, list) or len(end_center) != 2:
            errors.append("Ellipse end_center must be [x, y]")
    radius_end = node.get("radius_end")
    if radius_end is not None:
        if isinstance(radius_end, list):
            if len(radius_end) != 2 or any(v <= 0 for v in radius_end):
                errors.append("Ellipse radius_end as [rx, ry] must have 2 positive values")
        elif not isinstance(radius_end, (int, float)) or radius_end <= 0:
            errors.append("Ellipse radius_end must be a positive number")


# -- Tessellation entry point -------------------------------------------------

def tessellate_shape(node: dict, default_z_top: float | None = None,
                     default_z_bottom: float | None = None) -> Outline:
    """Convert a CSG shape tree into an Outline with z-attributed vertices."""
    geom = _eval_csg(node)
    if geom.is_empty:
        raise ValueError("CSG shape evaluates to an empty polygon")
    geom = _ensure_polygon(geom)

    primitives = _collect_primitives(node, default_z_top, default_z_bottom)
    vertices = _attribute_vertices(geom, primitives)

    holes: list[list[OutlineVertex]] = []
    for interior in geom.interiors:
        coords = list(interior.coords)[:-1]
        hole_verts = [
            OutlineVertex(x=round(x, 2), y=round(y, 2))
            for x, y in coords
        ]
        holes.append(hole_verts)

    return Outline(points=vertices, holes=holes)


# -- Internal types -----------------------------------------------------------

class _TaggedPoly:
    """A Shapely polygon with z_top/z_bottom metadata."""
    __slots__ = ("geom", "z_top", "z_bottom")

    def __init__(self, geom: Polygon, z_top: float | None, z_bottom: float | None):
        self.geom = geom
        self.z_top = z_top
        self.z_bottom = z_bottom


# -- CSG evaluation -----------------------------------------------------------

def _eval_csg(node: dict) -> Polygon:
    """Evaluate the CSG tree, returning the final Shapely geometry."""
    if "type" in node:
        geom = _make_primitive(node)
    else:
        op = node["op"]
        children = node["children"]
        geom = _eval_csg(children[0])
        for child in children[1:]:
            child_geom = _eval_csg(child)
            if op == "union":
                geom = geom.union(child_geom)
            elif op == "difference":
                geom = geom.difference(child_geom)
            elif op == "intersection":
                geom = geom.intersection(child_geom)
            else:
                raise ValueError(f"Unknown operation: {op}")

    if not geom.is_valid:
        geom = make_valid(geom)

    geom = _apply_transforms(geom, node)
    return geom


def _get_transform_origin(geom: Polygon, node: dict):
    """Return the pivot point for transforms: explicit origin or centroid."""
    origin_spec = node.get("origin")
    if origin_spec:
        return (origin_spec[0], origin_spec[1])
    return geom.centroid


def _apply_transforms(geom: Polygon, node: dict) -> Polygon:
    """Apply node-level transforms: scale → mirror → rotate → translate."""
    if "type" in node:
        cx, cy = node["center"]
        pivot = (cx, cy)
    else:
        pivot = _get_transform_origin(geom, node)

    scale_val = node.get("scale")
    if scale_val is not None:
        if isinstance(scale_val, (int, float)):
            sx = sy = float(scale_val)
        else:
            sx, sy = float(scale_val[0]), float(scale_val[1])
        geom = _shapely_scale(geom, xfact=sx, yfact=sy, origin=pivot)

    mirror = node.get("mirror")
    if mirror:
        mx = -1.0 if "x" in mirror else 1.0
        my = -1.0 if "y" in mirror else 1.0
        geom = _shapely_scale(geom, xfact=mx, yfact=my, origin=pivot)

    rotate = node.get("rotate")
    if rotate:
        geom = _shapely_rotate(geom, -rotate, origin=pivot)

    translate = node.get("translate")
    if translate:
        geom = _shapely_translate(geom, xoff=translate[0], yoff=translate[1])

    return geom


def _make_primitive(node: dict) -> Polygon:
    ptype = node["type"]
    if ptype == "rectangle":
        geom = _make_rectangle(node)
    elif ptype == "ellipse":
        geom = _make_ellipse(node)
    else:
        raise ValueError(f"Unknown primitive type: {ptype}")
    return geom


def _collect_primitives(node: dict, default_z_top: float | None,
                        default_z_bottom: float | None) -> list[_TaggedPoly]:
    """Walk the tree and collect all leaf primitives with their z values.

    Applies the same transforms as _eval_csg so tagged polygons match
    the final tessellated outline's coordinate space.
    """
    if "type" in node:
        z_top = node.get("z_top", default_z_top)
        z_bottom = node.get("z_bottom", default_z_bottom)
        geom = _make_primitive(node)
        geom = _apply_prim_transforms(geom, node)
        return [_TaggedPoly(geom, z_top, z_bottom)]

    result: list[_TaggedPoly] = []
    for child in node.get("children", []):
        result.extend(_collect_primitives(child, default_z_top, default_z_bottom))

    if _has_transforms(node):
        combined = result[0].geom
        for tp in result[1:]:
            combined = combined.union(tp.geom)
        origin_spec = node.get("origin")
        origin = (origin_spec[0], origin_spec[1]) if origin_spec else combined.centroid
        result = [
            _TaggedPoly(
                _apply_node_transforms(tp.geom, node, origin),
                tp.z_top, tp.z_bottom,
            )
            for tp in result
        ]

    return result


def _has_transforms(node: dict) -> bool:
    return (node.get("rotate") or node.get("scale") is not None
            or node.get("mirror") or node.get("translate"))


def _apply_prim_transforms(geom: Polygon, node: dict) -> Polygon:
    """Apply scale → mirror → rotate → translate to a primitive around its center."""
    cx, cy = node["center"]
    pivot = (cx, cy)

    scale_val = node.get("scale")
    if scale_val is not None:
        if isinstance(scale_val, (int, float)):
            sx = sy = float(scale_val)
        else:
            sx, sy = float(scale_val[0]), float(scale_val[1])
        geom = _shapely_scale(geom, xfact=sx, yfact=sy, origin=pivot)

    mirror = node.get("mirror")
    if mirror:
        mx = -1.0 if "x" in mirror else 1.0
        my = -1.0 if "y" in mirror else 1.0
        geom = _shapely_scale(geom, xfact=mx, yfact=my, origin=pivot)

    rotate = node.get("rotate")
    if rotate:
        geom = _shapely_rotate(geom, -rotate, origin=pivot)

    translate = node.get("translate")
    if translate:
        geom = _shapely_translate(geom, xoff=translate[0], yoff=translate[1])

    return geom


def _apply_node_transforms(geom: Polygon, node: dict, origin) -> Polygon:
    """Apply scale, mirror, rotate, translate to a geometry using a shared origin."""
    scale_val = node.get("scale")
    if scale_val is not None:
        if isinstance(scale_val, (int, float)):
            sx = sy = float(scale_val)
        else:
            sx, sy = float(scale_val[0]), float(scale_val[1])
        geom = _shapely_scale(geom, xfact=sx, yfact=sy, origin=origin)

    mirror = node.get("mirror")
    if mirror:
        mx = -1.0 if "x" in mirror else 1.0
        my = -1.0 if "y" in mirror else 1.0
        geom = _shapely_scale(geom, xfact=mx, yfact=my, origin=origin)

    rotate = node.get("rotate")
    if rotate:
        geom = _shapely_rotate(geom, -rotate, origin=origin)

    translate = node.get("translate")
    if translate:
        geom = _shapely_translate(geom, xoff=translate[0], yoff=translate[1])

    return geom


# -- Primitive geometry --------------------------------------------------------

def _make_rectangle(node: dict) -> Polygon:
    cx, cy = node["center"]
    w, h = node["size"]
    cr = node.get("corner_radius", 0)
    size_end = node.get("size_end")

    if size_end is None:
        base = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        if cr > 0:
            cr = min(cr, w / 2, h / 2)
            shrunk = box(cx - w / 2 + cr, cy - h / 2 + cr,
                         cx + w / 2 - cr, cy + h / 2 - cr)
            return shrunk.buffer(cr, resolution=RESOLUTION)
        return base

    axis = node.get("axis", "y")
    w_end, h_end = size_end

    if axis == "y":
        poly = Polygon([
            (cx - w / 2,     cy - h / 2),
            (cx + w / 2,     cy - h / 2),
            (cx + w_end / 2, cy + h / 2),
            (cx - w_end / 2, cy + h / 2),
        ])
    else:
        poly = Polygon([
            (cx - w / 2, cy - h / 2),
            (cx - w / 2, cy + h / 2),
            (cx + w / 2, cy + h_end / 2),
            (cx + w / 2, cy - h_end / 2),
        ])

    if cr > 0:
        eroded = poly.buffer(-cr, resolution=RESOLUTION)
        if not eroded.is_empty:
            return eroded.buffer(cr, resolution=RESOLUTION)
    return poly


def _make_ellipse(node: dict) -> Polygon:
    cx, cy = node["center"]
    radius = node["radius"]
    if isinstance(radius, (int, float)):
        rx = ry = float(radius)
    else:
        rx, ry = float(radius[0]), float(radius[1])

    start = Point(cx, cy).buffer(1.0, resolution=RESOLUTION)
    start = _shapely_scale(start, xfact=rx, yfact=ry, origin=(cx, cy))

    end_center = node.get("end_center")
    if end_center is not None:
        ex, ey = end_center
        r_end = node.get("radius_end")
        if r_end is None:
            erx, ery = rx, ry
        elif isinstance(r_end, (int, float)):
            erx = ery = float(r_end)
        else:
            erx, ery = float(r_end[0]), float(r_end[1])
        end = Point(ex, ey).buffer(1.0, resolution=RESOLUTION)
        end = _shapely_scale(end, xfact=erx, yfact=ery, origin=(ex, ey))
        return start.union(end).convex_hull

    return start


# -- Vertex attribution -------------------------------------------------------

def _ensure_polygon(geom) -> Polygon:
    """Extract the largest polygon from the geometry result."""
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda p: p.area)
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type == "Polygon"]
        if polys:
            return max(polys, key=lambda p: p.area)
    raise ValueError(f"CSG result is not a polygon: {geom.geom_type}")


def _attribute_vertices(geom: Polygon, primitives: list[_TaggedPoly]) -> list[OutlineVertex]:
    """Assign z_top/z_bottom to each vertex of the final polygon.

    For each output vertex, find which source primitives contain it.
    Use max(z_top) and max(z_bottom) from containing/nearest primitives.
    """
    coords = list(geom.exterior.coords)[:-1]  # drop closing duplicate
    vertices: list[OutlineVertex] = []

    for x, y in coords:
        pt = Point(x, y)
        z_top = None
        z_bottom = None

        for tp in primitives:
            if tp.geom.contains(pt) or tp.geom.boundary.distance(pt) < 0.01:
                if tp.z_top is not None:
                    z_top = max(z_top, tp.z_top) if z_top is not None else tp.z_top
                if tp.z_bottom is not None:
                    z_bottom = max(z_bottom, tp.z_bottom) if z_bottom is not None else tp.z_bottom

        if z_top is None and z_bottom is None:
            nearest = min(primitives, key=lambda tp: tp.geom.distance(pt))
            z_top = nearest.z_top
            z_bottom = nearest.z_bottom

        vertices.append(OutlineVertex(
            x=round(x, 2),
            y=round(y, 2),
            z_top=z_top,
            z_bottom=z_bottom,
        ))

    return vertices
