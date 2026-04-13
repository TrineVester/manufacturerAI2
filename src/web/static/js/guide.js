/* Step-by-step assembly guide — adapted from old web UI */

import { API, state } from './state.js';

// ── Module state ──────────────────────────────────────────────────

let guideSteps = [];
let guideSections = [];
let guideIndex = 0;
let guideComponents = [];  // enriched component list
let guideOutline = [];     // outline polygon for placement SVG

// ── Public API ────────────────────────────────────────────────────

/**
 * Enable / disable the Guide nav-bar button.
 * Enable once placement data exists for the session.
 */
export function enableGuideBtn(enabled = true) {
    const btn = document.getElementById('btn-guide');
    if (btn) btn.disabled = !enabled;
}

/**
 * Open the guide screen. Generates assembly guide on server, then renders.
 */
export async function openGuide() {
    const screen = document.getElementById('guide-screen');
    if (!screen) return;

    if (!state.session) return;

    // Generate assembly guide (server-side) — tolerates missing routing
    let guide;
    try {
        const genRes = await fetch(
            `${API}/api/session/assembly?session=${encodeURIComponent(state.session)}`,
            { method: 'POST' },
        );
        if (genRes.ok) {
            guide = await genRes.json();
        } else {
            // fallback: try to load existing
            const getRes = await fetch(
                `${API}/api/session/assembly/result?session=${encodeURIComponent(state.session)}`,
            );
            if (getRes.ok) guide = await getRes.json();
        }
    } catch { /* continue without server guide */ }

    // Always fetch placement data for SVG rendering
    let placementData;
    try {
        const res = await fetch(
            `${API}/api/session/placement/result?session=${encodeURIComponent(state.session)}`,
        );
        if (!res.ok) {
            _showEmpty('Run the placer first to generate a guide.');
            screen.hidden = false;
            return;
        }
        placementData = await res.json();
    } catch {
        _showEmpty('Could not load placement data.');
        screen.hidden = false;
        return;
    }

    // Build steps from server guide (preferred) or client-side fallback
    if (guide && guide.steps) {
        _buildStepsFromGuide(guide, placementData);
    } else {
        _buildSteps(placementData);
    }
    guideIndex = 0;
    _renderSectionNav();
    _renderGuideStep();
    screen.hidden = false;
}

/**
 * Close the guide screen.
 */
export function closeGuide() {
    const screen = document.getElementById('guide-screen');
    if (screen) screen.hidden = true;
}

// ── Step builder ──────────────────────────────────────────────────

function _buildSteps(data) {
    const components = data.components || [];
    const outline = data.outline || [];

    guideComponents = components;
    guideOutline = outline;
    guideSteps = [];
    guideSections = [];

    // Group components by catalog_id prefix (e.g. "led_5mm" → LED,
    // "resistor_*" → Resistor, "atmega*" → Microcontroller, etc.)
    const grouped = {};
    for (const comp of components) {
        const ctype = _componentType(comp);
        if (!grouped[ctype]) grouped[ctype] = [];
        grouped[ctype].push(comp);
    }

    // ── Introduction step ────────────────────────────────────────
    guideSections.push({ name: 'Introduction', index: guideSteps.length });
    guideSteps.push({
        title: 'Component Assembly Guide',
        subtitle: 'Introduction',
        body:
            `This guide walks you through inserting each electronic\n` +
            `component into the 3D-printed enclosure.\n\n` +
            `The placer has positioned <strong>${components.length} component${components.length !== 1 ? 's' : ''}</strong> ` +
            `inside the device outline.\n\n` +
            `Use the <em>Manual</em> button or the arrow navigation to move between steps.`,
        showPlacementView: false,
        section: 'Introduction',
    });

    // ── Component checklist ──────────────────────────────────────
    let listHTML = '';
    for (const [ctype, comps] of Object.entries(grouped)) {
        const label = _typeLabel(ctype);
        const count = comps.length;
        const countTag = count > 1 ? `<span class="component-count">× ${count}</span>` : '';
        listHTML += `<li><span class="component-name">${label}</span>${countTag}</li>`;
    }

    guideSections.push({ name: 'Checklist', index: guideSteps.length });
    guideSteps.push({
        title: 'Component Checklist',
        subtitle: 'Materials Needed',
        body:
            `Gather these components before starting:\n\n` +
            `<ul class="component-list">${listHTML}</ul>\n\n` +
            `Make sure you have everything ready before beginning assembly.`,
        showPlacementView: false,
        section: 'Checklist',
    });

    // ── Per-type placement steps ─────────────────────────────────
    for (const [ctype, comps] of Object.entries(grouped)) {
        const label = _typeLabel(ctype);
        const indices = comps.map(c => components.indexOf(c));
        const sectionName = _sectionName(ctype);

        guideSections.push({ name: sectionName, index: guideSteps.length });

        const count = comps.length;
        const plural = count > 1;

        guideSteps.push({
            title: `${label} Placement`,
            subtitle: plural ? `${count} to insert` : '1 to insert',
            body: _placementBody(ctype, comps),
            componentIndices: indices,
            showPlacementView: true,
            section: sectionName,
        });
    }

    // ── Final step ───────────────────────────────────────────────
    guideSections.push({ name: 'Finish', index: guideSteps.length });
    guideSteps.push({
        title: 'Assembly Complete',
        subtitle: 'Final Check',
        body:
            `All components have been placed.\n\n` +
            `<strong>Checklist before continuing:</strong>\n` +
            `• All components are seated flush in their pockets\n` +
            `• Pin 1 / polarity markers are correctly oriented\n` +
            `• All leads are fully inserted into their holes\n\n` +
            `You can now proceed to the next pipeline stage.`,
        showPlacementView: false,
        section: 'Finish',
    });
}

/**
 * Build guide steps from server-generated assembly data.
 * Uses structured instructions, wiring info, and warnings from the backend.
 */
function _buildStepsFromGuide(guide, placementData) {
    const components = placementData.components || [];
    guideComponents = components;
    guideOutline = placementData.outline || [];
    guideSteps = [];
    guideSections = [];

    // ── Introduction step ────────────────────────────────────────
    guideSections.push({ name: 'Introduction', index: 0 });
    guideSteps.push({
        title: 'Component Assembly Guide',
        subtitle: 'Introduction',
        body:
            `This guide walks you through inserting each electronic component ` +
            `into the 3D-printed enclosure.\n\n` +
            `<strong>${guide.total_components}</strong> component${guide.total_components !== 1 ? 's' : ''} to insert` +
            (guide.total_connections ? `, <strong>${guide.total_connections}</strong> traced connection${guide.total_connections !== 1 ? 's' : ''}` : '') +
            `.\n\nUse the arrow navigation or the Manual sidebar to move between steps.`,
        showPlacementView: false,
        section: 'Introduction',
    });

    // ── Checklist step ───────────────────────────────────────────
    let listHTML = '';
    for (const item of guide.checklist || []) {
        const countTag = item.count > 1 ? `<span class="component-count">× ${item.count}</span>` : '';
        listHTML += `<li><span class="component-name">${item.label}</span>${countTag}</li>`;
    }

    guideSections.push({ name: 'Checklist', index: guideSteps.length });
    guideSteps.push({
        title: 'Component Checklist',
        subtitle: 'Materials Needed',
        body:
            `Gather these components before starting:\n\n` +
            `<ul class="component-list">${listHTML}</ul>\n\n` +
            `Make sure you have everything ready before beginning assembly.`,
        showPlacementView: false,
        section: 'Checklist',
    });

    // ── Per-step from server ─────────────────────────────────────
    for (const step of guide.steps || []) {
        const sectionName = _sectionName(step.component_type);
        guideSections.push({ name: sectionName, index: guideSteps.length });

        // Find component indices in the placement array for SVG highlighting
        const instanceIds = new Set((step.instances || []).map(i => i.instance_id));
        const indices = [];
        components.forEach((c, i) => { if (instanceIds.has(c.instance_id)) indices.push(i); });

        // Build body HTML from server instructions + warnings + wiring
        let bodyParts = [];

        if (step.instructions?.length) {
            bodyParts.push(`<strong>Instructions:</strong>`);
            for (const instr of step.instructions) {
                bodyParts.push(instr.startsWith('  →') ? instr : `• ${instr}`);
            }
        }

        if (step.warnings?.length) {
            bodyParts.push('');
            bodyParts.push(`<strong>⚠ Warnings:</strong>`);
            for (const w of step.warnings) bodyParts.push(`• ${w}`);
        }

        if (step.wiring?.length) {
            bodyParts.push('');
            bodyParts.push(`<strong>Wiring Connections:</strong>`);
            for (const w of step.wiring) {
                const len = w.trace_length_mm ? ` (${w.trace_length_mm} mm)` : '';
                bodyParts.push(`• ${w.net_id}: ${w.from_instance}:${w.from_pin} → ${w.to_instance}:${w.to_pin}${len}`);
            }
        }

        const count = step.instances?.length || 0;
        guideSteps.push({
            title: step.title,
            subtitle: count > 1 ? `${count} to insert` : '1 to insert',
            body: bodyParts.join('\n'),
            componentIndices: indices,
            showPlacementView: true,
            section: sectionName,
        });
    }

    // ── Final checks step ────────────────────────────────────────
    guideSections.push({ name: 'Finish', index: guideSteps.length });
    let finalBody = `All components have been placed.\n\n<strong>Final Checks:</strong>\n`;
    for (const check of guide.final_checks || []) {
        finalBody += `• ${check}\n`;
    }
    finalBody += `\nYou can now proceed to the next pipeline stage.`;

    guideSteps.push({
        title: 'Assembly Complete',
        subtitle: 'Final Check',
        body: finalBody,
        showPlacementView: false,
        section: 'Finish',
    });
}

// ── Component type helpers ────────────────────────────────────────

function _componentType(comp) {
    const id = (comp.catalog_id || comp.instance_id || '').toLowerCase();
    if (id.includes('atmega') || id.includes('controller') || id.includes('mcu')) return 'controller';
    if (id.includes('button') || id.includes('tactile') || id.includes('switch')) return 'button';
    if (id.includes('battery')) return 'battery';
    if (id.includes('led')) return 'led';
    if (id.includes('resistor')) return 'resistor';
    if (id.includes('capacitor') || id.includes('cap_')) return 'capacitor';
    if (id.includes('transistor') || id.includes('npn') || id.includes('pnp')) return 'transistor';
    return 'component';
}

function _typeLabel(ctype) {
    const labels = {
        controller: 'Microcontroller (ATmega328P)',
        button: 'Tactile Push Button',
        battery: 'Battery Holder',
        led: 'LED',
        resistor: 'Resistor',
        capacitor: 'Capacitor',
        transistor: 'Transistor',
        component: 'Component',
    };
    return labels[ctype] || ctype;
}

function _sectionName(ctype) {
    const names = {
        controller: 'Microcontroller',
        button: 'Buttons',
        battery: 'Battery',
        led: 'LEDs',
        resistor: 'Resistors',
        capacitor: 'Capacitors',
        transistor: 'Transistors',
        component: 'Components',
    };
    return names[ctype] || ctype;
}

function _placementBody(ctype, comps) {
    const label = _typeLabel(ctype);
    const count = comps.length;

    const positionList = comps.map(c =>
        `• <strong>${c.instance_id}</strong> at (${c.x_mm.toFixed(1)}, ${c.y_mm.toFixed(1)}) mm, ${c.rotation_deg}°`
    ).join('\n');

    const instructions = {
        controller:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Locate the rectangular DIP pocket with pin holes\n` +
            `• Find the pin 1 marker on the chip (notch or dot)\n` +
            `• Carefully align ALL pins with the holes\n` +
            `• Press gently and evenly — do not force!\n\n` +
            `<strong>⚠ Important:</strong> Incorrect orientation will damage the chip.\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,

        button:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Locate the square pocket on the enclosure\n` +
            `• Orient the button so the pins align with the holes\n` +
            `• Press firmly until the button sits flush\n` +
            `• The button cap should protrude through the top hole\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,

        battery:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Locate the rectangular battery pocket\n` +
            `• Insert the holder with contacts facing the correct direction\n` +
            `• Press down until fully seated\n\n` +
            `<strong>Note:</strong> Batteries are inserted after printing is complete.\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,

        led:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Locate the round pocket\n` +
            `• Longer leg (anode, +) → marked hole\n` +
            `• Shorter leg (cathode, −) → other hole\n` +
            `• LED should point outward through the wall slot\n\n` +
            `<strong>⚠ Important:</strong> Wrong polarity = LED won't work!\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,

        resistor:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Resistors are not polarized — either direction works\n` +
            `• Bend leads at 90° to match hole spacing\n` +
            `• Insert and push flush\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,

        capacitor:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Ceramic capacitors are not polarized\n` +
            `• Insert leads into holes and seat flush\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,

        transistor:
            `<strong>Component:</strong> ${label}\n\n` +
            `<strong>How to insert:</strong>\n` +
            `• Match the flat side of the TO-92 package to the pocket shape\n` +
            `• Ensure E/B/C pins go into the correct holes\n` +
            `• Push gently until seated\n\n` +
            `<strong>Positions:</strong>\n${positionList}`,
    };

    return instructions[ctype] ||
        `<strong>Component:</strong> ${label}\n\n` +
        `Insert ${count} component${count > 1 ? 's' : ''} at the positions shown in the placement view.\n\n` +
        `<strong>Positions:</strong>\n${positionList}`;
}

// ── Section navigation sidebar ────────────────────────────────────

function _renderSectionNav() {
    const sidebarContent = document.getElementById('manualSidebarContent');
    if (!sidebarContent || guideSections.length <= 1) return;

    sidebarContent.innerHTML = '';
    for (const section of guideSections) {
        const btn = document.createElement('button');
        btn.className = 'manual-nav-item';
        btn.innerHTML = `
            <span class="manual-nav-icon">${_getSectionIcon(section.name)}</span>
            <span class="manual-nav-label">${section.name}</span>
        `;
        btn.dataset.index = section.index;
        btn.addEventListener('click', () => {
            guideIndex = section.index;
            _renderGuideStep();
            _updateSectionNavActive();
            // Close sidebar on narrow screens
            if (window.innerWidth < 1024) {
                _closeSidebar();
            }
        });
        sidebarContent.appendChild(btn);
    }
    _updateSectionNavActive();
}

function _getSectionIcon(name) {
    const icons = {
        Introduction: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
        Checklist: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>`,
        Buttons: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/><circle cx="12" cy="12" r="3"/></svg>`,
        Microcontroller: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="1"/><line x1="9" y1="4" x2="9" y2="1"/><line x1="15" y1="4" x2="15" y2="1"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/></svg>`,
        Battery: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="6" width="18" height="12" rx="2"/><line x1="23" y1="10" x2="23" y2="14"/></svg>`,
        LEDs: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="7"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m19.07 4.93-1.41 1.41"/><path d="m6.34 17.66-1.41 1.41"/></svg>`,
        Resistors: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12h4l2-4 2 8 2-8 2 8 2-4h4"/></svg>`,
        Capacitors: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="2" x2="12" y2="8"/><line x1="12" y1="16" x2="12" y2="22"/><line x1="6" y1="8" x2="18" y2="8"/><line x1="6" y1="16" x2="18" y2="16"/></svg>`,
        Transistors: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="6"/><line x1="8" y1="12" x2="16" y2="18"/><line x1="8" y1="6" x2="8" y2="18"/></svg>`,
        Finish: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
    };
    return icons[name] || `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>`;
}

function _updateSectionNavActive() {
    const btns = document.querySelectorAll('.manual-nav-item');
    let currentSectionIdx = 0;
    for (let i = guideSections.length - 1; i >= 0; i--) {
        if (guideIndex >= guideSections[i].index) {
            currentSectionIdx = i;
            break;
        }
    }
    btns.forEach((btn, i) => btn.classList.toggle('active', i === currentSectionIdx));
}

// ── Step renderer ─────────────────────────────────────────────────

function _renderGuideStep() {
    const guideContent = document.getElementById('guideContent');
    const placementView = document.getElementById('guidePlacementView');
    const placementSvg = document.getElementById('guidePlacementSvg');

    if (!guideSteps.length || !guideContent) return;
    const step = guideSteps[guideIndex];
    const total = guideSteps.length;

    const subtitleHTML = step.subtitle ? `<div class="guide-subtitle">${step.subtitle}</div>` : '';
    const bodyContainsHTML = step.body.includes('<ul') || step.body.includes('<strong>');
    const bodyStyle = bodyContainsHTML ? '' : 'style="white-space:pre-wrap;"';

    guideContent.innerHTML = `
        <div class="guide-header-section">
            <h2>${step.title}</h2>
            ${subtitleHTML}
        </div>
        <div class="guide-body-section">
            <div class="guide-body-text" ${bodyStyle}>${step.body}</div>
            <div class="guide-step-counter">Step ${guideIndex + 1} of ${total}</div>
        </div>
    `;

    // Placement view
    if (step.showPlacementView && placementView && placementSvg) {
        placementView.style.display = 'flex';
        const indices = step.componentIndices || [];
        _renderPlacementView(indices, placementSvg);
    } else if (placementView) {
        placementView.style.display = 'none';
    }

    _updateSectionNavActive();
}

// ── Placement SVG ─────────────────────────────────────────────────

function _renderPlacementView(highlightIndices, svg) {
    const components = guideComponents;
    const outline = guideOutline;
    const highlightSet = new Set(highlightIndices);

    if (!components.length) {
        svg.innerHTML = '';
        return;
    }

    // Compute bounding box
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

    for (const pt of outline) {
        minX = Math.min(minX, pt.x);
        minY = Math.min(minY, pt.y);
        maxX = Math.max(maxX, pt.x);
        maxY = Math.max(maxY, pt.y);
    }

    for (const comp of components) {
        const hw = ((comp.body?.width_mm || 10) / 2) + 2;
        const hh = ((comp.body?.length_mm || 10) / 2) + 2;
        minX = Math.min(minX, comp.x_mm - hw);
        minY = Math.min(minY, comp.y_mm - hh);
        maxX = Math.max(maxX, comp.x_mm + hw);
        maxY = Math.max(maxY, comp.y_mm + hh);
    }

    const PAD = 8;
    minX -= PAD; minY -= PAD; maxX += PAD; maxY += PAD;
    const w = maxX - minX;
    const h = maxY - minY;

    svg.setAttribute('viewBox', `${minX} ${minY} ${w} ${h}`);

    let s = '';

    // Outline
    if (outline.length > 1) {
        const pts = outline.map(p => `${p.x},${p.y}`).join(' ');
        s += `<polygon points="${pts}" fill="#161b22" stroke="#30363d" stroke-width="0.8"/>`;
    }

    // Draw each component
    for (let i = 0; i < components.length; i++) {
        const comp = components[i];
        const cx = comp.x_mm;
        const cy = comp.y_mm;
        const isCur = highlightSet.has(i);
        const body = comp.body || {};
        const shape = body.shape || 'rect';
        const bw = body.width_mm || 10;
        const bh = body.length_mm || 10;
        const rd = body.diameter_mm || bw;
        const rot = comp.rotation_deg || 0;

        const fill = isCur ? 'rgba(88,166,255,0.25)' : 'rgba(110,118,129,0.15)';
        const stroke = isCur ? '#58a6ff' : '#484f58';
        const sw = isCur ? '1.2' : '0.6';

        s += `<g class="guide-comp" data-idx="${i}" style="cursor:pointer;">`;

        if (shape === 'circle') {
            const r = rd / 2;
            s += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>`;
        } else {
            const rx = bw / 2;
            const ry = bh / 2;
            if (rot !== 0) {
                s += `<rect x="${cx - rx}" y="${cy - ry}" width="${bw}" height="${bh}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" rx="0.6" transform="rotate(${rot} ${cx} ${cy})"/>`;
            } else {
                s += `<rect x="${cx - rx}" y="${cy - ry}" width="${bw}" height="${bh}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" rx="0.6"/>`;
            }
        }

        // Pulsing highlight for current component
        if (isCur) {
            const hr = Math.max(bw, bh) / 2 + 3;
            s += `<rect x="${cx - hr}" y="${cy - hr}" width="${hr * 2}" height="${hr * 2}" fill="none" stroke="#58a6ff" stroke-width="0.8" rx="2" stroke-dasharray="3,2" opacity="0.7"><animate attributeName="stroke-dashoffset" from="0" to="10" dur="1s" repeatCount="indefinite"/></rect>`;
        }

        // Label
        const fontSize = Math.max(2, Math.min(4, Math.min(bw, bh) * 0.35));
        s += `<text x="${cx}" y="${cy + fontSize * 0.35}" text-anchor="middle" font-size="${fontSize}" fill="${isCur ? '#58a6ff' : '#8b949e'}" font-family="sans-serif">${_esc(comp.instance_id)}</text>`;

        s += `</g>`;
    }

    svg.innerHTML = s;

    // Click handlers: navigate to that component's step
    svg.querySelectorAll('.guide-comp').forEach(g => {
        g.addEventListener('click', e => {
            e.stopPropagation();
            const idx = parseInt(g.dataset.idx, 10);
            _navigateToComponentStep(idx);
        });
    });
}

function _navigateToComponentStep(componentIdx) {
    for (let i = 0; i < guideSteps.length; i++) {
        const indices = guideSteps[i].componentIndices || [];
        if (indices.includes(componentIdx)) {
            guideIndex = i;
            _renderGuideStep();
            return;
        }
    }
}

// ── Sidebar toggle ────────────────────────────────────────────────

function _closeSidebar() {
    document.getElementById('manualSidebar')?.classList.remove('open');
    document.getElementById('manualSidebarOverlay')?.classList.remove('visible');
    document.getElementById('manualSidebarToggle')?.classList.remove('active');
}

export function toggleManualSidebar() {
    const sidebar = document.getElementById('manualSidebar');
    const overlay = document.getElementById('manualSidebarOverlay');
    const toggle = document.getElementById('manualSidebarToggle');
    if (!sidebar) return;

    const isOpen = sidebar.classList.contains('open');
    if (isOpen) {
        _closeSidebar();
    } else {
        sidebar.classList.add('open');
        overlay?.classList.add('visible');
        toggle?.classList.add('active');
    }
}

// ── Init (wire static controls) ───────────────────────────────────

export function initGuide() {
    // Manual sidebar toggle
    document.getElementById('manualSidebarToggle')?.addEventListener('click', e => {
        e.stopPropagation();
        toggleManualSidebar();
    });
    document.getElementById('manualSidebarOverlay')?.addEventListener('click', () => {
        _closeSidebar();
    });

    // Back button
    document.getElementById('guideBackBtn')?.addEventListener('click', closeGuide);

    // Question window toggle
    const toggleQBtn = document.getElementById('toggleQuestionWindowBtn');
    const questionSidebar = document.getElementById('questionSidebar');
    toggleQBtn?.addEventListener('click', () => {
        const isVisible = questionSidebar.style.display !== 'none';
        questionSidebar.style.display = isVisible ? 'none' : 'flex';
        toggleQBtn.textContent = isVisible ? 'Question window: Off' : 'Question window: On';
        toggleQBtn.classList.toggle('active', !isVisible);
    });

    // Prev / Next
    document.getElementById('guidePrevBtn')?.addEventListener('click', () => {
        if (guideIndex > 0) { guideIndex--; _renderGuideStep(); }
    });
    document.getElementById('guideNextBtn')?.addEventListener('click', () => {
        if (guideIndex < guideSteps.length - 1) { guideIndex++; _renderGuideStep(); }
    });

    // Ask button (placeholder)
    document.getElementById('guideAskBtn')?.addEventListener('click', () => {
        const input = document.getElementById('guidePromptInput');
        const response = document.getElementById('guideResponse');
        const q = input?.value.trim();
        if (!q) return;
        if (response) response.textContent = 'Response will appear here…';
        if (input) input.value = '';
    });
}

// ── Helpers ───────────────────────────────────────────────────────

function _showEmpty(msg) {
    const content = document.getElementById('guideContent');
    if (content) {
        content.innerHTML = `<h2>Guide</h2><p>${msg}</p>`;
    }
}

function _esc(text) {
    const el = document.createElement('span');
    el.textContent = text ?? '';
    return el.innerHTML;
}
