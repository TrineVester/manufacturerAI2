/* Viewport handler for the manufacturing step — shows summary + status */

import { registerHandler } from './viewport.js';

registerHandler('manufacturing', {
    label: 'Manufacturing',
    placeholder: 'Run the manufacturing pipeline to see G-code and bitmap results',

    render(el, data) {
        if (!data) {
            el.innerHTML = '<p class="viewport-empty">No manufacturing data yet</p>';
            return;
        }

        const { manifest, bitmap } = data;
        el.innerHTML = '';

        const wrap = document.createElement('div');
        wrap.className = 'vp-manufacturing';

        // Summary header
        const header = document.createElement('div');
        header.className = 'vp-mfg-header';
        const success = manifest?.success !== false;
        header.innerHTML = `<h3>${success ? '✅' : '⚠️'} Manufacturing Output</h3>`;
        wrap.appendChild(header);

        // Info cards
        const grid = document.createElement('div');
        grid.className = 'vp-mfg-grid';

        if (manifest) {
            if (manifest.printer) {
                grid.innerHTML += infoCard('Printer', manifest.printer.label);
            }
            if (manifest.filament) {
                grid.innerHTML += infoCard('Filament', manifest.filament.label);
            }
            if (manifest.bed_center) {
                grid.innerHTML += infoCard('Bed center',
                    `${manifest.bed_center[0].toFixed(1)}, ${manifest.bed_center[1].toFixed(1)} mm`);
            }
            if (manifest.gcode_bytes) {
                grid.innerHTML += infoCard('G-code size',
                    `${(manifest.gcode_bytes / 1024).toFixed(1)} kB`);
            }
            if (manifest.pause_points) {
                const inkP = manifest.pause_points.find(p => p.label === 'ink');
                const compP = manifest.pause_points.filter(p => p.label === 'components');
                let pauseDesc = '';
                if (inkP) pauseDesc += `Ink @ Z=${inkP.z.toFixed(1)}mm`;
                if (compP.length) pauseDesc += `${pauseDesc ? ', ' : ''}${compP.length} component pause${compP.length > 1 ? 's' : ''}`;
                if (pauseDesc) grid.innerHTML += infoCard('Pauses', pauseDesc);
            }
        }

        if (bitmap) {
            grid.innerHTML += infoCard('Bitmap resolution', `${bitmap.cols} × ${bitmap.rows} px`);
            grid.innerHTML += infoCard('Nozzle pitch', `${bitmap.pixel_size_mm} mm`);
            grid.innerHTML += infoCard('Ink coverage',
                `${bitmap.ink_pixels?.toLocaleString() || 0} pixels (${bitmap.trace_count || 0} traces)`);
        }

        wrap.appendChild(grid);

        // Message
        if (manifest?.message) {
            const msg = document.createElement('p');
            msg.className = 'vp-mfg-message';
            msg.textContent = manifest.message;
            wrap.appendChild(msg);
        }

        el.appendChild(wrap);
    },

    clear(el) {
        el.innerHTML = '<p class="viewport-empty">No manufacturing data yet</p>';
    },
});

function infoCard(label, value) {
    return `<div class="vp-mfg-card">
        <div class="vp-mfg-card-label">${esc(label)}</div>
        <div class="vp-mfg-card-value">${esc(value)}</div>
    </div>`;
}

function esc(text) {
    const el = document.createElement('span');
    el.textContent = text;
    return el.innerHTML;
}
