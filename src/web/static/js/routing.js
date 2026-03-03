/* Routing tab — run the router and display results */

import { API, state } from './state.js';
import { setData as setViewportData, setStale } from './viewport.js';
import { enableScadTab } from './scad.js';

function addStaleBanner(el, msg) {
    if (!el) return;
    const existing = el.querySelector('.stale-banner');
    if (existing) { existing.textContent = msg; return; }
    const banner = document.createElement('div');
    banner.className = 'stale-banner';
    banner.textContent = msg;
    el.prepend(banner);
}

/**
 * Mark the routing panel as stale (e.g. because placement changed).
 * Only acts if routing results are currently visible.
 */
export function markRoutingStale() {
    // Always mark the viewport stale (visible even if the info panel isn't shown)
    setStale('routing', true);
    // Add the text banner only when routing results are already rendered
    const scroll = document.getElementById('routing-scroll');
    const info = infoDiv();
    if (!scroll || scroll.hidden || !info) return;
    addStaleBanner(info, '⚠ Placement changed — routing data is stale. Re-run the router to update.');
}

const statusSpan = () => document.getElementById('routing-status');
const infoDiv    = () => document.getElementById('routing-info');
const runBtn     = () => document.getElementById('btn-run-routing');

/**
 * Enable the routing nav tab. If flash=true, add a pulsing
 * animation to attract attention (placement done, routing not yet).
 */
export function enableRoutingTab(flash = false) {
    const btn = document.querySelector('#pipeline-nav .step[data-step="routing"]');
    if (!btn) return;
    btn.disabled = false;
    btn.classList.toggle('tab-flash', flash);
}

/**
 * Stop the tab flash (called when routing completes or user clicks tab).
 */
function stopTabFlash() {
    const btn = document.querySelector('#pipeline-nav .step[data-step="routing"]');
    if (btn) btn.classList.remove('tab-flash');
}

/**
 * Reset the routing panel back to its initial hero state.
 */
export function resetRoutingPanel() {
    const hero = document.getElementById('routing-hero');
    const scroll = document.getElementById('routing-scroll');
    const info = infoDiv();
    if (hero) hero.hidden = false;
    if (scroll) scroll.hidden = true;
    if (info) info.innerHTML = '';
    showStatus('');
}

/**
 * Run the router for the current session.
 * Calls POST /api/session/routing and renders the result.
 */
export async function runRouting() {
    if (!state.session) {
        showStatus('No active session', true);
        return;
    }

    // Mark current view stale while the new run is in progress
    addStaleBanner(infoDiv(), '⏳ Re-running router…');

    // Disable both the hero CTA and any toolbar re-run button
    const heroBtn = runBtn();
    const rerun = document.querySelector('#routing-info .placement-toolbar-rerun');
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
            `${API}/api/session/routing?session=${encodeURIComponent(state.session)}`,
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
            showStatus(`Routing failed: ${msg}`, true);
            renderError(msg);
            return;
        }

        const data = await res.json();
        renderResult(data);
        setViewportData('routing', data);
        stopTabFlash();
        enableScadTab(true);
    } catch (e) {
        if (rerun) {
            rerun.textContent = '❌ Error';
        }
        showStatus(`Error: ${e.message}`, true);
    } finally {
        if (heroBtn) {
            heroBtn.disabled = false;
            heroBtn.textContent = 'Run Router';
        }
        if (rerun) {
            rerun.disabled = false;
            rerun.textContent = '↻ Re-run Router';
        }
    }
}

/**
 * Load a previously saved routing result for the current session.
 * Called on session restore.
 */
export async function loadRoutingResult() {
    if (!state.session) return;

    try {
        const res = await fetch(
            `${API}/api/session/routing/result?session=${encodeURIComponent(state.session)}`
        );
        if (!res.ok) return;  // no routing yet
        const data = await res.json();
        renderResult(data);
        setViewportData('routing', data);
        stopTabFlash();
        enableScadTab(true);
    } catch {
        // No routing available yet — that's fine
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
    const hero = document.getElementById('routing-hero');
    const scroll = document.getElementById('routing-scroll');
    if (hero) hero.hidden = true;
    if (scroll) scroll.hidden = false;
}

function renderResult(data) {
    const el = infoDiv();
    if (!el) return;

    showResultView();

    const traces = data.traces || [];
    const failedNets = data.failed_nets || [];
    const pinAssignments = data.pin_assignments || {};
    const nets = data.nets || [];

    // Build a map: net_id -> list of pin strings from the design
    const netPinMap = {};
    for (const n of nets) {
        const key = n.id;
        if (!netPinMap[key]) {
            netPinMap[key] = new Set();
        }
        for (const p of (n.pins || [])) {
            netPinMap[key].add(p);
        }
    }

    // Merge traces by net_id: aggregate segments + total length
    const mergedNets = {};
    const netOrder = [];
    for (const t of traces) {
        const key = t.net_id;
        if (!mergedNets[key]) {
            mergedNets[key] = { net_id: key, segments: 0, length: 0 };
            netOrder.push(key);
        }
        mergedNets[key].segments += t.path.length - 1;
        mergedNets[key].length += traceLength(t.path);
    }

    el.innerHTML = '';

    // Toolbar: summary + re-run button
    const toolbar = document.createElement('div');
    toolbar.className = 'placement-toolbar';

    const ok = failedNets.length === 0;
    const icon = ok ? '✅' : '⚠️';
    const netCount = netOrder.length;
    toolbar.innerHTML = `
        <span class="placement-toolbar-summary">${icon} Routed <strong>${netCount}</strong> net${netCount !== 1 ? 's' : ''}${failedNets.length > 0 ? `, <span style="color:var(--error)">${failedNets.length} failed</span>` : ''}</span>
    `;
    const rerunBtn = document.createElement('button');
    rerunBtn.className = 'placement-toolbar-rerun';
    rerunBtn.textContent = '↻ Re-run Router';
    rerunBtn.addEventListener('click', runRouting);
    toolbar.appendChild(rerunBtn);
    el.appendChild(toolbar);

    // Failed nets warning with pins
    if (failedNets.length > 0) {
        const warn = document.createElement('div');
        warn.className = 'placement-error';
        const failedRows = failedNets.map(n => {
            const pins = netPinMap[n]
                ? [...netPinMap[n]].map(p => esc(p)).join(', ')
                : '?';
            return `<div style="margin:4px 0"><strong>${esc(n)}</strong> <span style="font-size:11px; opacity:0.85">${pins}</span></div>`;
        }).join('');
        warn.innerHTML = `<strong>Failed nets (${failedNets.length}):</strong>${failedRows}`;
        el.appendChild(warn);
    }

    // Net table (merged by net_id, with pins)
    if (netOrder.length > 0) {
        const table = document.createElement('table');
        table.className = 'vp-table';
        table.innerHTML = `
            <thead><tr>
                <th>Net</th>
                <th>Pins</th>
                <th>Segments</th>
                <th>Length (mm)</th>
            </tr></thead>
            <tbody>
                ${netOrder.map(key => {
                    const m = mergedNets[key];
                    const pins = netPinMap[key]
                        ? [...netPinMap[key]].map(p => esc(p)).join(', ')
                        : '';
                    return `
                    <tr>
                        <td class="vp-mono">${esc(m.net_id)}</td>
                        <td class="vp-mono" style="font-size:11px; color:var(--text-muted)">${pins}</td>
                        <td class="vp-mono">${m.segments}</td>
                        <td class="vp-mono">${m.length.toFixed(1)}</td>
                    </tr>`;
                }).join('')}
            </tbody>`;
        el.appendChild(table);
    }

    // Pin assignments
    const assignEntries = Object.entries(pinAssignments);
    if (assignEntries.length > 0) {
        const section = document.createElement('div');
        section.style.marginTop = '12px';
        const heading = document.createElement('h4');
        heading.textContent = `Pin Assignments (${assignEntries.length})`;
        heading.style.cssText = 'font-size:13px; color:var(--text-muted); margin-bottom:6px;';
        section.appendChild(heading);

        const table = document.createElement('table');
        table.className = 'vp-table';
        table.innerHTML = `
            <thead><tr>
                <th>Logical</th>
                <th>Physical</th>
            </tr></thead>
            <tbody>
                ${assignEntries.map(([logical, physical]) => `
                    <tr>
                        <td class="vp-mono">${esc(logical)}</td>
                        <td class="vp-mono">${esc(physical)}</td>
                    </tr>
                `).join('')}
            </tbody>`;
        section.appendChild(table);
        el.appendChild(section);
    }
}

function renderError(msg) {
    const el = infoDiv();
    if (!el) return;
    el.innerHTML = `<div class="placement-error"><strong>Routing failed</strong><p>${esc(msg)}</p></div>`;
}

function traceLength(path) {
    let len = 0;
    for (let i = 1; i < path.length; i++) {
        const dx = path[i][0] - path[i - 1][0];
        const dy = path[i][1] - path[i - 1][1];
        len += Math.abs(dx) + Math.abs(dy);
    }
    return len;
}

function esc(text) {
    const el = document.createElement('span');
    el.textContent = text ?? '';
    return el.innerHTML;
}
