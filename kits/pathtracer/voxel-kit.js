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
//
// Two modes (#299). Every change to a path-traced scene resets its accumulation,
// so an animated diorama cannot converge:
//   LIVE  (default) rasterised three.js preview of the same scene and materials,
//         real lights + soft shadow maps, the scene's animate callbacks every frame;
//   PHOTO the animation paused at t, the path tracer converging (the rules above).
// Toggle: key P, or the small button that shows while the pointer is over the
// page. URL: ?mode=live|photo, &t=<seconds> (photo pose), &ui=0 (no button).
//   7. Time comes ONLY from the page clock (the rAF timestamp / performance.now),
//      so Crow's render_page frames (#293: frozen clock, fixed steps) differ from
//      each other and repeat byte-identically. Never Date or Math.random in a
//      callback; use hash() for per-voxel or per-particle variation.
//   8. PCFSoftShadowMap is gone in three r182+ (the bundle warns and falls back);
//      PCFShadowMap with shadow.radius is the soft one now.
//   9. A light's own .visible decides whether the path tracer uses it (a hidden
//      parent does not), so the raster stand-ins of area/key lights are hidden
//      on the lights themselves in photo mode.

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
  constructor() {
    this.cells = new Map();
    this.props = [];                  // [{ name, keys: Set }] in build order (#299)
    this.propCells = new Set();       // every filled cell a prop wrote
    this._recording = [];             // the key sets of the props being built
  }

  get size() { return this.cells.size; }

  put(x, y, z, colour, kind = 'matte') {
    const k = key(x, y, z);
    if (colour === null || colour === undefined) { this.cells.delete(k); this.propCells.delete(k); return this; }
    if (!KINDS.includes(kind)) throw new Error(`unknown voxel kind '${kind}' -- use one of ${KINDS.join(', ')}`);
    this.cells.set(k, { colour: normalise(colour), kind });
    // #299: a write inside g.prop() belongs to that prop (and to every prop it
    // is nested in); a write outside any prop takes the cell back for the terrain.
    if (this._recording.length) { for (const keys of this._recording) keys.add(k); this.propCells.add(k); }
    else this.propCells.delete(k);
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
        for (let z = Math.min(z0, z1); z <= Math.max(z0, z1); z++) {
          const k = key(x, y, z);
          this.cells.delete(k); this.propCells.delete(k);
        }
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

  // #299. A NAMED PROP: every cell `build(grid)` fills is recorded under `name`
  // (a crate, a lantern, a grass tuft, a tree). Everything built OUTSIDE a prop
  // is terrain -- the base the props stand on. Props nest; names may repeat.
  // Returns { name, voxels, bbox: { min: [x,y,z], max: [x,y,z] } | null }.
  prop(name, build) {
    if (typeof name !== 'string' || !name.trim()) throw new Error('g.prop needs a name, e.g. g.prop(\'crate\', () => { ... })');
    if (typeof build !== 'function') throw new Error(`g.prop('${name}') needs a function that builds the prop`);
    const rec = { name: name.trim(), keys: new Set() };
    this._recording.push(rec.keys);
    try { build(this); } finally { this._recording.pop(); }
    this.props.push(rec);
    return summariseProp(this, rec);
  }

  // Every prop with the voxels it holds NOW (a later write outside any prop, or
  // a clear, takes cells away from it).
  propSummary() { return this.props.map((rec) => summariseProp(this, rec)); }

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

function summariseProp(grid, rec) {
  let voxels = 0;
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const k of rec.keys) {
    if (!grid.cells.has(k) || !grid.propCells.has(k)) continue;
    voxels++;
    const p = k.split(',').map(Number);
    for (let i = 0; i < 3; i++) { if (p[i] < min[i]) min[i] = p[i]; if (p[i] > max[i]) max[i] = p[i]; }
  }
  return { name: rec.name, voxels, bbox: voxels ? { min, max } : null };
}

// #299. The kind of a prop: its name in lower case without a trailing number
// ('rock 3', 'Rock#12', 'rock_7' -> 'rock'). Forty copies of one tuft are forty
// props but one kind.
export const propKind = (name) => String(name).toLowerCase().trim().replace(/[\s#_\-.\d]+$/, '') || String(name).toLowerCase().trim();

// #299. HOW MUCH OF THE GROUND IS DRESSED. Terrain = the grid's cells no prop
// wrote. Its top surface = per (x, z) column the highest terrain cell. A column
// is covered when any prop cell lies above that top cell in the same column
// (a tuft, a path set into the ground, a trunk, a canopy -- anything that hides
// the bare top from above). coverage = covered columns / terrain columns, null
// when there is no terrain (the base must be built outside g.prop).
export function coverage(grid) {
  const top = new Map(), propTop = new Map();
  for (const k of grid.cells.keys()) {
    const i = k.indexOf(','), j = k.lastIndexOf(',');
    const y = Number(k.slice(i + 1, j)), col = k.slice(0, i) + ',' + k.slice(j + 1);
    const m = grid.propCells.has(k) ? propTop : top;
    const have = m.get(col);
    if (have === undefined || y > have) m.set(col, y);
  }
  let covered = 0;
  for (const [col, y] of top) { const p = propTop.get(col); if (p !== undefined && p > y) covered++; }
  return { coverage: top.size ? covered / top.size : null, terrainColumns: top.size, coveredColumns: covered };
}

// #299. The page's mode from its query string: ?mode=live|photo (also 'foto'),
// &t=<seconds> (the pose; finite and >= 0, else 0), &ui=0 (no mode button).
export function parseMode(search = '') {
  const q = new URLSearchParams(search);
  const asked = (q.get('mode') || '').trim().toLowerCase();
  const photo = asked === 'photo' || asked === 'foto';
  const tv = Number(q.get('t'));
  return {
    mode: photo ? 'photo' : 'live',
    t: q.has('t') && Number.isFinite(tv) && tv >= 0 ? tv : 0,
    ui: q.get('ui') !== '0',
    forced: photo || asked === 'live',
  };
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

// Grid -> a THREE.Group of one Mesh per (kind, colour), in voxel units. With
// opts.cache (a Map) the main grid and every part share one material per
// (kind, colour) (#299). The meshes cast and receive shadows in live mode.
export function buildVoxelMeshes(grid, opts = {}) {
  const { buckets, faces, voxels } = meshVoxels(grid);
  const group = new THREE.Group();
  group.name = 'voxels';
  for (const b of buckets.values()) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(b.positions, 3));
    g.setAttribute('normal', new THREE.Float32BufferAttribute(b.normals, 3));
    const mk = b.kind + '|' + b.colour;
    let material = opts.cache ? opts.cache.get(mk) : null;
    if (!material) { material = materialFor(b.kind, b.colour, opts); if (opts.cache) opts.cache.set(mk, material); }
    const mesh = new THREE.Mesh(g, material);
    mesh.name = b.kind + ' ' + b.colour;
    mesh.castShadow = true; mesh.receiveShadow = true;
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

// ---- page errors (#299) -------------------------------------------------------
// Counted from the moment the kit is evaluated, which is before the scene's own
// module body runs: an exception in scene.js or in an animate callback shows up
// in window.__SCENE__.errors and in the [crow-scene] probe line.
const PAGE = { errors: 0 };
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('error', () => { PAGE.errors++; });
  window.addEventListener('unhandledrejection', () => { PAGE.errors++; });
}

// The mode button: hidden until a real pointer moves over the page, so a
// headless capture never shows it; ?ui=0 leaves it out altogether.
const UI_CSS = `.crow-holder{position:relative;touch-action:none;user-select:none;-webkit-user-select:none}
.crow-mode{position:absolute;right:10px;bottom:10px;font:12px/1 system-ui,sans-serif;letter-spacing:.02em;
padding:5px 10px;border-radius:12px;border:1px solid rgba(255,255,255,.4);background:rgba(20,20,24,.45);color:#fff;
cursor:pointer;opacity:0;pointer-events:none}
.crow-active:hover .crow-mode,.crow-mode:focus-visible{opacity:.85;pointer-events:auto}
@media (hover:none){.crow-active .crow-mode{opacity:.7;pointer-events:auto}}`;

// ---- the studio -------------------------------------------------------------
// A diorama around one grid: telephoto near-isometric PhysicalCamera with slight
// depth of field, a gradient dome, a ground plane in the background colour (soft
// ground shadow), a large soft key area light, ACES tone mapping -- the measured
// HD5 scene's numbers, scaled by the grid's size. LIVE mode rasterises it with
// shadow maps and runs the animate callbacks; PHOTO mode path-traces it (#299).
export function createDiorama(grid, opts = {}) {
  const o = {
    width: 1024, height: 1024, voxelSize: 0.4, background: '#ddd6c8',
    domeTop: '#fbf6ec', domeBottom: '#b9b1a3', domeIntensity: 0.55,
    keyColour: '#fff1dc', keyIntensity: 7.5, fov: 14, direction: [1, 0.82, 1], zoom: 1,
    fStop: 3.2, bounces: 6, despeckle: true, parent: document.body,
    shadows: true, shadowMapSize: 2048, shadowRadius: 4, liveLight: 1,
    orbit: true, minZoom: 0.5, maxZoom: 3, ...opts,
  };
  const S = o.voxelSize;
  const lights = [];                // { type, ..., rig }
  const parts = [];                 // { name, grid, pivot, group, built }
  const animators = [];
  const glow = {};                  // shade colour -> glow colour, from addLamp
  let emissiveIntensity = 0.8;
  let started = false;
  const api = { grid, options: o, lights, parts, THREE, voxelSize: S };

  // Voxel coordinates -> world, fixed from the grid's bounds when start() runs.
  let origin = null;
  const V = (x, y, z) => new THREE.Vector3((x - origin[0]) * S, (y - origin[1]) * S, (z - origin[2]) * S);
  api.voxelToWorld = (x, y, z) => V(x, y, z);

  // A light rig: a THREE.Group at the light's position that holds the light (and
  // its raster stand-in). The animate callback may rotate or move it; the rig is
  // empty until start() fills it.
  const rigFor = (type) => { const rig = new THREE.Group(); rig.name = type + ' rig'; return rig; };

  // A lamp: a SOLID emissive shade in the grid plus a spot light just BELOW it,
  // pointing down (rule 2). (x, z) is the shade's axis, bottom/top its y range.
  // Returns the light rig.
  api.addLamp = ({ x, z, bottom, top, radius = 4.5, shade = '#fff0d0', glow: glowColour = '#ffd9a0', light = '#ffc070',
                  intensity = 1600, glowIntensity = 0.8, cap = null, shadow = true } = {}) => {
    if ([x, z, bottom, top].some((v) => typeof v !== 'number')) throw new Error('addLamp needs x, z, bottom and top');
    grid.cyl(x, z, radius, bottom, top, shade, 'emit');
    grid.cyl(x, z, radius, top + 1, top + 1, cap || shade, 'matte');         // closed top
    glow[normalise(shade)] = normalise(glowColour);
    emissiveIntensity = Math.min(MAX_EMISSIVE, glowIntensity);
    const rig = rigFor('lamp');
    lights.push({ type: 'lamp', x, z, bottom, radius, light, intensity, shadow, rig });
    return rig;
  };

  // A soft rectangular (or circular) area light, e.g. daylight through a window.
  // Positions in voxels; width/height in voxels. Returns the light rig.
  api.addAreaLight = ({ at, lookAt, colour = '#dff1ff', intensity = 30, width = 18, height = 18, circular = false } = {}) => {
    if (!at || !lookAt) throw new Error('addAreaLight needs at: [x, y, z] and lookAt: [x, y, z]');
    const rig = rigFor('area');
    lights.push({ type: 'area', at, lookAt, colour, intensity, width, height, circular, rig });
    return rig;
  };

  // #299. A spot light at `at` aimed at `lookAt` (voxels): a lighthouse beam, a
  // street lamp, a torch. `angle` is the cone's half-angle in DEGREES, intensity
  // in candela at 0.4 world units per voxel (scaled like addLamp). Returns the
  // light rig: rotate it (rig.rotation.y = t) to sweep the beam around `at`.
  // Put it outside any hollow shade (rule 2): below or in front of solid voxels.
  api.addSpot = ({ at, lookAt, colour = '#fff1c8', intensity = 2000, angle = 20, penumbra = 0.4, radius = 1,
                  shadow = true } = {}) => {
    if (!at || !lookAt) throw new Error('addSpot needs at: [x, y, z] and lookAt: [x, y, z]');
    const rig = rigFor('spot');
    lights.push({ type: 'spot', at, lookAt, colour, intensity, angle, penumbra, radius, shadow, rig });
    return rig;
  };

  // #299. AN ANIMATED PART: its own VoxelGrid (same voxel coordinates as the
  // main grid), meshed into its own THREE.Group, returned for the animate
  // callback to move / rotate / show / hide. `pivot` (voxels, default: the
  // centre of the part's bounds) is the group's origin, so rotation turns the
  // part around it. group.userData.home holds the start position (world units;
  // one voxel = d.voxelSize). A hidden part is left out of the photo too.
  api.part = (name, partGrid, { pivot = null } = {}) => {
    if (typeof name !== 'string' || !name) throw new Error('d.part needs a name');
    if (!(partGrid instanceof VoxelGrid)) throw new Error(`d.part('${name}') needs its own VoxelGrid`);
    const group = new THREE.Group();
    group.name = 'part ' + name;
    const rec = { name, grid: partGrid, pivot, group, built: null };
    parts.push(rec);
    if (started) buildPart(rec);
    return group;
  };

  // #299. fn(t, dt, scene) runs before every live frame, and once at t when the
  // photo is posed. t = page-clock seconds; never Date or Math.random (rule 7).
  api.animate = (fn) => {
    if (typeof fn !== 'function') throw new Error('d.animate needs a function (t, dt, scene) => { ... }');
    animators.push(fn);
    return api;
  };

  let materialCache = null, scene = null, buildPart = null;

  api.start = () => {
    if (started) throw new Error('start() was called twice');
    const b = grid.bounds();
    if (!b) throw new Error('the grid is empty -- put voxels in it before start()');
    origin = [(b.min[0] + b.max[0] + 1) / 2, b.min[1], (b.min[2] + b.max[2] + 1) / 2];
    const ext = [(b.max[0] - b.min[0] + 1) * S, (b.max[1] - b.min[1] + 1) * S, (b.max[2] - b.min[2] + 1) * S];
    const L = Math.max(ext[0], ext[2]);
    const radius = 0.5 * Math.hypot(ext[0], ext[1], ext[2]);
    const url = parseMode(typeof location !== 'undefined' ? location.search : '');

    scene = new THREE.Scene();
    materialCache = new Map();
    const meshOpts = { emissiveIntensity, ...o, glow: { ...glow, ...(o.glow || {}) }, cache: materialCache };
    const built = buildVoxelMeshes(grid, meshOpts);
    built.group.scale.setScalar(S);
    built.group.position.set(-origin[0] * S, -origin[1] * S, -origin[2] * S);
    scene.add(built.group);

    buildPart = (rec) => {
      const pb = rec.grid.bounds();
      const pv = rec.pivot || (pb ? [(pb.min[0] + pb.max[0] + 1) / 2, (pb.min[1] + pb.max[1] + 1) / 2, (pb.min[2] + pb.max[2] + 1) / 2] : [0, 0, 0]);
      const inner = buildVoxelMeshes(rec.grid, meshOpts);
      inner.group.scale.setScalar(S);
      inner.group.position.set(-pv[0] * S, -pv[1] * S, -pv[2] * S);
      rec.group.add(inner.group);
      rec.group.position.copy(V(pv[0], pv[1], pv[2]));
      rec.group.userData.home = rec.group.position.clone();
      rec.built = inner;
      if (!rec.group.parent) scene.add(rec.group);
    };
    for (const rec of parts) buildPart(rec);

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(L * 16, L * 16),
      new THREE.MeshStandardMaterial({ color: o.background, roughness: 1 }));
    ground.name = 'ground';
    ground.rotation.x = -Math.PI / 2; ground.position.y = -0.001;
    ground.receiveShadow = true;
    scene.add(ground);

    const env = new GradientEquirectTexture();
    env.topColor.set(o.domeTop); env.bottomColor.set(o.domeBottom); env.update();
    scene.environment = env; scene.background = new THREE.Color(o.background);
    scene.environmentIntensity = o.domeIntensity; scene.backgroundIntensity = 1.0;

    // Lights. photoOnly / liveOnly are switched on the lights themselves (rule 9).
    const photoOnly = [], liveOnly = [];
    const shadowed = (light, far) => {
      light.castShadow = o.shadows;
      light.shadow.mapSize.set(1024, 1024);
      light.shadow.radius = o.shadowRadius;
      light.shadow.bias = -0.0004; light.shadow.normalBias = 0.05 * S;
      light.shadow.camera.near = 0.5 * S; light.shadow.camera.far = far;
    };

    // Key: a large soft area light up and to the right, scaled with the room.
    // Live stand-in: a directional light with the illuminance the area light
    // gives at the centre (luminance x area / distance^2) and a soft shadow map.
    const k = L / 25.6;
    const key1 = new ShapedAreaLight(new THREE.Color(o.keyColour), o.keyIntensity, 26 * k, 26 * k);
    key1.position.set(48 * k, 52 * k, 8 * k); key1.lookAt(0, 0, 0); key1.name = 'key';
    scene.add(key1); photoOnly.push(key1);
    const keyDist = key1.position.length();
    const sun = new THREE.DirectionalLight(o.keyColour, o.liveLight * o.keyIntensity * (26 * k) ** 2 / keyDist ** 2);
    sun.name = 'key (live)';
    sun.position.copy(key1.position);
    sun.castShadow = o.shadows;
    sun.shadow.mapSize.set(o.shadowMapSize, o.shadowMapSize);
    sun.shadow.radius = o.shadowRadius;
    sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.05 * S;
    Object.assign(sun.shadow.camera, { left: -radius * 1.15, right: radius * 1.15, top: radius * 1.15, bottom: -radius * 1.15,
      near: Math.max(0.1, keyDist - radius * 2), far: keyDist + radius * 2 });
    sun.shadow.camera.updateProjectionMatrix();
    scene.add(sun, sun.target); liveOnly.push(sun);

    const far = radius * 6;
    for (const l of lights) {
      const rig = l.rig;
      scene.add(rig);
      if (l.type === 'lamp') {
        // Candela: irradiance falls with distance squared, so the measured 1600
        // at 0.4 world units per voxel scales with the voxel size squared.
        const spot = new PhysicalSpotLight(l.light, l.intensity * (S / 0.4) ** 2, 0, Math.PI / 2.05, 0.8, 2);
        spot.radius = 3 * S; spot.name = 'lamp';
        rig.position.copy(V(l.x + 0.5, l.bottom - 0.8, l.z + 0.5));
        spot.target.position.copy(V(l.x + 0.5, b.min[1], l.z + 0.5)).sub(rig.position);
        if (l.shadow) shadowed(spot, far);
        rig.add(spot, spot.target);
      } else if (l.type === 'spot') {
        const spot = new PhysicalSpotLight(l.colour, l.intensity * (S / 0.4) ** 2, 0,
          Math.min(89, Math.max(1, l.angle)) * Math.PI / 180, l.penumbra, 2);
        spot.radius = l.radius * S; spot.name = 'spot';
        rig.position.copy(V(...l.at));
        spot.target.position.copy(V(...l.lookAt)).sub(rig.position);
        if (l.shadow) shadowed(spot, far);
        rig.add(spot, spot.target);
      } else {
        rig.position.copy(V(...l.at));
        const a = new ShapedAreaLight(new THREE.Color(l.colour), l.intensity, l.width * S, l.height * S);
        a.isCircular = !!l.circular; a.name = 'area';
        rig.add(a);
        rig.updateMatrixWorld(true);
        a.lookAt(V(...l.lookAt));
        photoOnly.push(a);
        // Live stand-in: a wide soft spot with the area light's luminous
        // intensity along its axis (luminance x area, Lambertian), no shadow.
        const stand = new THREE.SpotLight(l.colour, o.liveLight * l.intensity * (l.width * S) * (l.height * S) * (l.circular ? Math.PI / 4 : 1),
          0, Math.PI / 2.3, 1, 2);
        stand.name = 'area (live)';
        stand.target.position.copy(V(...l.lookAt)).sub(rig.position);
        rig.add(stand, stand.target);
        liveOnly.push(stand);
      }
    }

    // Camera: telephoto PhysicalCamera, near-isometric, slight depth of field.
    const cam = new PhysicalCamera(o.fov, o.width / o.height, 1, 10000);
    const target = new THREE.Vector3(0, ext[1] * 0.4, 0);
    const dist = (radius / Math.sin((o.fov * Math.PI) / 360)) * 1.02 / o.zoom;
    cam.position.copy(target).addScaledVector(new THREE.Vector3(...o.direction).normalize(), dist);
    cam.lookAt(target);
    cam.near = Math.max(0.1, dist - radius * 4); cam.far = dist * 4;
    cam.updateProjectionMatrix();
    cam.focusDistance = dist; cam.fStop = o.fStop; cam.apertureBlades = 6;

    const problems = sceneProblems(scene);
    if (problems.length) throw new Error('the scene would not path-trace cleanly:\n  ' + problems.join('\n  '));

    const renderer = new THREE.WebGLRenderer({ antialias: false, preserveDrawingBuffer: true });
    renderer.setPixelRatio(1); renderer.setSize(o.width, o.height);
    renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.shadowMap.enabled = !!o.shadows; renderer.shadowMap.type = THREE.PCFShadowMap;      // rule 8
    if (!document.getElementById('crow-kit-css')) {
      const css = document.createElement('style'); css.id = 'crow-kit-css'; css.textContent = UI_CSS;
      document.head.appendChild(css);
    }
    const holder = document.createElement('div');
    holder.className = 'crow-holder';
    Object.assign(holder.style, { width: o.width + 'px', height: o.height + 'px' });
    holder.appendChild(renderer.domElement);
    o.parent.appendChild(holder);
    const gl = renderer.getContext();
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');

    // ---- probes: what a checker or a judge reads ----------------------------
    const probe = window.__PT__ = {
      renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      mode: url.mode, samples: 0, samplesPerSecond: 0, seconds: 0, fps: 0, lost: 0, restored: 0, despeckled: 0, error: null,
    };
    const staticVoxels = built.voxels;
    const info = window.__SCENE__ = {
      voxels: 0, staticVoxels, faces: 0, triangles: 0, materials: 0,
      lights: lights.map((l) => l.type), size: { x: b.max[0] - b.min[0] + 1, y: b.max[1] - b.min[1] + 1, z: b.max[2] - b.min[2] + 1 },
      vertexColors: false, props: [], propCount: 0, propKinds: 0, propVoxels: 0,
      coverage: null, terrainColumns: 0, coveredColumns: 0, parts: [], animated: false, mode: url.mode, t: url.t, errors: 0,
    };
    const refreshInfo = () => {
      const all = grid.propSummary().filter((p) => p.voxels > 0);
      for (const rec of parts) for (const p of rec.grid.propSummary()) if (p.voxels > 0) all.push({ ...p, part: rec.name });
      const cov = coverage(grid);
      const partInfo = parts.filter((r) => r.built).map((r) => ({ name: r.name, voxels: r.built.voxels, triangles: r.built.triangles }));
      Object.assign(info, {
        voxels: staticVoxels + partInfo.reduce((n, p) => n + p.voxels, 0),
        faces: built.faces + parts.reduce((n, r) => n + (r.built ? r.built.faces : 0), 0),
        triangles: built.triangles + partInfo.reduce((n, p) => n + p.triangles, 0),
        materials: materialCache.size, props: all, propCount: all.length,
        propKinds: new Set(all.map((p) => propKind(p.name))).size,
        propVoxels: all.reduce((n, p) => n + p.voxels, 0),
        coverage: cov.coverage === null ? null : +cov.coverage.toFixed(4),
        terrainColumns: cov.terrainColumns, coveredColumns: cov.coveredColumns,
        parts: partInfo, animated: animators.length > 0, errors: PAGE.errors,
      });
    };
    refreshInfo();
    // The one line a checker parses (render_page carries the console's last lines).
    const probeLine = () => {
      info.errors = PAGE.errors;
      console.log('[crow-scene] ' + JSON.stringify({
        mode: info.mode, t: +info.t.toFixed(3), voxels: info.voxels, staticVoxels, triangles: info.triangles,
        props: info.propCount, propKinds: info.propKinds, propVoxels: info.propVoxels, coverage: info.coverage,
        terrainColumns: info.terrainColumns, parts: info.parts.length, animated: info.animated, errors: info.errors,
        samples: probe.samples, samplesPerSecond: probe.samplesPerSecond, fps: probe.fps, lost: probe.lost,
        restored: probe.restored, renderer: probe.renderer, error: probe.error,
      }));
    };

    // ---- animation: page clock only (rule 7) ---------------------------------
    let failures = 0;
    const runAnimators = (t, dt) => {
      for (const fn of animators) {
        try { fn(t, dt, scene); } catch (e) {
          PAGE.errors++;
          if (failures++ < 3) console.error('[crow-error] animate callback threw at t=' + t.toFixed(3) + ': ' + (e && e.stack || e));
        }
      }
    };

    // ---- modes ---------------------------------------------------------------
    let mode = url.mode, t = url.t, lastT = url.t;
    let tBase = t - performance.now() / 1000;        // t = tBase + page seconds
    let gen = 0;                                      // one render loop at a time
    let pt = null, t0 = 0, liveFrames = 0, fpsFrom = performance.now(), firstLive = true;
    const setLights = (m) => {
      for (const l of photoOnly) l.visible = m === 'photo';
      for (const l of liveOnly) l.visible = m === 'live';
    };
    const ensurePT = () => {
      if (pt) return pt;
      pt = new WebGLPathTracer(renderer);
      pt.bounces = o.bounces; pt.minSamples = 1; pt.renderDelay = 0; pt.fadeDuration = 0; pt.tiles.set(2, 2);
      return pt;
    };
    let overlay = null, octx = null;
    if (o.despeckle) {
      overlay = document.createElement('canvas');
      overlay.width = o.width; overlay.height = o.height;
      Object.assign(overlay.style, { position: 'absolute', left: '0', top: '0', pointerEvents: 'none', display: 'none' });
      holder.appendChild(overlay);
      octx = overlay.getContext('2d', { willReadFrequently: true });
    }

    const liveLoop = (my) => (ts) => {
      if (my !== gen) return;
      const now = typeof ts === 'number' ? ts : performance.now();
      t = Math.max(lastT, tBase + now / 1000);
      runAnimators(t, t - lastT); lastT = t;
      info.t = t;
      renderer.render(scene, cam);
      liveFrames++;
      if (firstLive) { firstLive = false; probeLine(); }
      requestAnimationFrame(liveLoop(my));
    };
    const photoLoop = (my) => () => {
      if (my !== gen) return;
      pt.renderSample();
      probe.samples = pt.samples;
      probe.seconds = (performance.now() - t0) / 1000;
      probe.samplesPerSecond = probe.seconds > 0 ? +(probe.samples / probe.seconds).toFixed(1) : 0;
      requestAnimationFrame(photoLoop(my));
    };

    const enterLive = () => {
      mode = 'live'; gen++;
      probe.mode = info.mode = 'live';
      setLights('live');
      if (overlay) overlay.style.display = 'none';
      tBase = t - performance.now() / 1000; lastT = t;
      liveFrames = 0; fpsFrom = performance.now();
      renderer.render(scene, cam);                    // a first frame even before rAF
      requestAnimationFrame(liveLoop(gen));
    };
    const enterPhoto = () => {
      mode = 'photo'; gen++;
      probe.mode = info.mode = 'photo';
      setLights('photo');
      ensurePT();
      try { pt.setScene(scene, cam); pt.reset(); probe.error = null; } catch (e) { probe.error = String(e); console.error('[crow-pt] setScene failed:', e); }
      probe.samples = 0; t0 = performance.now();
      if (overlay) { octx.clearRect(0, 0, o.width, o.height); overlay.style.display = ''; }
      requestAnimationFrame(photoLoop(gen));
    };
    const setMode = (m) => {
      if (m !== 'live' && m !== 'photo') throw new Error(`mode '${m}' -- use 'live' or 'photo'`);
      if (m === mode && started) return;
      if (m === 'photo') enterPhoto(); else enterLive();
      if (button) button.textContent = m === 'photo' ? 'Live' : 'Foto';
      probeLine();
    };

    // Context loss: keep the page alive and set the scene again when it is back.
    renderer.domElement.addEventListener('webglcontextlost', (e) => { e.preventDefault(); probe.lost++; });
    renderer.domElement.addEventListener('webglcontextrestored', () => {
      probe.restored++;
      if (mode === 'photo' && pt) { try { pt.setScene(scene, cam); pt.reset(); } catch (e) { probe.error = String(e); } }
    });

    // Despeckle on the overlay canvas, once a second, photo mode only (rule 5).
    if (overlay) setInterval(() => {
      if (mode !== 'photo' || !pt || pt.samples <= 8) return;
      octx.drawImage(renderer.domElement, 0, 0);
      const img = octx.getImageData(0, 0, o.width, o.height);
      probe.despeckled = despeckle(img.data, o.width, o.height);
      octx.putImageData(img, 0, 0);
    }, 1000);

    // ---- orbit: drag / touch turns around the target, wheel or pinch zooms ----
    const sph = new THREE.Spherical().setFromVector3(cam.position.clone().sub(target));
    const minR = dist / o.maxZoom, maxR = dist / o.minZoom;
    const applyOrbit = () => {
      sph.phi = Math.min(Math.PI / 2 - 0.02, Math.max(0.12, sph.phi));
      sph.radius = Math.min(maxR, Math.max(minR, sph.radius));
      cam.position.setFromSpherical(sph).add(target);
      cam.lookAt(target);
      cam.focusDistance = sph.radius;
      cam.near = Math.max(0.1, sph.radius - radius * 4); cam.far = sph.radius + radius * 4;
      cam.updateProjectionMatrix();
      if (mode === 'photo' && pt) { pt.updateCamera(); probe.samples = 0; t0 = performance.now(); if (octx) octx.clearRect(0, 0, o.width, o.height); }
    };
    const pointers = new Map();
    let pinch = 0;
    const activate = () => holder.classList.add('crow-active');
    holder.addEventListener('pointermove', activate);
    if (o.orbit) {
      holder.addEventListener('pointerdown', (e) => {
        if (e.target === button) return;
        activate();
        try { holder.setPointerCapture(e.pointerId); } catch (_) { /* a synthetic event has no capturable pointer */ }
        pointers.set(e.pointerId, [e.clientX, e.clientY]);
        if (pointers.size === 2) { const [a, c] = [...pointers.values()]; pinch = Math.hypot(a[0] - c[0], a[1] - c[1]); }
      });
      holder.addEventListener('pointermove', (e) => {
        const was = pointers.get(e.pointerId);
        if (!was) return;
        const dx = e.clientX - was[0], dy = e.clientY - was[1];
        pointers.set(e.pointerId, [e.clientX, e.clientY]);
        if (pointers.size === 1) {
          sph.theta -= dx * 0.006; sph.phi -= dy * 0.006;
        } else if (pointers.size === 2) {
          const [a, c] = [...pointers.values()];
          const d2 = Math.hypot(a[0] - c[0], a[1] - c[1]);
          if (pinch > 0 && d2 > 0) sph.radius *= pinch / d2;
          pinch = d2;
        }
        applyOrbit();
      });
      const up = (e) => { pointers.delete(e.pointerId); pinch = 0; };
      holder.addEventListener('pointerup', up);
      holder.addEventListener('pointercancel', up);
      holder.addEventListener('wheel', (e) => {
        e.preventDefault();
        sph.radius *= Math.exp(Math.max(-100, Math.min(100, e.deltaY)) * 0.0015);
        applyOrbit();
      }, { passive: false });
    }

    // ---- toggle: key P, and the small button -----------------------------------
    let button = null;
    if (url.ui) {
      button = document.createElement('button');
      button.className = 'crow-mode'; button.type = 'button';
      button.title = 'P: switch between the live preview and the path-traced photo';
      button.textContent = url.mode === 'photo' ? 'Live' : 'Foto';
      button.addEventListener('click', (e) => { e.stopPropagation(); setMode(mode === 'photo' ? 'live' : 'photo'); });
      holder.appendChild(button);
    }
    window.addEventListener('keydown', (e) => {
      activate();
      if ((e.key === 'p' || e.key === 'P') && !e.ctrlKey && !e.metaKey && !e.altKey) setMode(mode === 'photo' ? 'live' : 'photo');
    });

    // ---- go ------------------------------------------------------------------
    // Pose the scene at t first: the photo shows exactly that instant, and the
    // live preview's first frame too.
    runAnimators(t, 0);
    started = true;
    if (mode === 'photo') enterPhoto(); else enterLive();
    refreshInfo();

    console.log(`[crow-pt] ${info.voxels} voxels, ${info.triangles} triangles, ${info.materials} materials, renderer: ${probe.renderer}`);
    probeLine();
    setInterval(() => {
      if (mode === 'photo') {
        console.log(`[crow-pt] samples=${probe.samples} spp/s=${probe.samplesPerSecond} lost=${probe.lost} despeckled=${probe.despeckled}`);
      } else {
        const now = performance.now();
        probe.fps = now > fpsFrom ? +(liveFrames * 1000 / (now - fpsFrom)).toFixed(1) : 0;
        liveFrames = 0; fpsFrom = now;
        console.log(`[crow-live] fps=${probe.fps} t=${t.toFixed(2)} lost=${probe.lost}`);
      }
      probeLine();
    }, 5000);

    Object.assign(api, { scene, camera: cam, renderer, probe, voxels: built, info, setMode, mode: () => mode, time: () => t });
    // The path tracer exists from the first photo on (null in live until then).
    Object.defineProperty(api, 'pathTracer', { get: () => pt, enumerable: true });
    return api;
  };

  return api;
}
