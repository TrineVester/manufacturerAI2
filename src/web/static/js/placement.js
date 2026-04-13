/* Placement tab — run the placer and display results */

import { API, state } from './state.js';
import { setData as setViewportData } from './viewport.js';
import { enableGuideBtn } from './guide.js';
import { enableRoutingTab, markRoutingStale } from './routing.js';
import { refreshSession } from './session.js';

function addStaleBanner(el, msg) {
    if (!el) return;
    const existing = el.querySelector('.stale-banner');
    if (existing) { existing.textContent = msg; return; }
    const banner = document.createElement('div');
    banner.className = 'stale-banner';
    banner.textContent = msg;
    el.prepend(banner);
}
const statusSpan = () => document.getElementById('placement-status');
const infoDiv    = () => document.getElementById('placement-info');
const runBtn     = () => document.getElementById('btn-run-placement');

/**
 * Enable the placement nav tab. If flash=true, add a pulsing
 * animation to attract attention (design done, placement not yet).
 */
export function enablePlacementTab(flash = false) {
    const btn = document.querySelector('#pipeline-nav .step[data-step="placement"]');
    if (!btn) return;
    btn.disabled = false;
    btn.classList.toggle('tab-flash', flash);
}

/**
 * Stop the tab flash (called when placement completes or user clicks the tab).
 */
function stopTabFlash() {
    const btn = document.querySelector('#pipeline-nav .step[data-step="placement"]');
    if (btn) btn.classList.remove('tab-flash');
}

/**
 * Reset the placement panel back to its initial hero state.
 */
export function resetPlacementPanel() {
    const hero = document.getElementById('placement-hero');
    const scroll = document.getElementById('placement-scroll');
    const info = infoDiv();
    if (hero) hero.hidden = false;
    if (scroll) scroll.hidden = true;
    if (info) info.innerHTML = '';
    showStatus('');
}

/**
 * Run the placer for the current session.
 * Calls POST /api/session/placement and renders the result.
 */
export async function runPlacement() {
    if (!state.session) {
        showStatus('No active session', true);
        return;
    }

    // Mark current view stale while the new run is in progress
    addStaleBanner(infoDiv(), '⏳ Re-running placer…');

    // Disable both the hero CTA and any toolbar re-run button
    const heroBtn = runBtn();
    const rerun = document.querySelector('#placement-info .placement-toolbar-rerun');
    if (heroBtn) {
        heroBtn.disabled = true;
        heroBtn.textContent = '⏳ Running…';
    }
    if (rerun) {
        rerun.disabled = true;
        rerun.textContent = '⏳ Running…';
    }

    try {
        const res = await fetch(
            `${API}/api/session/placement?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            const msg = typeof err.detail === 'string'
                ? err.detail
                : err.detail?.reason || JSON.stringify(err.detail);
            if (rerun) {
                rerun.textContent = '❌ Failed';
            }
            showStatus(`Placement failed: ${msg}`, true);
            renderError(msg);
            return;
        }

        const data = await res.json();
        renderResult(data);
        setViewportData('placement', data);
        stopTabFlash();
        enableGuideBtn(true);
        // Mark routing as stale (invalidated by new placement)
        markRoutingStale();
        enableRoutingTab(true);
        refreshSession();
    } catch (e) {
        if (rerun) {
            rerun.textContent = '❌ Error';
        }
        showStatus(`Error: ${e.message}`, true);
    } finally {
        if (heroBtn) {
            heroBtn.disabled = false;
            heroBtn.textContent = 'Run Placer';
        }
        // rerun button is recreated by renderResult; re-enable the old
        // reference in case of error (renderError doesn't recreate it)
        if (rerun) {
            rerun.disabled = false;
            rerun.textContent = '↻ Re-run Placer';
        }
    }
}

/**
 * Load a previously saved placement result for the current session.
 * Called on session restore.
 */
export async function loadPlacementResult() {
    if (!state.session) return;

    try {
        const res = await fetch(
            `${API}/api/session/placement/result?session=${encodeURIComponent(state.session)}`
        );
        if (!res.ok) return;  // no placement yet
        const data = await res.json();
        renderResult(data);
        setViewportData('placement', data);
        stopTabFlash();
        enableGuideBtn(true);
        enableRoutingTab(true);
    } catch {
        // No placement available yet — that's fine
    }
}


// ── Render helpers ────────────────────────────────────────────────

function showStatus(msg, isError = false) {
    const span = statusSpan();
    if (!span) return;
    span.textContent = msg;
    span.style.color = isError ? 'var(--error)' : '';
}

function showResultView() {
    const hero = document.getElementById('placement-hero');
    const scroll = document.getElementById('placement-scroll');
    if (hero) hero.hidden = true;
    if (scroll) scroll.hidden = false;
}

function renderResult(data) {
    const el = infoDiv();
    if (!el) return;

    showResultView();

    const comps = data.components || [];

    el.innerHTML = '';

    // Toolbar: summary + re-run button
    const toolbar = document.createElement('div');
    toolbar.className = 'placement-toolbar';
    toolbar.innerHTML = `
        <span class="placement-toolbar-summary">✅ Placed <strong>${comps.length}</strong> component${comps.length !== 1 ? 's' : ''}</span>
    `;
    const rerunBtn = document.createElement('button');
    rerunBtn.className = 'placement-toolbar-rerun';
    rerunBtn.textContent = '↻ Re-run Placer';
    rerunBtn.addEventListener('click', runPlacement);
    toolbar.appendChild(rerunBtn);
    el.appendChild(toolbar);

    // Component table with color dots
    const COMP_COLORS = [
        '#58a6ff', '#3fb950', '#d29922', '#f778ba', '#bc8cff',
        '#79c0ff', '#56d364', '#e3b341', '#ff7b72', '#a5d6ff',
    ];
    if (comps.length > 0) {
        const table = document.createElement('table');
        table.className = 'vp-table';
        table.innerHTML = `
            <thead><tr>
                <th></th>
                <th>Instance</th>
                <th>Catalog ID</th>
                <th>X (mm)</th>
                <th>Y (mm)</th>
                <th>Rotation</th>
            </tr></thead>
            <tbody>
                ${comps.map((c, i) => `
                    <tr data-instance-id="${esc(c.instance_id)}">
                        <td><span class="color-dot" style="background:${COMP_COLORS[i % COMP_COLORS.length]}"></span></td>
                        <td class="vp-mono">${esc(c.instance_id)}</td>
                        <td>${esc(c.catalog_id)}</td>
                        <td class="vp-mono">${c.x_mm.toFixed(1)}</td>
                        <td class="vp-mono">${c.y_mm.toFixed(1)}</td>
                        <td class="vp-mono">${c.rotation_deg}°</td>
                    </tr>
                `).join('')}
            </tbody>`;
        el.appendChild(table);

        // Hover: table row ↔ SVG component highlighting
        table.addEventListener('mouseenter', e => {
            const row = e.target.closest('tr[data-instance-id]');
            if (!row) return;
            _highlightComponent(row.dataset.instanceId, true);
        }, true);
        table.addEventListener('mouseleave', e => {
            const row = e.target.closest('tr[data-instance-id]');
            if (!row) return;
            _highlightComponent(row.dataset.instanceId, false);
        }, true);
    }
}

function renderError(msg) {
    const el = infoDiv();
    if (!el) return;
    el.innerHTML = `<div class="placement-error"><strong>Placement failed</strong><p>${esc(msg)}</p></div>`;
}

/**
 * Highlight a component in the viewport SVG and the panel table.
 * @param {string} instanceId  Component instance_id
 * @param {boolean} on         true = highlight, false = remove highlight
 */
function _highlightComponent(instanceId, on) {
    // Highlight SVG group in viewport
    const viewport = document.getElementById('viewport-content');
    if (viewport) {
        const svgGroup = viewport.querySelector(`g.vp-comp-group[data-instance-id="${instanceId}"]`);
        if (svgGroup) svgGroup.classList.toggle('vp-hover', on);
    }
    // Highlight table row in panel
    const info = infoDiv();
    if (info) {
        const row = info.querySelector(`tr[data-instance-id="${instanceId}"]`);
        if (row) row.classList.toggle('vp-hover', on);
    }
}

function esc(text) {
    const el = document.createElement('span');
    el.textContent = text ?? '';
    return el.innerHTML;
}
