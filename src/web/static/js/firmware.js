/* Firmware generation panel — generates and displays Arduino sketch */

import { API, state } from './state.js';

const MAX_PREVIEW_LINES = 50;

export function initFirmwarePanel() {
    const section = document.getElementById('firmware-section');
    if (!section) return;

    document.getElementById('btn-gen-firmware')?.addEventListener('click', generateFirmware);
    document.getElementById('btn-download-firmware')?.addEventListener('click', downloadFirmware);
    document.getElementById('btn-open-simulator')?.addEventListener('click', () => {
        import('./simulator.js').then(m => m.openSimulator());
    });
}

export function showFirmwareSection() {
    const section = document.getElementById('firmware-section');
    if (section) section.hidden = false;
}

export async function generateFirmware() {
    if (!state.session) return;
    const status = document.getElementById('firmware-status');
    const preview = document.getElementById('firmware-preview');
    const report = document.getElementById('firmware-report');
    const btnGen = document.getElementById('btn-gen-firmware');
    const btnDl = document.getElementById('btn-download-firmware');
    const btnSim = document.getElementById('btn-open-simulator');

    if (btnGen) btnGen.disabled = true;
    if (status) {
        status.className = 'firmware-status firmware-status-loading';
        status.innerHTML = '<span class="firmware-spinner"></span> Generating firmware…';
    }
    if (preview) preview.hidden = true;
    if (report) report.hidden = true;

    try {
        const res = await fetch(
            `${API}/api/session/firmware?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Firmware generation failed');
        }

        const data = await res.json();
        state.firmware = data;
        _renderFirmwareResult(data);
    } catch (e) {
        if (status) {
            status.className = 'firmware-status firmware-status-error';
            status.textContent = `❌ ${e.message}`;
        }
    } finally {
        if (btnGen) btnGen.disabled = false;
    }
}

export async function loadFirmwareResult() {
    if (!state.session) return;
    try {
        const res = await fetch(
            `${API}/api/session/firmware/result?session=${encodeURIComponent(state.session)}`,
        );
        if (!res.ok) return;
        const data = await res.json();
        state.firmware = data;

        showFirmwareSection();
        _renderFirmwareResult(data);
    } catch { /* no firmware yet */ }
}

function _renderFirmwareResult(data) {
    const status = document.getElementById('firmware-status');
    const preview = document.getElementById('firmware-preview');
    const report = document.getElementById('firmware-report');
    const btnDl = document.getElementById('btn-download-firmware');
    const btnSim = document.getElementById('btn-open-simulator');

    const pinCount = data.pin_map?.length || 0;

    // Status
    if (status) {
        status.className = 'firmware-status firmware-status-ok';
        status.textContent = `✅ Firmware ready — ${pinCount} component${pinCount !== 1 ? 's' : ''} mapped`;
    }

    // Pin map summary table (more intuitive than raw text)
    if (report && data.pin_map && data.pin_map.length > 0) {
        let html = '<div class="firmware-pin-map">';
        html += '<h5>📌 Pin Assignments</h5>';
        html += '<table class="vp-table"><thead><tr><th>Component</th><th>Function</th><th>Arduino Pin</th></tr></thead><tbody>';
        for (const entry of data.pin_map) {
            const comp = _escapeHtml(entry.component || entry.instance_id || '?');
            const func = _escapeHtml(entry.function || entry.pin_label || '');
            const pin = _escapeHtml(String(entry.arduino_pin ?? entry.pin ?? ''));
            html += `<tr><td class="vp-mono">${comp}</td><td>${func}</td><td class="vp-mono">${pin}</td></tr>`;
        }
        html += '</tbody></table></div>';

        if (data.warnings?.length) {
            html += '<div class="firmware-warnings">';
            html += data.warnings.map(w => `<div class="fw-warn">⚠️ ${_escapeHtml(w)}</div>`).join('');
            html += '</div>';
        }

        if (data.pin_report) {
            html += `<details class="firmware-details"><summary>Full Pin Report</summary><pre>${_escapeHtml(data.pin_report)}</pre></details>`;
        }

        report.innerHTML = html;
        report.hidden = false;
    }

    // Sketch preview (with line numbers)
    if (preview && data.sketch) {
        const lines = data.sketch.split('\n');
        const shown = lines.slice(0, MAX_PREVIEW_LINES);
        const numbered = shown.map((line, i) =>
            `<span class="fw-line-num">${String(i + 1).padStart(3)}</span> ${_escapeHtml(line)}`
        ).join('\n');
        const truncMsg = lines.length > MAX_PREVIEW_LINES
            ? `\n<span class="fw-truncated">// … ${lines.length - MAX_PREVIEW_LINES} more lines — download for full sketch</span>`
            : '';
        preview.innerHTML = numbered + truncMsg;
        preview.hidden = false;
    }

    if (btnDl) btnDl.hidden = false;
    if (btnSim) btnSim.hidden = false;
}

function downloadFirmware() {
    if (!state.session) return;
    window.open(
        `${API}/api/session/firmware/download?session=${encodeURIComponent(state.session)}`,
        '_blank',
    );
}

function _escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
