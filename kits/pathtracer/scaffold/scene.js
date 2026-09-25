// Scaffold: a small cut-away room with one animated part. Copy this folder,
// then replace the content section with your own scene. Keep the rules in the
// kit's SKILL.md: room >= 64 voxels wide, every prop >= 8-16 voxels per
// dimension, every object inside g.prop('name', ...), one palette colour per
// material (never vertexColors), lamps through addLamp, emission <= 1, time
// only from the animate callback's t.
// The page opens LIVE (the island path-traced, the moving parts rasterised on
// top); P or ?mode=photo&t=2 path-traces all of it, ?mode=raster is the flat
// preview. width/height is the capture canvas (ui=0); a person's window fills
// the screen at the display's pixel ratio.
import { VoxelGrid, createDiorama, hash } from 'crow-voxel-kit';

// ---- palette: few, clean colours (MagicaVoxel style) -----------------------
const P = {
  wall: '#48c3b1', wall2: '#43bcaa', side: '#3fb5a4', side2: '#3bb0a0',
  wood: ['#c9622d', '#d26e35', '#bf5a27', '#cc6630'], groove: '#a94f22',
  white: '#f4f1ea', cream: '#fbf3d6', blue: '#2f93b6', blueD: '#26809f',
  yellow: '#f2c430', orange: '#ef8a2b', red: '#e2493b', green: '#58a34a', green2: '#3f8b3a',
  brown: '#8a5a3a', brass: '#caa24a', glass: '#cfe9f3',
};

const g = new VoxelGrid();
const N = 64, H = 48;                      // floor 64 x 64, walls 48 high

// ---- shell: plank floor, two walls (back z = 0..1, left x = 0..1) ----------
g.box(0, 0, 0, N - 1, 1, N - 1, (x, y, z) => {
  const p = Math.floor(x / 6), off = (p * 11) % 20;
  if (y === 1 && (x % 6 === 5 || (z + off) % 20 === 19)) return P.groove;
  return P.wood[(p + Math.floor((z + off) / 20) * 3) % 4];
});
g.box(0, 2, 0, N - 1, H, 1, (x, y) => (hash(x >> 1, y >> 1, 0) < 0.5 ? P.wall : P.wall2));
g.box(0, 2, 0, 1, H, N - 1, (x, y, z) => (hash(0, y >> 1, z >> 1) < 0.5 ? P.side : P.side2));
g.box(2, 2, 2, N - 1, 4, 2, P.white); g.box(2, 2, 2, 2, 4, N - 1, P.white);     // skirting

// ---- window in the back wall: frame, mullion, glass-free opening -----------
g.clear(36, 22, 0, 53, 39, 1);
g.box(35, 21, 0, 54, 21, 3, P.white); g.box(35, 40, 0, 54, 41, 2, P.white);
g.box(35, 22, 0, 35, 39, 2, P.white); g.box(54, 22, 0, 54, 39, 2, P.white);
g.box(44, 22, 1, 45, 39, 1, P.white);

// ---- props: each one inside g.prop('name', ...), >= 8 voxels per dimension --
// Everything built outside g.prop is terrain (here: floor and walls).
g.prop('cabinet', () => {                   // against the left wall, two drawers with knobs
  g.box(2, 2, 10, 13, 22, 28, P.blue);
  g.box(14, 14, 11, 14, 20, 18, P.blueD); g.box(14, 14, 20, 14, 20, 27, P.blueD);
  g.put(15, 17, 14, P.orange); g.put(15, 17, 24, P.orange);
});
g.prop('round table', () => {               // with a bowl of fruit
  for (const [dx, dz] of [[-4, -4], [4, -4], [-4, 4], [4, 4]]) g.box(24 + dx, 2, 40 + dz, 24 + dx, 11, 40 + dz, P.white);
  g.cyl(24, 40, 7, 12, 12, P.white); g.cyl(24, 40, 2.8, 13, 14, P.cream);
  for (const [dx, dz, c] of [[-1, 0, P.red], [1, 0, P.orange], [0, 1, P.yellow], [0, -1, P.green]]) g.put(24 + dx, 15, 40 + dz, c);
});
g.prop('potted plant', () => { g.cyl(8, 50, 3, 2, 8, P.brown); g.blob(8, 14, 50, 5, 6, 5, (x, y, z) => (hash(x, y, z) < 0.5 ? P.green : P.green2)); });
g.prop('glass vase', () => g.cyl(40, 4, 1.6, 22, 26, P.glass, 'glass'));
// floor lamp: brass base and pole; addLamp adds the solid glowing shade + light
const LX = 54, LZ = 48;
g.prop('floor lamp', () => { g.cyl(LX, LZ, 3, 2, 3, P.brass, 'metal'); g.box(LX, 4, LZ, LX, 33, LZ, P.brass, 'metal'); });
// pinwheel stand on the cabinet: the pole is a prop, the wheel an animated part
const HX = 14, HY = 36, HZ = 19;            // hub
g.prop('pinwheel stand', () => { g.box(8, 23, HZ, 8, HY, HZ, P.white); g.box(9, HY, HZ, HX - 1, HY, HZ, P.white); });

// ---- animated part: its own grid, turned by d.animate -----------------------
const wheel = new VoxelGrid();
wheel.box(HX, HY - 1, HZ - 1, HX, HY + 1, HZ + 1, P.yellow);                    // hub
wheel.box(HX, HY + 2, HZ - 2, HX, HY + 9, HZ, P.red);                           // four blades,
wheel.box(HX, HY - 9, HZ, HX, HY - 2, HZ + 2, P.blue);                          // one colour each
wheel.box(HX, HY, HZ + 2, HX, HY + 2, HZ + 9, P.orange);
wheel.box(HX, HY - 2, HZ - 9, HX, HY, HZ - 2, P.green);

// ---- studio: camera, dome, ground, key light; lamp and window light --------
const d = createDiorama(g, { width: 1024, height: 1024, background: '#ddd6c8' });
d.addLamp({ x: LX, z: LZ, bottom: 34, top: 42 });
d.addAreaLight({ at: [45, 31, -4], lookAt: [45, 18, 20], colour: '#dff1ff', intensity: 30, width: 18, height: 18 });
const pinwheel = d.part('pinwheel', wheel, { pivot: [HX + 0.5, HY + 0.5, HZ + 0.5] });
d.animate((t) => { pinwheel.rotation.x = -t * (2 * Math.PI / 3); });       // one turn in 3 s
d.start();
