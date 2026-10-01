"""Negative control for check_stack.py's offline checks.

Each case breaks exactly one promise in a copy of the real manifest and requires
the checker to go red at THAT check and name it; the first case requires the real
manifest to be green. The --online half needs the network and is not run here.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_stack as C  # noqa: E402

with open(C.MANIFEST, encoding="utf-8") as _f:
    REAL = json.load(_f)


def point(doc, pid):
    return next(p for p in doc["points"] if p["id"] == pid)


def file_(doc, fid):
    return next(f for f in doc["files"] if f["id"] == fid)


class Base(unittest.TestCase):
    def setUp(self):
        self.doc = copy.deepcopy(REAL)

    def red(self, label, needle):
        r = C.run(self.doc)
        fails = [ln for ln in r.lines if ln.startswith("  FAIL")]
        self.assertTrue(r.failed, "expected a failure at %s" % label)
        hit = [ln for ln in fails if label in ln]
        self.assertTrue(hit, "no FAIL at %r in %s" % (label, fails))
        self.assertIn(needle, "\n".join(r.lines))


class RealManifest(unittest.TestCase):
    def test_real_manifest_is_green(self):
        r = C.run(REAL)
        self.assertEqual(r.failed, 0, "\n".join(r.lines))
        self.assertEqual([p["id"] for p in REAL["points"]], list(C.POINT_IDS))


class Schema(Base):
    def test_unknown_status(self):
        file_(self.doc, "27b-cnq")["status"] = "local"
        self.red("schema", "status 'local'")

    def test_mirror_pending_with_a_revision(self):
        file_(self.doc, "27b-mmproj")["revision"] = "0" * 40
        self.red("schema", "revision must be null")

    def test_published_without_a_revision(self):
        file_(self.doc, "fn-cnq")["revision"] = None
        self.red("schema", "40-hex commit revision")

    def test_source_disagreeing_with_the_file(self):
        file_(self.doc, "fn-tokenizer")["source"]["sha256"] = "a" * 64
        self.red("schema", "differ from its source")

    def test_non_commercial_licence_hidden(self):
        self.doc["licenses"]["qwen-research"]["show_at_install"] = False
        self.red("schema", "not shown at install")

    def test_missing_point(self):
        self.doc["points"] = [p for p in self.doc["points"] if p["id"] != "27b"]
        self.red("schema", "expected")

    def test_shared_dest(self):
        file_(self.doc, "fn-sidecar")["dest"] = file_(self.doc, "fn-cnq")["dest"]
        self.red("schema", "share dest")


class Paths(Base):
    def test_absolute_env_path(self):
        point(self.doc, "27b")["engine"]["env"]["CROW_CNQ"] = "D:/models/Qwen3.8-27B-CNQ4.5.cnq"
        self.red("placeholders and paths", "absolute or personal path")

    def test_home_path_in_documentation(self):
        self.doc["_what"] += " See /home/someone/models."
        self.red("placeholders and paths", "/_what")

    def test_unknown_placeholder(self):
        file_(self.doc, "qi-vae")["dest"] = "${HOME}/vae.safetensors"
        self.red("placeholders and paths", "${HOME}")

    def test_user_name(self):
        with mock.patch.dict(os.environ, {"USERNAME": "zzqbuilder"}):
            self.doc["files"][0]["role"] = "container of zzqbuilder"
            self.red("placeholders and paths", "names the user")


class References(Base):
    def test_unused_file(self):
        point(self.doc, "flash-next")["files"].remove("fn-sidecar")
        self.red("references", "fn-sidecar is used by no point")

    def test_undeclared_file(self):
        point(self.doc, "27b")["files"].append("27b-ghost")
        self.red("references", "27b-ghost")

    def test_derived_input_missing_from_point(self):
        point(self.doc, "image-stack")["files"].remove("qi-text-encoder-3")
        self.red("references", "without its input qi-text-encoder-3")


class Sums(Base):
    def test_file_bytes_change_without_the_totals(self):
        file_(self.doc, "27b-sidecar")["bytes"] += 1
        file_(self.doc, "27b-sidecar")["source"] = None
        self.red("byte sums", "point 27b bytes.published")

    def test_preflight_disk(self):
        point(self.doc, "image-stack")["preflight"]["disk_bytes"] -= 1
        self.red("byte sums", "preflight.disk_bytes")

    def test_derived_outputs(self):
        self.doc["derived"][0]["outputs"][0]["bytes"] += 7
        self.red("byte sums", "outputs sum to")


class Wiring(Base):
    def test_env_names_another_points_file(self):
        point(self.doc, "27b")["engine"]["env"]["CROW_VIT_MMPROJ"] = \
            file_(self.doc, "fn-mmproj")["dest"]
        self.red("engine wiring", "CROW_VIT_MMPROJ")

    def test_identity_of_the_wrong_model(self):
        point(self.doc, "27b")["engine"]["identity"]["endswith"] = "Qwen3.8-Flash-Next-CNQ4.5-M.cnq"
        self.red("engine wiring", "identity")

    def test_menu_claims_more_context_than_served(self):
        point(self.doc, "image-stack")["menu"]["line"] = "200k context \u2014 pictures"
        self.red("engine wiring", "claims 200k context")

    def test_slot_dir_not_created(self):
        point(self.doc, "flash-next")["engine"]["dirs"] = []
        self.red("engine wiring", "--slot-save-path")

    def test_sd_server_path_not_installed(self):
        argv = point(self.doc, "image-stack")["image_server"]["argv"]
        i = argv.index("--llm") + 1
        argv[i] = argv[i].replace("text_encoder_sdcli", "text_encoder_missing")
        self.red("engine wiring", "neither installed nor derived")

    def test_port_mismatch(self):
        point(self.doc, "image-stack")["image_server"]["port"] = 8098
        self.red("engine wiring", "--listen-port")


class Cli(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "check_stack.py"), *args],
                              capture_output=True, text=True)

    def test_exit_codes(self):
        ok = self.cli()
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("RESULT:", ok.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            bad = copy.deepcopy(REAL)
            bad["files"][0]["bytes"] = -1
            path = os.path.join(tmp, "stack.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            self.assertEqual(self.cli("--manifest", path).returncode, 1)
            self.assertEqual(self.cli("--manifest", os.path.join(tmp, "none.json")).returncode, 2)


class ImageServerArgvDrift(unittest.TestCase):
    """stack.json's sd-server line is the one Crow itself builds (#196).

    The manifest copies crow_core.image_server_command; a change on either side
    without the other would boot the installed image stack on a line nobody
    measured. Paths are compared after expanding ${MODELS}, the flags verbatim.
    """

    def test_the_manifest_argv_is_crow_cores_argv(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "cli"))
        import crow_core
        import crow_platform
        server = point(REAL, "image-stack")["image_server"]
        models = os.path.join("M", "models")
        model_dir = os.path.join(models, "qwen-image-2.1")
        for plat, extra in server["argv_platform"].items():
            with self.subTest(platform=plat), \
                    mock.patch.object(crow_core, "image_model_dir", return_value=model_dir), \
                    mock.patch.object(crow_core, "image_server_binary", return_value="sd-server"), \
                    mock.patch.object(crow_platform, "image_server_platform_args", return_value=list(extra)):
                built = crow_core.image_server_command(server["port"])[1:]
            want = [a.replace("${MODELS}", models).replace("/", os.sep) if "${MODELS}" in a else a
                    for a in server["argv"]] + list(extra)
            self.assertEqual(built, want)


if __name__ == "__main__":
    unittest.main()
