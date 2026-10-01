"""Tests for tools/repack-release.py: what may ship, and the privacy gate (#196 C2).

Run: python -m unittest discover -s tools   (or python tools/test_repack_release.py)

Everything here is synthetic. The fake builder is "fakebuilder" on "FAKEHOST1";
no check depends on who runs the suite.
"""
import contextlib
import hashlib
import importlib.util
import io
import os
import re
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PS1 = os.path.join(HERE, "pack-release.ps1")

_spec = importlib.util.spec_from_file_location("repack_release", os.path.join(HERE, "repack-release.py"))
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

FAKE_PROFILE = "C:\\Users\\fakebuilder"
FAKE_HOST = "FAKEHOST1"


def put(root, rel, data=b"x"):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def make_repo(root):
    """A checkout with everything that ships and everything that must not."""
    bundle = b"// the bundle\n"
    kit = '{"bundle": {"sha256": "%s"}}' % hashlib.sha256(bundle).hexdigest()
    put(root, "LICENSE", b"MIT")
    put(root, "NOTICE", b"notice")
    put(root, "README.md", b"# crow")
    put(root, "cli/crow_core.py", b'VERSION = "9.9.9"\n')
    put(root, "cli/fonts/OFL.txt", b"OFL")
    put(root, "manifests/0731-chat-template.jinja", b"{{ x }}")
    put(root, "manifests/operating-point.json", b"{}")
    put(root, "manifests/shared-core.json", b"{}")
    put(root, "kits/pathtracer/crow-pathtracer.js", bundle)
    put(root, "kits/pathtracer/kit.json", kit.encode())
    for f in ("voxel-kit.js", "SKILL.md", "check_diorama.py", "scaffold/index.html", "scaffold/scene.js",
              "LICENSE.three", "LICENSE.three-mesh-bvh", "LICENSE.three-gpu-pathtracer"):
        put(root, "kits/pathtracer/" + f)
    # not for shipping
    put(root, "cli/runs/llama-server-8080.log", b"loading C:\\Users\\fakebuilder\\models\\x.gguf")
    put(root, "cli/runs/x.log")
    put(root, "cli/sub/notes.log")
    put(root, "cli/__pycache__/crow_core.cpython-313.pyc")
    put(root, "cli/test_crow_core.py")
    put(root, "cli/.env", b"KEY=1")
    put(root, "cli/secrets.json", b"{}")
    put(root, "cli/session.json", b"{}")
    put(root, "kits/pathtracer/__pycache__/c.pyc")
    put(root, "kits/pathtracer/runs/y.log")
    return root


def make_previous(path, dll=b"MZ clean binary"):
    rr.write_package(path, {"bin\\llama-server.exe": dll})
    return path


def run_main(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rr.main(args)
    return code, buf.getvalue()


class ShippedSetTest(unittest.TestCase):
    def test_staging_leaves_out_runs_logs_and_state(self):
        with tempfile.TemporaryDirectory() as d:
            files = rr.stage_from_checkout(make_repo(d))
        names = sorted(files)
        for gone in ("runs", ".log", ".pyc", "test_", ".env", "secrets.json", "session.json", "shared-core"):
            self.assertFalse([n for n in names if gone in n], "%s shipped: %s" % (gone, names))
        for kept in ("cli\\crow_core.py", "cli\\fonts\\OFL.txt", "kits\\pathtracer\\kit.json",
                     "kits\\pathtracer\\scaffold\\scene.js", "templates\\0731-chat-template.jinja",
                     "manifests\\operating-point.json", "LICENSE", "NOTICE", "README.md"):
            self.assertIn(kept, names)

    def test_staging_ships_the_kit(self):
        # repack-release used to stage no kits\ at all; pack-release.ps1 always did
        with tempfile.TemporaryDirectory() as d:
            files = rr.stage_from_checkout(make_repo(d))
        self.assertTrue([n for n in files if n.startswith("kits\\pathtracer\\")])

    def test_a_kit_whose_bundle_does_not_match_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            put(d, "kits/pathtracer/crow-pathtracer.js", b"tampered")
            with self.assertRaises(SystemExit):
                rr.stage_from_checkout(d)

    def test_the_declared_set_is_a_closed_list(self):
        ok = ["LICENSE", "cli\\crow_core.py", "bin\\llama-server.exe", "kits\\pathtracer\\kit.json",
              "templates\\0731-chat-template.jinja", "manifests\\operating-point.json", "MANIFEST.json"]
        self.assertEqual(rr.shipped_set_violations(ok), [])
        bad = ["docs\\x.md", "stray.txt", "manifests\\shared-core.json", "templates\\other.jinja",
               "cli\\runs\\x.log", "bin\\a.log", "cli\\.env.local", "kits\\pathtracer\\runs\\y.log",
               "cli\\sessions\\a.txt", "cli/test_x.py"]
        got = {p.replace("/", "\\") for p, _ in rr.shipped_set_violations(bad)}
        self.assertEqual(got, {b.replace("/", "\\") for b in bad})

    def test_lists_match_the_ones_in_pack_release_ps1(self):
        with open(PS1, encoding="utf-8") as fh:
            ps = fh.read()

        def ps_list(name):
            m = re.search(r"\$" + name + r"\s*=\s*@\((.*?)\)", ps, re.S)
            self.assertTrue(m, "$%s not declared in pack-release.ps1" % name)
            return tuple(re.findall(r"'([^']*)'", m.group(1)))
        self.assertEqual(ps_list("SHIP_ROOT_FILES"), rr.SHIP_ROOT_FILES)
        self.assertEqual(ps_list("SHIP_TOP_DIRS"), rr.SHIP_TOP_DIRS)
        self.assertEqual(ps_list("SHIP_SINGLE_FILES"), rr.SHIP_SINGLE_FILES)
        self.assertEqual(ps_list("EXCLUDE_DIRS"), rr.EXCLUDE_DIRS)
        self.assertEqual(ps_list("EXCLUDE_FILES"), rr.EXCLUDE_FILES)
        self.assertEqual(ps_list("KIT_REQUIRED"), rr.KIT_REQUIRED)


class PrivacyGateTest(unittest.TestCase):
    def pats(self, extra=()):
        return rr.private_patterns(extra, profile=FAKE_PROFILE, user="fakebuilder", host=FAKE_HOST)[0]

    def hit(self, blob, extra=()):
        return rr.scan_private({"bin\\x.dll": blob}, self.pats(extra))

    def test_profile_path_in_utf16le_binary_is_found(self):
        blob = b"MZ\x00\x90" + (FAKE_PROFILE + "\\dev\\llama.cpp\\ggml.c").encode("utf-16le") + b"\x00" * 9
        hits = self.hit(blob)
        self.assertTrue(hits and all(h[2] == "utf-16le" for h in hits), hits)
        self.assertEqual(hits[0][0], "bin\\x.dll")

    def test_profile_path_in_utf8_is_found_and_counted(self):
        blob = (FAKE_PROFILE + "\\a ").encode() * 3
        hits = self.hit(blob)
        self.assertIn(("bin\\x.dll", FAKE_PROFILE, "utf-8", 3), hits)

    def test_spellings_case_and_user_segments(self):
        for blob in (b"C:/Users/fakebuilder/dev", b"c:\\USERS\\FakeBuilder\\x", b"C:\\\\Users\\\\fakebuilder\\\\x",
                     b"see /home/fakebuilder/.cache", b"\\Users\\fakebuilder\\Desktop"):
            self.assertTrue(self.hit(blob), blob)

    def test_host_name_and_extra_patterns(self):
        self.assertTrue(self.hit(b"built on " + FAKE_HOST.lower().encode()))
        self.assertTrue(self.hit(b"x", extra=("x",)))
        self.assertTrue(self.hit(("secret-lab-name").encode("utf-16le"), extra=("secret-lab-name",)))

    def test_clean_bytes_pass(self):
        blob = b"MZ" + b"\x00" * 100 + "C:\\Windows\\System32\\kernel32.dll".encode("utf-16le") + b"/usr/lib/x"
        self.assertEqual(self.hit(blob), [])

    def test_the_gate_derives_this_machines_profile_by_default(self):
        pats, _ = rr.private_patterns()
        home = os.path.expanduser("~").rstrip("\\/")
        self.assertIn(home.replace("/", "\\").lower(), [p.lower() for p in pats])

    def test_the_bare_user_name_in_prose_is_found_in_both_encodings(self):
        text = "Write a report for FakeBuilder about the run"
        for enc in ("utf-8", "utf-16le"):
            hits = self.hit(text.encode(enc))
            self.assertTrue([h for h in hits if h[2] == enc], (enc, hits))

    def test_a_very_short_user_name_is_noted_not_searched_bare(self):
        pats, notes = rr.private_patterns(profile=FAKE_PROFILE, user="abc", host=FAKE_HOST)
        self.assertNotIn("abc", pats)
        self.assertTrue([n for n in notes if "'abc'" in n])

    def test_a_very_short_host_name_is_noted_not_searched(self):
        pats, notes = rr.private_patterns(profile=FAKE_PROFILE, user="fakebuilder", host="pc")
        self.assertNotIn("pc", pats)
        self.assertTrue(notes)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.repo = make_repo(os.path.join(self.tmp, "repo"))
        self.out = os.path.join(self.tmp, "out")

    def args(self, previous, *more):
        return ["--previous", previous, "--repo", self.repo, "--out", self.out, "--version", "9.9.9", *more]

    def test_a_dirty_bin_refuses_and_writes_nothing(self):
        dll = b"MZ" + (FAKE_PROFILE + "\\dev\\x.cpp").encode("utf-16le")
        prev = make_previous(os.path.join(self.tmp, "prev.zip"), dll)
        code, out = run_main(self.args(prev, "--private-pattern", FAKE_PROFILE))
        self.assertEqual(code, 1, out)
        self.assertIn("REFUSING TO PACK", out)
        self.assertIn("bin\\llama-server.exe", out)
        self.assertIn("utf-16le", out)
        self.assertIn(FAKE_PROFILE, out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "crow-9.9.9-win-x64.zip")))

    def test_dirty_source_text_refuses_too(self):
        put(self.repo, "cli/crow_core.py", b'VERSION = "9.9.9"\n' + rb'P = "C:\Users\fakebuilder\Desktop"' + b'\n')
        prev = make_previous(os.path.join(self.tmp, "prev.zip"))
        code, out = run_main(self.args(prev, "--private-pattern", FAKE_PROFILE))
        self.assertEqual(code, 1, out)
        self.assertIn("cli\\crow_core.py", out)
        self.assertFalse(os.path.exists(self.out) and os.listdir(self.out))

    def test_a_clean_tree_packs_without_the_logs(self):
        prev = make_previous(os.path.join(self.tmp, "prev.zip"))
        code, out = run_main(self.args(prev, "--private-pattern", FAKE_PROFILE))
        self.assertEqual(code, 0, out)
        zpath = os.path.join(self.out, "crow-9.9.9-win-x64.zip")
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
        self.assertIn("cli/crow_core.py", names)
        self.assertIn("kits/pathtracer/kit.json", names)
        self.assertFalse([n for n in names if n.endswith(".log") or "/runs/" in n or ".pyc" in n], names)

    def test_a_previous_bin_with_a_log_in_it_is_refused(self):
        rr.write_package(os.path.join(self.tmp, "prev.zip"),
                         {"bin\\llama-server.exe": b"MZ", "bin\\server.log": b"hello"})
        code, out = run_main(self.args(os.path.join(self.tmp, "prev.zip")))
        self.assertEqual(code, 1, out)
        self.assertIn("bin\\server.log", out)


if __name__ == "__main__":
    unittest.main()
