/**
 * Viewport handler for the Routing step.
 *
 * Renders routed traces overlaid on the component layout.
 * Reuses the same outline + component rendering as viewportPlacement,
 * adding colored trace polylines on top.
 *
 * Data shape (from routing API):
 * {
 *   traces:          [{ net_id, path: [[x,y], …] }],
 *   pin_assignments: { "mcu_1:gpio": "mcu_1:PD2", … },
 *   failed_nets:     ["NET_X", …],
 *   outline:         [{ x, y, ease_in?, ease_out? }],
 *   components:      [{ instance_id, catalog_id, x_mm, y_mm, rotation_deg, body }]
 * }
 */

import { registerHandler } from './viewport.js';
import { drawComponentIcon } from './componentRenderer.js';
import { normaliseOutline, buildOutlinePath, esc, SCALE, PAD, NS, attachViewToggle } from './viewportUtils.js';

// ── Toggle controller ───────────────────────────────────────────

const _toggle = attachViewToggle(
    'routing',
    (el, data) => { el.innerHTML = ''; el.appendChild(buildPreview(data)); },
    async (host) => {
        const { create3DScene } = await import('./viewport3d.js');
        return create3DScene(host);
    },
);

// ── Register ────────────────────────────────────────────────

registerHandler('routing', {
    label: 'Routing Preview',
    placeholder: 'Run the router to see trace layout',

    render(el, data)  { _toggle.render(el, data); },

    clear(el) {
        _toggle.clear(el);
        el.innerHTML = '<p class="viewport-empty">Run the router to see trace layout</p>';
    },

    unmount()        { _toggle.unmount(); },
    onResize(el,w,h) { _toggle.resize(w, h); },
});


// Build a colour map for a list of net names by distributing hues
// evenly around the HSL wheel so every net gets a maximally distinct colour.
function buildNetColorMap(netIds) {
    const unique = [...new Set(netIds)];
    const n = unique.length;
    const map = {};
    unique.forEach((id, i) => {
        const hue = Math.round((i * 360) / (n || 1));
        map[id] = `hsl(${hue}, 75%, 60%)`;
    });
    return map;
}


// ── Preview builder ───────────────────────────────────────────

function buildPreview(data) {
    const wrap = document.createElement('div');
    wrap.className = 'vp-placement';   // reuse placement layout styles

    wrap.appendChild(buildRoutingSVG(data));
    wrap.appendChild(buildTraceTable(data.traces));
    if (data.failed_nets && data.failed_nets.length > 0) {
        wrap.appendChild(buildFailedNets(data.failed_nets));
    }

    return wrap;
}


// ── Routing SVG ───────────────────────────────────────────────

function buildRoutingSVG(data) {
    const { outline, components = [], traces = [], failed_nets = [] } = data;

    const { verts, corners } = normaliseOutline(outline);
    if (verts.length < 3) {
        const p = document.createElement('p');
        p.className = 'viewport-empty';
        p.textContent = 'Outline has fewer than 3 vertices';
        return p;
    }

    // Bounding box (in mm)
    const xs = verts.map(v => v[0]);
    const ys = verts.map(v => v[1]);
    const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
    const [minY, maxY] = [Math.min(...ys), Math.max(...ys)];

    const w = (maxX - minX) * SCALE + PAD * 2;
    const h = (maxY - minY) * SCALE + PAD * 2;
    const ox = PAD - minX * SCALE;
    const oy = PAD - minY * SCALE;

    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('class', 'vp-outline-svg');

    // ── Grid pattern ──
    const gridSize = 10 * SCALE;
    const defs = document.createElementNS(NS, 'defs');
    const gridPat = document.createElementNS(NS, 'pattern');
    gridPat.id = 'vp-routing-grid';
    gridPat.setAttribute('width', gridSize);
    gridPat.setAttribute('height', gridSize);
    gridPat.setAttribute('patternUnits', 'userSpaceOnUse');
    const gridLine = document.createElementNS(NS, 'path');
    gridLine.setAttribute('d', `M ${gridSize} 0 L 0 0 0 ${gridSize}`);
    gridLine.setAttribute('fill', 'none');
    gridLine.setAttribute('stroke', 'rgba(255,255,255,0.04)');
    gridLine.setAttribute('stroke-width', '1');
    gridPat.appendChild(gridLine);
    defs.appendChild(gridPat);
    svg.appendChild(defs);

    const gridRect = document.createElementNS(NS, 'rect');
    gridRect.setAttribute('width', '100%');
    gridRect.setAttribute('height', '100%');
    gridRect.setAttribute('fill', 'url(#vp-routing-grid)');
    svg.appendChild(gridRect);

    // ── Outline path ──
    const pathD = buildOutlinePath(verts, corners, ox, oy, SCALE);
    const pathEl = document.createElementNS(NS, 'path');
    pathEl.setAttribute('d', pathD);
    pathEl.setAttribute('class', 'vp-outline-path');
    svg.appendChild(pathEl);

    // ── Components (dimmed) ──
    const COMP_COLORS = [
        '#58a6ff', '#3fb950', '#d29922', '#f778ba', '#bc8cff',
        '#79c0ff', '#56d364', '#e3b341', '#ff7b72', '#a5d6ff',
    ];
    components.forEach((comp, idx) => {
        const color = COMP_COLORS[idx % COMP_COLORS.length];
        drawComponentIcon(svg, comp, ox, oy, SCALE, {
            color,
            bodyOpacity: 0.10,
            showPins: !!(comp.pins && comp.pins.length),
            pinRadius: 2,
        });
    });

    // ── Traces ──
    const netColorMap = buildNetColorMap(traces.map(t => t.net_id));
    traces.forEach(trace => {
        drawTrace(svg, trace, ox, oy, netColorMap[trace.net_id]);
    });

    // ── SVG → panel hover linking for traces ──
    svg.addEventListener('mouseenter', e => {
        const g = e.target.closest('g.vp-trace-group[data-net-id]');
        if (!g) return;
        const id = g.dataset.netId;
        // Highlight all trace groups for this net
        svg.querySelectorAll(`g.vp-trace-group[data-net-id="${id}"]`).forEach(el =>
            el.classList.add('vp-hover'));
        const row = document.querySelector(`#routing-info tr[data-net-id="${id}"]`);
        if (row) row.classList.add('vp-hover');
    }, true);
    svg.addEventListener('mouseleave', e => {
        const g = e.target.closest('g.vp-trace-group[data-net-id]');
        if (!g) return;
        const id = g.dataset.netId;
        svg.querySelectorAll(`g.vp-trace-group[data-net-id="${id}"]`).forEach(el =>
            el.classList.remove('vp-hover'));
        const row = document.querySelector(`#routing-info tr[data-net-id="${id}"]`);
        if (row) row.classList.remove('vp-hover');
    }, true);

    // ── Trace legend ──
    if (traces.length > 0) {
        const legendY = h - 8;
        let legendX = ox;
        for (const [netId, color] of Object.entries(netColorMap)) {
            const dot = document.createElementNS(NS, 'circle');
            dot.setAttribute('cx', legendX);
            dot.setAttribute('cy', legendY);
            dot.setAttribute('r', '4');
            dot.setAttribute('fill', color);
            svg.appendChild(dot);

            const label = document.createElementNS(NS, 'text');
            label.setAttribute('x', legendX + 8);
            label.setAttribute('y', legendY + 3);
            label.setAttribute('class', 'vp-dim-label');
            label.setAttribute('fill', color);
            label.setAttribute('text-anchor', 'start');
            label.setAttribute('font-size', '10');
            label.textContent = netId;
            svg.appendChild(label);

            legendX += netId.length * 7 + 22;
        }
    }

    // ── Dimension labels ──
    const dimH = document.createElementNS(NS, 'text');
    dimH.setAttribute('x', ox + ((maxX - minX) / 2) * SCALE);
    dimH.setAttribute('y', h - 20);
    dimH.setAttribute('class', 'vp-dim-label');
    dimH.textContent = `${(maxX - minX).toFixed(1)} mm`;
    svg.appendChild(dimH);

    const dimV = document.createElementNS(NS, 'text');
    dimV.setAttribute('x', 8);
    dimV.setAttribute('y', oy + ((maxY + minY) / 2) * SCALE);
    dimV.setAttribute('class', 'vp-dim-label');
    dimV.setAttribute('transform', `rotate(-90, 8, ${oy + ((maxY + minY) / 2) * SCALE})`);
    dimV.textContent = `${(maxY - minY).toFixed(1)} mm`;
    svg.appendChild(dimV);

    // ── Status badge ──
    const ok = failed_nets.length === 0;
    const badge = document.createElementNS(NS, 'text');
    badge.setAttribute('x', w - 10);
    badge.setAttribute('y', 16);
    badge.setAttribute('text-anchor', 'end');
    badge.setAttribute('font-size', '12');
    badge.setAttribute('fill', ok ? '#3fb950' : '#f85149');
    badge.textContent = ok ? `✓ All ${traces.length} nets routed` : `⚠ ${failed_nets.length} net${failed_nets.length > 1 ? 's' : ''} failed`;
    svg.appendChild(badge);

    // ── Wrap ──
    const section = document.createElement('div');
    section.className = 'vp-section';
    const heading = document.createElement('h4');
    heading.textContent = 'Trace Layout';
    section.appendChild(heading);
    section.appendChild(svg);
    return section;
}


// ── Draw a trace polyline ─────────────────────────────────────

function drawTrace(svg, trace, ox, oy, color) {
    const path = trace.path;
    if (!path || path.length < 2) return;

    // Wrap all trace elements in a group with data-net-id for hover linking
    const group = document.createElementNS(NS, 'g');
    group.classList.add('vp-trace-group');
    group.setAttribute('data-net-id', trace.net_id);

    // Main trace line — stroke-width matches physical trace width (1 mm × SCALE px/mm)
    const TRACE_W_PX = 1.0 * SCALE;   // 4 px = 1 mm at current scale
    const points = path.map(p => `${ox + p[0] * SCALE},${oy + p[1] * SCALE}`).join(' ');
    const polyline = document.createElementNS(NS, 'polyline');
    polyline.setAttribute('points', points);
    polyline.setAttribute('fill', 'none');
    polyline.setAttribute('stroke', color);
    polyline.setAttribute('stroke-width', String(TRACE_W_PX));
    polyline.setAttribute('stroke-linecap', 'round');
    polyline.setAttribute('stroke-linejoin', 'round');
    polyline.setAttribute('opacity', '0.85');
    group.appendChild(polyline);

    // Via dots at each waypoint (intermediate points)
    for (let i = 1; i < path.length - 1; i++) {
        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('cx', ox + path[i][0] * SCALE);
        dot.setAttribute('cy', oy + path[i][1] * SCALE);
        dot.setAttribute('r', '2');
        dot.setAttribute('fill', color);
        dot.setAttribute('opacity', '0.7');
        group.appendChild(dot);
    }

    // Endpoint pads (start and end)
    for (const idx of [0, path.length - 1]) {
        const pad = document.createElementNS(NS, 'circle');
        pad.setAttribute('cx', ox + path[idx][0] * SCALE);
        pad.setAttribute('cy', oy + path[idx][1] * SCALE);
        pad.setAttribute('r', '3.5');
        pad.setAttribute('fill', color);
        pad.setAttribute('stroke', '#0d1117');
        pad.setAttribute('stroke-width', '1');
        group.appendChild(pad);
    }

    svg.appendChild(group);
}


// ── Trace table ───────────────────────────────────────────────

function buildTraceTable(traces = []) {
    const section = document.createElement('div');
    section.className = 'vp-section';
    const heading = document.createElement('h4');
    heading.textContent = `Traces (${traces.length})`;
    section.appendChild(heading);

    if (traces.length === 0) {
        const p = document.createElement('p');
        p.className = 'viewport-empty';
        p.textContent = 'No traces routed';
        section.appendChild(p);
        return section;
    }

    const table = document.createElement('table');
    table.className = 'vp-table';
    table.innerHTML = `
        <thead><tr>
            <th>Net</th>
            <th>Waypoints</th>
            <th>Length (mm)</th>
        </tr></thead>
        <tbody>
            ${traces.map(t => {
                const len = traceLength(t.path);
                return `
                <tr>
                    <td class="vp-mono">${esc(t.net_id)}</td>
                    <td class="vp-mono">${t.path.length}</td>
                    <td class="vp-mono">${len.toFixed(1)}</td>
                </tr>`;
            }).join('')}
        </tbody>`;
    section.appendChild(table);
    return section;
}


// ── Failed nets ───────────────────────────────────────────────

function buildFailedNets(failedNets) {
    const section = document.createElement('div');
    section.className = 'vp-section';
    const heading = document.createElement('h4');
    heading.style.color = 'var(--error, #f85149)';
    heading.textContent = `Failed Nets (${failedNets.length})`;
    section.appendChild(heading);

    const list = document.createElement('div');
    list.className = 'vp-net-list';
    for (const net of failedNets) {
        const row = document.createElement('div');
        row.className = 'vp-net-row';
        row.innerHTML = `<span class="vp-net-id" style="color:var(--error, #f85149)">${esc(net)}</span>`;
        list.appendChild(row);
    }
    section.appendChild(list);
    return section;
}


// ── Routing helpers ───────────────────────────────────────────────────

function traceLength(path) {
    let len = 0;
    for (let i = 1; i < path.length; i++) {
        const dx = path[i][0] - path[i - 1][0];
        const dy = path[i][1] - path[i - 1][1];
        len += Math.abs(dx) + Math.abs(dy);
    }
    return len;
}
