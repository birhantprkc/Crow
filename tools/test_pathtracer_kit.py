#!/usr/bin/env python3
"""The path-tracing kit's own suite (#298): the voxel mesher and the
checker that holds the vendored bundle together.

THE MESHER IS DRIVEN IN NODE, against the real kits/pathtracer/voxel-kit.js and
the real vendored three.js -- not a Python re-implementation of it, which would
test a second mesher. The cases pin what the pt-proof runs measured:

  * culled faces: a face exists only where the neighbour is empty (counts);
  * ONE MeshStandardMaterial PER PALETTE COLOUR and no vertex colours -- with
    vertex colours the room rendered black in 1 of 3 starts (2026-09-25);
  * the winding agrees with the normal on every triangle (a flipped face is a
    hole to a path tracer);
  * emission is capped at 1 and a scene that breaks a rule is refused;
  * despeckle removes a lone bright pixel and keeps an edge.

Skipped (not failed) when node is not on PATH, like #251's node --check case.

THE CHECKER CASES each break one link of the chain and require red: a changed
byte in the bundle, a missing licence text, a NOTICE without the version, a
vertexColors: true in the kit.

Usage:  python tools/test_pathtracer_kit.py      (unittest; exit 0 = green)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
KIT = os.path.join(REPO, "kits", "pathtracer")
sys.path.insert(0, HERE)
import check_pathtracer_kit as checker  # noqa: E402

NODE = shutil.which("node")

HARNESS = r"""
const kit = await import(process.argv[1]);
const { VoxelGrid, meshVoxels, buildVoxelMeshes, materialFor, sceneProblems, despeckle, THREE } = kit;
const out = {};
const faces = (fill) => { const g = new VoxelGrid(); fill(g); return meshVoxels(g).faces; };
out.one = faces((g) => g.put(0, 0, 0, '#ff0000'));
out.two = faces((g) => g.box(0, 0, 0, 1, 0, 0, '#ff0000'));
out.cube = faces((g) => g.box(0, 0, 0, 2, 2, 2, '#ff0000'));
out.hollow = faces((g) => g.box(0, 0, 0, 2, 2, 2, '#ff0000').clear(1, 1, 1, 1, 1, 1));
out.nullCell = (() => { const g = new VoxelGrid(); g.box(0, 0, 0, 3, 0, 0, (x) => (x % 2 ? null : '#00ff00')); return g.size; })();

// two colours side by side: one bucket each, the shared face culled on both sides
const g = new VoxelGrid();
g.put(0, 0, 0, '#ff0000'); g.put(1, 0, 0, '#0000FF'); g.put(2, 0, 0, '#0000ff', 'metal');
const m = meshVoxels(g);
out.buckets = [...m.buckets.values()].map((b) => [b.kind, b.colour, b.faces]).sort();
const built = buildVoxelMeshes(g);
out.meshes = built.group.children.map((c) => ({
  type: c.material.type, vertexColors: c.material.vertexColors,
  attrs: Object.keys(c.geometry.attributes).sort(), colour: '#' + c.material.color.getHexString(),
  metalness: c.material.metalness,
})).sort((a, b) => (a.colour + a.metalness).localeCompare(b.colour + b.metalness));
out.materials = built.materials; out.triangles = built.triangles;

// winding vs normal, every triangle of a mixed shape
{
  const w = new VoxelGrid();
  w.box(0, 0, 0, 4, 1, 3, '#abcdef'); w.blob(2, 4, 2, 2, 2, 2, '#123456'); w.clear(1, 0, 1, 1, 1, 1);
  let bad = 0, total = 0;
  for (const b of meshVoxels(w).buckets.values()) {
    const p = b.positions, n = b.normals;
    for (let i = 0; i < p.length; i += 9) {
      const e1 = [p[i + 3] - p[i], p[i + 4] - p[i + 1], p[i + 5] - p[i + 2]];
      const e2 = [p[i + 6] - p[i], p[i + 7] - p[i + 1], p[i + 8] - p[i + 2]];
      const c = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]];
      if (c[0] * n[i] + c[1] * n[i + 1] + c[2] * n[i + 2] <= 0) bad++;
      total++;
    }
  }
  out.winding = { bad, total };
}

out.emitCapped = materialFor('emit', '#ffffff', { emissiveIntensity: 5 }).emissiveIntensity;
out.glass = materialFor('glass', '#cfe9f3').type;
{
  const sc = new THREE.Scene();
  sc.add(new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial({ vertexColors: true })));
  sc.add(new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshBasicMaterial()));
  sc.add(new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial({ emissive: 0xffffff, emissiveIntensity: 4 })));
  out.problems = sceneProblems(sc).map((s) => s.split(' -- ')[0].split(': ')[1]);
  const ok = new THREE.Scene(); ok.add(buildVoxelMeshes(g).group);
  out.cleanProblems = sceneProblems(ok).length;
}

// despeckle: one white pixel in grey is fixed; a black/white edge is kept
{
  const W = 5, H = 5, img = new Uint8ClampedArray(W * H * 4).fill(100);
  const c = (2 * W + 2) * 4; img[c] = img[c + 1] = img[c + 2] = 255;
  out.speck = [despeckle(img, W, H), img[c]];
  const e = new Uint8ClampedArray(W * H * 4);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { const i = (y * W + x) * 4; e[i] = e[i + 1] = e[i + 2] = x < 2 ? 0 : 255; }
  out.edge = despeckle(e, W, H);
}
const bad = [];
try { new VoxelGrid().put(0, 0, 0, 'red'); } catch (e) { bad.push('colour'); }
try { new VoxelGrid().put(0, 0, 0, '#ff0000', 'shiny'); } catch (e) { bad.push('kind'); }
out.refused = bad;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not on PATH")
class VoxelKitTests(unittest.TestCase):
    """The mesher and the materials, run in node against the real files."""

    @classmethod
    def setUpClass(cls):
        url = "file://" + os.path.join(KIT, "voxel-kit.js").replace("\\", "/")
        if not url.startswith("file:///"):
            url = url.replace("file://", "file:///", 1)
        proc = subprocess.run([NODE, "--input-type=module", "-e", HARNESS, url],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError("the node harness failed:\n" + proc.stderr[-2000:])
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_culled_face_counts(self):
        """6 for one voxel, 10 for two touching, 54 for a solid 3x3x3 (the 26
        inner faces culled), 60 with the centre hollowed out (6 faces inside)."""
        o = self.out
        self.assertEqual((o["one"], o["two"], o["cube"], o["hollow"]), (6, 10, 54, 60))

    def test_a_colour_function_returning_null_leaves_the_cell_empty(self):
        self.assertEqual(self.out["nullCell"], 2)

    def test_one_bucket_and_one_material_per_kind_and_colour(self):
        """'#0000FF' and '#0000ff' are ONE colour (normalised), the metal one is
        its own material; the faces between the three voxels are culled."""
        self.assertEqual(self.out["buckets"], [["matte", "#0000ff", 4], ["matte", "#ff0000", 5],
                                               ["metal", "#0000ff", 5]])
        self.assertEqual(self.out["materials"], 3)
        self.assertEqual(self.out["triangles"], 28)

    def test_meshes_are_standard_materials_without_vertex_colours(self):
        """THE MEASURED RULE: palette materials, never vertexColors, and no
        colour attribute on the geometry for anything to switch on."""
        for mesh in self.out["meshes"]:
            self.assertEqual(mesh["type"], "MeshStandardMaterial")
            self.assertFalse(mesh["vertexColors"])
            self.assertEqual(mesh["attrs"], ["normal", "position"])
        self.assertEqual([m["colour"] for m in self.out["meshes"]], ["#0000ff", "#0000ff", "#ff0000"])

    def test_every_triangle_winds_with_its_normal(self):
        self.assertGreater(self.out["winding"]["total"], 100)
        self.assertEqual(self.out["winding"]["bad"], 0)

    def test_emission_is_capped_and_glass_is_physical(self):
        self.assertEqual(self.out["emitCapped"], 1)
        self.assertEqual(self.out["glass"], "MeshPhysicalMaterial")

    def test_a_scene_that_breaks_a_rule_is_named(self):
        """NEGATIVE: vertexColors, a non-Standard material and a bright emitter
        are each reported; the kit's own meshes report nothing."""
        self.assertEqual(sorted(self.out["problems"]),
                         ["MeshBasicMaterial", "emissiveIntensity 4 > 1", "vertexColors"])
        self.assertEqual(self.out["cleanProblems"], 0)

    def test_despeckle_fixes_a_speck_and_keeps_an_edge(self):
        self.assertEqual(self.out["speck"], [1, 100])
        self.assertEqual(self.out["edge"], 0)

    def test_a_colour_that_is_not_hex_and_an_unknown_kind_are_refused(self):
        self.assertEqual(self.out["refused"], ["colour", "kind"])


class CheckerTests(unittest.TestCase):
    """tools/check_pathtracer_kit.py: green on the repository, red per broken link."""

    def fixture(self):
        tmp = tempfile.mkdtemp(prefix="crow-kitcheck-")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(KIT, os.path.join(tmp, "kits", "pathtracer"))
        shutil.copy(os.path.join(REPO, "NOTICE"), os.path.join(tmp, "NOTICE"))
        return tmp

    def failed(self, repo):
        return [name for name, ok, _ in checker.checks(repo) if not ok]

    def test_the_repository_is_green(self):
        self.assertEqual(self.failed(REPO), [])
        self.assertEqual(checker.main(["x", REPO]), 0)

    def test_a_changed_byte_in_the_bundle_is_red(self):
        repo = self.fixture()
        path = os.path.join(repo, "kits", "pathtracer", "crow-pathtracer.js")
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        data[1000] ^= 1
        with open(path, "wb") as fh:
            fh.write(data)
        self.assertEqual(self.failed(repo), ["bundle sha256 matches kit.json"])

    def test_a_missing_licence_text_is_red(self):
        repo = self.fixture()
        os.remove(os.path.join(repo, "kits", "pathtracer", "LICENSE.three-mesh-bvh"))
        self.assertEqual(self.failed(repo), ["licence text three-mesh-bvh present and unchanged"])

    def test_a_notice_without_the_pinned_version_is_red(self):
        repo = self.fixture()
        path = os.path.join(repo, "NOTICE")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("three-gpu-pathtracer 0.0.24", "three-gpu-pathtracer 0.0.23"))
        self.assertEqual(self.failed(repo), ["NOTICE names three-gpu-pathtracer 0.0.24"])

    def test_vertex_colours_in_the_kit_are_red(self):
        repo = self.fixture()
        path = os.path.join(repo, "kits", "pathtracer", "voxel-kit.js")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("roughness: 0.9, emissive:", "roughness: 0.9, vertexColors: true, emissive:"))
        self.assertEqual(self.failed(repo), ["voxel-kit.js never enables vertexColors"])

    def test_no_kit_is_a_setup_error(self):
        tmp = tempfile.mkdtemp(prefix="crow-kitcheck-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(checker.main(["x", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
