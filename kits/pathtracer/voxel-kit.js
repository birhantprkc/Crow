// Crow voxel kit: a voxel grid, a mesher and a path-traced studio around
// three-gpu-pathtracer. Import it as 'crow-voxel-kit'; build_bundle resolves the
// name and inlines this file and the vendored library into one offline page.
//
// Every rule below was measured on 2026-09-25 (Crow ticket #298, the
// pt-proof runs): headless Chromium, ANGLE/Vulkan on an RTX 5090, 1024x1024,
// ~15 samples/s for 60k triangles, PSNR 60 s vs 180 s = 39.8 dB with this setup.
//
//   1. ONE MeshStandardMaterial PER PALETTE COLOUR, NEVER vertexColors. With
//      vertex colours the room rendered black (albedo lost) in 1 of 3 starts, on
//      both ANGLE backends. Palette materials: 5 of 5 normal.
//   2. A lamp's spot light sits BELOW a SOLID shade. A spot inside a hollow shade
//      gave persistent fireflies: PSNR 30.6 dB -> 38.6 dB when moved below.
//   3. Emission stays dim (emissiveIntensity <= 1). Emissive surfaces are not
//      importance-sampled by the library, so bright small emitters are fireflies;
//      light the room with spot/area lights and let the shade glow a little.
//   4. Culled faces only, no instancing (the library does not support it).
//   5. A despeckle pass removes the last isolated fireflies (HD4 -> HD5).
//   6. webglcontextlost/restored are handled: the scene is set again.

import * as THREE from './crow-pathtracer.js';
import {
  WebGLPathTracer, PhysicalCamera, GradientEquirectTexture, PhysicalSpotLight, ShapedAreaLight,
} from './crow-pathtracer.js';

export { THREE, WebGLPathTracer, PhysicalCamera, GradientEquirectTexture, PhysicalSpotLight, ShapedAreaLight };

// Material kinds a voxel can have. 'matte' is the default and the voxel-art look.
export const KINDS = ['matte', 'metal', 'glass', 'emit'];
export const MAX_EMISSIVE = 1.0;

const key = (x, y, z) => x + ',' + y + ',' + z;
const pick = (colour) => (typeof colour === 'function' ? colour : () => colour);

// ---- the grid ---------------------------------------------------------------
// Integer cells, y is up. A colour is '#rrggbb', or a function (x, y, z) -> colour
// for patterns; a function that returns null leaves that cell empty.
export class VoxelGrid {
  constructor() { this.cells = new Map(); }

  get size() { return this.cells.size; }

  put(x, y, z, colour, kind = 'matte') {
    if (colour === null || colour === undefined) { this.cells.delete(key(x, y, z)); return this; }
    if (!KINDS.includes(kind)) throw new Error(`unknown voxel kind '${kind}' -- use one of ${KINDS.join(', ')}`);
    this.cells.set(key(x, y, z), { colour: normalise(colour), kind });
    return this;
  }

  get(x, y, z) { return this.cells.get(key(x, y, z)) || null; }

  has(x, y, z) { return this.cells.has(key(x, y, z)); }

  // Filled box, corners inclusive.
  box(x0, y0, z0, x1, y1, z1, colour, kind = 'matte') {
    const f = pick(colour);
    for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++)
      for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++)
        for (let z = Math.min(z0, z1); z <= Math.max(z0, z1); z++) this.put(x, y, z, f(x, y, z), kind);
    return this;
  }

  // Empty a box, corners inclusive (windows, doorways, shelf cubbies).
  clear(x0, y0, z0, x1, y1, z1) {
    for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++)
      for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++)
        for (let z = Math.min(z0, z1); z <= Math.max(z0, z1); z++) this.cells.delete(key(x, y, z));
    return this;
  }

  // Vertical cylinder (axis y) centred on (cx, cz), radius r in voxels.
  cyl(cx, cz, r, y0, y1, colour, kind = 'matte') {
    const f = pick(colour);
    for (let x = Math.floor(cx - r); x <= Math.ceil(cx + r); x++)
      for (let z = Math.floor(cz - r); z <= Math.ceil(cz + r); z++)
        if ((x - cx) ** 2 + (z - cz) ** 2 <= r * r)
          for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++) this.put(x, y, z, f(x, y, z), kind);
    return this;
  }

  // Ellipsoid centred on (cx, cy, cz) with radii rx, ry, rz in voxels.
  blob(cx, cy, cz, rx, ry, rz, colour, kind = 'matte') {
    const f = pick(colour);
    for (let x = Math.floor(cx - rx); x <= Math.ceil(cx + rx); x++)
      for (let y = Math.floor(cy - ry); y <= Math.ceil(cy + ry); y++)
        for (let z = Math.floor(cz - rz); z <= Math.ceil(cz + rz); z++)
          if (((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 + ((z - cz) / rz) ** 2 <= 1) this.put(x, y, z, f(x, y, z), kind);
    return this;
  }

  // Inclusive integer bounds of the filled cells, or null for an empty grid.
  bounds() {
    if (!this.cells.size) return null;
    const b = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
    for (const k of this.cells.keys()) {
      const p = k.split(',').map(Number);
      for (let i = 0; i < 3; i++) { b.min[i] = Math.min(b.min[i], p[i]); b.max[i] = Math.max(b.max[i], p[i]); }
    }
    return b;
  }
}

function normalise(colour) {
  const c = String(colour).trim().toLowerCase();
  if (/^#[0-9a-f]{6}$/.test(c)) return c;
  if (/^#[0-9a-f]{3}$/.test(c)) return '#' + c[1] + c[1] + c[2] + c[2] + c[3] + c[3];
  throw new Error(`voxel colour '${colour}' is not '#rrggbb' -- the palette is keyed by hex`);
}

// Stable hash in [0, 1) for per-voxel colour variation without Math.random.
export const hash = (x, y, z) => (((x * 73856093) ^ (y * 19349663) ^ (z * 83492791)) >>> 0) % 1000 / 1000;

// ---- meshing ----------------------------------------------------------------
const DIRS = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]];

// Culled faces: a face is emitted only where the neighbour cell is empty. The
// faces are grouped per (kind, colour) -- one bucket becomes one geometry with
// one material. Pure data, no three.js: { buckets: Map, faces, voxels }.
export function meshVoxels(grid) {
  const buckets = new Map();
  let faces = 0;
  for (const [k, v] of grid.cells) {
    const [x, y, z] = k.split(',').map(Number);
    for (const [dx, dy, dz] of DIRS) {
      if (grid.cells.has(key(x + dx, y + dy, z + dz))) continue;
      const bk = v.kind + '|' + v.colour;
      let b = buckets.get(bk);
      if (!b) { b = { kind: v.kind, colour: v.colour, positions: [], normals: [], faces: 0 }; buckets.set(bk, b); }
      const cx = x + (dx > 0 ? 1 : 0), cy = y + (dy > 0 ? 1 : 0), cz = z + (dz > 0 ? 1 : 0);
      let u, w;
      if (dx !== 0) { u = [0, 1, 0]; w = [0, 0, 1]; } else if (dy !== 0) { u = [0, 0, 1]; w = [1, 0, 0]; } else { u = [1, 0, 0]; w = [0, 1, 0]; }
      const flip = (dx + dy + dz) < 0;
      const Q = (s, t) => [cx + u[0] * s + w[0] * t, cy + u[1] * s + w[1] * t, cz + u[2] * s + w[2] * t];
      const quad = flip ? [Q(0, 0), Q(0, 1), Q(1, 1), Q(0, 0), Q(1, 1), Q(1, 0)]
                        : [Q(0, 0), Q(1, 0), Q(1, 1), Q(0, 0), Q(1, 1), Q(0, 1)];
      for (const q of quad) { b.positions.push(q[0], q[1], q[2]); b.normals.push(dx, dy, dz); }
      b.faces++;
      faces++;
    }
  }
  return { buckets, faces, voxels: grid.size };
}

// The material of one bucket. Never vertexColors (rule 1).
export function materialFor(kind, colour, opts = {}) {
  if (kind === 'glass')
    return new THREE.MeshPhysicalMaterial({ color: colour, roughness: 0.05, transmission: 0.9, ior: 1.5, thickness: 0.4 });
  if (kind === 'metal')
    return new THREE.MeshStandardMaterial({ color: colour, roughness: opts.metalRoughness ?? 0.85, metalness: 0.6 });
  if (kind === 'emit')
    // The glow colour may differ from the surface colour (a cream shade glowing
    // warm): opts.glow maps a voxel colour to its emissive colour.
    return new THREE.MeshStandardMaterial({
      color: colour, roughness: 0.9, emissive: new THREE.Color((opts.glow && opts.glow[colour]) || colour),
      emissiveIntensity: Math.min(MAX_EMISSIVE, opts.emissiveIntensity ?? 0.8),
    });
  return new THREE.MeshStandardMaterial({ color: colour, roughness: opts.roughness ?? 0.85, metalness: 0 });
}

// Grid -> a THREE.Group of one Mesh per (kind, colour), in voxel units.
export function buildVoxelMeshes(grid, opts = {}) {
  const { buckets, faces, voxels } = meshVoxels(grid);
  const group = new THREE.Group();
  group.name = 'voxels';
  for (const b of buckets.values()) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(b.positions, 3));
    g.setAttribute('normal', new THREE.Float32BufferAttribute(b.normals, 3));
    const mesh = new THREE.Mesh(g, materialFor(b.kind, b.colour, opts));
    mesh.name = b.kind + ' ' + b.colour;
    group.add(mesh);
  }
  return { group, faces, triangles: faces * 2, voxels, materials: buckets.size };
}

// Every material in the scene that the path tracer would render wrongly.
export function sceneProblems(scene) {
  const out = [];
  scene.traverse((o) => {
    if (!o.isMesh) return;
    for (const m of [].concat(o.material)) {
      if (m.vertexColors) out.push(`${o.name || o.type}: vertexColors -- render black intermittently; use one material per colour`);
      if (!m.isMeshStandardMaterial) out.push(`${o.name || o.type}: ${m.type} -- the path tracer supports only MeshStandardMaterial/MeshPhysicalMaterial`);
      if (m.emissiveIntensity > MAX_EMISSIVE && m.emissive && m.emissive.getHex() !== 0) out.push(`${o.name || o.type}: emissiveIntensity ${m.emissiveIntensity} > ${MAX_EMISSIVE} -- fireflies; light with a spot/area light instead`);
    }
    if (o.isInstancedMesh) out.push(`${o.name || o.type}: InstancedMesh -- not supported by the path tracer`);
  });
  return out;
}

// ---- despeckle ------------------------------------------------------------
// Blender's Despeckle node, one pass: a pixel brighter than 1.12x the brightest
// of its 8 neighbours + 6 is replaced by their mean. An edge pixel always has a
// similar neighbour, so edges survive. RGBA bytes in, count of fixed pixels out.
export function despeckle(data, width, height, ratio = 1.12, floor = 6) {
  const src = new Uint8ClampedArray(data);
  const lum = (i) => 0.2126 * src[i] + 0.7152 * src[i + 1] + 0.0722 * src[i + 2];
  let n = 0;
  for (let y = 1; y < height - 1; y++) for (let x = 1; x < width - 1; x++) {
    const i = (y * width + x) * 4, L = lum(i);
    let mx = 0, r = 0, g = 0, b = 0;
    for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
      if (!dx && !dy) continue;
      const j = ((y + dy) * width + x + dx) * 4, l = lum(j);
      if (l > mx) mx = l;
      r += src[j]; g += src[j + 1]; b += src[j + 2];
    }
    if (L > mx * ratio + floor) { data[i] = r / 8; data[i + 1] = g / 8; data[i + 2] = b / 8; n++; }
  }
  return n;
}

// ---- the studio -------------------------------------------------------------
// A path-traced diorama around one grid: telephoto near-isometric PhysicalCamera
// with slight depth of field, a gradient dome, a ground plane in the background
// colour (soft ground shadow), a large soft key area light, ACES tone mapping.
// Numbers are the measured HD5 scene's, scaled by the grid's size.
export function createDiorama(grid, opts = {}) {
  const o = {
    width: 1024, height: 1024, voxelSize: 0.4, background: '#ddd6c8',
    domeTop: '#fbf6ec', domeBottom: '#b9b1a3', domeIntensity: 0.55,
    keyColour: '#fff1dc', keyIntensity: 7.5, fov: 14, direction: [1, 0.82, 1], zoom: 1,
    fStop: 3.2, bounces: 6, despeckle: true, parent: document.body, ...opts,
  };
  const S = o.voxelSize;
  const lights = [];
  const glow = {};                  // shade colour -> glow colour, from addLamp
  let emissiveIntensity = 0.8;
  const api = { grid, options: o, lights, THREE };

  // Voxel coordinates -> world, fixed from the grid's bounds when start() runs.
  let origin = null;
  const V = (x, y, z) => new THREE.Vector3((x - origin[0]) * S, (y - origin[1]) * S, (z - origin[2]) * S);
  api.voxelToWorld = (x, y, z) => V(x, y, z);

  // A lamp: a SOLID emissive shade in the grid plus a spot light just BELOW it,
  // pointing down (rule 2). (x, z) is the shade's axis, bottom/top its y range.
  api.addLamp = ({ x, z, bottom, top, radius = 4.5, shade = '#fff0d0', glow: glowColour = '#ffd9a0', light = '#ffc070',
                  intensity = 1600, glowIntensity = 0.8, cap = null } = {}) => {
    if ([x, z, bottom, top].some((v) => typeof v !== 'number')) throw new Error('addLamp needs x, z, bottom and top');
    grid.cyl(x, z, radius, bottom, top, shade, 'emit');
    grid.cyl(x, z, radius, top + 1, top + 1, cap || shade, 'matte');         // closed top
    glow[normalise(shade)] = normalise(glowColour);
    emissiveIntensity = Math.min(MAX_EMISSIVE, glowIntensity);
    lights.push({ type: 'lamp', x, z, bottom, radius, light, intensity });
    return api;
  };

  // A soft rectangular (or circular) area light, e.g. daylight through a window.
  // Positions in voxels; width/height in voxels.
  api.addAreaLight = ({ at, lookAt, colour = '#dff1ff', intensity = 30, width = 18, height = 18, circular = false } = {}) => {
    if (!at || !lookAt) throw new Error('addAreaLight needs at: [x, y, z] and lookAt: [x, y, z]');
    lights.push({ type: 'area', at, lookAt, colour, intensity, width, height, circular });
    return api;
  };

  api.start = () => {
    const b = grid.bounds();
    if (!b) throw new Error('the grid is empty -- put voxels in it before start()');
    origin = [(b.min[0] + b.max[0] + 1) / 2, b.min[1], (b.min[2] + b.max[2] + 1) / 2];
    const ext = [(b.max[0] - b.min[0] + 1) * S, (b.max[1] - b.min[1] + 1) * S, (b.max[2] - b.min[2] + 1) * S];
    const L = Math.max(ext[0], ext[2]);

    const scene = new THREE.Scene();
    const built = buildVoxelMeshes(grid, { emissiveIntensity, ...o, glow: { ...glow, ...(o.glow || {}) } });
    built.group.scale.setScalar(S);
    built.group.position.set(-origin[0] * S, -origin[1] * S, -origin[2] * S);
    scene.add(built.group);

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(L * 16, L * 16),
      new THREE.MeshStandardMaterial({ color: o.background, roughness: 1 }));
    ground.name = 'ground';
    ground.rotation.x = -Math.PI / 2; ground.position.y = -0.001;
    scene.add(ground);

    const env = new GradientEquirectTexture();
    env.topColor.set(o.domeTop); env.bottomColor.set(o.domeBottom); env.update();
    scene.environment = env; scene.background = new THREE.Color(o.background);
    scene.environmentIntensity = o.domeIntensity; scene.backgroundIntensity = 1.0;

    // Key: a large soft area light up and to the right, scaled with the room.
    const k = L / 25.6;
    const key1 = new ShapedAreaLight(new THREE.Color(o.keyColour), o.keyIntensity, 26 * k, 26 * k);
    key1.position.set(48 * k, 52 * k, 8 * k); key1.lookAt(0, 0, 0); key1.name = 'key';
    scene.add(key1);

    for (const l of lights) {
      if (l.type === 'lamp') {
        // Candela: irradiance falls with distance squared, so the measured 1600
        // at 0.4 world units per voxel scales with the voxel size squared.
        const spot = new PhysicalSpotLight(l.light, l.intensity * (S / 0.4) ** 2, 0, Math.PI / 2.05, 0.8, 2);
        spot.radius = 3 * S;
        spot.position.copy(V(l.x + 0.5, l.bottom - 0.8, l.z + 0.5));
        spot.target.position.copy(V(l.x + 0.5, b.min[1], l.z + 0.5));
        spot.name = 'lamp';
        scene.add(spot, spot.target);
      } else {
        const a = new ShapedAreaLight(new THREE.Color(l.colour), l.intensity, l.width * S, l.height * S);
        a.isCircular = !!l.circular;
        a.position.copy(V(...l.at)); a.lookAt(V(...l.lookAt)); a.name = 'area';
        scene.add(a);
      }
    }

    // Camera: telephoto PhysicalCamera, near-isometric, slight depth of field.
    const cam = new PhysicalCamera(o.fov, o.width / o.height, 1, 10000);
    const target = new THREE.Vector3(0, ext[1] * 0.4, 0);
    const radius = 0.5 * Math.hypot(ext[0], ext[1], ext[2]);
    const dist = (radius / Math.sin((o.fov * Math.PI) / 360)) * 1.02 / o.zoom;
    cam.position.copy(target).addScaledVector(new THREE.Vector3(...o.direction).normalize(), dist);
    cam.lookAt(target);
    cam.far = dist * 4;
    cam.updateProjectionMatrix();
    cam.focusDistance = dist; cam.fStop = o.fStop; cam.apertureBlades = 6;

    const problems = sceneProblems(scene);
    if (problems.length) throw new Error('the scene would not path-trace cleanly:\n  ' + problems.join('\n  '));

    const renderer = new THREE.WebGLRenderer({ antialias: false, preserveDrawingBuffer: true });
    renderer.setPixelRatio(1); renderer.setSize(o.width, o.height);
    renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.outputColorSpace = THREE.SRGBColorSpace;
    const holder = document.createElement('div');
    Object.assign(holder.style, { position: 'relative', width: o.width + 'px', height: o.height + 'px' });
    holder.appendChild(renderer.domElement);
    o.parent.appendChild(holder);
    const gl = renderer.getContext();
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');

    // The probes a checker or a judge reads (and the console line render_page shows).
    const probe = window.__PT__ = {
      renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      samples: 0, samplesPerSecond: 0, seconds: 0, lost: 0, restored: 0, despeckled: 0, error: null,
    };
    window.__SCENE__ = {
      voxels: built.voxels, faces: built.faces, triangles: built.triangles, materials: built.materials,
      lights: lights.map((l) => l.type), size: { x: b.max[0] - b.min[0] + 1, y: b.max[1] - b.min[1] + 1, z: b.max[2] - b.min[2] + 1 },
      vertexColors: false,
    };

    const pt = new WebGLPathTracer(renderer);
    pt.bounces = o.bounces; pt.minSamples = 1; pt.renderDelay = 0; pt.fadeDuration = 0; pt.tiles.set(2, 2);
    try { pt.setScene(scene, cam); } catch (e) { probe.error = String(e); console.error('[crow-pt] setScene failed:', e); }
    const t0 = performance.now();

    // Context loss: keep the page alive and set the scene again when it is back.
    renderer.domElement.addEventListener('webglcontextlost', (e) => { e.preventDefault(); probe.lost++; });
    renderer.domElement.addEventListener('webglcontextrestored', () => {
      probe.restored++;
      try { pt.setScene(scene, cam); pt.reset(); } catch (e) { probe.error = String(e); }
    });

    const loop = () => {
      pt.renderSample();
      probe.samples = pt.samples;
      probe.seconds = (performance.now() - t0) / 1000;
      probe.samplesPerSecond = probe.seconds > 0 ? +(probe.samples / probe.seconds).toFixed(1) : 0;
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);

    // Despeckle on an overlay canvas, once a second (rule 5).
    if (o.despeckle) {
      const overlay = document.createElement('canvas');
      overlay.width = o.width; overlay.height = o.height;
      Object.assign(overlay.style, { position: 'absolute', left: '0', top: '0' });
      holder.appendChild(overlay);
      const ctx = overlay.getContext('2d', { willReadFrequently: true });
      setInterval(() => {
        if (pt.samples <= 8) return;
        ctx.drawImage(renderer.domElement, 0, 0);
        const img = ctx.getImageData(0, 0, o.width, o.height);
        probe.despeckled = despeckle(img.data, o.width, o.height);
        ctx.putImageData(img, 0, 0);
      }, 1000);
    }

    console.log(`[crow-pt] ${built.voxels} voxels, ${built.triangles} triangles, ${built.materials} materials, renderer: ${probe.renderer}`);
    setInterval(() => console.log(`[crow-pt] samples=${probe.samples} spp/s=${probe.samplesPerSecond} lost=${probe.lost} despeckled=${probe.despeckled}`), 5000);

    Object.assign(api, { scene, camera: cam, renderer, pathTracer: pt, probe, voxels: built });
    return api;
  };

  return api;
}
