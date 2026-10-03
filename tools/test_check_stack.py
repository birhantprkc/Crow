"""Negative control for check_stack.py's offline checks.

Each case breaks exactly one promise in a copy of the real manifest and requires
the checker to go red at THAT check and name it; the first case requires the real
manifest to be green. The --online half needs the network and is not run here.
"""

import copy
import json
import os
import re
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

    def test_repo_owner_is_not_the_user(self):
        # #343: a Hugging Face repo id's namespace is the public owner the installer
        # downloads from, not a leak, even when it equals the builder's login.
        owner = REAL["files"][0]["repo"].split("/")[0]
        with mock.patch.dict(os.environ, {"USER": owner, "LOGNAME": owner}):
            r = C.run(self.doc)
            self.assertFalse([ln for ln in r.lines if "names the user" in ln], "\n".join(r.lines))

    def test_user_name_in_the_repo_name_part(self):
        with mock.patch.dict(os.environ, {"USERNAME": "zzqbuilder"}):
            self.doc["files"][0]["repo"] = "someorg/zzqbuilder-model"
            self.red("placeholders and paths", "names the user")

    def test_user_name_in_a_dest_beside_a_repo_owner(self):
        owner = REAL["files"][0]["repo"].split("/")[0]
        with mock.patch.dict(os.environ, {"USER": owner}):
            self.doc["files"][0]["dest"] = "${MODELS}/%s/x.cnq" % owner
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


class Platforms(Base):
    """#341: both platforms start every point; the real manifest named Windows only."""

    def test_engine_without_a_linux_binary(self):
        del point(self.doc, "27b")["engine"]["binary"]["linux"]
        self.red("platforms", "point 27b engine.binary names ['windows']")

    def test_image_server_without_a_linux_binary(self):
        del point(self.doc, "image-stack")["image_server"]["binary"]["linux"]
        self.red("platforms", "point image-stack image_server.binary names ['windows']")

    def test_linux_binary_is_another_program(self):
        point(self.doc, "flash-next")["engine"]["binary"]["linux"] = "${INSTALL}/bin/serve.exe"
        self.red("platforms", "are not one program")

    def test_lib_path_missing(self):
        del self.doc["lib_path"]
        self.red("platforms", "lib_path must map windows / linux")

    def test_lib_path_without_the_engine_folder(self):
        self.doc["lib_path"]["linux"] = ["${INSTALL}/cuda/lib"]
        self.red("platforms", "lib_path.linux lacks ${INSTALL}/bin")

    def test_lib_path_outside_the_install(self):
        self.doc["lib_path"]["linux"].append("${MODELS}/lib")
        self.red("platforms", "is not a list of ${INSTALL}/ folders")


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


def crow_file(doc, fid):
    return next(f for f in doc["crow_files"] if f["id"] == fid)


class CrowFiles(Base):
    """The Crow-wide group: the dictation model every install gets (#196 P2-T2)."""

    def test_group_missing(self):
        del self.doc["crow_files"]
        self.red("crow files", "crow_files missing or empty")

    def test_unpinned_revision(self):
        crow_file(self.doc, "whisper-model")["revision"] = "main"
        self.red("crow files", "whisper-model needs a 40-hex commit revision")

    def test_mirror_pending_refused(self):
        crow_file(self.doc, "whisper-config")["status"] = "mirror-pending"
        self.red("crow files", "status 'mirror-pending'")

    def test_dest_under_models(self):
        f = crow_file(self.doc, "whisper-tokenizer")
        f["dest"] = f["dest"].replace("${INSTALL}/models", "${MODELS}")
        self.red("crow files", "does not start with ${INSTALL}/")

    def test_no_model_bin(self):
        self.doc["crow_files"] = [f for f in self.doc["crow_files"] if f["path"] != "model.bin"]
        self.red("crow files", "holds no ${INSTALL}/models/whisper-small/model.bin")

    def test_id_shared_with_files(self):
        crow_file(self.doc, "whisper-config")["id"] = "fn-cnq"
        self.red("crow files", "crow file id fn-cnq twice")

    def test_bad_sha(self):
        crow_file(self.doc, "whisper-vocabulary")["sha256"] = "XYZ"
        self.red("crow files", "whisper-vocabulary sha256 is not 64 lowercase hex")

    def test_field_missing(self):
        del crow_file(self.doc, "whisper-model")["bytes"]
        self.red("crow files", "whisper-model lacks bytes")

    def test_real_group_values(self):
        got = {f["path"]: f["bytes"] for f in REAL["crow_files"]}
        self.assertEqual(got, {"config.json": 2370, "vocabulary.txt": 459861,
                               "tokenizer.json": 2203239, "model.bin": 483546902})
        self.assertEqual(sum(got.values()), 486212372)
        self.assertEqual({f["repo"] for f in REAL["crow_files"]}, {"Systran/faster-whisper-small"})


class CrowFilesOnline(unittest.TestCase):
    """The online half of the group against a fake hub (no network)."""

    class FakeHub:
        def __init__(self, measured, head):
            self.measured, self._head, self.limits = measured, head, []

        def measure(self, repo, rev, path, limit=C.SMALL):
            self.limits.append(limit)
            return self.measured[path]

        def head(self, repo):
            return self._head

    def run_online(self, measured, head=None):
        doc = copy.deepcopy(REAL)
        rev = doc["crow_files"][0]["revision"]
        hub = self.FakeHub(measured, head or rev)
        r = C.Report()
        C.check_online_crow(doc, r, hub)
        return r, hub

    def truth(self):
        return {f["path"]: (f["bytes"], f["sha256"]) for f in REAL["crow_files"]}

    def test_matching_source_is_green_and_allows_the_2mb_tokenizer(self):
        r, hub = self.run_online(self.truth())
        self.assertEqual(r.failed, 0, "\n".join(r.lines))
        self.assertEqual(r.total, 4)
        self.assertGreater(C.CROW_SMALL, 2203239)
        self.assertEqual(set(hub.limits), {C.CROW_SMALL})

    def test_changed_source_fails(self):
        m = self.truth()
        m["model.bin"] = (m["model.bin"][0], "0" * 64)
        r, _ = self.run_online(m)
        self.assertEqual(r.failed, 1)
        self.assertIn("whisper-model: source has 483546902 B 000000000000", "\n".join(r.lines))

    def test_moved_head_is_a_note(self):
        r, _ = self.run_online(self.truth(), head="f" * 40)
        self.assertEqual(r.failed, 0)
        self.assertIn("HEAD is ffffffffffff", "\n".join(r.lines))


class DictationDrift(unittest.TestCase):
    """crow_files holds what Crow loads: the directory cli/crow_voice.py reads and the
    four files install.ps1 fetched into it."""

    def test_manifest_matches_crow_voice_and_install_ps1(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "cli", "crow_voice.py"), encoding="utf-8") as f:
            voice = f.read()
        with open(os.path.join(root, "install.ps1"), encoding="utf-8") as f:
            ps1 = f.read()
        dirname = re.search(r'^MODEL_DIRNAME = "([^"]+)"', voice, re.M).group(1)
        repo = re.search(r'^\$WHISPER_REPO\s*=\s*"([^"]+)"', ps1, re.M).group(1)
        # the loop right after $whisperDir is made: the four files it fetches
        after = ps1[ps1.index("$whisperDir = "):]
        files = re.findall(r'"([^"]+)"', re.search(
            r'foreach \(\$f in @\(([^)]*)\)\)', after).group(1))
        group = REAL["crow_files"]
        self.assertEqual(dirname, "whisper-small")
        self.assertEqual({f["repo"] for f in group}, {repo})
        self.assertEqual(sorted(f["path"] for f in group), sorted(files))
        for f in group:
            self.assertEqual(f["dest"], "${INSTALL}/models/%s/%s" % (dirname, f["path"]))


class TheWindowsLine(Base):
    """#196: menu.gui is the operating-point window's line (the terminal menu
    keeps menu.line). Present for every point, no dash, its context claim held
    to the engine, no user name."""

    def test_the_real_texts_are_the_approved_ones(self):
        self.assertEqual({p["id"]: p["menu"]["gui"] for p in REAL["points"]}, {
            "flash-next": "200k context. Great for coding and vision.",
            "27b": "128k context. Great speed, coding and vision.",
            "image-stack": "27B with Qwen-Image 2.1. Create pictures.",
        })

    def test_a_point_without_one(self):
        del point(self.doc, "27b")["menu"]["gui"]
        self.red("engine wiring", "point 27b has no menu gui text")

    def test_a_dash_in_it(self):
        point(self.doc, "flash-next")["menu"]["gui"] = "200k context \u2014 great for coding."
        self.red("engine wiring", "menu gui text has a dash")

    def test_it_claims_more_context_than_served(self):
        point(self.doc, "image-stack")["menu"]["gui"] = "200k context. Pictures."
        self.red("engine wiring", "menu gui text claims 200k context")

    def test_it_names_the_user(self):
        with mock.patch.dict(os.environ, {"USERNAME": "zzqbuilder"}):
            point(self.doc, "27b")["menu"]["gui"] = "Built by zzqbuilder."
            self.red("placeholders and paths", "names the user")


def media_like(doc):
    """27b rebuilt as a llama-server point with a video server (#340), valid as built."""
    base = file_(doc, "27b-cnq")
    doc["files"] += [
        dict(base, id="g-model", path="model-Q8_0.gguf", dest="${MODELS}/g/model-Q8_0.gguf"),
        dict(base, id="g-mmproj", path="mmproj-F16.gguf", dest="${MODELS}/g/mmproj-F16.gguf",
             role="projector"),
        dict(base, id="g-runtime", path="comfyui.zip", dest="${INSTALL}/setup/comfyui.zip", role="runtime"),
    ]
    pt = point(doc, "27b")
    pt["files"] = ["g-model", "g-mmproj", "g-runtime"]
    pt["engine"] = {
        "kind": "llama-server",
        "binary": {"windows": "${INSTALL}/bin/llama-server.exe", "linux": "${INSTALL}/bin/llama-server"},
        "cwd": "${INSTALL}", "env": {}, "dirs": [], "port": 8099, "context": 131072,
        "argv": ["-m", "${MODELS}/g/model-Q8_0.gguf", "--mmproj", "${MODELS}/g/mmproj-F16.gguf",
                 "--port", "8099"],
        "readiness": {"method": "GET", "path": "/health", "status": 200, "json": {"status": "ok"}},
        "identity": {"method": "GET", "path": "/props", "field": "model_path", "endswith": "model-Q8_0.gguf"},
    }
    pt["video_server"] = {
        "binary": {"windows": "${INSTALL}/comfyui/python_embeded/python.exe",
                   "linux": "${INSTALL}/comfyui/venv/bin/python"},
        "argv": ["-s", "${INSTALL}/comfyui/ComfyUI/main.py", "--port", "8188"],
        "port": 8188,
        "readiness": {"method": "GET", "path": "/system_stats", "status": 200},
        "runtime": {"windows": {"file": "g-runtime", "dir": "${INSTALL}/comfyui"},
                    "linux": {"file": "g-runtime", "dir": "${INSTALL}/comfyui"}},
    }
    return pt


class LlamaEngineAndVideo(unittest.TestCase):
    """engine.kind llama-server and video_server (#340), on a rebuilt 27b."""

    def setUp(self):
        self.doc = copy.deepcopy(REAL)
        self.pt = media_like(self.doc)

    def problems(self):
        return C.check_schema(self.doc) + C.check_wiring(self.doc) + C.check_platforms(self.doc)

    def red(self, needle):
        self.assertTrue(any(needle in p for p in self.problems()), self.problems())

    def test_the_fixture_is_green(self):
        self.assertEqual(self.problems(), [])

    def test_unknown_engine_kind(self):
        self.pt["engine"]["kind"] = "vllm"
        self.red("engine kind 'vllm' is not one of")

    def test_model_flag_names_no_container(self):
        self.pt["engine"]["argv"][1] = "${MODELS}/g/mmproj-F16.gguf"
        self.red("llama-server -m does not name its container")

    def test_identity_of_another_file(self):
        self.pt["engine"]["identity"]["endswith"] = "other.gguf"
        self.red("identity must be /props model_path ending in model-Q8_0.gguf")

    def test_mmproj_not_installed(self):
        self.pt["engine"]["argv"][3] = "${MODELS}/g/missing.gguf"
        self.red("--mmproj ${MODELS}/g/missing.gguf is no projector")

    def test_serve_still_needs_its_slot_dir(self):
        self.pt["engine"]["kind"] = "serve"
        self.red("--slot-save-path None is not created")

    def test_video_server_field_missing(self):
        del self.pt["video_server"]["runtime"]
        self.red("video_server lacks runtime")

    def test_video_port_differs_from_argv(self):
        self.pt["video_server"]["port"] = 8189
        self.red("video server --port differs from port 8189")

    def test_video_shares_the_engine_port(self):
        self.pt["video_server"]["port"] = 8099
        self.pt["video_server"]["argv"][3] = "8099"
        self.red("video server shares port 8099")

    def test_runtime_file_without_role_runtime(self):
        self.pt["video_server"]["runtime"]["linux"]["file"] = "g-model"
        self.red("video_server.runtime.linux.file 'g-model' is no runtime file")

    def test_video_path_outside_its_runtime(self):
        self.pt["video_server"]["argv"][1] = "${INSTALL}/elsewhere/main.py"
        self.red("video server path ${INSTALL}/elsewhere/main.py is neither installed nor in its runtime")

    def test_video_without_a_linux_binary(self):
        del self.pt["video_server"]["binary"]["linux"]
        self.red("video_server.binary names ['windows'], expected windows and linux")

    def test_video_binary_outside_its_runtime_dir(self):
        self.pt["video_server"]["binary"]["windows"] = "${INSTALL}/bin/python.exe"
        self.red("video_server.binary.windows is not inside its runtime dir")


class PointLists(unittest.TestCase):
    """The hand-written point lists (#340), each broken in a copy of its source file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for rel in {entry[1] for entry in C.POINT_LISTS}:
            os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
            with open(os.path.join(C.REPO, rel), encoding="utf-8") as f:
                text = f.read()
            with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
                f.write(text)

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, rel, old, new):
        path = os.path.join(self.root, rel)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(old, text, "fixture text moved in %s" % rel)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new, 1))

    def problems(self, doc=REAL):
        return C.check_point_lists(doc, self.root)[0]

    def test_the_repo_is_green(self):
        problems, n = C.check_point_lists(REAL)
        self.assertEqual(problems, [])
        self.assertEqual(n, 9)

    def test_a_new_point_only_in_stack_json(self):
        doc = copy.deepcopy(REAL)
        doc["points"].append(dict(point(doc, "image-stack"), id="media-stack"))
        problems = self.problems(doc)
        hit = {label for label, *_ in C.POINT_LISTS if any(label in p for p in problems)}
        self.assertEqual(hit, {label for label, _, _, rule, *_ in C.POINT_LISTS if rule != "subset"})

    def test_cli_points_without_a_point(self):
        self.edit("installer/app/src/cli.rs", '"27b", "image-stack"]', '"27b"]')
        self.assertIn("cli.rs POINTS", "\n".join(self.problems()))

    def test_boot_icon_missing(self):
        self.edit("cli/crow_boot.py", '    "image-stack": ', '    "image-stackz": ')
        self.assertTrue(any("crow_boot ICONS" in p and "lacks ['image-stack']" in p for p in self.problems()))

    def test_fake_plan_without_a_point(self):
        self.edit("installer/core/src/run.rs", 'if has("flash-next")', 'if false')
        self.assertTrue(any("run.rs fake plan" in p and "flash-next" in p for p in self.problems()))

    def test_mock_selects_an_unknown_point(self):
        self.edit("installer/ui/mock.js", "var points = ['flash-next', '27b']", "var points = ['flash-next', 'video']")
        self.assertTrue(any("mock.js selection" in p and "video" in p for p in self.problems()))

    def test_a_list_that_moved(self):
        self.edit("installer/ui/index.html", "var DESC = {", "var DESCRIPTIONS = {")
        self.assertTrue(any("setup page DESC: list not found" in p for p in self.problems()))


if __name__ == "__main__":
    unittest.main()
