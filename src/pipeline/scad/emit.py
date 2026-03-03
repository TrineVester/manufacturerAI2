"""emit.py — assemble shell-body lines and cutouts into a .scad file string.

The final SCAD structure is::

    // header
    $fn = 32;

    difference() {
        // ─── Shell body ───
        union() { ... }          // from layers.shell_body_lines()

        // ─── Cutouts ──────
        translate([0,0,z])       // one block per Cutout
            linear_extrude(h)
                polygon(...);
    }

When the cutout list is empty the ``difference()`` wrapper is omitted and the
shell body union is emitted directly (no unnecessary CSG overhead in OpenSCAD).
"""

from __future__ import annotations

from datetime import datetime

from .cutouts import Cutout


# ── Helpers ────────────────────────────────────────────────────────


def _poly_str(pts: list) -> str:
    """Format [[x,y],...] as an OpenSCAD points literal."""
    return ", ".join(f"[{float(x):.3f}, {float(y):.3f}]" for x, y in pts)


def _indent(lines: list[str], prefix: str) -> list[str]:
    return [prefix + line for line in lines]


# ── Public API ─────────────────────────────────────────────────────


def generate_scad(
    shell_body_lines: list[str],
    cutouts: list[Cutout],
    session_id: str = "",
    metadata: dict | None = None,
) -> str:
    """Return a complete OpenSCAD source string.

    Parameters
    ----------
    shell_body_lines : list[str]
        Lines produced by ``layers.shell_body_lines()`` — forms the
        ``union()`` solid body block.
    cutouts : list[Cutout]
        Cutouts to subtract.  May be empty (solid shell, no booleans).
    session_id : str
        Written into the header comment.
    metadata : dict, optional
        Extra key-value pairs for the header comment (e.g. component count).
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    meta_str = ""
    if metadata:
        meta_str = "".join(f"//   {k}: {v}\n" for k, v in metadata.items())

    header = (
        "// ============================================================\n"
        "// manufacturerAI -- auto-generated enclosure\n"
        f"// Session  : {session_id}\n"
        f"// Generated: {now}\n"
        + meta_str
        + "// ============================================================\n"
        "\n"
        "$fn = 16;\n"
        "\n"
    )

    # ── No cutouts — just emit the shell body directly ─────────────
    if not cutouts:
        return header + "\n".join(shell_body_lines) + "\n"

    # ── With cutouts — wrap in difference() ───────────────────────
    out_lines: list[str] = ["difference() {", ""]

    # Shell body (indented by 2 spaces) — wrapped in render() so OpenSCAD
    # pre-computes the CGAL mesh once before subtracting all cutouts.
    out_lines.append("  // --- Shell body -------------------------------------------------")
    out_lines.append("  render(convexity = 10)")
    out_lines += _indent(shell_body_lines, "  ")
    out_lines.append("")

    # Cutouts
    out_lines.append("  // --- Cutouts ----------------------------------------------------")

    prev_label = None
    for cut in cutouts:
        # Blank line + label comment between groups
        if cut.label != prev_label:
            out_lines.append("")
            if cut.label:
                out_lines.append(f"  // {cut.label}")
            prev_label = cut.label

        if cut.cylinder_r is not None:
            out_lines += [
                f"  translate([{cut.cylinder_cx:.3f}, {cut.cylinder_cy:.3f}, {cut.z_base:.3f}])",
                f"    cylinder(h = {cut.depth:.3f}, r = {cut.cylinder_r:.3f});",
            ]
        else:
            poly = _poly_str(cut.polygon)
            out_lines += [
                f"  translate([0, 0, {cut.z_base:.3f}])",
                f"    linear_extrude(height = {cut.depth:.3f})",
                f"      polygon(points = [{poly}]);",
            ]

    out_lines += ["", "}"]

    return header + "\n".join(out_lines) + "\n"
