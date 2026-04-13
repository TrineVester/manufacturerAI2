/* Manufacturing tab — G-code pipeline + bitmap generation */

import { API, state } from './state.js';
import { setData as setViewportData } from './viewport.js';
import { refreshSession } from './session.js';

let _pollTimer = null;

const statusSpan = () => document.getElementById('manufacturing-status');
const infoDiv    = () => document.getElementById('manufacturing-info');
const runBtn     = () => document.getElementById('btn-run-manufacturing');

/**
 * Enable the manufacturing nav tab. If flash=true, pulse to attract attention.
 */
export function enableManufacturingTab(flash = false) {
    const btn = document.querySelector('#pipeline-nav .step[data-step="manufacturing"]');
    if (!btn) return;
    btn.disabled = false;
    btn.classList.toggle('tab-flash', flash);
}

function stopTabFlash() {
    const btn = document.querySelector('#pipeline-nav .step[data-step="manufacturing"]');
    if (btn) btn.classList.remove('tab-flash');
}

export function resetManufacturingPanel() {
    stopPolling();
    const hero = document.getElementById('manufacturing-hero');
    const scroll = document.getElementById('manufacturing-scroll');
    const info = infoDiv();
    if (hero) hero.hidden = false;
    if (scroll) scroll.hidden = true;
    if (info) info.innerHTML = '';
    showStatus('');
}

function showStatus(msg, isError = false) {
    const span = statusSpan();
    if (!span) return;
    span.textContent = msg;
    span.style.color = isError ? 'var(--error)' : '';
}

function showResultView() {
    const hero = document.getElementById('manufacturing-hero');
    const scroll = document.getElementById('manufacturing-scroll');
    if (hero) hero.hidden = true;
    if (scroll) scroll.hidden = false;
}

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

function esc(text) {
    const el = document.createElement('span');
    el.textContent = text;
    return el.innerHTML;
}

// ── Main run action ───────────────────────────────────────────

/**
 * Run the full manufacturing pipeline: G-code + bitmap.
 */
export async function runManufacturing() {
    if (!state.session) {
        showStatus('No active session', true);
        return;
    }

    const heroBtn = runBtn();
    const rerun = document.querySelector('#manufacturing-info .mfg-toolbar-rerun');
    if (heroBtn) { heroBtn.disabled = true; heroBtn.textContent = '⏳ Running…'; }
    if (rerun) { rerun.disabled = true; rerun.textContent = '⏳ Running…'; }

    showStatus('Starting G-code pipeline…');

    try {
        // 1. Start G-code pipeline (async on server)
        const gcRes = await fetch(
            `${API}/api/session/manufacturing/gcode?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );
        if (!gcRes.ok) {
            const err = await gcRes.json().catch(() => ({ detail: gcRes.statusText }));
            showStatus(`G-code failed: ${err.detail || err.message || gcRes.statusText}`, true);
            return;
        }

        // 2. Poll G-code status
        await pollGcodeUntilDone();

        // 3. Run bitmap generation (synchronous)
        showStatus('Generating trace bitmap…');
        const bmRes = await fetch(
            `${API}/api/session/manufacturing/bitmap?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );
        let bitmapData = null;
        if (bmRes.ok) {
            bitmapData = await bmRes.json();
        }

        // 4. Fetch manifest
        let manifest = null;
        try {
            const mfRes = await fetch(
                `${API}/api/session/manufacturing/manifest?session=${encodeURIComponent(state.session)}`
            );
            if (mfRes.ok) manifest = await mfRes.json();
        } catch { /* optional */ }

        // 5. Render results
        renderResult(manifest, bitmapData);
        stopTabFlash();
        setViewportData('manufacturing', { manifest, bitmap: bitmapData });
        refreshSession();
        showStatus('');

        // Show firmware section (routing must exist to reach here)
        import('./firmware.js').then(m => m.showFirmwareSection());
    } catch (e) {
        showStatus(`Error: ${e.message}`, true);
    } finally {
        if (heroBtn) { heroBtn.disabled = false; heroBtn.textContent = 'Run Manufacturing'; }
        if (rerun) { rerun.disabled = false; rerun.textContent = '↻ Re-run'; }
    }
}

/**
 * Poll G-code status until done or error.
 */
function pollGcodeUntilDone() {
    return new Promise((resolve, reject) => {
        const check = async () => {
            try {
                const res = await fetch(
                    `${API}/api/session/manufacturing/gcode?session=${encodeURIComponent(state.session)}`
                );
                const st = await res.json();
                if (st.status === 'done') {
                    stopPolling();
                    resolve(st);
                } else if (st.status === 'error') {
                    stopPolling();
                    showStatus(`G-code error: ${st.message}`, true);
                    resolve(st);
                } else {
                    showStatus(`Slicing… ${st.stages?.length || 0} stages`);
                }
            } catch (e) {
                // transient — keep polling
            }
        };
        check();
        _pollTimer = setInterval(check, 2000);
    });
}

/**
 * Load existing manufacturing results on session restore.
 */
export async function loadManufacturingResult() {
    if (!state.session) return;

    let manifest = null;
    let bitmapData = null;

    try {
        const mfRes = await fetch(
            `${API}/api/session/manufacturing/manifest?session=${encodeURIComponent(state.session)}`
        );
        if (mfRes.ok) manifest = await mfRes.json();
    } catch { /* no manifest yet */ }

    try {
        const bmRes = await fetch(
            `${API}/api/session/manufacturing/bitmap?session=${encodeURIComponent(state.session)}`
        );
        if (bmRes.ok) bitmapData = await bmRes.json();
    } catch { /* no bitmap yet */ }

    if (!manifest && !bitmapData) return;

    renderResult(manifest, bitmapData);
    stopTabFlash();
    setViewportData('manufacturing', { manifest, bitmap: bitmapData });
}


// ── Render helpers ────────────────────────────────────────────

function renderResult(manifest, bitmapData) {
    const el = infoDiv();
    if (!el) return;
    showResultView();
    el.innerHTML = '';

    const success = manifest?.success !== false;
    const icon = success ? '✅' : '⚠️';

    // Toolbar
    const toolbar = document.createElement('div');
    toolbar.className = 'placement-toolbar';
    toolbar.innerHTML = `<span class="placement-toolbar-summary">${icon} Manufacturing ${success ? 'complete' : 'partial'}</span>`;

    const rerunBtn = document.createElement('button');
    rerunBtn.className = 'placement-toolbar-rerun mfg-toolbar-rerun';
    rerunBtn.textContent = '↻ Re-run';
    rerunBtn.addEventListener('click', runManufacturing);
    toolbar.appendChild(rerunBtn);
    el.appendChild(toolbar);

    // Stats grid
    const grid = document.createElement('div');
    grid.className = 'mfg-stats-grid';

    if (manifest) {
        grid.innerHTML += card('Printer', manifest.printer?.label || '—');
        grid.innerHTML += card('Filament', manifest.filament?.label || '—');
        if (manifest.gcode_bytes) {
            grid.innerHTML += card('G-code', `${(manifest.gcode_bytes / 1024).toFixed(1)} kB`);
        }
        if (manifest.pause_points) {
            grid.innerHTML += card('Pauses', `${manifest.pause_points.length}`);
        }
    }

    if (bitmapData) {
        grid.innerHTML += card('Bitmap', `${bitmapData.cols} × ${bitmapData.rows}`);
        grid.innerHTML += card('Ink pixels', bitmapData.ink_pixels?.toLocaleString() || '0');
        grid.innerHTML += card('Pixel pitch', `${bitmapData.pixel_size_mm} mm`);
    }

    el.appendChild(grid);

    // Pause points table
    if (manifest?.pause_points?.length) {
        const section = document.createElement('div');
        section.style.marginTop = '14px';
        const heading = document.createElement('h4');
        heading.textContent = 'Pause Points';
        heading.style.cssText = 'font-size:13px; color:var(--text-muted); margin-bottom:6px;';
        section.appendChild(heading);

        const table = document.createElement('table');
        table.className = 'vp-table';
        table.innerHTML = `
            <thead><tr>
                <th>Z (mm)</th><th>Layer</th><th>Type</th><th>Components</th>
            </tr></thead>
            <tbody>
                ${manifest.pause_points.map(p => `
                    <tr>
                        <td class="vp-mono">${p.z.toFixed(2)}</td>
                        <td class="vp-mono">${p.layer_number}</td>
                        <td>${esc(p.label)}</td>
                        <td class="vp-mono" style="font-size:11px">${(p.components || []).join(', ') || '—'}</td>
                    </tr>
                `).join('')}
            </tbody>
        `;
        section.appendChild(table);
        el.appendChild(section);
    }

    // Pipeline stages log
    if (manifest?.stages?.length) {
        const section = document.createElement('div');
        section.style.marginTop = '14px';
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = `Pipeline log (${manifest.stages.length} stages)`;
        summary.style.cssText = 'font-size:12px; color:var(--text-muted); cursor:pointer;';
        const pre = document.createElement('pre');
        pre.style.cssText = 'font-size:11px; color:var(--text-dim); padding:8px; background:var(--surface); border-radius:4px; margin-top:6px; overflow-x:auto;';
        pre.textContent = manifest.stages.join('\n');
        details.appendChild(summary);
        details.appendChild(pre);
        section.appendChild(details);
        el.appendChild(section);
    }

    // Download buttons
    const dlRow = document.createElement('div');
    dlRow.style.cssText = 'display:flex; gap:8px; margin-top:16px; flex-wrap:wrap;';

    if (manifest?.gcode_bytes) {
        const btn = document.createElement('button');
        btn.textContent = '⬇ Download G-code';
        btn.addEventListener('click', () => {
            window.open(`${API}/api/session/manufacturing/gcode/download?session=${encodeURIComponent(state.session)}`);
        });
        dlRow.appendChild(btn);
    }

    if (bitmapData?.success !== false) {
        const btn = document.createElement('button');
        btn.textContent = '⬇ Download Bitmap';
        btn.addEventListener('click', () => {
            window.open(`${API}/api/session/manufacturing/bitmap/download?session=${encodeURIComponent(state.session)}`);
        });
        dlRow.appendChild(btn);
    }

    if (dlRow.children.length) el.appendChild(dlRow);
}

function card(label, value) {
    return `<div class="mfg-stat-card">
        <div class="mfg-stat-label">${esc(label)}</div>
        <div class="mfg-stat-value">${esc(value)}</div>
    </div>`;
}
