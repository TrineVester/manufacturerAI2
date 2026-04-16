/**
 * viewportScad.js — 3D STL preview for the SCAD pipeline step.
 *
 * Renders enclosure.stl using Three.js STLLoader + OrbitControls.
 * Shows a loading/spinner overlay while STL is compiling.
 *
 * Data shape (from scad.js via setData('scad', ...)):
 * {
 *   stlStatus:  'compiling' | 'done' | 'error' | 'pending'
 *   stlUrl:     string    — fetch URL for the binary STL
 *   stlBytes:   number    — file size
 *   scadLines:  number
 *   scadBytes:  number
 * }
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { registerHandler, getData } from './viewport.js';
import { getSharedRenderer, setSharedRenderer } from './viewport.js';
import { create3DScene } from './viewport3d.js';

// ── Renderer singleton (for the final STL scene) ─────────────────

function getOrCreateRenderer() {
    let r = getSharedRenderer();
    if (!r) {
        r = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        r.setClearColor(0x000000, 0);
        r.setPixelRatio(window.devicePixelRatio);
        setSharedRenderer(r);
    }
    return r;
}

// ── State ─────────────────────────────────────────────────────────

let _scene     = null;   // active ScadScene (STL viewer) | null
let _preview3d = null;   // private-renderer preview scene while compiling
let _previewBanner = null; // banner element inside the preview host
let _retryTimer = null;  // preview retry interval
let _lastEl   = null;
let _lastData  = null;

// ── createScadScene — shared renderer, shows the compiled STL ────

function createScadScene(container) {
    const renderer = getOrCreateRenderer();
    const canvas   = renderer.domElement;

    container.style.position = 'relative';
    canvas.style.display = 'block';
    container.appendChild(canvas);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0d1117);
    scene.fog = new THREE.FogExp2(0x0d1117, 0.0015);

    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000);
    camera.position.set(80, 120, 180);
    camera.lookAt(0, 0, 0);

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.update();

    scene.add(new THREE.AmbientLight(0xffffff, 0.65));
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(100, 250, 150);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xaac8ff, 0.50);
    fill.position.set(-100, 60, -150);
    scene.add(fill);
    const back = new THREE.DirectionalLight(0xffeedd, 0.25);
    back.position.set(0, -80, -200);
    scene.add(back);

    const grid = new THREE.GridHelper(600, 40, 0x1a2a3a, 0x151f2a);
    grid.position.y = -0.5;
    scene.add(grid);

    let meshGroup = null;
    let extrasGroup = null;
    let animId = null;
    // Shared translation offset so extras align with the enclosure
    let _enclosureCentre = new THREE.Vector3();
    let _enclosureMinY = 0;

    function animate() {
        animId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    function resize(w, h) {
        if (!w || !h) return;
        renderer.setSize(w, h);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.render(scene, camera);
    }

    const ro = new ResizeObserver(entries => {
        const entry = entries[0];
        if (!entry) return;
        const { width, height } = entry.contentRect;
        resize(width, height);
    });
    ro.observe(container);

    function loadStl(url) {
        // Remove any previous mesh
        if (meshGroup) { scene.remove(meshGroup); meshGroup = null; }

        const loader = new STLLoader();
        loader.load(
            url,
            (geometry) => {
                geometry.computeVertexNormals();

                // OpenSCAD uses Z-up; Three.js uses Y-up — rotate to lay flat
                geometry.rotateX(-Math.PI / 2);

                geometry.computeBoundingBox();

                // Centre + position the mesh just above the grid
                const box = geometry.boundingBox;
                const centre = new THREE.Vector3();
                box.getCenter(centre);
                _enclosureCentre.copy(centre);
                _enclosureMinY = box.min.y;
                geometry.translate(-centre.x, -box.min.y, -centre.z);

                const mat = new THREE.MeshPhongMaterial({
                    color: 0x4a8abf,
                    shininess: 40,
                    specular: 0x224466,
                    side: THREE.DoubleSide,
                });
                const mesh = new THREE.Mesh(geometry, mat);

                meshGroup = new THREE.Group();
                meshGroup.add(mesh);
                scene.add(meshGroup);

                // Fit camera to model
                const size = new THREE.Vector3();
                box.getSize(size);
                const maxDim = Math.max(size.x, size.y, size.z);
                const dist = maxDim * 1.8;
                camera.position.set(dist * 0.6, dist * 0.7, dist * 0.8);
                camera.lookAt(0, size.y / 2, 0);
                controls.target.set(0, size.y / 2, 0);
                controls.update();

                // Reposition grid
                grid.position.y = -0.5;
            },
            undefined,
            (err) => console.error('STLLoader error:', err),
        );
    }

    function loadExtras(url) {
        if (extrasGroup) { scene.remove(extrasGroup); extrasGroup = null; }
        const loader = new STLLoader();
        loader.load(
            url,
            (geometry) => {
                geometry.computeVertexNormals();
                geometry.rotateX(-Math.PI / 2);
                // Apply the same translation as the enclosure so they align
                geometry.translate(-_enclosureCentre.x, -_enclosureMinY, -_enclosureCentre.z);
                const mat = new THREE.MeshPhongMaterial({
                    color: 0xd29922,
                    shininess: 50,
                    specular: 0x664422,
                    side: THREE.DoubleSide,
                });
                const mesh = new THREE.Mesh(geometry, mat);
                extrasGroup = new THREE.Group();
                extrasGroup.add(mesh);
                scene.add(extrasGroup);
            },
            undefined,
            (err) => console.error('STLLoader extras error:', err),
        );
    }

    function setExtrasVisible(visible) {
        if (extrasGroup) extrasGroup.visible = visible;
    }

    function hasExtras() { return !!extrasGroup; }

    return {
        loadStl,
        loadExtras,
        setExtrasVisible,
        hasExtras,
        resize,
        destroy() {
            cancelAnimationFrame(animId);
            ro.disconnect();
            if (meshGroup) { scene.remove(meshGroup); meshGroup = null; }
            if (extrasGroup) { scene.remove(extrasGroup); extrasGroup = null; }
            if (canvas.parentElement === container) container.removeChild(canvas);
        },
    };
}

// ── Utilities ─────────────────────────────────────────────────────

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Render helpers ────────────────────────────────────────────────

function renderView(el, data) {
    _lastEl   = el;
    _lastData = data;

    const { stlStatus, stlUrl } = data || {};

    if (!stlStatus || stlStatus === 'pending') {
        // Show the routing/placement preview so the user can see the enclosure shell
        // even before clicking "Compile STL".
        const previewData = getData('routing') || getData('placement');
        if (previewData) {
            if (!_preview3d) {
                _teardown(el);
                el.innerHTML = '';
                const host = document.createElement('div');
                host.style.cssText = 'width:100%;height:100%;min-height:480px;position:relative;';
                el.appendChild(host);

                // Info banner
                _previewBanner = document.createElement('div');
                _previewBanner.style.cssText = [
                    'position:absolute; top:10px; left:50%; transform:translateX(-50%);',
                    'background:rgba(13,17,23,0.82); border:1px solid var(--border,#2e3d4f);',
                    'border-radius:8px; padding:7px 16px; display:flex; align-items:center;',
                    'gap:10px; z-index:10; pointer-events:none; white-space:nowrap;',
                ].join('');
                host.appendChild(_previewBanner);
                _preview3d = create3DScene(host);
            }
            if (_previewBanner) {
                _previewBanner.innerHTML = '<span style="font-size:12px;color:var(--text-muted);">📦 SCAD generated — click <strong style="color:var(--text);">Compile STL</strong> to build the 3D model</span>';
            }
            _preview3d.update(previewData);
        } else {
            _teardown();
            el.innerHTML = `
                <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:12px;color:var(--text-muted);">
                    <span style="font-size:36px;">📦</span>
                    <p style="margin:0">SCAD generated — click <strong>Compile STL</strong> to build the 3D model</p>
                </div>`;
        }
        return;
    }

    if (stlStatus === 'compiling') {
        // Show the existing routing/placement 3D scene as a preview with an overlay banner.
        // getData may return undefined if routing data hasn't loaded yet (race on session restore).
        const previewData = getData('routing') || getData('placement');
        if (previewData) {
            // Only rebuild if we don't already have a live preview scene
            if (!_preview3d) {
                _teardown(el);   // destroy any prior STL scene
                el.innerHTML = '';
                const host = document.createElement('div');
                host.style.cssText = 'width:100%;height:100%;min-height:480px;position:relative;';
                el.appendChild(host);

                // Overlay banner
                _previewBanner = document.createElement('div');
                _previewBanner.style.cssText = [
                    'position:absolute; top:10px; left:50%; transform:translateX(-50%);',
                    'background:rgba(13,17,23,0.82); border:1px solid var(--border,#2e3d4f);',
                    'border-radius:8px; padding:7px 16px; display:flex; align-items:center;',
                    'gap:10px; z-index:10; pointer-events:none; white-space:nowrap;',
                ].join('');
                host.appendChild(_previewBanner);
                _preview3d = create3DScene(host);
            }
            if (_previewBanner) {
                _previewBanner.innerHTML = [
                    '<div class="vp-spinner" style="width:18px;height:18px;border-width:2px;"></div>',
                    '<span style="font-size:12px;color:var(--text-muted);">Compiling STL\u2026 this is a preview</span>',
                ].join('');
            }
            _preview3d.update(previewData);
        } else {
            // Routing data not cached yet (session restore race) — show spinner
            // and schedule a retry after routing data has had time to load.
            _teardown(el);
            el.innerHTML = `
                <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:12px;color:var(--text-muted);">
                    <div class="vp-spinner"></div>
                    <p style="margin:0">Compiling STL\u2026 this can take a minute</p>
                </div>`;
            // Retry once routing data arrives (≤1 s delay on session restore)
            setTimeout(() => {
                if (_lastData?.stlStatus === 'compiling' && _lastEl) {
                    renderView(_lastEl, _lastData);
                }
            }, 1200);
        }
        return;
    }

    if (stlStatus === 'error') {
        _teardown();
        const errDetail = data.message
            ? `<pre style="margin:8px 0 0;font-size:11px;color:var(--text-muted);max-width:540px;white-space:pre-wrap;text-align:left;">${escapeHtml(data.message)}</pre>`
            : '';
        el.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:12px;color:var(--error,#f87171);padding:24px;">
                <span style="font-size:32px;">&#9888;&#65039;</span>
                <p style="margin:0;">STL compilation failed</p>
                ${errDetail}
                <button id="btn-retry-stl" style="margin-top:8px;padding:6px 18px;border:1px solid var(--border,#2e3d4f);background:var(--surface-2,#161b22);color:var(--text);border-radius:6px;cursor:pointer;font-size:13px;">&#8635; Retry compile</button>
            </div>`;
        el.querySelector('#btn-retry-stl')?.addEventListener('click', () => {
            document.dispatchEvent(new CustomEvent('scad:retry-stl'));
        });
        return;
    }

    if (stlStatus === 'done' && stlUrl) {
        _teardown();
        el.innerHTML = '';
        const host = document.createElement('div');
        host.style.cssText = 'width:100%;height:100%;min-height:480px;position:relative;';
        el.appendChild(host);
        _scene = createScadScene(host);
        _scene.loadStl(stlUrl);

        // Extras toggle button — load extras.stl (buttons, battery hatch, etc.)
        const extrasUrl = stlUrl.replace('/scad/stl?', '/scad/extras-stl?');
        const toolbar = document.createElement('div');
        toolbar.style.cssText = [
            'position:absolute; bottom:12px; left:50%; transform:translateX(-50%);',
            'display:flex; gap:8px; z-index:10;',
        ].join('');
        host.appendChild(toolbar);

        const extrasBtn = document.createElement('button');
        extrasBtn.textContent = '🔩 Show Extras';
        extrasBtn.style.cssText = [
            'padding:6px 16px; border:1px solid var(--border,#2e3d4f);',
            'background:var(--surface-2,#161b22); color:var(--text,#e6edf3);',
            'border-radius:6px; cursor:pointer; font-size:13px;',
            'backdrop-filter:blur(6px); transition:background 0.15s;',
        ].join('');
        let extrasVisible = false;
        let extrasLoaded = false;
        extrasBtn.addEventListener('click', () => {
            extrasVisible = !extrasVisible;
            if (extrasVisible && !extrasLoaded) {
                _scene.loadExtras(extrasUrl);
                extrasLoaded = true;
            } else {
                _scene.setExtrasVisible(extrasVisible);
            }
            extrasBtn.textContent = extrasVisible ? '🔩 Hide Extras' : '🔩 Show Extras';
            extrasBtn.style.background = extrasVisible
                ? 'var(--surface-raised,#2a2a2a)' : 'var(--surface-2,#161b22)';
        });
        toolbar.appendChild(extrasBtn);

        // Probe whether extras.stl exists; hide button if not
        fetch(extrasUrl, { headers: { 'Range': 'bytes=0-0' } }).then(r => {
            if (!r.ok) toolbar.remove();
        }).catch(() => toolbar.remove());
    }
}

function _teardown() {
    if (_scene)    { _scene.destroy();    _scene    = null; }
    if (_preview3d) { _preview3d.destroy(); _preview3d = null; }
    _previewBanner = null;
    clearRetry();
}

function scheduleRetry() {
    clearRetry();
    let attempts = 0;
    _retryTimer = setInterval(() => {
        attempts++;
        if (_lastData?.stlStatus === 'compiling' && _lastEl) {
            const d = getData('routing') || getData('placement');
            if (d) {
                clearRetry();
                renderView(_lastEl, _lastData);
            } else if (attempts >= 10) {
                clearRetry();  // give up after ~5 s
            }
        } else {
            clearRetry();
        }
    }, 500);
}

function clearRetry() {
    if (_retryTimer) { clearInterval(_retryTimer); _retryTimer = null; }
}

// ── Register handler ──────────────────────────────────────────────

registerHandler('scad', {
    label: 'Enclosure 3D Preview',
    placeholder: 'Generate SCAD to see the enclosure model',

    render(el, data) { renderView(el, data); },

    clear(el) {
        _teardown();
        _lastEl = null; _lastData = null;
        el.innerHTML = '<p class="viewport-empty">Generate SCAD to see the enclosure model</p>';
    },

    unmount() { _teardown(); },

    onResize(el, w, h) {
        if (_scene)     _scene.resize(w, h);
        if (_preview3d) _preview3d.resize(w, h);
    },
});
