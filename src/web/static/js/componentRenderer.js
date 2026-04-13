/**
 * Shared component renderer — draws component bodies and pins
 * consistently across design, placement, and routing views.
 *
 * Uses actual body outlines (rect / circle) and pin positions
 * from the catalog so every pipeline stage looks identical.
 *
 * Public API:
 *   drawComponentIcon(svg, comp, ox, oy, scale, opts)
 *
 * comp shape:
 *   { x_mm, y_mm, rotation_deg, instance_id, catalog_id,
 *     body: { shape, width_mm, length_mm, diameter_mm },
 *     pins: [{ id, position_mm: [x,y] }, ...] }
 */

const NS = 'http://www.w3.org/2000/svg';

// ── Public API ────────────────────────────────────────────────

/**
 * Draw a component icon (body outline + pin dots + label) onto an SVG.
 *
 * @param {SVGElement}  svg    Target SVG element
 * @param {object}      comp   Component data (must include body + pins)
 * @param {number}      ox     X origin offset (px)
 * @param {number}      oy     Y origin offset (px)
 * @param {number}      scale  mm-to-px scale factor
 * @param {object}      opts   Optional overrides
 * @param {string}      opts.color       Stroke/label colour (default '#58a6ff')
 * @param {number}      opts.bodyOpacity Fill opacity of the body (default 0.15)
 * @param {boolean}     opts.showLabel   Show instance_id label (default true)
 * @param {boolean}     opts.showPins    Show pin dots (default true)
 * @param {string}      opts.pinColor    Pin dot fill colour (default same as color)
 * @param {number}      opts.pinRadius   Pin dot radius in px (default 2.5)
 */
export function drawComponentIcon(svg, comp, ox, oy, scale, opts = {}) {
    const {
        color = '#58a6ff',
        bodyOpacity = 0.15,
        showLabel = true,
        showPins = true,
        pinColor = null,
        pinRadius = 2.5,
    } = opts;

    const body = comp.body || {};
    const cx = ox + comp.x_mm * scale;
    const cy = oy + comp.y_mm * scale;
    const rot = comp.rotation_deg || 0;

    // ── 1. Body outline ───────────────────────────────────────
    const group = document.createElementNS(NS, 'g');
    group.classList.add('vp-comp-group');
    if (comp.instance_id) group.setAttribute('data-instance-id', comp.instance_id);

    if (body.shape === 'circle') {
        const r = ((body.diameter_mm || 5) / 2) * scale;
        const circle = document.createElementNS(NS, 'circle');
        circle.setAttribute('cx', cx);
        circle.setAttribute('cy', cy);
        circle.setAttribute('r', r);
        circle.setAttribute('fill', color);
        circle.setAttribute('fill-opacity', String(bodyOpacity));
        circle.setAttribute('stroke', color);
        circle.setAttribute('stroke-width', '1.5');
        circle.setAttribute('stroke-opacity', String(Math.min(1, bodyOpacity + 0.4)));
        group.appendChild(circle);

        // Flat edge indicator for LEDs / circular components (pin-1 side)
        if (comp.pins && comp.pins.length >= 2) {
            const flatR = r * 0.85;
            const flat = document.createElementNS(NS, 'line');
            // Flat edge on the cathode side (negative x in local space)
            const angle = rot * Math.PI / 180;
            const fx = cx + Math.cos(angle + Math.PI) * flatR;
            const fy = cy + Math.sin(angle + Math.PI) * flatR;
            const perpX = Math.sin(angle + Math.PI) * flatR * 0.5;
            const perpY = -Math.cos(angle + Math.PI) * flatR * 0.5;
            flat.setAttribute('x1', fx - perpX);
            flat.setAttribute('y1', fy - perpY);
            flat.setAttribute('x2', fx + perpX);
            flat.setAttribute('y2', fy + perpY);
            flat.setAttribute('stroke', color);
            flat.setAttribute('stroke-width', '1');
            flat.setAttribute('stroke-opacity', String(Math.min(1, bodyOpacity + 0.3)));
            group.appendChild(flat);
        }
    } else {
        // Rectangular body
        let bw = (body.width_mm || 4) * scale;
        let bh = (body.length_mm || 4) * scale;
        if (rot === 90 || rot === 270) [bw, bh] = [bh, bw];

        const rect = document.createElementNS(NS, 'rect');
        rect.setAttribute('x', cx - bw / 2);
        rect.setAttribute('y', cy - bh / 2);
        rect.setAttribute('width', bw);
        rect.setAttribute('height', bh);
        rect.setAttribute('rx', '2');
        rect.setAttribute('fill', color);
        rect.setAttribute('fill-opacity', String(bodyOpacity));
        rect.setAttribute('stroke', color);
        rect.setAttribute('stroke-width', '1.5');
        rect.setAttribute('stroke-opacity', String(Math.min(1, bodyOpacity + 0.4)));
        group.appendChild(rect);

        // Pin-1 notch for DIP/IC packages (many pins)
        if (comp.pins && comp.pins.length > 4) {
            const notchR = Math.min(bw, bh) * 0.08;
            const notch = document.createElementNS(NS, 'circle');
            // Notch at top-left (rotated)
            const angle = rot * Math.PI / 180;
            const localX = -(body.width_mm || 4) / 2 * scale + notchR * 2;
            const localY = -(body.length_mm || 4) / 2 * scale + notchR * 2;
            const cosA = Math.cos(angle), sinA = Math.sin(angle);
            const nx = cx + localX * cosA - localY * sinA;
            const ny = cy + localX * sinA + localY * cosA;
            notch.setAttribute('cx', nx);
            notch.setAttribute('cy', ny);
            notch.setAttribute('r', notchR);
            notch.setAttribute('fill', 'none');
            notch.setAttribute('stroke', color);
            notch.setAttribute('stroke-width', '1');
            notch.setAttribute('stroke-opacity', String(Math.min(1, bodyOpacity + 0.3)));
            group.appendChild(notch);
        }
    }

    svg.appendChild(group);

    // ── 2. Pins ───────────────────────────────────────────────
    if (showPins && comp.pins && comp.pins.length > 0) {
        const pColor = pinColor || color;
        const angle = rot * Math.PI / 180;
        const cosA = Math.cos(angle);
        const sinA = Math.sin(angle);

        for (const pin of comp.pins) {
            if (!pin.position_mm) continue;
            const [lpx, lpy] = pin.position_mm;
            // Rotate local pin position
            const wpx = cx + (lpx * cosA - lpy * sinA) * scale;
            const wpy = cy + (lpx * sinA + lpy * cosA) * scale;

            const dot = document.createElementNS(NS, 'circle');
            dot.setAttribute('cx', wpx);
            dot.setAttribute('cy', wpy);
            dot.setAttribute('r', String(pinRadius));
            dot.setAttribute('fill', pColor);
            dot.setAttribute('fill-opacity', String(Math.min(1, bodyOpacity + 0.5)));
            dot.setAttribute('stroke', pColor);
            dot.setAttribute('stroke-width', '0.8');
            dot.setAttribute('stroke-opacity', String(Math.min(1, bodyOpacity + 0.6)));
            group.appendChild(dot);

            // Pin ID label (only when zoomed enough / few pins)
            if (comp.pins.length <= 6 && scale >= 3) {
                const plabel = document.createElementNS(NS, 'text');
                plabel.setAttribute('x', wpx);
                plabel.setAttribute('y', wpy - pinRadius - 2);
                plabel.setAttribute('text-anchor', 'middle');
                plabel.setAttribute('font-size', '7');
                plabel.setAttribute('fill', pColor);
                plabel.setAttribute('opacity', String(Math.min(1, bodyOpacity + 0.3)));
                plabel.textContent = pin.id;
                group.appendChild(plabel);
            }
        }
    }

    // ── 3. Label ──────────────────────────────────────────────
    if (showLabel) {
        const labelOffset = _labelOffset(body, rot, scale);
        const label = document.createElementNS(NS, 'text');
        label.setAttribute('x', cx);
        label.setAttribute('y', cy - labelOffset - 4);
        label.setAttribute('text-anchor', 'middle');
        label.setAttribute('class', 'vp-placed-label');
        label.setAttribute('fill', color);
        label.setAttribute('opacity', String(Math.min(1, bodyOpacity + 0.5)));
        label.textContent = comp.instance_id;
        svg.appendChild(label);
    }
}


// ── Helpers ───────────────────────────────────────────────────

function _labelOffset(body, rot, scale) {
    if (body.shape === 'circle') {
        return ((body.diameter_mm || 5) / 2) * scale;
    }
    const h = rot === 90 || rot === 270
        ? (body.width_mm || 4) / 2
        : (body.length_mm || 4) / 2;
    return h * scale;
}
