"""SCAD fragment resolver — turns placed components into cutout geometry.

All component-specific behaviour is driven by catalog data (body.shape,
mounting.style, mounting.cap, pin positions).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from src.catalog.models import Component
from src.pipeline.config import CAVITY_START_MM, FLOOR_MM, PIN_FLOOR_PENETRATION, SPLIT_OVERLAP_MM, TRACE_RULES, component_z_range
from src.pipeline.design.models import Outline, Enclosure
from src.pipeline.pin_geometry import pin_shaft_dimensions
from src.pipeline.placer.models import PlacedComponent

from .fragment import (
    ScadFragment, RectGeometry, CylinderGeometry,
    PolygonGeometry, rotated_polygon, rotate_point,
)

SURFACE_OVERSHOOT: float = 1.0


@dataclass
class ResolverContext:
    """Shared data available to the resolver."""
    outline: Outline
    enclosure: Enclosure
    base_h: float
    ceil_start: float
    cavity_depth: float
    blended_height_fn: Callable[..., float]
    pause_z: float | None = None  # per-component insertion pause Z (caps pin grooves)
    part: str = "full"            # "full" | "bottom" | "top" — two-part filtering
    split_z: float | None = None  # Z where halves meet (two-part mode)


class ComponentResolver:
    """Resolves a single placed component into SCAD cutout fragments.

    Dispatches by ``mounting.style`` (top / bottom / side / internal)
    and uses catalog fields (cap, body shape, pin positions) to
    derive all geometry.
    """

    def __init__(
        self,
        placed: PlacedComponent,
        catalog: Component,
        ctx: ResolverContext,
    ) -> None:
        self.placed = placed
        self.catalog = catalog
        self.ctx = ctx
        self.cx = placed.x_mm
        self.cy = placed.y_mm
        self.rot = placed.rotation_deg
        self.cid = placed.instance_id

    def resolve(self) -> list[ScadFragment]:
        part = self.ctx.part

        style = self.placed.mounting_style or self.catalog.mounting.style
        if style == "top":
            frags = self._top_mount()
        elif style == "bottom":
            frags = self._bottom_mount()
        elif style == "side":
            frags = self._side_mount()
        else:
            frags = self._internal_mount()

        frags.extend(self._pinhole_fragments())
        frags.extend(self._scad_feature_fragments())

        if part == "top":
            # Top part: keep fragments whose top edge is above split_z,
            # plus ceiling cutouts.  Drop floor-level stuff.
            split_z = self.ctx.split_z or 0.0
            frags = [f for f in frags if (f.z_base + f.depth) > split_z + 0.01]
            frags.extend(self._top_only_ceiling_cutouts())
        elif part == "bottom":
            # Bottom part: remove ceiling cutouts
            frags = [f for f in frags if f.z_base < self.ctx.ceil_start - 0.01]

        return frags

    # ── Mounting-style handlers ────────────────────────────────────

    def _top_mount(self) -> list[ScadFragment]:
        frags: list[ScadFragment] = []
        body = self.catalog.body
        mounting = self.catalog.mounting
        s_depth = max(self._surface_depth(), 1.0)

        # Custom button outline: the ceiling hole matches the cap outline
        # (+ clearance) so the button top slides freely.
        if self.placed.button_outline is not None and mounting.cap is not None and mounting.cap.actuator is not None:
            from .buttons import _offset_polygon, BUTTON_CLEARANCE_MM
            hole = _offset_polygon(self.placed.button_outline, BUTTON_CLEARANCE_MM)
            # Translate to world position
            world_pts = [[p[0] + self.cx, p[1] + self.cy] for p in hole]
            frags.append(ScadFragment(
                type="cutout",
                geometry=PolygonGeometry(world_pts),
                z_base=self.ctx.ceil_start,
                depth=s_depth,
                label=f"button hole — {self.cid}",
            ))
        elif mounting.cap is not None:
            cap = mounting.cap
            cap_r = (cap.diameter_mm + 2 * cap.hole_clearance_mm) / 2
            # When an actuator is defined but no custom outline, the hole
            # matches the default cap circle so the button top slides freely.
            if cap.actuator is not None:
                from .buttons import _offset_polygon, BUTTON_CLEARANCE_MM
                cap_r = cap.diameter_mm / 2 + BUTTON_CLEARANCE_MM
                frags.append(ScadFragment(
                    type="cutout",
                    geometry=CylinderGeometry(self.cx, self.cy, cap_r),
                    z_base=self.ctx.ceil_start,
                    depth=s_depth,
                    label=f"button hole — {self.cid}",
                ))
            else:
                frags.append(ScadFragment(
                    type="cutout",
                    geometry=CylinderGeometry(self.cx, self.cy, cap_r),
                    z_base=self.ctx.ceil_start,
                    depth=s_depth,
                    label=f"cap hole — {self.cid}",
                ))
        elif body.shape == "circle":
            frags.append(ScadFragment(
                type="cutout",
                geometry=CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2),
                z_base=self.ctx.ceil_start,
                depth=s_depth,
                label=f"top surface hole — {self.cid}",
            ))
        else:
            frags.append(ScadFragment(
                type="cutout",
                geometry=self._rect_geom(body.width_mm, body.length_mm),
                z_base=self.ctx.ceil_start,
                depth=s_depth,
                label=f"top surface hole — {self.cid}",
            ))

        frags.append(self._body_pocket())

        _, body_top = self._z_range()
        gap = self.ctx.ceil_start - body_top
        if gap > 0:
            if body.shape == "circle":
                geom = CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2)
            else:
                geom = self._rect_geom(body.width_mm, body.length_mm)
            frags.append(ScadFragment(
                type="cutout",
                geometry=geom,
                z_base=body_top,
                depth=gap,
                label=f"top-mount upper cavity — {self.cid}",
            ))

        return frags

    def _bottom_mount(self) -> list[ScadFragment]:
        frags: list[ScadFragment] = []
        body = self.catalog.body
        mounting = self.catalog.mounting

        pocket_depth = min(body.height_mm, self.ctx.cavity_depth)

        if body.channels:
            ch = body.channels
            cz = CAVITY_START_MM + ch.center_z_mm
            r = ch.diameter_mm / 2

            if body.shape == "circle":
                pocket_geom = CylinderGeometry(
                    self.cx, self.cy, body.diameter_mm / 2,
                )
            else:
                pocket_geom = self._rect_geom(body.width_mm, body.length_mm)
            frags.append(ScadFragment(
                type="cutout",
                geometry=pocket_geom,
                z_base=CAVITY_START_MM,
                depth=ch.center_z_mm,
                label=f"channel pocket — {self.cid}",
            ))

            for i in range(ch.count):
                offset = (i - (ch.count - 1) / 2) * ch.spacing_mm
                if ch.axis == "y":
                    dx, dy = offset, 0.0
                    base_rot = (-90.0, 0.0, 0.0)
                else:
                    dx, dy = 0.0, offset
                    base_rot = (0.0, -90.0, 0.0)

                if self.rot:
                    dx, dy = rotate_point(dx, dy, self.rot)

                ccx = self.cx + dx
                ccy = self.cy + dy
                rot = (base_rot[0], base_rot[1], base_rot[2] + self.rot)

                frags.append(ScadFragment(
                    type="cutout",
                    geometry=CylinderGeometry(ccx, ccy, r),
                    z_base=cz,
                    depth=ch.length_mm,
                    label=f"cell channel {i + 1} — {self.cid}",
                    rotate_3d=rot,
                    clip_half="top",
                ))
        else:
            if body.shape == "circle":
                geom = CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2)
            else:
                geom = self._rect_geom(body.width_mm, body.length_mm)

            frags.append(ScadFragment(
                type="cutout",
                geometry=geom,
                z_base=CAVITY_START_MM,
                depth=pocket_depth,
                label=f"bottom-mount body — {self.cid}",
            ))

        return frags

    def _side_mount(self) -> list[ScadFragment]:
        body = self.catalog.body
        is_reoriented = self.catalog.mounting.style != "side"

        if is_reoriented:
            # Compute distance from component center to nearest outline edge
            # so the cutout cylinder extends far enough through the wall.
            wall_dist = self._distance_to_outline()
            # Minimum length: 2 * wall_dist + 10mm penetration, or body height
            min_length = max(body.height_mm, 2 * wall_dist + 10.0)

            if body.shape == "circle":
                geom = CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2)
                z_ext = min(body.diameter_mm, self.ctx.cavity_depth)
                return [ScadFragment(
                    type="cutout",
                    geometry=geom,
                    z_base=CAVITY_START_MM,
                    depth=z_ext,
                    tilt_deg=90,
                    tilt_length=min_length,
                    rotation_deg=self.rot + 90,
                    label=f"side slot — {self.cid}",
                )]
            else:
                geom = RectGeometry(self.cx, self.cy, body.width_mm, body.height_mm)
                z_ext = min(body.length_mm, self.ctx.cavity_depth)
                return [ScadFragment(
                    type="cutout",
                    geometry=geom,
                    z_base=CAVITY_START_MM,
                    depth=z_ext,
                    tilt_deg=90,
                    tilt_length=min_length,
                    rotation_deg=self.rot + 90,
                    label=f"side slot — {self.cid}",
                )]
        else:
            slot_depth = min(body.height_mm, self.ctx.cavity_depth)
            if body.shape == "circle":
                geom = CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2)
            else:
                geom = self._rect_geom(body.width_mm, body.length_mm)
            return [ScadFragment(
                type="cutout",
                geometry=geom,
                z_base=CAVITY_START_MM,
                depth=slot_depth,
                label=f"side slot — {self.cid}",
            )]

    def _internal_mount(self) -> list[ScadFragment]:
        return [self._body_pocket()]

    # ── Geometry helpers ───────────────────────────────────────────

    def _distance_to_outline(self) -> float:
        """Minimum distance from component center to the nearest outline edge."""
        verts = self.ctx.outline.vertices
        px, py = self.cx, self.cy
        best = float("inf")
        for i in range(len(verts)):
            ax, ay = verts[i]
            bx, by = verts[(i + 1) % len(verts)]
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            if seg_len2 < 1e-12:
                t = 0.0
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
            cx, cy = ax + t * dx, ay + t * dy
            d = math.hypot(px - cx, py - cy)
            if d < best:
                best = d
        return best

    def _dome_z(self) -> float:
        fn = self.ctx.blended_height_fn
        return fn(self.cx, self.cy, self.ctx.outline, self.ctx.enclosure)

    def _surface_depth(self) -> float:
        return self._dome_z() - self.ctx.ceil_start + SURFACE_OVERSHOOT

    def _rect_geom(self, w: float, h: float):
        if self.rot:
            pts = RectGeometry(self.cx, self.cy, w, h).to_polygon()
            pts = rotated_polygon(pts, self.rot, self.cx, self.cy)
            return PolygonGeometry(pts)
        return RectGeometry(self.cx, self.cy, w, h)

    def _body_pocket(self) -> ScadFragment:
        body = self.catalog.body
        body_floor, body_top = self._z_range()
        pocket_depth = body_top - body_floor
        if body.shape == "circle":
            geom = CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2)
        else:
            geom = self._rect_geom(body.width_mm, body.length_mm)
        return ScadFragment(
            type="cutout",
            geometry=geom,
            z_base=body_floor,
            depth=pocket_depth,
            label=f"body pocket — {self.cid}",
        )

    def _z_range(self) -> tuple[float, float]:
        """Return (body_floor_z, body_top_z) for this component."""
        style = self.placed.mounting_style or self.catalog.mounting.style
        return component_z_range(
            style,
            self.catalog.body.height_mm,
            self.catalog.pin_length_mm,
            self.ctx.ceil_start,
        )

    def _component_z_top(self) -> float:
        """Z where this component's body cutout ends."""
        style = self.placed.mounting_style or self.catalog.mounting.style
        if style == "side":
            body = self.catalog.body
            is_reoriented = self.catalog.mounting.style != "side"
            if is_reoriented:
                if body.shape == "circle":
                    z_ext = body.diameter_mm
                else:
                    z_ext = body.length_mm
                z_ext = min(z_ext, self.ctx.cavity_depth)
            else:
                z_ext = min(body.height_mm, self.ctx.cavity_depth)
            return CAVITY_START_MM + z_ext
        _, body_top = self._z_range()
        return body_top

    # ── Pinholes ───────────────────────────────────────────────────

    def _pinhole_fragments(self) -> list[ScadFragment]:
        """Pin shafts with smooth tapered funnel just below the body floor.

        Each pin gets up to two vertical zones:

          1. **Shaft** ((FLOOR_MM - penetration) → funnel_bottom): straight
             hole that descends a few layers below the trace surface,
             creating small holes on the trace layer for pins to seat into.
          2. **Tapered funnel** (funnel_bottom → body_floor_z): smooth
             cone/pyramid widening downward for easy pin insertion.
             The funnel sits directly below the body pocket so the
             narrow end feeds into the cavity.  The funnel is clamped
             to the body pocket width so it does not cut into adjacent
             walls.

        Pin holes stop at body_floor_z because the body pocket itself
        provides the opening above that level.
        """
        frags: list[ScadFragment] = []
        body = self.catalog.body
        body_floor, _ = self._z_range()

        funnel_top = body_floor
        funnel_bottom = max(funnel_top - TRACE_RULES.pinhole_taper_depth_mm, FLOOR_MM)
        actual_taper = funnel_top - funnel_bottom

        shaft_bottom = FLOOR_MM - PIN_FLOOR_PENETRATION
        shaft_h = max(funnel_bottom - shaft_bottom, 0.0)

        for pin in self.catalog.pins:
            pos = self.placed.pin_positions.get(pin.id)
            if pos is not None:
                px, py = pos[0], pos[1]
            else:
                px_rel, py_rel = float(pin.position_mm[0]), float(pin.position_mm[1])
                if self.rot:
                    px_rel, py_rel = rotate_point(px_rel, py_rel, self.rot)
                px = self.cx + px_rel
                py = self.cy + py_rel

            shaft_w, shaft_h_dim = pin_shaft_dimensions(pin)

            if shaft_h > 0:
                frags.append(ScadFragment(
                    type="cutout",
                    geometry=RectGeometry(px, py, shaft_w, shaft_h_dim),
                    z_base=shaft_bottom,
                    depth=shaft_h,
                    label=f"pin {self.cid}:{pin.id}",
                ))

            if actual_taper > 0:
                extra = TRACE_RULES.pinhole_taper_extra_mm
                scale_x = (shaft_w + extra) / shaft_w
                scale_y = (shaft_h_dim + extra) / shaft_h_dim
                taper = max(scale_x, scale_y)
                frags.append(ScadFragment(
                    type="cutout",
                    geometry=RectGeometry(px, py, shaft_w, shaft_h_dim),
                    z_base=funnel_bottom,
                    depth=actual_taper,
                    taper_scale=taper,
                    label=f"pin funnel {self.cid}:{pin.id}",
                ))

        return frags

    # ── Catalog scad_features ──────────────────────────────────────

    def _scad_feature_fragments(self) -> list[ScadFragment]:
        style = self.placed.mounting_style or self.catalog.mounting.style
        is_reoriented_side = (
            style == "side" and self.catalog.mounting.style != "side"
        )
        if style != self.catalog.mounting.style and not is_reoriented_side:
            return []

        if is_reoriented_side:
            return self._tilted_feature_fragments()

        frags: list[ScadFragment] = []
        for feat in self.catalog.scad_features:
            fx, fy = float(feat.position_mm[0]), float(feat.position_mm[1])
            if self.rot:
                fx, fy = rotate_point(fx, fy, self.rot)
            wx, wy = self.cx + fx, self.cy + fy

            if feat.z_anchor == "ground":
                z_base = 0.0
            elif feat.z_anchor == "floor":
                z_base = FLOOR_MM
            elif feat.z_anchor == "ceil_start":
                z_base = self.ctx.ceil_start
            else:
                z_base = CAVITY_START_MM

            # In top-part mode, extend cavity_start features down to
            # the bottom of the top shell so plate slits are open for
            # inserting metal plates from below.
            z_extend = 0.0
            if (self.ctx.part == "top" and self.ctx.split_z is not None
                    and feat.z_anchor in (None, "cavity_start", "")):
                top_bottom = self.ctx.split_z - SPLIT_OVERLAP_MM
                z_extend = z_base - top_bottom
                if z_extend > 0:
                    z_base = top_bottom
                else:
                    z_extend = 0.0

            if feat.through_surface:
                depth = max(self._surface_depth(), 1.0)
            elif feat.depth_mm:
                depth = feat.depth_mm + z_extend
            else:
                depth = self.ctx.cavity_depth

            if feat.pattern and feat.pattern.type == "grid":
                frags.extend(self._grid_pattern_fragments(
                    feat, wx, wy, z_base, depth,
                ))
            else:
                if feat.shape == "circle":
                    geom = CylinderGeometry(wx, wy, (feat.diameter_mm or 1.0) / 2)
                else:
                    w = feat.width_mm or 1.0
                    h = feat.length_mm or 1.0
                    geom = self._rect_geom_at(wx, wy, w, h)

                rotate_3d = None
                if feat.rotate:
                    rotate_3d = feat.rotate
                if feat.z_center_mm is not None:
                    z_base = z_base + feat.z_center_mm

                frags.append(ScadFragment(
                    type="cutout",
                    geometry=geom,
                    z_base=z_base,
                    depth=depth,
                    label=f"{feat.label} — {self.cid}",
                    rotate_3d=rotate_3d,
                ))
        return frags

    def _tilted_feature_fragments(self) -> list[ScadFragment]:
        """Emit SCAD features for a non-native side-mount with 90° tilt.

        Each feature's local (fx, fy) offset is transformed through the
        same rotate([0, 90, rot+90]) that the body uses, producing the
        correct world position for the tilted frame.
        """
        body = self.catalog.body
        if body.shape == "circle":
            z_ext = min(body.diameter_mm, self.ctx.cavity_depth)
        else:
            z_ext = min(body.length_mm, self.ctx.cavity_depth)
        body_z_center = CAVITY_START_MM + z_ext / 2

        theta = math.radians(self.rot + 90)
        cos_t, sin_t = math.cos(theta), math.sin(theta)

        frags: list[ScadFragment] = []
        for feat in self.catalog.scad_features:
            if feat.pattern:
                continue
            fx, fy = float(feat.position_mm[0]), float(feat.position_mm[1])

            feat_wy = self.cy + fx * sin_t + fy * cos_t
            feat_wz = body_z_center - (fx * cos_t - fy * sin_t)

            feat_depth = feat.depth_mm or self.ctx.cavity_depth
            feat_z_base = feat_wz - feat_depth / 2

            if feat.shape == "circle":
                geom = CylinderGeometry(self.cx, feat_wy, (feat.diameter_mm or 1.0) / 2)
            else:
                w = feat.width_mm or 1.0
                h = feat.length_mm or 1.0
                geom = RectGeometry(self.cx, feat_wy, w, h)

            frags.append(ScadFragment(
                type="cutout",
                geometry=geom,
                z_base=feat_z_base,
                depth=feat_depth,
                tilt_deg=90,
                tilt_length=feat_depth,
                rotation_deg=self.rot + 90,
                label=f"{feat.label} — {self.cid}",
            ))
        return frags

    def _grid_pattern_fragments(
        self, feat, cx: float, cy: float, z_base: float, depth: float,
    ) -> list[ScadFragment]:
        """Expand a single feature with a grid pattern into multiple fragments."""
        frags: list[ScadFragment] = []
        spacing = feat.pattern.spacing_mm
        body = self.catalog.body

        if feat.pattern.clip_to_body:
            if body.shape == "circle":
                limit_r = body.diameter_mm / 2 - spacing / 2
            else:
                limit_r = min(body.width_mm, body.length_mm) / 2 - spacing / 2
        else:
            limit_r = 1000.0

        n = int(limit_r / spacing)
        for ix in range(-n, n + 1):
            for iy in range(-n, n + 1):
                dx, dy = ix * spacing, iy * spacing
                if math.hypot(dx, dy) > limit_r:
                    continue
                if self.rot:
                    dx, dy = rotate_point(dx, dy, self.rot)

                if feat.shape == "circle":
                    geom = CylinderGeometry(
                        cx + dx, cy + dy, (feat.diameter_mm or 1.0) / 2,
                    )
                else:
                    w = feat.width_mm or 1.0
                    h = feat.length_mm or 1.0
                    geom = self._rect_geom_at(cx + dx, cy + dy, w, h)

                frags.append(ScadFragment(
                    type="cutout",
                    geometry=geom,
                    z_base=z_base,
                    depth=depth,
                    label=f"{feat.label} — {self.cid}",
                ))
        return frags

    def _rect_geom_at(self, cx: float, cy: float, w: float, h: float):
        """Rotated rect geometry centered at an absolute position."""
        if self.rot:
            pts = RectGeometry(cx, cy, w, h).to_polygon()
            pts = rotated_polygon(pts, self.rot, cx, cy)
            return PolygonGeometry(pts)
        return RectGeometry(cx, cy, w, h)

    # ── Two-part helpers ──────────────────────────────────────────────

    def _support_platform_fragments(self) -> list[ScadFragment]:
        """Solid platforms under components for two-part bottom tray.

        These are *additions* that fill the space from CAVITY_START_MM
        up to the component's body floor, so the component has something
        to rest on when the top shell is removed.
        """
        body = self.catalog.body
        style = self.placed.mounting_style or self.catalog.mounting.style

        if style in ("bottom",):
            # Bottom-mounted components already sit at CAVITY_START_MM
            return []

        body_floor, _ = self._z_range()
        platform_height = body_floor - CAVITY_START_MM
        if platform_height < 0.2:
            return []

        if body.shape == "circle":
            geom = CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2)
        else:
            geom = self._rect_geom(body.width_mm, body.length_mm)

        return [ScadFragment(
            type="addition",
            geometry=geom,
            z_base=CAVITY_START_MM,
            depth=platform_height,
            label=f"support platform — {self.cid}",
        )]

    def _top_only_ceiling_cutouts(self) -> list[ScadFragment]:
        """For the top part: only emit ceiling hole cutouts (buttons, LEDs).

        Skips body pockets, pin holes, bridges, and everything below
        the ceiling zone.
        """
        style = self.placed.mounting_style or self.catalog.mounting.style
        if style != "top":
            # Only top-mounted components punch through the ceiling
            return []

        frags: list[ScadFragment] = []
        body = self.catalog.body
        mounting = self.catalog.mounting
        s_depth = max(self._surface_depth(), 1.0)

        # Ceiling hole (same logic as _top_mount, just the ceiling cutout part)
        if self.placed.button_outline is not None and mounting.cap is not None and mounting.cap.actuator is not None:
            from .buttons import _offset_polygon, BUTTON_CLEARANCE_MM
            hole = _offset_polygon(self.placed.button_outline, BUTTON_CLEARANCE_MM)
            world_pts = [[p[0] + self.cx, p[1] + self.cy] for p in hole]
            frags.append(ScadFragment(
                type="cutout",
                geometry=PolygonGeometry(world_pts),
                z_base=self.ctx.ceil_start,
                depth=s_depth,
                label=f"button hole — {self.cid}",
            ))
        elif mounting.cap is not None:
            cap = mounting.cap
            cap_r = (cap.diameter_mm + 2 * cap.hole_clearance_mm) / 2
            if cap.actuator is not None:
                from .buttons import _offset_polygon, BUTTON_CLEARANCE_MM
                cap_r = cap.diameter_mm / 2 + BUTTON_CLEARANCE_MM
                frags.append(ScadFragment(
                    type="cutout",
                    geometry=CylinderGeometry(self.cx, self.cy, cap_r),
                    z_base=self.ctx.ceil_start,
                    depth=s_depth,
                    label=f"button hole — {self.cid}",
                ))
            else:
                frags.append(ScadFragment(
                    type="cutout",
                    geometry=CylinderGeometry(self.cx, self.cy, cap_r),
                    z_base=self.ctx.ceil_start,
                    depth=s_depth,
                    label=f"cap hole — {self.cid}",
                ))
        elif body.shape == "circle":
            frags.append(ScadFragment(
                type="cutout",
                geometry=CylinderGeometry(self.cx, self.cy, body.diameter_mm / 2),
                z_base=self.ctx.ceil_start,
                depth=s_depth,
                label=f"top surface hole — {self.cid}",
            ))
        else:
            frags.append(ScadFragment(
                type="cutout",
                geometry=self._rect_geom(body.width_mm, body.length_mm),
                z_base=self.ctx.ceil_start,
                depth=s_depth,
                label=f"top surface hole — {self.cid}",
            ))

        # Also emit scad_features that punch through the ceiling
        for feat in self.catalog.scad_features:
            if feat.through_surface:
                frags.extend(self._scad_feature_fragments())
                break

        return frags


def resolve_component(
    placed: PlacedComponent,
    catalog: Component,
    ctx: ResolverContext,
) -> list[ScadFragment]:
    """Resolve SCAD fragments for a placed component."""
    return ComponentResolver(placed, catalog, ctx).resolve()
