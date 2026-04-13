/* Device simulator — interactive mock of the manufactured device.

Reads the firmware data (pin_map, sketch) to build an SVG representation
of the device with interactive buttons and visual LED/motor state.

This is a client-side simulation — no actual Arduino code execution.
It interprets the pin_map to connect button presses to LED/motor toggles
via the net topology.
*/

import { API, state } from './state.js';
import { closeModal } from './utils.js';

let simState = {};  // pin_name -> HIGH/LOW
let serialLog = [];

export async function openSimulator() {
    const modal = document.getElementById('simulator-modal');
    if (!modal) return;

    // Load firmware data if not cached
    if (!state.firmware?.pin_map) {
        try {
            const res = await fetch(
                `${API}/api/session/firmware/result?session=${encodeURIComponent(state.session)}`,
            );
            if (res.ok) state.firmware = await res.json();
        } catch { /* no firmware */ }
    }

    if (!state.firmware?.pin_map?.length) {
        const container = document.getElementById('simulator-device');
        if (container) container.innerHTML = '<p class="sim-empty">No firmware pin map available. Generate firmware first.</p>';
        modal.hidden = false;
        return;
    }

    // Load placement for device outline
    let placement = null;
    try {
        const res = await fetch(
            `${API}/api/session/placement/result?session=${encodeURIComponent(state.session)}`,
        );
        if (res.ok) placement = await res.json();
    } catch { /* proceed without outline */ }

    _buildSimulator(state.firmware.pin_map, placement);
    modal.hidden = false;

    // Wire close button
    modal.querySelector('.modal-close')?.addEventListener('click', () => {
        modal.hidden = true;
    }, { once: true });
}

function _buildSimulator(pinMap, placement) {
    const deviceEl = document.getElementById('simulator-device');
    const serialEl = document.getElementById('simulator-serial');
    if (!deviceEl) return;

    simState = {};
    serialLog = [];
    if (serialEl) serialEl.textContent = '> Device powered on\n';

    // Classify components from pin_map
    const buttons = pinMap.filter(e => e.type === 'button');
    const leds = pinMap.filter(e => e.type === 'led');
    const motors = pinMap.filter(e => e.type === 'motor');
    const irReceivers = pinMap.filter(e => e.type === 'ir_receiver');

    // Initialize output states
    for (const led of leds) {
        simState[led.instance_id] = false;
    }
    for (const motor of motors) {
        simState[motor.instance_id] = false;
    }

    // Build HTML
    let html = '<div class="sim-device-inner">';

    // Device outline
    if (placement?.outline?.length) {
        html += _renderOutlineSVG(placement.outline, placement.components, pinMap);
    }

    // Interactive components
    html += '<div class="sim-components">';

    for (const btn of buttons) {
        html += `
            <div class="sim-comp sim-button" data-id="${btn.instance_id}">
                <div class="sim-btn-cap" data-id="${btn.instance_id}"></div>
                <span class="sim-label">${btn.instance_id}</span>
            </div>`;
    }

    for (const led of leds) {
        html += `
            <div class="sim-comp sim-led" data-id="${led.instance_id}">
                <div class="sim-led-bulb off" data-id="${led.instance_id}"></div>
                <span class="sim-label">${led.instance_id}</span>
            </div>`;
    }

    for (const motor of motors) {
        html += `
            <div class="sim-comp sim-motor" data-id="${motor.instance_id}">
                <div class="sim-motor-icon off" data-id="${motor.instance_id}">⚙</div>
                <span class="sim-label">${motor.instance_id}</span>
            </div>`;
    }

    html += '</div></div>';
    deviceEl.innerHTML = html;

    // Wire button events — simple simulation: toggle connected outputs
    const netConnections = _buildNetMap(pinMap);

    deviceEl.querySelectorAll('.sim-btn-cap').forEach(cap => {
        cap.addEventListener('mousedown', () => {
            const id = cap.dataset.id;
            _onButtonPress(id, netConnections, serialEl);
            cap.classList.add('pressed');
        });
        cap.addEventListener('mouseup', () => {
            cap.classList.remove('pressed');
        });
        cap.addEventListener('mouseleave', () => {
            cap.classList.remove('pressed');
        });
    });
}

function _buildNetMap(pinMap) {
    // Map button instance_ids to led/motor instance_ids via shared nets
    // This is a simplified simulation — in real firmware, the MCU processes
    // inputs and drives outputs based on the sketch logic.
    const connections = {};

    // For each button, find outputs that share any net
    const buttonNets = {};
    const outputsByNet = {};

    for (const entry of pinMap) {
        for (const pin of entry.pins || []) {
            if (entry.type === 'button') {
                if (!buttonNets[entry.instance_id]) buttonNets[entry.instance_id] = new Set();
                buttonNets[entry.instance_id].add(pin.net_id);
            } else if (entry.type === 'led' || entry.type === 'motor') {
                if (!outputsByNet[pin.net_id]) outputsByNet[pin.net_id] = [];
                outputsByNet[pin.net_id].push(entry.instance_id);
            }
        }
    }

    // In a real device, the MCU mediates. For simulation, cycle through outputs
    // on each button press. If no direct net connection, just toggle all outputs.
    for (const [btnId, nets] of Object.entries(buttonNets)) {
        const targets = new Set();
        for (const net of nets) {
            for (const t of (outputsByNet[net] || [])) targets.add(t);
        }
        connections[btnId] = [...targets];
    }

    return connections;
}

function _onButtonPress(buttonId, netConnections, serialEl) {
    const targets = netConnections[buttonId] || Object.keys(simState);

    _logSerial(serialEl, `Button ${buttonId} pressed`);

    for (const target of targets) {
        simState[target] = !simState[target];
        const isOn = simState[target];

        // Update LED visual
        document.querySelectorAll(`.sim-led-bulb[data-id="${target}"]`).forEach(el => {
            el.classList.toggle('on', isOn);
            el.classList.toggle('off', !isOn);
        });

        // Update motor visual
        document.querySelectorAll(`.sim-motor-icon[data-id="${target}"]`).forEach(el => {
            el.classList.toggle('on', isOn);
            el.classList.toggle('off', !isOn);
        });

        _logSerial(serialEl, `  ${target} → ${isOn ? 'ON' : 'OFF'}`);
    }
}

function _logSerial(el, msg) {
    if (!el) return;
    serialLog.push(msg);
    if (serialLog.length > 100) serialLog.shift();
    el.textContent += msg + '\n';
    el.scrollTop = el.scrollHeight;
}

function _renderOutlineSVG(outline, components, pinMap) {
    if (!outline?.length) return '';

    // Calculate bounds
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const v of outline) {
        minX = Math.min(minX, v.x);
        minY = Math.min(minY, v.y);
        maxX = Math.max(maxX, v.x);
        maxY = Math.max(maxY, v.y);
    }
    const pad = 5;
    const w = maxX - minX + pad * 2;
    const h = maxY - minY + pad * 2;

    // Outline polygon
    const pts = outline.map(v => `${v.x - minX + pad},${v.y - minY + pad}`).join(' ');

    // Component markers
    const pinMapIds = new Set((pinMap || []).map(e => e.instance_id));
    let compSvg = '';
    for (const c of (components || [])) {
        const cx = c.x_mm - minX + pad;
        const cy = c.y_mm - minY + pad;
        const isActive = pinMapIds.has(c.instance_id);
        const fill = isActive ? 'var(--accent)' : 'var(--text-dim)';
        compSvg += `<circle cx="${cx}" cy="${cy}" r="2.5" fill="${fill}" opacity="0.6"/>`;
        compSvg += `<text x="${cx}" y="${cy - 4}" fill="${fill}" font-size="3" text-anchor="middle">${c.instance_id}</text>`;
    }

    return `
        <svg class="sim-outline" viewBox="0 0 ${w} ${h}" width="100%" preserveAspectRatio="xMidYMid meet">
            <polygon points="${pts}" fill="none" stroke="var(--border)" stroke-width="0.5"/>
            ${compSvg}
        </svg>`;
}
