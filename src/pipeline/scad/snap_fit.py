"""snap_fit.py — snap-fit post/clip geometry for two-part enclosures.

Generates ScadFragment objects for:
  - Male snap posts (additions on the bottom part's inner wall)
  - Female snap clips (cutouts on the top part's inner wall)
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass

from src.pipeline.config import (
    SNAP_POST_WIDTH, SNAP_POST_HEIGHT, SNAP_POST_THICKNESS,
    SNAP_BARB_MM, SNAP_CLEARANCE_MM, SNAP_SPACING_MM, MIN_SNAP_POSTS,
    SPLIT_OVERLAP_MM,
)
from .fragment import ScadFragment, RectGeometry, PolygonGeometry

log = logging.getLogger(__name__)


@dataclass
class SnapPosition:
    """A snap-fit location on the outline perimeter."""
    x: float          # position on the wall (mm)
    y: float
    angle_deg: float  # outward wall-normal angle (degrees, 0 = +X)


def _polygon_perimeter(pts: list[list[float]]) -> float:
    """Total perimeter length of a closed polygon."""
    n = len(pts)
    return sum(
        math.hypot(pts[(i + 1) % n][0] - pts[i][0],
                    pts[(i + 1) % n][1] - pts[i][1])
        for i in range(n)
    )


def compute_snap_positions(
    flat_pts: list[list[float]],
    spacing: float = SNAP_SPACING_MM,
    min_posts: int = MIN_SNAP_POSTS,
) -> list[SnapPosition]:
    """Distribute snap-fit positions evenly around the outline perimeter.

    Returns positions on the *inner* face of the wall (inset by a small
    amount is done by the caller using the wall-normal angle).
    """
    n = len(flat_pts)
    perimeter = _polygon_perimeter(flat_pts)
    num_posts = max(min_posts, math.ceil(perimeter / spacing))
    target_spacing = perimeter / num_posts

    # Walk the perimeter placing posts at regular intervals
    positions: list[SnapPosition] = []
    accum = 0.0
    next_at = target_spacing / 2  # start half a spacing in

    for i in range(n):
        j = (i + 1) % n
        dx = flat_pts[j][0] - flat_pts[i][0]
        dy = flat_pts[j][1] - flat_pts[i][1]
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-9:
            continue

        # Outward normal (for CCW polygon: rotate edge 90° CW)
        nx = dy / seg_len
        ny = -dx / seg_len
        normal_angle = math.degrees(math.atan2(ny, nx))

        seg_start = accum
        seg_end = accum + seg_len

        while next_at < seg_end and len(positions) < num_posts:
            t = (next_at - seg_start) / seg_len
            px = flat_pts[i][0] + t * dx
            py = flat_pts[i][1] + t * dy
            positions.append(SnapPosition(px, py, normal_angle))
            next_at += target_spacing

        accum = seg_end

    log.info("Snap-fit: %d positions on %.1f mm perimeter (%.1f mm spacing)",
             len(positions), perimeter, target_spacing)
    return positions


def snap_post_fragments(
    positions: list[SnapPosition],
    split_z: float,
) -> list[ScadFragment]:
    """Generate male snap-post fragments (additions) for the bottom part.

    Each post is a rectangular tab protruding upward from the inner wall
    at the split height, with a small barb at the tip.
    """
    frags: list[ScadFragment] = []

    for i, pos in enumerate(positions):
        # Post body: rect centered at (pos.x, pos.y) oriented along wall normal
        # The post protrudes inward from the wall
        angle_rad = math.radians(pos.angle_deg)
        # Offset inward by half the thickness
        inset = SNAP_POST_THICKNESS / 2
        cx = pos.x - inset * math.cos(angle_rad)
        cy = pos.y - inset * math.sin(angle_rad)

        frags.append(ScadFragment(
            type="addition",
            geometry=RectGeometry(cx, cy, SNAP_POST_WIDTH, SNAP_POST_THICKNESS),
            z_base=split_z - 1.0,  # starts slightly below split for anchoring
            depth=SNAP_POST_HEIGHT + 1.0,
            rotation_deg=pos.angle_deg,
            rotate_cx=cx,
            rotate_cy=cy,
            label=f"snap post {i}",
        ))

        # Barb: small triangle bump near the tip — modeled as a thin rect
        barb_z = split_z + SNAP_POST_HEIGHT - 0.8
        barb_cx = pos.x - (SNAP_POST_THICKNESS / 2 + SNAP_BARB_MM / 2) * math.cos(angle_rad)
        barb_cy = pos.y - (SNAP_POST_THICKNESS / 2 + SNAP_BARB_MM / 2) * math.sin(angle_rad)

        frags.append(ScadFragment(
            type="addition",
            geometry=RectGeometry(barb_cx, barb_cy, SNAP_POST_WIDTH * 0.7, SNAP_BARB_MM),
            z_base=barb_z,
            depth=0.8,
            rotation_deg=pos.angle_deg,
            rotate_cx=barb_cx,
            rotate_cy=barb_cy,
            label=f"snap barb {i}",
        ))

    log.info("Generated %d snap post fragments (%d posts)", len(frags), len(positions))
    return frags


def snap_clip_fragments(
    positions: list[SnapPosition],
    split_z: float,
) -> list[ScadFragment]:
    """Generate female snap-clip fragments (cutouts) for the top part.

    Each clip is a rectangular slot in the inner wall, sized with clearance
    to accept the male post + barb.
    """
    frags: list[ScadFragment] = []

    slot_w = SNAP_POST_WIDTH + 2 * SNAP_CLEARANCE_MM
    slot_d = SNAP_POST_THICKNESS + 2 * SNAP_CLEARANCE_MM
    slot_h = SNAP_POST_HEIGHT + SNAP_CLEARANCE_MM

    for i, pos in enumerate(positions):
        angle_rad = math.radians(pos.angle_deg)
        inset = slot_d / 2
        cx = pos.x - inset * math.cos(angle_rad)
        cy = pos.y - inset * math.sin(angle_rad)

        # The slot starts at the bottom of the top part (split_z - overlap)
        # and extends up enough to accept the post
        slot_z_base = split_z - SPLIT_OVERLAP_MM
        slot_total_h = slot_h + SPLIT_OVERLAP_MM

        frags.append(ScadFragment(
            type="cutout",
            geometry=RectGeometry(cx, cy, slot_w, slot_d),
            z_base=slot_z_base,
            depth=slot_total_h,
            rotation_deg=pos.angle_deg,
            rotate_cx=cx,
            rotate_cy=cy,
            label=f"snap clip slot {i}",
        ))

    log.info("Generated %d snap clip fragments", len(frags))
    return frags
