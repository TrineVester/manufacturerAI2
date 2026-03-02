/**
 * Viewport handler for the Design step.
 *
 * Renders a visual preview of the DesignSpec:
 *   - SVG outline with UI placement markers
 *   - Component summary table
 *   - Net connection list
 *
 * Data shape (matches DesignSpec JSON from the backend):
 * {
 *   components: [{ catalog_id, instance_id, config?, mounting_style? }]
 *   nets:       [{ id, pins: ["instance:pin", …] }]
 *   outline:    [{ x, y, ease_in?, ease_out? }, ...]
 *   ui_placements: [{ instance_id, x_mm, y_mm }]
 * }
 */

import { registerHandler } from './viewport.js';
import { drawComponentIcon } from './componentRenderer.js';
import { normaliseOutline, buildOutlinePath, snapToEdge, esc, SCALE, PAD, NS, attachViewToggle } from './viewportUtils.js';
import { state, API } from './state.js';

// ── Toggle controller ───────────────────────────────────────────

const _toggle = attachViewToggle(
    'design',
    (el, design) => { el.innerHTML = ''; el.appendChild(buildPreview(design)); },
    async (host) => {
        const { create3DScene } = await import('./viewport3d.js');
        const scene = create3DScene(host);
        // Wrap to also manage the edge profile panel overlay
        let panel = null;
        return {
            update(data) {
                scene.update(data);
                if (!panel) {
                    panel = _mountEdgePanel(host, data, scene);
                } else {
                    panel.syncData(data);
                }
            },
            resize(w, h) { scene.resize(w, h); },
            destroy() {
                if (panel) { panel.destroy(); panel = null; }
                scene.destroy();
            },
        };
    },
);

// ── Register ────────────────────────────────────────────────

registerHandler('design', {
    label: 'Design Preview',
    placeholder: 'Submit a design prompt to see the preview',

    render(el, design) { _toggle.render(el, design); },

    clear(el) {
        _toggle.clear(el);
        el.innerHTML = '<p class="viewport-empty">Submit a design prompt to see the preview</p>';
    },

    unmount()        { _toggle.unmount(); },
    onResize(el,w,h) { _toggle.resize(w, h); },
});


// ── Preview builder ───────────────────────────────────────────

function buildPreview(design) {
    const wrap = document.createElement('div');
    wrap.className = 'vp-design';

    wrap.appendChild(buildOutlineSVG(design));
    wrap.appendChild(buildComponentList(design.components));
    wrap.appendChild(buildNetList(design.nets));

    return wrap;
}


// ── Outline SVG ───────────────────────────────────────────────

function buildOutlineSVG(design) {
    const { outline, ui_placements = [] } = design;

    // Normalise outline to { verts: [[x,y],...], corners: [{ease_in, ease_out},...] }
    const { verts, corners } = normaliseOutline(outline);

    if (verts.length < 3) {
        const p = document.createElement('p');
        p.className = 'viewport-empty';
        p.textContent = 'Outline has fewer than 3 vertices';
        return p;
    }

    // Bounding box
    const xs = verts.map(v => v[0]);
    const ys = verts.map(v => v[1]);
    const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
    const [minY, maxY] = [Math.min(...ys), Math.max(...ys)];

    const w = (maxX - minX) * SCALE + PAD * 2;
    const h = (maxY - minY) * SCALE + PAD * 2;
    const ox = PAD - minX * SCALE;
    // Screen convention: y=0 at top, y increases downward (matches SVG).
    const oy = PAD - minY * SCALE;

    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('class', 'vp-outline-svg');

    // Grid (subtle)
    const gridSize = 10 * SCALE;  // 10 mm grid
    const grid = document.createElementNS(NS, 'pattern');
    grid.id = 'vp-grid';
    grid.setAttribute('width', gridSize);
    grid.setAttribute('height', gridSize);
    grid.setAttribute('patternUnits', 'userSpaceOnUse');
    const gridLine1 = document.createElementNS(NS, 'path');
    gridLine1.setAttribute('d', `M ${gridSize} 0 L 0 0 0 ${gridSize}`);
    gridLine1.setAttribute('fill', 'none');
    gridLine1.setAttribute('stroke', 'rgba(255,255,255,0.04)');
    gridLine1.setAttribute('stroke-width', '1');
    grid.appendChild(gridLine1);

    const defs = document.createElementNS(NS, 'defs');
    defs.appendChild(grid);
    svg.appendChild(defs);

    const gridRect = document.createElementNS(NS, 'rect');
    gridRect.setAttribute('width', '100%');
    gridRect.setAttribute('height', '100%');
    gridRect.setAttribute('fill', 'url(#vp-grid)');
    svg.appendChild(gridRect);

    // Build outline path with proper rounded corners
    const pathD = buildOutlinePath(verts, corners, ox, oy, SCALE);
    const pathEl = document.createElementNS(NS, 'path');
    pathEl.setAttribute('d', pathD);
    pathEl.setAttribute('class', 'vp-outline-path');
    svg.appendChild(pathEl);

    // UI placements — use shared component renderer when body data
    // is available, otherwise fall back to simple marker dots.
    const compMap = {};
    for (const c of (design.components || [])) {
        compMap[c.instance_id] = c;
    }

    const UI_COLORS = [
        '#58a6ff', '#3fb950', '#d29922', '#f778ba', '#bc8cff',
        '#79c0ff', '#56d364', '#e3b341', '#ff7b72', '#a5d6ff',
    ];

    ui_placements.forEach((up, idx) => {
        const comp = compMap[up.instance_id];
        const color = UI_COLORS[idx % UI_COLORS.length];

        if (up.edge_index != null) {
            // Side-mount — snap to wall, then draw component icon
            const snapInfo = snapToEdge(up, verts, normaliseOutline(design.outline).zTops, (design.enclosure?.height_mm ?? 25));
            if (comp && comp.body) {
                const fakeComp = {
                    ...comp,
                    x_mm: snapInfo.x, y_mm: snapInfo.y,
                    rotation_deg: snapInfo.rot,
                };
                drawComponentIcon(svg, fakeComp, ox, oy, SCALE, {
                    color, bodyOpacity: 0.2, showPins: !!(comp.pins),
                });
            } else {
                drawSideMountMarker(svg, NS, up, { vertices: verts }, ox, oy);
            }
        } else {
            // Interior UI component
            if (comp && comp.body) {
                const fakeComp = {
                    ...comp,
                    x_mm: up.x_mm, y_mm: up.y_mm,
                    rotation_deg: 0,
                };
                drawComponentIcon(svg, fakeComp, ox, oy, SCALE, {
                    color, bodyOpacity: 0.2, showPins: !!(comp.pins),
                });
            } else {
                const cx = ox + up.x_mm * SCALE;
                const cy = oy + up.y_mm * SCALE;

                const marker = document.createElementNS(NS, 'circle');
                marker.setAttribute('cx', cx);
                marker.setAttribute('cy', cy);
                marker.setAttribute('r', '6');
                marker.setAttribute('class', 'vp-ui-marker');

                const label = document.createElementNS(NS, 'text');
                label.setAttribute('x', cx);
                label.setAttribute('y', cy - 10);
                label.setAttribute('class', 'vp-ui-label');
                label.textContent = up.instance_id;

                svg.appendChild(marker);
                svg.appendChild(label);
            }
        }
    });

    // Dimension labels
    const dimLabel = document.createElementNS(NS, 'text');
    dimLabel.setAttribute('x', ox + ((maxX - minX) / 2) * SCALE);
    dimLabel.setAttribute('y', h - 6);
    dimLabel.setAttribute('class', 'vp-dim-label');
    dimLabel.textContent = `${(maxX - minX).toFixed(1)} mm`;
    svg.appendChild(dimLabel);

    const dimLabelV = document.createElementNS(NS, 'text');
    dimLabelV.setAttribute('x', 8);
    dimLabelV.setAttribute('y', oy + ((maxY + minY) / 2) * SCALE);
    dimLabelV.setAttribute('class', 'vp-dim-label');
    dimLabelV.setAttribute('transform', `rotate(-90, 8, ${oy + ((maxY + minY) / 2) * SCALE})`);
    dimLabelV.textContent = `${(maxY - minY).toFixed(1)} mm`;
    svg.appendChild(dimLabelV);

    const section = document.createElement('div');
    section.className = 'vp-section';
    const heading = document.createElement('h4');
    heading.textContent = 'Outline';
    section.appendChild(heading);
    section.appendChild(svg);
    return section;
}


// ── Component list ────────────────────────────────────────────

function buildComponentList(components = []) {
    const section = document.createElement('div');
    section.className = 'vp-section';

    const heading = document.createElement('h4');
    heading.textContent = `Components (${components.length})`;
    section.appendChild(heading);

    if (components.length === 0) {
        const p = document.createElement('p');
        p.className = 'viewport-empty';
        p.textContent = 'No components';
        section.appendChild(p);
        return section;
    }

    const table = document.createElement('table');
    table.className = 'vp-table';
    table.innerHTML = `
        <thead><tr><th>Instance</th><th>Catalog ID</th><th>Mount</th></tr></thead>
        <tbody>
            ${components.map(c => `
                <tr>
                    <td class="vp-mono">${esc(c.instance_id)}</td>
                    <td>${esc(c.catalog_id)}</td>
                    <td>${esc(c.mounting_style || '—')}</td>
                </tr>
            `).join('')}
        </tbody>`;
    section.appendChild(table);
    return section;
}


// ── Net list ──────────────────────────────────────────────────

function buildNetList(nets = []) {
    const section = document.createElement('div');
    section.className = 'vp-section';

    const heading = document.createElement('h4');
    heading.textContent = `Nets (${nets.length})`;
    section.appendChild(heading);

    if (nets.length === 0) {
        const p = document.createElement('p');
        p.className = 'viewport-empty';
        p.textContent = 'No nets';
        section.appendChild(p);
        return section;
    }

    const list = document.createElement('div');
    list.className = 'vp-net-list';
    for (const net of nets) {
        const row = document.createElement('div');
        row.className = 'vp-net-row';
        row.innerHTML = `
            <span class="vp-net-id">${esc(net.id)}</span>
            <span class="vp-net-pins">${net.pins.map(p => `<code>${esc(p)}</code>`).join(' · ')}</span>
        `;
        list.appendChild(row);
    }
    section.appendChild(list);
    return section;
}


// ── Side-mount component rendering ────────────────────────────

/**
 * Draw a side-mount component marker on the specified outline edge.
 * The marker is a small diamond/arrow shape sitting on the wall to
 * indicate the component protrudes through.
 */
function drawSideMountMarker(svg, NS, up, outline, ox, oy) {
    const verts = outline.vertices;
    const n = verts.length;
    const i = up.edge_index;

    // Edge endpoints
    const v0 = verts[i];
    const v1 = verts[(i + 1) % n];

    // Project x/y onto the edge to find position along it
    const ex = v1[0] - v0[0], ey = v1[1] - v0[1];
    const edgeLen = Math.hypot(ex, ey);
    if (edgeLen === 0) return;

    // Normalised edge direction
    const dx = ex / edgeLen, dy = ey / edgeLen;

    // Vector from v0 to placement point
    const px = up.x_mm - v0[0], py = up.y_mm - v0[1];

    // Project onto edge (clamp to edge bounds)
    let t = (px * dx + py * dy) / edgeLen;
    t = Math.max(0.02, Math.min(0.98, t));

    // Position on the edge (screen convention: Y not flipped)
    const cx = ox + (v0[0] + t * ex) * SCALE;
    const cy = oy + (v0[1] + t * ey) * SCALE;

    // Edge direction in screen space (no Y flip)
    const sdx = dx, sdy = dy;
    // Inward normal in screen space: perpendicular to (sdx,sdy) rotated 90° CW
    // For clockwise winding, inward normal points right of edge direction
    const nx = sdy, ny = -sdx;

    // Draw a small triangle/arrow pointing inward from the wall
    const arrowLen = 8;   // length of arrow in px
    const arrowW   = 5;   // half-width of arrow base in px

    // Tip of arrow (pointing inward)
    const tipX = cx + nx * arrowLen * SCALE / 4;
    const tipY = cy + ny * arrowLen * SCALE / 4;

    // Base corners (on the wall)
    const b1x = cx + sdx * arrowW;
    const b1y = cy + sdy * arrowW;
    const b2x = cx - sdx * arrowW;
    const b2y = cy - sdy * arrowW;

    const arrow = document.createElementNS(NS, 'polygon');
    arrow.setAttribute('points', `${b1x},${b1y} ${tipX},${tipY} ${b2x},${b2y}`);
    arrow.setAttribute('class', 'vp-side-marker');

    // Small circle on the wall edge itself
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('cx', cx);
    dot.setAttribute('cy', cy);
    dot.setAttribute('r', '3');
    dot.setAttribute('class', 'vp-side-dot');

    // Label — offset inward from the wall
    const label = document.createElementNS(NS, 'text');
    label.setAttribute('x', cx + nx * 16);
    label.setAttribute('y', cy + ny * 16);
    label.setAttribute('class', 'vp-ui-label');
    label.textContent = up.instance_id;

    svg.appendChild(arrow);
    svg.appendChild(dot);
    svg.appendChild(label);
}


// ── Edge Profile Panel ─────────────────────────────────────────────────────────

/**
 * Mount a floating edge-profile control panel overlaid on the 3D viewport host.
 * Lets the user pick top / bottom wall profile (sharp, chamfer, fillet) and size,
 * live-previews changes via scene.update(), and persists via PATCH API.
 *
 * Returns { syncData(data), destroy() }.
 */
function _mountEdgePanel(host, initialData, scene) {
    let design = initialData;

    const panel = document.createElement('div');
    panel.className = 'ep-panel';
    panel.innerHTML = `
        <div class="ep-header">
            <span class="ep-title">Wall Edge</span>
            <button class="ep-close" title="Close">✕</button>
        </div>
        <div class="ep-body">
            <div class="ep-tabs">
                <button class="ep-tab ep-tab-active" data-side="top" title="Where wall meets lid">Top</button>
                <button class="ep-tab" data-side="bottom" title="Where wall meets floor">Bottom</button>
            </div>
            <div class="ep-types">
                <label class="ep-type-opt" title="Sharp 90° corner">
                    <input type="radio" name="ep-type" value="none" checked>
                    <span class="ep-type-icon">▐</span> Sharp
                </label>
                <label class="ep-type-opt" title="Flat 45° bevel">
                    <input type="radio" name="ep-type" value="chamfer">
                    <span class="ep-type-icon">◥</span> Chamfer
                </label>
                <label class="ep-type-opt" title="Smooth curved round-over">
                    <input type="radio" name="ep-type" value="fillet">
                    <span class="ep-type-icon">◜</span> Fillet
                </label>
            </div>
            <div class="ep-size-row" hidden>
                <span class="ep-size-lbl">Size</span>
                <input type="range" class="ep-size-slider" min="0.5" max="10" step="0.5" value="3">
                <span class="ep-size-val">3.0 mm</span>
            </div>
            <p class="ep-hint">Viewed from the side — the wall edge profile</p>
        </div>
    `;
    host.appendChild(panel);

    let activeSide = 'top';

    const _profileFor = (side) =>
        (design.enclosure ?? {})[`edge_${side}`] ?? { type: 'none', size_mm: 2.0 };

    function _refreshUI() {
        const prof = _profileFor(activeSide);
        const type = prof.type ?? 'none';
        panel.querySelectorAll('[name="ep-type"]').forEach(r => { r.checked = (r.value === type); });
        const size = prof.size_mm ?? 3.0;
        panel.querySelector('.ep-size-slider').value = size;
        panel.querySelector('.ep-size-val').textContent = size.toFixed(1) + ' mm';
        panel.querySelector('.ep-size-row').hidden = (type === 'none');
    }

    async function _apply(side, type, size_mm) {
        if (!design.enclosure) design.enclosure = { height_mm: 25 };
        design.enclosure[`edge_${side}`] = { type, size_mm };
        scene.update(design);   // live preview

        const sid = state.session;
        if (!sid) return;
        try {
            await fetch(`${API}/api/session/design/enclosure?session=${encodeURIComponent(sid)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [`edge_${side}`]: { type, size_mm } }),
            });
        } catch { /* non-fatal — user sees the live preview regardless */ }
    }

    // Tab clicks
    panel.querySelectorAll('.ep-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            activeSide = btn.dataset.side;
            panel.querySelectorAll('.ep-tab').forEach(b =>
                b.classList.toggle('ep-tab-active', b.dataset.side === activeSide));
            _refreshUI();
        });
    });

    // Radio changes
    panel.querySelectorAll('[name="ep-type"]').forEach(radio => {
        radio.addEventListener('change', () => {
            if (!radio.checked) return;
            const type = radio.value;
            const size = parseFloat(panel.querySelector('.ep-size-slider').value);
            panel.querySelector('.ep-size-row').hidden = (type === 'none');
            _apply(activeSide, type, size);
        });
    });

    // Slider changes (apply on release for performance, preview on input)
    const slider = panel.querySelector('.ep-size-slider');
    slider.addEventListener('input', () => {
        panel.querySelector('.ep-size-val').textContent = parseFloat(slider.value).toFixed(1) + ' mm';
    });
    slider.addEventListener('change', () => {
        const type = panel.querySelector('[name="ep-type"]:checked')?.value ?? 'none';
        if (type !== 'none') _apply(activeSide, type, parseFloat(slider.value));
    });

    // Close
    panel.querySelector('.ep-close').addEventListener('click', () => panel.remove());

    _refreshUI();

    return {
        syncData(data) { design = data; _refreshUI(); },
        destroy()      { panel.remove(); },
    };
}