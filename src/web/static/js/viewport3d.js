/**
 * viewport3d.js — Three.js 3D scene builder for the ManufacturerAI viewport.
 *
 * Coordinate system  (we stay in mm throughout):
 *   Three.js x  =  design x_mm  (width, left→right)
 *   Three.js y  =  z_mm         (height, floor→ceiling)
 *   Three.js z  =  design y_mm  (depth, front→back — no sign flip; camera is placed to compensate)
 *
 * All geometry is in mm; renderer.setSize / camera aspect handle px scaling.
 *
 * Exports:
 *   create3DScene(container)  →  Scene3D{ update(data), resize(w,h), destroy() }
 */

import * as THREE from 'three';
import { OrbitControls }   from 'three/addons/controls/OrbitControls.js';
import { getSharedRenderer, setSharedRenderer } from './viewport.js';
import { normaliseOutline, expandOutlineVertices } from './viewportUtils.js';

// ── Colour palette ────────────────────────────────────────────────────────────

// Component colours — distinct enough to tell apart on a dark PCB.
const PALETTE = [
    0x4ea8d8, 0x52d474, 0xeeb830, 0xee6e6e, 0xb890e8,
    0x40c0d0, 0x60e090, 0xd8b040, 0xe88080, 0x90d0e0,
];

const MAT = {
    pcb           : () => new THREE.MeshPhongMaterial({ color: 0x1c3824, shininess: 10, side: THREE.DoubleSide }),
    trace         : (colHex) => new THREE.LineBasicMaterial({ color: colHex, linewidth: 2 }),
    component     : (colHex) => new THREE.MeshPhongMaterial({ color: colHex, shininess: 60 }),
    wallFill      : () => new THREE.MeshPhongMaterial({
        color: 0x4a6888, side: THREE.FrontSide,
        transparent: true, opacity: 0.35, shininess: 0,
        depthWrite: false,
    }),
    lidFill       : () => new THREE.MeshPhongMaterial({
        color: 0x5a7898, side: THREE.FrontSide,
        transparent: true, opacity: 0.18, shininess: 0,
        depthWrite: false,
        polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
    }),
};

// ── Renderer singleton ────────────────────────────────────────────────────────

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

// ── Public factory ────────────────────────────────────────────────────────────

/**
 * Create a 3-D scene bound to `container` (a DIV).
 * Returns a Scene3D object with .update(data), .resize(w, h), .destroy().
 */
export function create3DScene(container) {
    const renderer = getOrCreateRenderer();
    const canvas   = renderer.domElement;

    // Append canvas to container
    container.style.position = 'relative';
    canvas.style.display = 'block';
    container.appendChild(canvas);

    const scene  = new THREE.Scene();
    scene.background = new THREE.Color(0x0d1117);
    // Subtle depth fog — makes distant lines quietly fade into the bg
    scene.fog = new THREE.FogExp2(0x0d1117, 0.0028);

    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000);
    camera.position.set(50, 100, 150);
    camera.lookAt(0, 0, 0);

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.update();

    // Lighting — bright key from top-right, warm fill from bottom-left.
    scene.add(new THREE.AmbientLight(0xffffff, 0.80));
    const dirLight = new THREE.DirectionalLight(0xffffff, 1.4);
    dirLight.position.set(80, 250, 120);
    scene.add(dirLight);
    const fillLight = new THREE.DirectionalLight(0xaac8ff, 0.55);
    fillLight.position.set(-80, 60, -120);
    scene.add(fillLight);

    // Ground grid — low-contrast reference plane anchors the model in space
    const grid = new THREE.GridHelper(400, 40, 0x1a2a3a, 0x151f2a);
    grid.position.y = -0.5;
    scene.add(grid);

    // Current content group
    let contentGroup = null;
    let animId = null;

    function animate() {
        animId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Initial size
    const w = container.clientWidth  || 400;
    const h = container.clientHeight || 400;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();

    // ── Scene3D public API ──────────────────────────────────────

    return {
        update(data) {
            if (contentGroup) {
                scene.remove(contentGroup);
                contentGroup.traverse(obj => {
                    if (obj.geometry) obj.geometry.dispose();
                    if (obj.material) {
                        if (Array.isArray(obj.material)) obj.material.forEach(m => m.dispose());
                        else obj.material.dispose();
                    }
                });
            }
            contentGroup = buildSceneContent(data);
            scene.add(contentGroup);

            // Fit camera to content bounding box
            const box = new THREE.Box3().setFromObject(contentGroup);
            if (!box.isEmpty()) {
                const center = box.getCenter(new THREE.Vector3());
                const size   = box.getSize(new THREE.Vector3());
                const maxDim = Math.max(size.x, size.y, size.z);
                const dist   = maxDim * 1.8 / Math.tan((camera.fov / 2) * Math.PI / 180);
                // Classic 30° elevation product-CAD angle — more frontal, less top-down
                camera.position.set(center.x + dist * 0.65, center.y + dist * 0.50, center.z + dist * 0.85);
                camera.lookAt(center);
                controls.target.copy(center);
                controls.update();
            }
        },

        resize(w, h) {
            if (w <= 0 || h <= 0) return;
            renderer.setSize(w, h);
            camera.aspect = w / h;
            camera.updateProjectionMatrix();
        },

        destroy() {
            if (animId !== null) cancelAnimationFrame(animId);
            controls.dispose();
            if (canvas.parentNode === container) container.removeChild(canvas);
            // Don't dispose renderer — it's the shared singleton
        },
    };
}

// ── Scene content builder ─────────────────────────────────────────────────────

export function buildSceneContent(data) {
    const group = new THREE.Group();
    if (!data) return group;

    const outline    = data.outline    ?? [];
    const enclosure  = data.enclosure  ?? { height_mm: 25 };
    const heightGrid = data.height_grid ?? null;
    const components = data.components  ?? [];
    const traces     = data.traces      ?? [];

    const { verts, corners, zTops } = normaliseOutline(outline);
    if (verts.length < 3) return group;

    const defaultZ = enclosure.height_mm ?? 25;
    // Expand bezier corners into sub-points so walls, floor, lid, and
    // wireframe all follow the same smooth rounded profile as the 2D view.
    const { pts: expanded, zs: expandedZ, cornerIndices } = expandOutlineVertices(
        verts, corners, zTops, defaultZ);

    // Build ui_placements lookup before shell so lid cutouts can use it.
    const uiPos = {};
    (data.ui_placements ?? []).forEach(up => {
        uiPos[up.instance_id] = {
            x_mm: up.x_mm,
            y_mm: up.y_mm,
            rotation_deg: up.rotation_deg ?? 0,
        };
    });

    // 1. Enclosure shell
    const shellGroup = buildEnclosureShell(expanded, expandedZ, enclosure, heightGrid, components, uiPos, cornerIndices);
    group.add(shellGroup);

    // 2. PCB floor
    group.add(buildPCBFloor(expanded));

    // 3. Placed components
    const FLOOR_Z = 2;  // mm above PCB floor
    components.forEach((comp, i) => {
        // Resolve position: prefer explicit x_mm (placement step), fall back
        // to ui_placements map (design step), skip if neither is available.
        let x = comp.x_mm, y = comp.y_mm, rot = comp.rotation_deg ?? 0;
        if (x == null || y == null) {
            const ui = uiPos[comp.instance_id];
            if (!ui) return;   // auto-placed; no position yet at design step
            x = ui.x_mm; y = ui.y_mm; rot = ui.rotation_deg;
        }
        const placed = { ...comp, x_mm: x, y_mm: y, rotation_deg: rot };
        const mesh = buildComponentBox(placed, FLOOR_Z, PALETTE[i % PALETTE.length]);
        if (mesh) group.add(mesh);
    });

    // 4. Routed traces
    const netColors = buildNetColorMap(traces.map(t => t.net_id));
    traces.forEach(trace => {
        const lines = buildTraceLine(trace, FLOOR_Z, netColors[trace.net_id] ?? 0xffffff);
        if (lines) group.add(lines);
    });

    return group;
}

// ── Enclosure shell ───────────────────────────────────────────────────────────

// pts: expanded bezier polygon [[x,y],...]
// expandedZ: per-point ceiling heights already interpolated from z_top vertices
function buildEnclosureShell(pts, expandedZ, enclosure, heightGrid, compList = [], uiPosMap = {}, cornerIndices = []) {
    const group = new THREE.Group();
    const eTop  = enclosure.edge_top;
    const eBot  = enclosure.edge_bottom;
    const N     = pts.length;

    // Centroid — used for uniform inward scaling (same as old buildShellPreview).
    let cx = 0, cz = 0;
    pts.forEach(p => { cx += p[0]; cz += p[1]; });
    cx /= N; cz /= N;

    // Inset a vertex toward the centroid by `off` mm.
    // This is the same as the old makeShape(outline, inset) — uniform shrink
    // from centroid, so every corner stays clean regardless of polygon shape.
    function insetPt(x, y, off) {
        if (off === 0) return [x, y];
        const dx = x - cx, dz = y - cz;
        const dist = Math.hypot(dx, dz);
        if (dist < 0.01) return [x, y];
        const f = Math.max(0, (dist - off)) / dist;
        return [cx + dx * f, cz + dz * f];
    }

    // ── Wall fill — very low opacity solid, gives volume without obscuring internals ─
    {
        const wallPos = [], wallIdx = [];
        let vi = 0;
        for (let i = 0; i < N; i++) {
            const j  = (i + 1) % N;
            const x0 = pts[i][0], y0 = pts[i][1], z0 = expandedZ[i];
            const x1 = pts[j][0], y1 = pts[j][1], z1 = expandedZ[j];
            const profL = _edgeProfile(z0, eBot, eTop);
            const profR = _edgeProfile(z1, eBot, eTop);
            for (let k = 0; k < profL.length - 1; k++) {
                const { h: hLb, off: oLb } = profL[k];
                const { h: hLt, off: oLt } = profL[k + 1];
                const { h: hRb, off: oRb } = profR[k];
                const { h: hRt, off: oRt } = profR[k + 1];
                const [xLb, zLb] = insetPt(x0, y0, oLb);
                const [xLt, zLt] = insetPt(x0, y0, oLt);
                const [xRb, zRb] = insetPt(x1, y1, oRb);
                const [xRt, zRt] = insetPt(x1, y1, oRt);
                wallPos.push(xLb, hLb, zLb, xLt, hLt, zLt, xRt, hRt, zRt, xRb, hRb, zRb);
                wallIdx.push(vi, vi+1, vi+2, vi, vi+2, vi+3);
                vi += 4;
            }
        }
        const wallGeo = new THREE.BufferGeometry();
        wallGeo.setAttribute('position', new THREE.Float32BufferAttribute(wallPos, 3));
        wallGeo.setIndex(wallIdx);
        wallGeo.computeVertexNormals();
        const wallMesh = new THREE.Mesh(wallGeo, MAT.wallFill());
        wallMesh.renderOrder = 0;
        group.add(wallMesh);
    }

    const outlineMat = new THREE.LineBasicMaterial({ color: 0x90c8ff });

    // ── Lid surface (wireframe) with component cutout rings ─────────────────
    {
        const lidPts = pts.map((v, i) => {
            const prof = _edgeProfile(expandedZ[i], eBot, eTop);
            const { off } = prof[prof.length - 1];
            return insetPt(v[0], v[1], off);
        });

        // Build outline shape with holes punched for every UI-placed component.
        const CLR = 0.5;   // mm clearance around each cutout (used in rings below)
        const defH = enclosure.height_mm ?? 25;

        // Lid fill mesh — caps the wall fill so walls don't visually bleed above the lid.
        // Interior heights use grid/IDW; the tessellated mesh covers the full surface.
        {
            const lidShape = new THREE.Shape(lidPts.map(v => new THREE.Vector2(v[0], v[1])));
            const lidGeo   = new THREE.ShapeGeometry(lidShape);
            lidGeo.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));
            const pos = lidGeo.attributes.position;
            for (let i = 0; i < pos.count; i++) {
                pos.setY(i, _lidSampleHeight(pos.getX(i), pos.getZ(i), lidPts, expandedZ, heightGrid, defH));
            }
            pos.needsUpdate = true;
            lidGeo.computeVertexNormals();
            const lidMesh = new THREE.Mesh(lidGeo, MAT.lidFill());
            lidMesh.renderOrder = 1;
            group.add(lidMesh);
        }

        // Lid wireframe: perimeter ring pinned directly to expandedZ[i] at each
        // boundary point — bypasses grid/IDW which can differ at horn/chin tips.
        const lidLoopPts = lidPts.map((v, i) =>
            new THREE.Vector3(v[0], expandedZ[i] + 0.15, v[1])
        );
        lidLoopPts.push(lidLoopPts[0].clone());
        group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(lidLoopPts), outlineMat));

        // Spoke lines from centroid to each original corner — reads clearly as
        // a surface and reflects the actual corner structure of the design.
        if (cornerIndices.length >= 2) {
            const ocx = lidPts.reduce((s, v) => s + v[0], 0) / lidPts.length;
            const ocy = lidPts.reduce((s, v) => s + v[1], 0) / lidPts.length;
            const ch  = _lidSampleHeight(ocx, ocy, lidPts, expandedZ, heightGrid, defH) + 0.15;
            const hub = new THREE.Vector3(ocx, ch, ocy);
            for (const ci of cornerIndices) {
                const v  = lidPts[ci];
                // Pin to expandedZ directly — same as wall profile top, no grid mismatch.
                const vh = expandedZ[ci] + 0.15;
                group.add(new THREE.Line(
                    new THREE.BufferGeometry().setFromPoints([
                        hub, new THREE.Vector3(v[0], vh, v[1]),
                    ]), outlineMat));
            }
        }

        // Draw a bright outline ring around every cutout so it reads clearly.
        const cutoutMat = new THREE.LineBasicMaterial({ color: 0xffdd66 });
        const RING_SEGS = 24;
        for (const comp of compList) {
            if (!comp.ui_placement) continue;
            let x = comp.x_mm, y = comp.y_mm;
            if (x == null || y == null) {
                const ui = uiPosMap[comp.instance_id];
                if (!ui) continue;
                x = ui.x_mm; y = ui.y_mm;
            }
            const body = comp.body;
            if (!body) continue;

            // Sample lid height at cutout centre for ring height
            const rh = _lidSampleHeight(x, y, lidPts, expandedZ, heightGrid, defH) + 0.2;

            let ringPts;
            if (body.shape === 'circle') {
                const r = comp.cap_diameter_mm != null
                    ? (comp.cap_diameter_mm / 2 + (comp.cap_clearance_mm ?? CLR))
                    : (body.diameter_mm / 2 + CLR);
                ringPts = [];
                for (let s = 0; s <= RING_SEGS; s++) {
                    const a = (s / RING_SEGS) * Math.PI * 2;
                    ringPts.push(new THREE.Vector3(x + Math.cos(a) * r, rh, y + Math.sin(a) * r));
                }
            } else {
                const hw = ((body.width_mm  ?? 6) / 2) + CLR;
                const hh = ((body.length_mm ?? 6) / 2) + CLR;
                ringPts = [
                    new THREE.Vector3(x - hw, rh, y - hh),
                    new THREE.Vector3(x + hw, rh, y - hh),
                    new THREE.Vector3(x + hw, rh, y + hh),
                    new THREE.Vector3(x - hw, rh, y + hh),
                    new THREE.Vector3(x - hw, rh, y - hh),
                ];
            }
            group.add(new THREE.Line(
                new THREE.BufferGeometry().setFromPoints(ringPts), cutoutMat));
        }
    }

    // ── Wireframe ─────────────────────────────────────────────────────────────

    // Bottom loop — inset by the floor-level offset of each vertex's profile
    const botLoopPts = pts.map((v, i) => {
        const prof = _edgeProfile(expandedZ[i], eBot, eTop);
        const [ix, iz] = insetPt(v[0], v[1], prof[0].off);
        return new THREE.Vector3(ix, prof[0].h, iz);
    });
    botLoopPts.push(botLoopPts[0].clone());
    group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(botLoopPts), outlineMat));

    // (Top loop is drawn above as lidLoopPts with IDW height warping.)

    // Vertical pillars — one per original design corner for clean, intentional lines
    const pillarSet = new Set(cornerIndices.length > 0 ? cornerIndices : []);
    // Fallback: if no cornerIndices provided, every 7th point (old behaviour)
    pts.forEach((v, i) => {
        if (pillarSet.size > 0 ? !pillarSet.has(i) : (i % 7 !== 0)) return;
        const prof = _edgeProfile(expandedZ[i], eBot, eTop);
        const pillarPts = prof.map(({ h, off }) => {
            const [ix, iz] = insetPt(v[0], v[1], off);
            return new THREE.Vector3(ix, h, iz);
        });
        group.add(new THREE.Line(
            new THREE.BufferGeometry().setFromPoints(pillarPts), outlineMat));
    });

    return group;
}

/**
 * Build the wall cross-section profile for one wall column.
 *
 * Returns [{h, off}, ...] where:
 *   h   = height in mm (0 = floor, z_top = ceiling)
 *   off = inward offset from the polygon boundary (0 = flush, >0 = inward)
 *
 * The fillet/chamfer curves the outer face INWARD near the lid/floor edge.
 * The lid sits flush at z_top on the original boundary, so from the side you
 * see the wall face gently retreat — a smooth rounded shoulder on the corner.
 *
 * ASCII cross-section (outer face on right, floor at bottom):
 *
 *   none     chamfer (top)     fillet (top)
 *    ┤         ╲┤                ╯┤
 *    │           │                │
 *    │           │                │
 *
 * @param {number} z_top  Ceiling height for this wall column (mm)
 * @param {object} eBot   {type:'none'|'chamfer'|'fillet', size_mm}
 * @param {object} eTop   {type:'none'|'chamfer'|'fillet', size_mm}
 */
function _edgeProfile(z_top, eBot, eTop) {
    const botType = eBot?.type ?? 'none';
    const topType = eTop?.type ?? 'none';
    const botS = Math.min(eBot?.size_mm ?? 3.0, z_top * 0.42);
    const topS = Math.min(eTop?.size_mm ?? 3.0, z_top * 0.42);
    const ARC  = 8;
    const pts  = [];

    // ── Bottom edge ───────────────────────────────────────────────────────────
    if (botType === 'chamfer') {
        pts.push({ h: 0,    off: botS });   // floor: corner cut inward
        pts.push({ h: botS, off: 0   });   // above bevel: flush
    } else if (botType === 'fillet') {
        for (let k = 0; k <= ARC; k++) {
            const a = (k / ARC) * (Math.PI / 2);
            // Quarter-circle: (h=0, off=botS) → (h=botS, off=0)
            pts.push({ h: botS * Math.sin(a), off: botS * Math.cos(a) });
        }
    } else {
        pts.push({ h: 0, off: 0 });
    }

    // ── Top edge ──────────────────────────────────────────────────────────────
    if (topType === 'chamfer') {
        pts.push({ h: z_top - topS, off: 0    });   // below bevel: flush
        pts.push({ h: z_top,        off: topS });   // lid level: corner cut inward
    } else if (topType === 'fillet') {
        for (let k = 0; k <= ARC; k++) {
            const a = (k / ARC) * (Math.PI / 2);
            // Quarter-circle: (h=z_top-topS, off=0) → (h=z_top, off=topS)
            pts.push({
                h:   (z_top - topS) + topS * Math.sin(a),
                off: topS * (1 - Math.cos(a)),
            });
        }
    } else {
        pts.push({ h: z_top, off: 0 });
    }

    return pts;
}

/**
 * Build the enclosure lid as a tessellated polygon over the expanded outline.
 * Heights are warped from the grid (bilinear) or, when the grid has no cell
 * for a vertex (e.g. near convex spike tips), by inverse-distance-weighted
 * interpolation from the known boundary heights (expandedZ).  This ensures
 * the lid surface matches the wall tops everywhere with no visible gap.
 */
function buildLid(pts, expandedZ, defaultH, heightGrid) {
    const shape = new THREE.Shape(pts.map(v => new THREE.Vector2(v[0], v[1])));
    const geo   = new THREE.ShapeGeometry(shape);
    // Rotate XY → XZ: Three.js y becomes height
    geo.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));

    // Always warp vertex heights — first try the grid, then fall back to IDW
    // from the boundary heights (expandedZ).  This ensures the lid matches
    // the actual wall tops everywhere, including horn tips that sit higher
    // than the flat enclosure.height_mm default.
    const pos = geo.attributes.position;
    for (let i = 0; i < pos.count; i++) {
        const dx = pos.getX(i);
        const dy = pos.getZ(i);   // design y lives in Three.js z after rotation
        pos.setY(i, _lidSampleHeight(dx, dy, pts, expandedZ, heightGrid, defaultH));
    }
    pos.needsUpdate = true;

    geo.computeVertexNormals();
    return geo;
}

/**
 * Sample the lid height at design position (x, y).
 * Priority: bilinear grid → IDW from boundary pts → defaultH.
 */
function _lidSampleHeight(x, y, boundaryPts, boundaryZ, heightGrid, defaultH) {
    const gridZ = heightGrid ? _gridSampleHeight(x, y, heightGrid) : null;
    if (gridZ !== null) return gridZ;

    // Vertex falls outside the grid mask (spike tip or just outside border).
    // Interpolate from the nearest boundary points using inverse-distance weighting.
    let sumW = 0, sumWZ = 0;
    for (let i = 0; i < boundaryPts.length; i++) {
        const d2 = (x - boundaryPts[i][0]) ** 2 + (y - boundaryPts[i][1]) ** 2;
        if (d2 < 1e-6) return boundaryZ[i];   // exactly on the boundary
        const w = 1.0 / d2;
        sumW  += w;
        sumWZ += w * boundaryZ[i];
    }
    return sumW > 0 ? sumWZ / sumW : defaultH;
}

/** Bilinear interpolation of a height value from the grid at world (x, y).
 *  Returns null when the position has no coverage (outside the mask). */
function _gridSampleHeight(x, y, hg) {
    const fc = (x - hg.origin_x) / hg.step_mm;
    const fr = (y - hg.origin_y) / hg.step_mm;
    const c0 = Math.floor(fc), r0 = Math.floor(fr);
    const c1 = c0 + 1,         r1 = r0 + 1;
    const tc = fc - c0,         tr = fr - r0;

    const v = (r, c) => {
        if (r < 0 || r >= hg.rows || c < 0 || c >= hg.cols) return null;
        return hg.grid[r]?.[c] ?? null;
    };

    const z00 = v(r0,c0), z10 = v(r0,c1), z01 = v(r1,c0), z11 = v(r1,c1);

    // Full bilinear if all four corners available
    if (z00!=null && z10!=null && z01!=null && z11!=null) {
        return (1-tr)*((1-tc)*z00 + tc*z10) + tr*((1-tc)*z01 + tc*z11);
    }
    // Nearest non-null in 3×3 neighbourhood
    let best = null, bestD = Infinity;
    for (let dr = -1; dr <= 2; dr++) {
        for (let dc = -1; dc <= 2; dc++) {
            const val = v(r0+dr, c0+dc);
            if (val == null) continue;
            const d = (dr-tr)*(dr-tr) + (dc-tc)*(dc-tc);
            if (d < bestD) { bestD = d; best = val; }
        }
    }
    return best ?? null;
}

function buildFlatLid(pts, h) {
    const shape = new THREE.Shape(pts.map(v => new THREE.Vector2(v[0], v[1])));
    const geo   = new THREE.ShapeGeometry(shape);
    // +π/2 maps ShapeGeometry's XY → XZ plane with normals pointing up (+Y).
    // Using -π/2 would mirror the shape (design y → -Three.js z), placing
    // it in front of the model, which is wrong.
    geo.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));
    geo.applyMatrix4(new THREE.Matrix4().makeTranslation(0, h, 0));
    return geo;
}

// ── PCB floor ─────────────────────────────────────────────────────────────────

function buildPCBFloor(pts) {
    const shape = new THREE.Shape(pts.map(v => new THREE.Vector2(v[0], v[1])));
    const geo   = new THREE.ShapeGeometry(shape);
    // +π/2: maps design XY → Three.js XZ with normals pointing upward.
    geo.applyMatrix4(new THREE.Matrix4().makeRotationX(Math.PI / 2));   // flat in XZ
    geo.applyMatrix4(new THREE.Matrix4().makeTranslation(0, 0.5, 0));   // 0.5 mm above 0
    const mesh = new THREE.Mesh(geo, MAT.pcb());

    // Perimeter outline so the board edge reads clearly (muted to not compete with enclosure).
    const rimPts = pts.map(v => new THREE.Vector3(v[0], 0.6, v[1]));
    rimPts.push(rimPts[0].clone());
    const rimLine = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(rimPts),
        new THREE.LineBasicMaterial({ color: 0x4a8a5a }),
    );

    const group = new THREE.Group();
    group.add(mesh);
    group.add(rimLine);
    return group;
}

// ── Component boxes ───────────────────────────────────────────────────────────

function buildComponentBox(comp, floorZ, colorHex) {
    const body = comp.body;
    if (!body) return null;

    let W, L;
    if (body.shape === 'cylinder') {
        W = L = (body.diameter_mm ?? 5);
    } else {
        W = body.width_mm  ?? 5;
        L = body.length_mm ?? 5;
    }
    const H = body.height_mm ?? 3;

    const geo = new THREE.BoxGeometry(W, H, L);
    const mat = MAT.component(colorHex);
    const mesh = new THREE.Mesh(geo, mat);

    const x   = comp.x_mm ?? 0;
    const y   = comp.y_mm ?? 0;
    const rot = ((comp.rotation_deg ?? 0) * Math.PI) / 180;

    mesh.position.set(x, floorZ + H / 2, y);
    mesh.rotation.y = -rot;   // negative: Three.js Y-up rotation counter-clockwise from top

    // Edge outline in a lighter tint of the same colour — keeps wireframe style
    const edgeGeo  = new THREE.EdgesGeometry(geo, 15);  // threshold 15° skips micro-facets
    const edgeCol  = new THREE.Color(colorHex).lerp(new THREE.Color(0xffffff), 0.45);
    const edgeLine = new THREE.LineSegments(
        edgeGeo,
        new THREE.LineBasicMaterial({ color: edgeCol }),
    );
    mesh.add(edgeLine);

    return mesh;
}

// ── Trace lines ───────────────────────────────────────────────────────────────

function buildTraceLine(trace, floorZ, colorHex) {
    const path = trace.path;
    if (!path || path.length < 2) return null;

    const pts = path.map(([x, y]) => new THREE.Vector3(x, floorZ, y));
    const geo = new THREE.BufferGeometry().setFromPoints(pts);
    return new THREE.Line(geo, MAT.trace(colorHex));
}

function buildNetColorMap(netIds) {
    const unique = [...new Set(netIds)];
    const n = unique.length;
    const map = {};
    unique.forEach((id, i) => {
        const hue = (i * 360) / (n || 1);
        map[id] = new THREE.Color().setHSL(hue / 360, 0.75, 0.60).getHex();
    });
    return map;
}
