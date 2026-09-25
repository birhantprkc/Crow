---
name: voxel-diorama
description: When asked for a voxel diorama, voxel room or MagicaVoxel-look scene as one offline HTML page: path-trace it with Crow's voxel kit -- never hand-write a ray tracer or shaders.
---

Crow ships a path-tracing kit: three.js 0.186.1 + three-gpu-pathtracer 0.0.24 (pre-built)
and `crow-voxel-kit`, a voxel grid + mesher + studio. It gives global illumination, soft
and contact shadows, colour bleeding and a lamp glow with no shader code of your own.
Kit folder: `@CROW_KITS@/pathtracer` (scaffold in `scaffold/`, API in `voxel-kit.js`).

## Steps

1. Copy the scaffold. `read_file` `@CROW_KITS@/pathtracer/scaffold/index.html` and
   `.../scaffold/scene.js`, `write_file` them as `src/index.html` and `src/scene.js` in the
   working area. Do NOT copy `crow-pathtracer.js`: `build_bundle` resolves the bare imports
   `crow-voxel-kit` and `crow-pathtracer` to the kit itself.
2. Plan on paper first (a short list in your reply): grid size, a palette of named hex
   colours, every prop with its voxel box. DENSITY RULE: a room is at least 64 voxels wide
   (walls ~48 high); every prop is at least 8-16 voxels in each dimension. Coarse voxels
   make objects unreadable -- a 3-voxel chair is a blob.
3. Write the scene in `src/scene.js`, content section only:
   - `const g = new VoxelGrid()`; `g.box(x0,y0,z0, x1,y1,z1, colour, kind)`, `g.put`,
     `g.cyl(cx,cz,r, y0,y1, ...)`, `g.blob(cx,cy,cz, rx,ry,rz, ...)`, `g.clear(...)`.
     y is up; corners inclusive. `colour` is `'#rrggbb'` or `(x,y,z) => colour|null`
     (use `hash(x,y,z)` for variation). `kind`: `'matte'` (default), `'metal'`, `'glass'`,
     `'emit'`.
   - `const d = createDiorama(g, { width: 1024, height: 1024, background: '#ddd6c8' })`.
   - Lamps: `d.addLamp({ x, z, bottom, top })` -- it builds the SOLID glowing shade and puts
     the spot light just below it. Build the pole/base yourself up to `bottom - 1`.
   - Daylight: `d.addAreaLight({ at: [x,y,z], lookAt: [x,y,z], intensity: 30, width: 18, height: 18 })`
     just outside a window opening.
   - `d.start()` last. It throws, with the reason, on anything that would render wrong.
4. Build: `build_bundle` entry `src/index.html`, out `index.html`. That one file is the
   deliverable; it opens offline from file://. Edit `src/`, build again, never patch
   `index.html`.
5. Verify: `render_page` `index.html`, `wait_ms` 20000, `width`/`height` = the canvas size,
   one frame. Read the console lines `[crow-pt] ...`:
   - `renderer:` must name the GPU (NVIDIA/AMD/Intel), not SwiftShader/llvmpipe;
   - `samples=` should be >= 200 at 20 s (measured ~15 samples/s at 1024x1024 on an
     RTX 5090; 60k triangles cost the same as 9k); `lost=0`, or a restore followed.
   Then `read_image` the screenshot and check, item by item, against the user's reference
   image if there is one: colour bleeding from walls onto floor, dark contact shadows in
   creases, soft shadow edges, a warm light pool under each lamp, clean background with a
   soft ground shadow, no grain or bright specks, every prop recognisable by name.
   `window.__SCENE__` (voxels, triangles, materials) and `window.__PT__` (samples,
   samplesPerSecond, renderer, lost) hold the same numbers for a checker.
6. Fix what the check found in `src/scene.js` and repeat 4-5. Change content and light,
   not the kit.

## Traps (each one measured, 2026-09-25)

- vertexColors: the room rendered BLACK in 1 of 3 starts. The kit makes one material per
  palette colour; never add a mesh with `vertexColors`, InstancedMesh or a non-Standard
  material (`start()` refuses them).
- A spot light inside a hollow lamp shade: persistent fireflies (PSNR 30.6 dB); below a
  solid shade: 38.6 dB. Use `addLamp`, do not place lamp lights by hand.
- Bright emissive voxels are fireflies (the library does not importance-sample them).
  Keep `emit` dim (the kit caps emissiveIntensity at 1) and light with lamps/area lights.
- The source page `src/index.html` shows nothing from file:// (module imports are
  blocked). Always render the built `index.html`.
- `build_bundle` needs an esbuild on this machine. If it reports none, tell the user --
  do not concatenate the library by hand.
