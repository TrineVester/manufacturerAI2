/**
 * Viewport — step-dependent preview panel (right side of split layout).
 *
 * Framework only — no step-specific rendering logic lives here.
 * Each pipeline step registers a ViewportHandler via registerHandler().
 *
 * ── ViewportHandler interface ──────────────────────────────────
 *   label:       string                — title shown in the viewport header
 *   placeholder: string                — message when no data is loaded
 *   render:      (el: Element, data: any) => void  — draw step content
 *   clear:       (el: Element) => void             — reset to placeholder
 *
 *   Optional lifecycle hooks (primarily for 3D / WebGL renderers):
 *   mount:       (el: Element, data?: any) => void — called when step activates
 *   unmount:     (el: Element) => void             — called when step deactivates
 *   onResize:    (el: Element, w: number, h: number) => void
 * ───────────────────────────────────────────────────────────────
 */

const handlers   = new Map();
const cache      = new Map();      // step -> last data payload
const staleSteps = new Set();      // steps whose cached data is outdated
const viewState  = new Map();      // step -> { mode: '2d'|'3d', camera?, ... }
let   activeStep = null;

// ── Shared WebGL renderer (single instance avoids context exhaustion) ──────
let _sharedRenderer = null;   // populated lazily by viewport3d.js

/**
 * Get or store the single shared THREE.WebGLRenderer.
 * viewport3d.js calls setSharedRenderer() on first use; all subsequent
 * Three.js views reuse the same canvas / context.
 */
export function getSharedRenderer() { return _sharedRenderer; }
export function setSharedRenderer(r) { _sharedRenderer = r; }

/**
 * Per-step state bag (mode, saved camera position, etc.).
 * Initialises to { mode: '2d' } on first access.
 */
export function getViewState(step) {
    if (!viewState.has(step)) viewState.set(step, { mode: '2d' });
    return viewState.get(step);
}
export function patchViewState(step, patch) {
    const s = getViewState(step);
    Object.assign(s, patch);
}

// ── DOM refs (lazy) ───────────────────────────────────────────

const contentEl  = () => document.getElementById('viewport-content');
const viewportEl = () => document.getElementById('viewport');

// ── Internal helpers ──────────────────────────────────────────

function applyStaleClass() {
    const el = contentEl();
    if (!el) return;
    el.classList.toggle('viewport-stale', staleSteps.has(activeStep));
}

// ── Public API ────────────────────────────────────────────────

/**
 * Register a handler for a pipeline step.
 * @param {string} step   — matches data-step in nav ("design", "placement", …)
 * @param {ViewportHandler} handler
 */
export function registerHandler(step, handler) {
    handlers.set(step, handler);
}

/**
 * Switch the viewport to a new step.
 * If cached data exists for the step, it is re-rendered automatically.
 */
export function setStep(step) {
    // Unmount the leaving handler
    const prevHandler = handlers.get(activeStep);
    const el = contentEl();
    if (prevHandler && prevHandler.unmount && el) {
        try { prevHandler.unmount(el); } catch (e) { console.warn('viewport unmount:', e); }
    }

    activeStep = step;
    const handler = handlers.get(step);
    if (!el) return;

    if (!handler) {
        el.innerHTML = '<p class="viewport-empty">No preview available for this step</p>';
        applyStaleClass();
        return;
    }

    // Mount the arriving handler first (so it can set up the container)
    if (handler.mount) {
        try { handler.mount(el, cache.get(step)); } catch (e) { console.warn('viewport mount:', e); }
    }

    const data = cache.get(step);
    if (data !== undefined) {
        handler.render(el, data);
    } else {
        handler.clear(el);
    }
    applyStaleClass();
}

/**
 * Push new data for a step.
 * If the step is currently active the viewport re-renders immediately.
 */
export function setData(step, data) {
    cache.set(step, data);
    staleSteps.delete(step);   // fresh data clears stale flag
    if (step === activeStep) {
        const handler = handlers.get(step);
        if (handler) handler.render(contentEl(), data);
        applyStaleClass();
    }
}

/**
 * Clear cached data (and viewport) for a step (or all steps).
 */
export function clearData(step) {
    if (step) {
        cache.delete(step);
        staleSteps.delete(step);
        if (step === activeStep) {
            const handler = handlers.get(step);
            if (handler) handler.clear(contentEl());
            applyStaleClass();
        }
    } else {
        cache.clear();
        staleSteps.clear();
        const handler = handlers.get(activeStep);
        if (handler) handler.clear(contentEl());
        applyStaleClass();
    }
}

/**
 * Mark a step's viewport data as stale (or clear the stale flag).
 * If the step is currently active, applies/removes the visual stale style.
 */
export function setStale(step, isStale) {
    if (isStale) {
        staleSteps.add(step);
    } else {
        staleSteps.delete(step);
    }
    if (step === activeStep) applyStaleClass();
}

// ── Drag-resize ───────────────────────────────────────────────

function initResize() {
    const handle = document.getElementById('viewport-resize-handle');
    const vp = viewportEl();
    if (!handle || !vp) return;

    let startX, startW;

    handle.addEventListener('mousedown', (e) => {
        e.preventDefault();
        startX = e.clientX;
        startW = vp.offsetWidth;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });

    function onMove(e) {
        const delta = startX - e.clientX;  // dragging left = wider
        const newW = Math.max(200, Math.min(startW + delta, window.innerWidth * 0.6));
        vp.style.width = newW + 'px';
    }

    function onUp() {
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
    }
}

// Auto-init once DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

function init() {
    initResize();
    initResizeObserver();
}

function initResizeObserver() {
    const el = contentEl();
    if (!el || typeof ResizeObserver === 'undefined') return;
    new ResizeObserver(entries => {
        const entry = entries[0];
        if (!entry) return;
        const { width, height } = entry.contentRect;
        const handler = handlers.get(activeStep);
        if (handler && handler.onResize) {
            try { handler.onResize(el, width, height); }
            catch (e) { console.warn('viewport onResize:', e); }
        }
    }).observe(el);
}
