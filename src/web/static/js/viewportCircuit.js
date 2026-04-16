/**
 * Viewport handler for the Circuit step.
 *
 * Renders the electrical design:
 *   - Component list (instance, catalog, mounting style)
 *   - Net list (net id + connected pins)
 *
 * Data shape (from circuit.json, enriched):
 * {
 *   components: [{ catalog_id, instance_id, mounting_style?, config?, body?, pins? }],
 *   nets:       [{ id, pins: ["instance:pin", …] }]
 * }
 */

import { registerHandler } from './viewport.js';

// ── Register ────────────────────────────────────────────────

registerHandler('circuit', {
    label: 'Circuit Preview',
    placeholder: 'Run the circuit agent to see components and nets',

    render(el, data) {
        el.innerHTML = '';
        el.appendChild(buildPreview(data));
    },

    clear(el) {
        el.innerHTML = '<p class="viewport-empty">Run the circuit agent to see components and nets</p>';
    },
});


// ── Preview builder ───────────────────────────────────────────

function buildPreview(data) {
    const wrap = document.createElement('div');
    wrap.className = 'vp-design';

    wrap.appendChild(buildComponentList(data.components));
    wrap.appendChild(buildNetList(data.nets));

    return wrap;
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


function esc(text) {
    const el = document.createElement('span');
    el.textContent = text;
    return el.innerHTML;
}
