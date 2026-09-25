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

sys.path.insert(0, KIT)
try:
    import check_diorama as diorama  # noqa: E402
except ImportError:                  # the #298 kit has no checker: the cases below go red
    diorama = None

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

// #299: the props registry, the coverage maths, the mode parser
if (kit.coverage) {
  const { coverage, parseMode, propKind } = kit;
  const p = new VoxelGrid();
  p.box(0, 0, 0, 3, 0, 3, '#777777');                       // terrain: 4 x 4 columns, top y = 0
  const crate = p.prop('crate', () => p.box(0, 1, 0, 1, 2, 1, '#aa7744'));
  const outer = p.prop('lamp post', () => { p.box(3, 1, 3, 3, 4, 3, '#222222'); p.prop('lantern', () => p.put(3, 5, 3, '#ffcc66', 'emit')); });
  p.prop('bird', () => p.put(2, 9, 0, '#ffffff'));          // high above one column: still covers it
  p.prop('ghost', () => { p.put(1, 7, 3, '#123456'); p.clear(1, 7, 3, 1, 7, 3); });
  p.prop('path stone', () => p.put(0, 0, 3, '#999999'));    // set INTO the terrain's top
  p.prop('dirt', () => p.put(1, 1, 2, '#553311'));
  p.put(1, 1, 2, '#553311');                                // re-laid outside any prop: terrain now
  out.crate = crate; out.outer = outer;
  out.summary = p.propSummary().map((r) => [r.name, r.voxels]);
  out.cov = coverage(p);
  const empty = new VoxelGrid(); empty.prop('only', () => empty.put(0, 0, 0, '#ffffff'));
  out.covEmpty = coverage(empty);
  out.modes = ['', '?mode=photo&t=2.5', '?mode=foto', '?mode=live&t=4', '?mode=photo&t=-1', '?mode=photo&t=abc',
               '?mode=PHOTO&ui=0', '?mode=xyz&t=1', '?mode=raster&ui=0', '?mode=preview'].map((q) => parseMode(q));
  out.kinds = ['Rock 3', 'rock#12', 'rock_7', 'grass tuft 12', 'crate', '42'].map(propKind);
  const bad = [];
  try { p.prop('', () => {}); } catch (e) { bad.push('name'); }
  try { p.prop('x'); } catch (e) { bad.push('fn'); }
  out.propRefused = bad;
}
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


@unittest.skipUnless(NODE, "node is not on PATH")
class PropsCoverageModeTests(unittest.TestCase):
    """#299: the props registry, coverage and the URL modes, in node against the
    real voxel-kit.js. Red on the #298 kit (no g.prop / coverage / parseMode)."""

    @classmethod
    def setUpClass(cls):
        VoxelKitTests.setUpClass()
        cls.out = VoxelKitTests.out

    def test_the_kit_has_the_registry(self):
        self.assertIn("cov", self.out, "voxel-kit.js exports no coverage() -- the #299 registry is missing")

    def test_a_prop_records_its_voxels_and_bbox(self):
        self.assertIn("crate", self.out)
        self.assertEqual(self.out["crate"], {"name": "crate", "voxels": 8,
                                             "bbox": {"min": [0, 1, 0], "max": [1, 2, 1]}})

    def test_nested_props_count_for_both(self):
        """The lantern inside the lamp post is its own prop AND part of the post."""
        self.assertEqual(self.out["outer"]["voxels"], 5)
        self.assertEqual(self.out["outer"]["bbox"], {"min": [3, 1, 3], "max": [3, 5, 3]})

    def test_a_cleared_or_retaken_cell_leaves_the_prop(self):
        """'ghost' cleared its own voxel; 'dirt' was re-laid outside any prop and
        is terrain now; 'path stone' replaced a terrain cell and stays a prop."""
        self.assertEqual(self.out["summary"], [["crate", 8], ["lantern", 1], ["lamp post", 5], ["bird", 1],
                                               ["ghost", 0], ["path stone", 1], ["dirt", 0]])

    def test_coverage_counts_columns_with_a_prop_above_the_top_terrain_cell(self):
        """16 terrain columns. Covered: the crate's 4, the post's 1, the bird's 1.
        (0,3): the path stone replaced the top cell -- terrain there is gone, the
        column is not terrain any more. (1,2): 'dirt' re-laid as terrain, top y=1,
        nothing above -> bare. 6 of 15."""
        self.assertEqual(self.out["cov"], {"coverage": 6 / 15, "terrainColumns": 15, "coveredColumns": 6})

    def test_coverage_without_terrain_is_none(self):
        self.assertEqual(self.out["covEmpty"], {"coverage": None, "terrainColumns": 0, "coveredColumns": 0})

    def test_mode_parsing(self):
        modes = [(m["mode"], m["t"], m["ui"], m["forced"]) for m in self.out["modes"]]
        self.assertEqual(modes, [("live", 0, True, False), ("photo", 2.5, True, True), ("photo", 0, True, True),
                                 ("live", 4, True, True), ("photo", 0, True, True), ("photo", 0, True, True),
                                 ("photo", 0, False, True), ("live", 1, True, False),
                                 ("raster", 0, False, True), ("raster", 0, True, True)])

    def test_prop_kinds_drop_trailing_numbers(self):
        self.assertEqual(self.out["kinds"], ["rock", "rock", "rock", "grass tuft", "crate", "42"])

    def test_a_prop_needs_a_name_and_a_function(self):
        self.assertEqual(self.out["propRefused"], ["name", "fn"])


def _fake_render(mode="gpu", renderer="ANGLE (NVIDIA, Vulkan 1.4.341 (NVIDIA GeForce RTX 5090), NVIDIA)",
                 probe=None, precheck=None, extra_console=()):
    """A render_page result as tool_render_page writes it (#293 record line,
    Chromium's console lines with their `", source:` tail)."""
    record = {"render_mode": mode, "renderer": renderer, "frames": ["/x/render-1.png"], "contact_sheet": None,
              "precheck": precheck if precheck is not None else
              {"uniform": False, "distinct_colours": 30000, "one_colour_pct": 3.1, "dark_pct": 12.0,
               "clipped_pct": 0.4, "max_frame_diff": None, "identical_frames": None}}
    lines = ["render: " + json.dumps(record), "/x/render-1.png -- 1000 bytes, 1024x1024, done", "console (last 3):"]
    lines += ["console: %s" % c for c in extra_console]
    if probe is not None:
        lines.append('console: "[crow-pt] samples=10 spp/s=14.5 lost=0 despeckled=3", source: file:///x/index.html (8537)')
        lines.append('console: "[crow-scene] %s", source: file:///x/index.html?mode=photo&t=2 (8537)' % json.dumps(probe))
    return "\n".join(lines)


GOOD_SCENE = {"voxels": 70000, "triangles": 90000, "props": 45, "propKinds": 14, "propVoxels": 20000,
              "coverage": 0.62, "terrainColumns": 5000, "parts": 30, "animated": True, "errors": 0,
              "samples": 290, "samplesPerSecond": 14.5, "renderer": "ANGLE (NVIDIA ...)", "error": None}
OPTS = {"min_props": 40, "min_voxels": 60000, "min_coverage": 0.5, "min_kinds": 12, "min_samples": 200}
MOVING = [(0, 1, 1.2), (0, 2, 1.3), (0, 3, 0.9), (1, 2, 1.2), (1, 3, 1.3), (2, 3, 1.1)]


class DioramaCheckerTests(unittest.TestCase):
    """#299: kits/pathtracer/check_diorama.py on fake render results -- the
    parsing and every verdict, no browser."""

    def setUp(self):
        self.assertIsNotNone(diorama, "kits/pathtracer/check_diorama.py is missing (#299)")

    def cap(self, mode="live", render_mode="gpu", **kw):
        probe = dict(GOOD_SCENE, mode=mode, **kw.pop("scene", {}))
        text = _fake_render(mode=render_mode, probe=probe, **kw)
        return {"text": text, "record": diorama.parse_record(text), "probe": diorama.parse_probe(text)}

    def verdicts(self, live=None, photo=None, diffs=MOVING, repeat=True, opts=OPTS):
        live = live or self.cap("live")
        photo = photo or self.cap("photo")
        return {name: ok for name, ok, _ in diorama.evaluate(live, photo, opts, diffs=diffs, repeat=repeat)}

    def failed(self, **kw):
        return sorted(k for k, ok in self.verdicts(**kw).items() if not ok)

    def test_the_record_and_the_probe_are_parsed_from_the_result_text(self):
        text = _fake_render(probe=dict(GOOD_SCENE, mode="photo", t=2))
        self.assertEqual(diorama.parse_record(text)["render_mode"], "gpu")
        probe = diorama.parse_probe(text)
        self.assertEqual((probe["mode"], probe["t"], probe["props"], probe["coverage"]), ("photo", 2, 45, 0.62))
        self.assertIsNone(diorama.parse_probe("render: {}\nconsole: [crow-scene] {not json"))
        self.assertIsNone(diorama.parse_record("error: no such page"))

    def test_the_last_probe_line_wins(self):
        text = _fake_render(probe=dict(GOOD_SCENE, mode="photo", samples=10))
        text += '\nconsole: "[crow-scene] %s", source: x (1)' % json.dumps(dict(GOOD_SCENE, mode="photo", samples=300))
        self.assertEqual(diorama.parse_probe(text)["samples"], 300)

    def test_a_good_dense_animated_gpu_page_passes_every_check(self):
        self.assertEqual(self.failed(), [])
        self.assertEqual(len(self.verdicts()), 11)

    def test_software_rendering_fails_gpu(self):
        self.assertEqual(self.failed(photo=self.cap("photo", renderer="ANGLE (Google, SwiftShader Device)")), ["gpu"])
        self.assertIn("gpu", self.failed(live=self.cap("live", render_mode="unavailable")))

    def test_too_few_samples_props_kinds_voxels_or_coverage_fail_their_check(self):
        scene = {"samples": 150, "props": 12, "propKinds": 5, "voxels": 20000, "coverage": 0.2}
        self.assertEqual(self.failed(live=self.cap("live", scene=scene), photo=self.cap("photo", scene=scene)),
                         ["coverage", "kinds", "props", "samples", "voxels"])

    def test_no_terrain_fails_coverage(self):
        scene = {"coverage": None, "terrainColumns": 0}
        self.assertEqual(self.failed(photo=self.cap("photo", scene=scene)), ["coverage"])

    def test_one_still_pair_or_no_animation_fails_motion(self):
        still = MOVING[:5] + [(2, 3, 0.2)]
        self.assertEqual(self.failed(diffs=still), ["motion"])
        self.assertEqual(self.failed(live=self.cap("live", scene={"animated": False})), ["motion"])
        self.assertEqual(self.failed(diffs=None), ["motion"])

    def test_a_non_repeating_capture_fails_repeat_and_a_skipped_one_is_absent(self):
        self.assertEqual(self.failed(repeat=False), ["repeat"])
        self.assertNotIn("repeat", self.verdicts(repeat=None))

    def test_a_uniform_or_black_photo_fails_photo(self):
        black = {"uniform": False, "distinct_colours": 40, "one_colour_pct": 60, "dark_pct": 97.0,
                 "clipped_pct": 0, "max_frame_diff": None, "identical_frames": None}
        self.assertEqual(self.failed(photo=self.cap("photo", precheck=black)), ["photo"])

    def test_page_errors_fail_errors(self):
        uncaught = ['"Uncaught TypeError: x is undefined", source: file:///x/index.html (12)']
        self.assertEqual(self.failed(live=self.cap("live", extra_console=uncaught)), ["errors"])
        self.assertEqual(self.failed(photo=self.cap("photo", scene={"errors": 2})), ["errors"])
        self.assertEqual(self.failed(photo=self.cap("photo", scene={"error": "setScene: bad"})), ["errors"])

    def test_no_probe_line_fails_probe_errors_and_motion(self):
        text = _fake_render(probe=None)
        live = {"text": text, "record": diorama.parse_record(text), "probe": None}
        # motion too: without the live probe nothing says the scene is animated
        self.assertEqual(self.failed(live=live), ["errors", "motion", "probe"])

    def test_the_wrong_mode_fails_probe(self):
        self.assertEqual(self.failed(photo=self.cap("live")), ["probe"])

    def test_motion_is_captured_on_the_raster_preview(self):
        """2026-09-25: live mode path-traces the static island, whose
        accumulating samples would make a still scene 'move'; the clock-stepped
        motion/repeat captures use ?mode=raster, and its probe passes."""
        self.assertEqual(diorama.MOTION_QUERY, "?mode=raster&ui=0")
        self.assertEqual(self.failed(live=self.cap("raster")), [])

    def test_pair_diffs_counts_pixels_over_the_channel_delta(self):
        a = [(10, 10, 10)] * 100
        b = [(10, 10, 10)] * 98 + [(40, 10, 10), (30, 10, 10)]      # one over 25, one at 20
        self.assertEqual(diorama.pair_diffs([a, b, a]), [(0, 1, 1.0), (0, 2, 0.0), (1, 2, 1.0)])


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



class CheckerBorrowsVramTests(unittest.TestCase):
    """#304: the checker runs outside a Crow turn and must name serve for lending."""

    def test_the_checker_binds_the_local_serve(self):
        class Core:
            _TURN_SPOT = {}
        core = Core()
        self.assertTrue(diorama.bind_lend_spot(core, "http://127.0.0.1:8099/v1"))
        self.assertEqual(core._TURN_SPOT, {"base_url": "http://127.0.0.1:8099/v1", "remote": False})

    def test_none_leaves_it_unbound(self):
        class Core:
            _TURN_SPOT = {}
        core = Core()
        self.assertFalse(diorama.bind_lend_spot(core, "none"))
        self.assertEqual(core._TURN_SPOT, {})

    def test_main_binds_before_the_first_capture(self):
        src = open(diorama.__file__, encoding="utf-8").read()
        self.assertLess(src.index("bind_lend_spot(core, a.serve)"), src.index('live = capture(core'))


if __name__ == "__main__":
    unittest.main()
