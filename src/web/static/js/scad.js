/* SCAD tab — generate enclosure.scad and display results */

import { API, state } from './state.js';
import { setData as setViewportData } from './viewport.js';
import { enableManufacturingTab } from './manufacturing.js';

// Active poll timer handle
let _pollTimer = null;

const statusSpan = () => document.getElementById('scad-status');
const infoDiv    = () => document.getElementById('scad-info');
const runBtn     = () => document.getElementById('btn-run-scad');

/**
 * Enable the SCAD nav tab. If flash=true, add a pulsing
 * animation to attract attention (routing done, SCAD not yet).
 */
export function enableScadTab(flash = false) {
    const btn = document.querySelector('#pipeline-nav .step[data-step="scad"]');
    if (!btn) return;
    btn.disabled = false;
    btn.classList.toggle('tab-flash', flash);
}

/**
 * Stop the tab flash (called when SCAD completes or user clicks tab).
 */
function stopTabFlash() {
    const btn = document.querySelector('#pipeline-nav .step[data-step="scad"]');
    if (btn) btn.classList.remove('tab-flash');
}

/**
 * Reset the SCAD panel back to its initial hero state.
 */
export function resetScadPanel() {
    stopPolling();
    const hero = document.getElementById('scad-hero');
    const scroll = document.getElementById('scad-scroll');
    const info = infoDiv();
    if (hero) hero.hidden = false;
    if (scroll) scroll.hidden = true;
    if (info) info.innerHTML = '';
    showStatus('');
}

/**
 * Run the SCAD generator for the current session.
 * Calls POST /api/session/scad and renders the result.
 */
export async function runScad() {
    if (!state.session) {
        showStatus('No active session', true);
        return;
    }

    const heroBtn = runBtn();
    const rerun = document.querySelector('#scad-info .placement-toolbar-rerun');
    if (heroBtn) {
        heroBtn.disabled = true;
        heroBtn.textContent = '⏳ Generating…';
    }
    if (rerun) {
        rerun.disabled = true;
        rerun.textContent = '⏳ Generating…';
    }

    try {
        const res = await fetch(
            `${API}/api/session/scad?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            const msg = typeof err.detail === 'string'
                ? err.detail
                : err.detail?.reason || JSON.stringify(err.detail);
            if (rerun) rerun.textContent = '❌ Failed';
            showStatus(`SCAD failed: ${msg}`, true);
            renderError(msg);
            return;
        }

        const data = await res.json();
        renderResult(data);
        stopTabFlash();
        enableManufacturingTab(true);
        // Kick off STL compile in the background
        startStlCompile(data);
    } catch (e) {
        if (rerun) rerun.textContent = '❌ Error';
        showStatus(`Error: ${e.message}`, true);
    } finally {
        if (heroBtn) {
            heroBtn.disabled = false;
            heroBtn.textContent = 'Generate SCAD';
        }
        if (rerun) {
            rerun.disabled = false;
            rerun.textContent = '↻ Re-generate';
        }
    }
}

/**
 * Load a previously generated enclosure.scad for the current session.
 * Called on session restore.
 */
export async function loadScadResult() {
    if (!state.session) return;

    try {
        const res = await fetch(
            `${API}/api/session/scad/result?session=${encodeURIComponent(state.session)}`
        );
        if (!res.ok) return;  // no SCAD yet
        const data = await res.json();
        renderResult(data);
        stopTabFlash();
        enableManufacturingTab(true);
        // Also check STL status
        pollOrRestoreStl(data);
    } catch {
        // No SCAD available yet -- that's fine
    }
}


// ── STL compile / poll ────────────────────────────────────────────

function stlUrl() {
    return `${API}/api/session/scad/stl?session=${encodeURIComponent(state.session)}`;
}

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

/**
 * Trigger STL compilation after a fresh SCAD generate.
 */
async function startStlCompile(scadData) {
    if (!state.session) return;
    // Push 'compiling' state to viewport immediately
    setViewportData('scad', {
        stlStatus: 'compiling',
        scadLines: scadData.scad_lines,
        scadBytes: scadData.scad_bytes,
    });

    try {
        await fetch(
            `${API}/api/session/scad/compile?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );
    } catch { /* ignore — poll will catch status */ }

    pollStlStatus(scadData);
}

/**
 * Check STL status on session restore; if already done restore the model,
 * if still compiling start polling, if pending auto-start compile.
 */
async function pollOrRestoreStl(scadData) {
    if (!state.session) return;
    try {
        const res = await fetch(
            `${API}/api/session/scad/compile?session=${encodeURIComponent(state.session)}`
        );
        const st = await res.json();
        if (st.status === 'done') {
            setViewportData('scad', {
                stlStatus: 'done',
                stlUrl: stlUrl(),
                stlBytes: st.stl_bytes,
                scadLines: scadData?.scad_lines,
                scadBytes: scadData?.scad_bytes,
            });
        } else if (st.status === 'compiling') {
            setViewportData('scad', { stlStatus: 'compiling' });
            pollStlStatus(scadData);
        } else if (st.status === 'error') {
            setViewportData('scad', { stlStatus: 'error', message: st.message || '' });
        } else {
            // pending — auto-start compile
            await startStlCompile(scadData);
        }
    } catch { /* ignore */ }
}

// ── Retry listener (from viewportScad.js error panel) ───────────────────────

document.addEventListener('scad:retry-stl', async () => {
    if (!state.session) return;
    stopPolling();
    setViewportData('scad', { stlStatus: 'compiling' });
    try {
        await fetch(
            `${API}/api/session/scad/compile?session=${encodeURIComponent(state.session)}&force=true`,
            { method: 'POST' },
        );
    } catch { /* ignore */ }
    pollStlStatus(null);
});

/** Poll every 3 s until compile finishes or errors. */
function pollStlStatus(scadData) {
    stopPolling();
    _pollTimer = setInterval(async () => {
        if (!state.session) { stopPolling(); return; }
        try {
            const res = await fetch(
                `${API}/api/session/scad/compile?session=${encodeURIComponent(state.session)}`
            );
            const st = await res.json();
            if (st.status === 'done') {
                stopPolling();
                setViewportData('scad', {
                    stlStatus: 'done',
                    stlUrl: stlUrl(),
                    stlBytes: st.stl_bytes,
                    scadLines: scadData?.scad_lines,
                    scadBytes: scadData?.scad_bytes,
                });
                // Update panel label
                const el = document.getElementById('scad-info');
                if (el) {
                    const note = el.querySelector('p');
                    if (note) note.textContent = 'enclosure.scad + enclosure.stl saved to session folder.';
                }
            } else if (st.status === 'error') {
                stopPolling();
                setViewportData('scad', { stlStatus: 'error', message: st.message || '' });
            }
        } catch { /* transient error — keep polling */ }
    }, 3000);
}


// ── Render helpers ────────────────────────────────────────────────

function showStatus(msg, isError = false) {
    const span = statusSpan();
    if (!span) return;
    span.textContent = msg;
    span.style.color = isError ? 'var(--error)' : '';
}

function showResultView() {
    const hero = document.getElementById('scad-hero');
    const scroll = document.getElementById('scad-scroll');
    if (hero) hero.hidden = true;
    if (scroll) scroll.hidden = false;
}

function renderResult(data) {
    const el = infoDiv();
    if (!el) return;

    showResultView();

    const lines = data.scad_lines ?? 0;
    const bytes = data.scad_bytes ?? (data.scad ? data.scad.length : 0);
    const scadText = data.scad ?? null;

    el.innerHTML = '';

    // Toolbar: summary + re-run + download
    const toolbar = document.createElement('div');
    toolbar.className = 'placement-toolbar';
    toolbar.innerHTML = `
        <span class="placement-toolbar-summary">✅ Generated <strong>${lines.toLocaleString()}</strong> lines &nbsp;·&nbsp; <strong>${(bytes / 1024).toFixed(1)} kB</strong></span>
    `;

    const rerunBtn = document.createElement('button');
    rerunBtn.className = 'placement-toolbar-rerun';
    rerunBtn.textContent = '↻ Re-generate';
    rerunBtn.addEventListener('click', runScad);
    toolbar.appendChild(rerunBtn);

    // Download button
    const dlBtn = document.createElement('button');
    dlBtn.className = 'placement-toolbar-rerun';
    dlBtn.textContent = '⬇ Download .scad';
    dlBtn.style.marginLeft = '6px';
    dlBtn.addEventListener('click', async () => {
        let text = scadText;
        if (!text) {
            try {
                const r = await fetch(
                    `${API}/api/session/scad/result?session=${encodeURIComponent(state.session)}`
                );
                const d = await r.json();
                text = d.scad;
            } catch {
                alert('Could not fetch enclosure.scad');
                return;
            }
        }
        const blob = new Blob([text], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'enclosure.scad';
        a.click();
        URL.revokeObjectURL(url);
    });
    toolbar.appendChild(dlBtn);

    el.appendChild(toolbar);

    // Stats card
    const card = document.createElement('div');
    card.style.cssText = 'margin-top:12px; display:grid; grid-template-columns:1fr 1fr; gap:8px;';
    card.innerHTML = `
        <div style="background:var(--surface-raised,#2a2a2a); border-radius:6px; padding:10px 14px;">
            <div style="font-size:11px; color:var(--text-muted); margin-bottom:2px;">Lines</div>
            <div style="font-size:20px; font-weight:600;">${lines.toLocaleString()}</div>
        </div>
        <div style="background:var(--surface-raised,#2a2a2a); border-radius:6px; padding:10px 14px;">
            <div style="font-size:11px; color:var(--text-muted); margin-bottom:2px;">File size</div>
            <div style="font-size:20px; font-weight:600;">${(bytes / 1024).toFixed(1)} kB</div>
        </div>
    `;
    el.appendChild(card);

    // Info note
    const note = document.createElement('p');
    note.style.cssText = 'margin-top:14px; font-size:12px; color:var(--text-muted);';
    note.textContent = 'enclosure.scad saved to session folder. Compiling STL for 3D preview…';
    el.appendChild(note);
}

function renderError(msg) {
    const el = infoDiv();
    if (!el) return;
    el.innerHTML = `<div class="placement-error"><strong>SCAD generation failed</strong><p>${esc(msg)}</p></div>`;
}

function esc(text) {
    const el = document.createElement('span');
    el.textContent = text ?? '';
    return el.innerHTML;
}
