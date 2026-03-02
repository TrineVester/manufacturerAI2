/**
 * viewportUtils.js — shared utilities for all viewport render handlers.
 * ...
 */

import { getViewState, patchViewState } from './viewport.js';

// ── Shared constants ──────────────────────────────────────────────────────────

export const SCALE = 4;      // mm → px
export const PAD   = 40;     // px padding around the SVG content
export const NS    = 'http://www.w3.org/2000/svg';


// ── Outline normalisation ─────────────────────────────────────────────────────

/**
 * Normalise an outline array to a consistent internal shape.
 *
 * Input:   [{x, y, ease_in?, ease_out?, z_top?}, ...]
 * Returns: { verts: [[x,y],...], corners: [{ease_in, ease_out},...],
 *            zTops: [z_top | null, ...] }
 */
export function normaliseOutline(outline) {
    if (!outline || !Array.isArray(outline)) return { verts: [], corners: [], zTops: [] };

    const verts  = outline.map(p => [p.x, p.y]);
    const zTops  = outline.map(p => (p.z_top != null ? p.z_top : null));
    const corners = outline.map(p => {
        let ein  = p.ease_in  ?? null;
        let eout = p.ease_out ?? null;
        // Mirror if only one side provided
        if (ein  != null && eout == null) eout = ein;
        if (eout != null && ein  == null) ein  = eout;
        return { ease_in: ein ?? 0, ease_out: eout ?? 0 };
    });
    return { verts, corners, zTops };
}


// ── Outline bezier expansion ────────────────────────────────────────────────────

/**
 * Expand a normalised outline into a dense polygon by approximating each
 * eased corner with a quadratic Bézier arc.
 *
 * Returns { pts: [[x,y],...], zs: [z,...] } where each expanded point also
 * carries an interpolated z_top value (for use in 3‑D wall/wireframe heights).
 *
 * @param {number[][]}          verts    Raw control-point positions [[x,y],...]
 * @param {{ease_in,ease_out}[]} corners  Per-vertex easing
 * @param {(number|null)[]}     zTopRaw  Per-vertex z_top (null → defaultZ)
 * @param {number}              defaultZ Fallback z when z_top absent
 * @param {number}              [segs=6] Bézier sub-segments per eased corner
 */
export function expandOutlineVertices(verts, corners, zTopRaw, defaultZ, segs = 6) {
    const n   = verts.length;
    const pts = [];
    const zs  = [];

    for (let i = 0; i < n; i++) {
        const prev = (i - 1 + n) % n;
        const next = (i + 1)     % n;
        const C = verts[i],  P = verts[prev], N = verts[next];
        const zC = zTopRaw[i]    ?? defaultZ;
        const zP = zTopRaw[prev] ?? defaultZ;
        const zN = zTopRaw[next] ?? defaultZ;
        const eIn  = corners[i].ease_in  ?? 0;
        const eOut = corners[i].ease_out ?? 0;

        if (eIn === 0 && eOut === 0) { pts.push(C); zs.push(zC); continue; }

        const dPx = P[0]-C[0], dPy = P[1]-C[1];
        const dNx = N[0]-C[0], dNy = N[1]-C[1];
        const lenP = Math.hypot(dPx, dPy);
        const lenN = Math.hypot(dNx, dNy);
        if (lenP === 0 || lenN === 0) { pts.push(C); zs.push(zC); continue; }

        const safeIn  = Math.min(eIn,  lenP * 0.45);
        const safeOut = Math.min(eOut, lenN * 0.45);
        const t1 = [C[0] + dPx*(safeIn /lenP), C[1] + dPy*(safeIn /lenP)];
        const t2 = [C[0] + dNx*(safeOut/lenN), C[1] + dNy*(safeOut/lenN)];
        const zT1 = zC + (zP - zC) * (safeIn  / lenP);
        const zT2 = zC + (zN - zC) * (safeOut / lenN);

        for (let s = 0; s <= segs; s++) {
            const u = s / segs, ku = 1 - u;
            pts.push([
                ku*ku*t1[0] + 2*ku*u*C[0] + u*u*t2[0],
                ku*ku*t1[1] + 2*ku*u*C[1] + u*u*t2[1],
            ]);
            zs.push(ku*ku*zT1 + 2*ku*u*zC + u*u*zT2);
        }
    }
    return { pts, zs };
}


// ── SVG outline path builder ──────────────────────────────────────────────────

/**
 * Build an SVG path `d` string for the outline polygon with optional rounded
 * corners.  Sharp corners get straight line-to segments; eased corners get a
 * quadratic Bézier with the vertex as the control point and tangent points at
 * ease_in / ease_out distances along the adjacent edges.
 */
export function buildOutlinePath(verts, corners, ox, oy, scale) {
    const n = verts.length;
    if (n < 3) return '';

    // Convert to screen coords
    const pts = verts.map(v => ({ x: ox + v[0] * scale, y: oy + v[1] * scale }));

    // Pre-compute ease info per vertex in px
    const ci = corners.map(edge => {
        const eIn  = (edge.ease_in  ?? 0) * scale;
        const eOut = (edge.ease_out ?? 0) * scale;
        return { round: eIn > 0 || eOut > 0, eIn, eOut };
    });

    const segments = [];

    for (let i = 0; i < n; i++) {
        const prev = (i - 1 + n) % n;
        const next = (i + 1)     % n;
        const P = pts[prev], C = pts[i], N = pts[next];

        if (!ci[i].round) {
            segments.push(i === 0 ? `M ${C.x} ${C.y}` : `L ${C.x} ${C.y}`);
            continue;
        }

        let { eIn, eOut } = ci[i];
        const dPx = P.x - C.x, dPy = P.y - C.y;
        const dNx = N.x - C.x, dNy = N.y - C.y;
        const lenP = Math.hypot(dPx, dPy);
        const lenN = Math.hypot(dNx, dNy);

        if (lenP === 0 || lenN === 0) {
            segments.push(i === 0 ? `M ${C.x} ${C.y}` : `L ${C.x} ${C.y}`);
            continue;
        }

        eIn  = Math.min(eIn,  lenP * 0.45);
        eOut = Math.min(eOut, lenN * 0.45);

        const t1x = C.x + (dPx / lenP) * eIn;
        const t1y = C.y + (dPy / lenP) * eIn;
        const t2x = C.x + (dNx / lenN) * eOut;
        const t2y = C.y + (dNy / lenN) * eOut;

        segments.push(i === 0 ? `M ${t1x} ${t1y}` : `L ${t1x} ${t1y}`);
        segments.push(`Q ${C.x} ${C.y} ${t2x} ${t2y}`);
    }

    segments.push('Z');
    return segments.join(' ');
}


// ── Edge snap helper ──────────────────────────────────────────────────────────

/**
 * Snap a UI side-mount placement onto its outline edge and compute:
 *   x, y      — snapped position in mm (XY world space)
 *   z         — interpolated ceiling height at this position (mm); requires
 *               zTops array and defaultZ from the enclosure
 *   rot       — rotation angle (degrees) for the component icon
 *   wallNormal — 3D outward face normal [nx, ny, nz] for the wall at this point
 *                (z component accounts for the z_top taper of the wall face)
 *
 * @param {object}   up        UIPlacement with .x_mm, .y_mm, .edge_index
 * @param {number[][]} verts    Outline vertices [[x,y], ...]
 * @param {Array}    zTops     Per-vertex z_top values (null = use defaultZ)
 * @param {number}   defaultZ  Fallback ceiling height from enclosure.height_mm
 */
export function snapToEdge(up, verts, zTops = [], defaultZ = 25) {
    const n = verts.length;
    const i = up.edge_index ?? 0;
    const v0 = verts[i];
    const v1 = verts[(i + 1) % n];

    const ex = v1[0] - v0[0];
    const ey = v1[1] - v0[1];
    const edgeLen = Math.hypot(ex, ey);
    if (edgeLen === 0) return { x: up.x_mm, y: up.y_mm, z: defaultZ, rot: 0, wallNormal: [0, 1, 0] };

    const dx = ex / edgeLen;
    const dy = ey / edgeLen;

    // Project onto edge
    const px = up.x_mm - v0[0];
    const py = up.y_mm - v0[1];
    let t = (px * dx + py * dy) / edgeLen;
    t = Math.max(0, Math.min(1, t));

    const x = v0[0] + t * ex;
    const y = v0[1] + t * ey;

    // Interpolated ceiling height along this edge
    const z0 = zTops[i] ?? defaultZ;
    const z1 = zTops[(i + 1) % n] ?? defaultZ;
    const z  = z0 + t * (z1 - z0);

    // 2D outward normal (for clockwise CW winding, inward is left of direction)
    // outward = right of edge direction = (dy, -dx)
    const outNx = dy;
    const outNy = -dx;

    // Wall face 3D normal: the wall goes from (v0, z=0) to (v0, z=z0) to (v1, z=z1)
    // Two vectors in the wall face:
    //   along = (ex, ey, 0) / edgeLen
    //   up_v  = (dz_x, dz_y, 1) approximately — the slope vector per unit outward
    // Normal = along × up_slope (but simplified: outward 2D normal tilted by dz/dx)
    const dzAlong = (z1 - z0) / edgeLen;  // slope along the edge
    // Normal in 3D: outward XY normal, with Z-tilt
    const wnLen = Math.sqrt(outNx * outNx + outNy * outNy + dzAlong * dzAlong) || 1;
    const wallNormal = [outNx / wnLen, outNy / wnLen, dzAlong / wnLen];

    // Rotation for 2D icon rendering
    const angle = Math.atan2(ey, ex) * 180 / Math.PI;
    const normalAngle = angle - 90;
    const rot = ((Math.round(normalAngle / 90) * 90) % 360 + 360) % 360;

    return { x, y, z, rot, wallNormal };
}


// ── HTML escape helper ────────────────────────────────────────────────────────

export function esc(text) {
    const el = document.createElement('span');
    el.textContent = text ?? '';
    return el.innerHTML;
}


// ── Height grid helpers ───────────────────────────────────────────────────────

/**
 * Sample a height from a precomputed grid dict (from design.json height_grid).
 * Returns null if (x,y) is outside the grid or outside the polygon mask.
 *
 * @param {number} x  World X in mm
 * @param {number} y  World Y in mm
 * @param {object} grid   height_grid from design.json
 */
export function sampleGrid(x, y, grid) {
    if (!grid || grid.cols === 0) return null;
    const c = Math.round((x - grid.origin_x) / grid.step_mm);
    const r = Math.round((y - grid.origin_y) / grid.step_mm);
    if (r < 0 || r >= grid.rows || c < 0 || c >= grid.cols) return null;
    const v = grid.grid[r][c];
    return v;  // may be null if outside polygon
}


// ── 2D / 3D toggle controller ─────────────────────────────────────────────────

/**
 * Attach a 2D/3D toggle to a viewport step.
 *
 * Usage (inside a viewport handler registration):
 *   const _toggle = attachViewToggle('design', render2DFn, create3DSceneFn);
 *   registerHandler('design', {
 *     render(el, data) { _toggle.render(el, data); },
 *     clear(el)        { _toggle.clear(el); },
 *     unmount(el)      { _toggle.unmount(); },
 *     onResize(el,w,h) { _toggle.resize(w, h); },
 *   });
 *
 * @param {string}   step       – viewport step key ('design', 'placement', 'routing')
 * @param {Function} render2DFn – (el, data) => void   — builds SVG/HTML 2D content
 * @param {Function} create3DFn – (host: HTMLElement) → Scene3D{ update, resize, destroy }
 */
export function attachViewToggle(step, render2DFn, create3DFn) {
    let scene    = null;
    let lastEl   = null;
    let lastData = null;

    function _toolbar() {
        return document.getElementById('viewport-toolbar');
    }

    function _updateToggleBtn() {
        const tb = _toolbar();
        if (!tb) return;
        // Remove any existing toggle button for this step
        tb.querySelectorAll('.vp-3d-toggle-btn').forEach(b => b.remove());

        const state = getViewState(step);
        const btn = document.createElement('button');
        btn.className = 'vp-3d-toggle-btn';
        if (state.mode === '3d') {
            btn.textContent = '2D';
            btn.title = 'Switch to 2D view';
        } else {
            btn.textContent = '3D';
            btn.title = 'Switch to 3D view';
        }
        btn.addEventListener('click', () => {
            const cur = getViewState(step).mode;
            if (cur === '3d') {
                if (scene) { scene.destroy(); scene = null; }
                patchViewState(step, { mode: '2d' });
            } else {
                patchViewState(step, { mode: '3d' });
            }
            if (lastEl) _render(lastEl, lastData);
        });
        tb.appendChild(btn);
    }

    function _render(el, data) {
        lastEl   = el;
        lastData = data;
        const state = getViewState(step);

        if (state.mode === '3d') {
            if (!scene) {
                el.innerHTML = '';
                const host = document.createElement('div');
                host.className = 'vp-3d-host';
                host.style.cssText = 'width:100%;height:calc(100% - 0px);min-height:480px;position:relative;';
                el.appendChild(host);
                _updateToggleBtn();

                const loading = document.createElement('p');
                loading.className = 'vp-3d-loading';
                loading.textContent = 'Loading 3D…';
                host.appendChild(loading);

                Promise.resolve(create3DFn(host)).then(s => {
                    scene = s;
                    loading.remove();
                    if (lastData) scene.update(lastData);
                }).catch(err => {
                    console.error('3D scene failed to load:', err);
                    patchViewState(step, { mode: '2d' });
                    loading.textContent = '3D unavailable — falling back to 2D';
                    setTimeout(() => _render(el, lastData), 1200);
                });
            } else {
                if (data) scene.update(data);
            }
        } else {
            // 2D mode
            if (scene) { scene.destroy(); scene = null; }
            render2DFn(el, data);
            _updateToggleBtn();
        }
    }

    return {
        render(el, data)  { _render(el, data); },
        clear(el)         {
            if (scene) { scene.destroy(); scene = null; }
            lastEl = null; lastData = null;
            const tb = _toolbar();
            if (tb) tb.querySelectorAll('.vp-3d-toggle-btn').forEach(b => b.remove());
        },
        unmount()         {
            if (scene) { scene.destroy(); scene = null; }
            const tb = _toolbar();
            if (tb) tb.querySelectorAll('.vp-3d-toggle-btn').forEach(b => b.remove());
        },
        resize(w, h)      { if (scene) scene.resize(w, h); },
    };
}
