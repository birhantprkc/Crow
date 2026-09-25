[← README](../../README.md) · [Docs index](../README.md)

# Voxel kit (path tracing)

Path-traced voxel scenes (the MagicaVoxel look) as one offline HTML page (#298). The model writes
only the content: voxels, a palette, lamps. Global illumination, soft and contact shadows, colour
bleeding and the lamp glow come from a vendored path tracer. The model never writes a ray tracer
or a shader.

<div align="center">
<img src="../images/voxel-kit-scaffold.png" alt="The kit's scaffold room, path traced: teal walls, plank floor, cabinet, table, plant, floor lamp, window" width="480">
</div>

## Switch it on

| | |
|---|---|
| Linux | `bash install.sh --pathtracer` |
| Windows | `&([scriptblock]::Create((irm https://raw.githubusercontent.com/nibor1896/Crow/main/install.ps1))) -PathTracer` |
| By hand | Settings → Skills → `voxel-diorama` on |

| | |
|---|---|
| What ships | `kits/pathtracer/` in every install, ~1 MB. The flag installs nothing more |
| What the flag does | checks the bundle against `kit.json`, switches the `voxel-diorama` skill on, and reports whether `build_bundle` has an esbuild |
| Already on | `the voxel-diorama skill was already on` |
| Off | the skill is seeded **off** at Crow's next start: 0 prompt tokens until it is switched on |
| Needs | an esbuild for `build_bundle` ([install](install.md), row *esbuild*). Crow never downloads one |

## What the model does

| step | |
|---|---|
| 1 | reads the skill `voxel-diorama` |
| 2 | copies `kits/pathtracer/scaffold/{index.html,scene.js}` to `src/` in the working area |
| 3 | plans the grid, palette and props (density rule below) |
| 4 | writes `src/scene.js` with `crow-voxel-kit` |
| 5 | `build_bundle` entry `src/index.html`, out `index.html`: one offline file |
| 6 | `render_page index.html` with `wait_ms` 20000, reads the `[crow-pt]` console lines, then `read_image` against the reference |

`import … from 'crow-voxel-kit'` and `'crow-pathtracer'` are resolved by `build_bundle` to the
installed kit. The library never has to be in the working area. An import map in the page for
the same name wins.

## Rules (measured 2026-09-25, pt-proof, RTX 5090, 1024×1024)

| rule | why |
|---|---|
| room ≥ 64 voxels wide, every prop ≥ 8–16 voxels per dimension | coarse voxels make objects unreadable (robin: HD scene at 2.5× density "far more legible") |
| one `MeshStandardMaterial` per palette colour, never `vertexColors` | with vertex colours the room rendered black in 1 of 3 starts; palette materials 5 of 5 normal |
| lamps through `addLamp`: the spot light sits **below a solid** shade | a spot inside a hollow shade: 30.6 dB PSNR (fireflies); below a solid shade: 38.6 dB |
| emission ≤ 1 (the kit caps it) | emissive surfaces are not importance-sampled; bright small emitters are fireflies |
| culled faces, no instancing | the library does not support instanced geometry |
| despeckle pass | removes the isolated fireflies left after 2,700 samples (HD4 → HD5) |
| context lost → restored → scene set again | headless ANGLE/Vulkan lost the context once at start in 3 of 3 runs |

`start()` refuses a scene with `vertexColors`, a non-Standard material, an `InstancedMesh` or
`emissiveIntensity` > 1, and names the mesh.

## API

| | |
|---|---|
| `new VoxelGrid()` | integer cells, y up |
| `.put(x,y,z, colour, kind)` | `colour` `'#rrggbb'` or `(x,y,z) => colour \| null`; `kind` `matte` (default) · `metal` · `glass` · `emit` |
| `.box(x0,y0,z0, x1,y1,z1, …)` · `.clear(…)` | corners inclusive |
| `.cyl(cx,cz, r, y0,y1, …)` · `.blob(cx,cy,cz, rx,ry,rz, …)` | vertical cylinder · ellipsoid |
| `hash(x,y,z)` | stable value in [0,1) for colour variation |
| `createDiorama(grid, opts)` | `width` `height` (1024), `voxelSize` (0.4), `background` (`#ddd6c8`), `fov` (14), `direction` ([1, 0.82, 1]), `zoom`, `fStop` (3.2), `bounces` (6), `despeckle` (true) |
| `.addLamp({x, z, bottom, top})` | solid glowing shade from `bottom` to `top`, closed top, spot light below it |
| `.addAreaLight({at, lookAt, intensity, width, height})` | soft rectangle light, voxel coordinates, e.g. daylight outside a window |
| `.start()` | meshes, lights, camera, path tracer; returns `{scene, camera, renderer, pathTracer, probe}` |
| `window.__SCENE__` | `voxels` `faces` `triangles` `materials` `lights` `size` |
| `window.__PT__` | `renderer` `samples` `samplesPerSecond` `lost` `restored` `despeckled` `error` |

## Numbers

| | |
|---|---|
| samples/s | 13.0 at 10 s, 14.3 at 40 s, scaffold room (26,627 voxels, 46,168 triangles, 23 materials), headless Chromium 152, ANGLE/Vulkan, RTX 5090, 2026-09-25 |
| proof scene | ~15 samples/s at 9,124 and at 59,964 triangles; PSNR 60 s vs 180 s = 39.79 dB (HD5) |
| page size | 970,310 bytes (scaffold, built with esbuild 0.28.2) |
| bundle | `crow-pathtracer.js` 958,266 bytes, sha256 in `kits/pathtracer/kit.json` |

## Third party

three 0.186.1, three-mesh-bvh 0.9.15, three-gpu-pathtracer 0.0.24, all MIT. Licence texts beside
the bundle, entry in [NOTICE](../../NOTICE). Rebuild: `bash tools/build-pathtracer-kit.sh`
(exact npm pins). Check: `python tools/check_pathtracer_kit.py`.
