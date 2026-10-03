"""cli/crow_boot.py: the boot menu (#196 phase 1).

Every outside effect -- Popen, the HTTP probe, the clock, input, the process
scan -- is handed in, so these run without a server, a GPU or a console. The
contract file is the real one, written into a temporary config folder.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crow_boot  # noqa: E402
import crow_core  # noqa: E402
import crow_platform  # noqa: E402

STACK = crow_boot.load_stack(crow_boot.DEFAULT_STACK)
# The two binaries as stack.json names them on the platform the suite runs on
# (#341: Windows and Linux both, so the suite runs on both).
EXE = ".exe" if crow_boot.PLATFORM_KEY == "windows" else ""
SERVE, SD_SERVER = "serve" + EXE, "sd-server" + EXE
OK_HEALTH = (200, b'{"status": "ok"}')
NOTHING = (None, b"")


class Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeProc:
    def __init__(self, pid, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code
        self.killed = False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True


class FakePopen:
    """Records every start; writes `log_lines` into the child's stdout file."""

    def __init__(self, events=None, exit_code=None, log_lines=()):
        self.calls = []
        self.procs = []
        self.events = events if events is not None else []
        self.exit_code = exit_code
        self.log_lines = log_lines

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        self.events.append(("popen", os.path.basename(argv[0])))
        sink = kw.get("stdout")
        if hasattr(sink, "write") and self.log_lines:
            sink.write("\n".join(self.log_lines).encode("utf-8") + b"\n")
        proc = FakeProc(4000 + len(self.calls), self.exit_code)
        self.procs.append(proc)
        return proc


LLAMA_27B = {"key": "qwen35-q4-k-xl", "title": "Qwen3.8-27B-UD-Q4_K_XL", "port": 8082,
             "argv": ["llama-server.exe", "-m", "Qwen3.8-27B-UD-Q4_K_XL.gguf", "--port", "8082"]}


class FakeLlama:
    """The optional llama.cpp lines, as crow_boot.LlamaLines answers them."""

    def __init__(self, lines=(), env=None):
        self.lines = [dict(line) for line in lines]
        self._env = env or {}

    def installed(self):
        return [dict(line) for line in self.lines]

    def command(self, key):
        return list(next(line["argv"] for line in self.lines if line["key"] == key))

    def env(self, key):
        return dict(self._env)


class BootCase(unittest.TestCase):
    """A temporary install, models root, config and state folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crow-boot-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = os.path.join(self.tmp, "install")
        self.models = os.path.join(self.tmp, "models")
        self.config = os.path.join(self.tmp, "config")
        self.state = os.path.join(self.tmp, "state")
        self.logs = os.path.join(self.tmp, "logs")
        for d in (self.install, self.models, self.config, self.state, self.logs):
            os.makedirs(d)
        patches = [mock.patch.object(crow_platform, "config_dir", lambda: self.config),
                   mock.patch.object(crow_platform, "state_dir", lambda: self.state),
                   mock.patch.object(crow_platform, "pid_alive", lambda pid: True),
                   # argv[0] is the server here; the Linux scope has its own tests
                   mock.patch.object(crow_platform, "server_scope_prefix", lambda: [])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = Clock()
        self.out = io.StringIO()
        self.terminated = []

    def layout(self, point_id):
        """Create every file the point needs, empty."""
        plan = crow_boot.plan_point(STACK, point_id, self.install, self.models)
        for folder in plan["serve"]["dirs"]:
            os.makedirs(folder, exist_ok=True)
        for path in crow_boot.missing_files(plan, self.install, self.models):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()
        return plan

    def boot(self, **kw):
        defaults = dict(out=self.out, style=crow_boot.Style(), popen=FakePopen(),
                        get=lambda url, timeout: NOTHING, sleep=self.clock.sleep,
                        clock=self.clock, read=lambda prompt="": "0", scan=lambda: [],
                        llama=FakeLlama(),
                        point_for=lambda url, timeout=3.0: None,
                        model_path=lambda url, timeout=3.0: None,
                        terminate=self.terminated.append, log_dir=self.logs)
        defaults.update(kw)
        return crow_boot.Boot(STACK, self.install, self.models, **defaults)

    def contract(self):
        path = os.path.join(self.config, "active-point.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


class ThePlaceholdersResolveFromTheStackTests(BootCase):
    def test_the_27b_plan_is_stack_json_with_install_and_models_filled_in(self):
        plan = crow_boot.plan_point(STACK, "27b", self.install, self.models)
        serve = plan["serve"]
        self.assertEqual(serve["argv"][0], os.path.normpath(
            os.path.join(self.install, "bin", SERVE)))
        self.assertEqual(serve["argv"][1:3], ["--port", "8099"])
        self.assertEqual(serve["argv"][4], os.path.join(self.install, "session"))
        self.assertEqual(serve["env"]["CROW_CNQ"], os.path.join(
            self.models, "Qwen3.8-27B-CNQ4.5", "Qwen3.8-27B-CNQ4.5.cnq"))
        self.assertEqual(serve["env"]["CROW_VIT_MMPROJ"], os.path.join(
            self.models, "Qwen3.8-27B-CNQ4.5", "mmproj-F16.gguf"))
        self.assertEqual(serve["cwd"], self.install)
        self.assertEqual(plan["base_url"], "http://127.0.0.1:8099/v1")
        self.assertIsNone(plan["image"])
        self.assertNotIn("${", json.dumps(plan), "a placeholder was left unresolved")

    def test_the_image_stack_carries_sd_server_and_the_window_env(self):
        plan = crow_boot.plan_point(STACK, "image-stack", self.install, self.models)
        argv = plan["image"]["argv"]
        self.assertEqual(argv[0], os.path.join(self.install, "bin", SD_SERVER))
        self.assertIn("te=cpu", argv, "a value without a placeholder is passed as written")
        self.assertIn(os.path.join(self.models, "qwen-image-2.1", "text_encoder_sdcli",
                                   "model.safetensors.index.json"), argv)
        self.assertEqual(argv[argv.index("--listen-port") + 1], "8097")
        self.assertEqual("--mmap" in argv, crow_platform.IS_WINDOWS,
                         "argv_platform is this platform's")
        self.assertEqual(plan["image"]["port"], 8097)
        self.assertEqual(plan["crow_env"], {"CROW_IMAGE_MODEL_DIR":
                                            os.path.join(self.models, "qwen-image-2.1")})

    def test_an_unknown_point_is_a_setup_error(self):
        with self.assertRaises(crow_boot.SetupError):
            crow_boot.plan_point(STACK, "flash-later", self.install, self.models)

    def test_the_menu_text_is_the_stack_s_and_the_27b_claims_its_128k(self):
        # owner decision 2026-10-01: the 27B alone runs at 128k, the image stack keeps 65,536
        rows = self.boot().entries()
        titles = [r[2] for r in rows]
        for point in STACK["points"]:
            self.assertIn(point["menu"]["title"], titles)
            self.assertIn(point["menu"]["line"], [r[3] for r in rows])
        line_27b = next(r[3] for r in rows if r[4] == ("point", "27b"))
        line_image = next(r[3] for r in rows if r[4] == ("point", "image-stack"))
        self.assertIn("128k", line_27b)
        self.assertNotIn("k context", line_image)
        env = crow_boot.plan_point(STACK, "image-stack", self.install, self.models)
        self.assertNotIn("CROW_CONTEXT", str(env), "the image stack inherits no CROW_CONTEXT")
        self.assertEqual(titles[0], "Start Crow")
        self.assertEqual(titles[-2:], ["Stop the running point", "Quit"])


class OnlyOnePointRunsAtATimeTests(BootCase):
    def test_a_second_start_names_the_running_point_and_how_to_stop_it(self):
        crow_core.write_active_point("27b", "http://127.0.0.1:8099/v1",
                                     {"serve": 5151, "image": None})
        popen = FakePopen()
        code = self.boot(popen=popen).start("image-stack")
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_REFUSED)
        self.assertIn("Qwen3.8-27B (27b)", text)
        self.assertIn('"Stop the running point"', text)
        self.assertIn("--stop", text)
        self.assertIn(crow_boot.kill_hint(5151), text)
        self.assertIn("taskkill /PID 5151 /F" if crow_platform.IS_WINDOWS else "kill 5151", text)
        self.assertEqual(popen.calls, [], "nothing may start beside a running point")

    def test_a_scanned_serve_is_named_by_its_point(self):
        scan = lambda: [("7001", r"C:\x\bin\serve.exe --port 8099"),  # noqa: E731
                        ("7002", r"C:\x\bin\sd-server.exe --listen-port 8097")]
        popen = FakePopen()
        code = self.boot(popen=popen, scan=scan,
                         point_for=lambda url, timeout=3.0: "image-stack").start("27b")
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_REFUSED)
        self.assertIn("Image Stack (image-stack)", text)
        self.assertIn("serve pid 7001", text)
        self.assertIn(crow_boot.kill_hint("7002"), text)
        self.assertEqual(popen.calls, [])

    def test_a_llama_server_blocks_a_baseline_start_and_the_menu_can_stop_it(self):
        scan = lambda: [("6001", "llama-server.exe -m D:/m/model.gguf --port 8081")]  # noqa: E731
        popen = FakePopen()
        code = self.boot(popen=popen, scan=scan).start("27b")
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_REFUSED)
        self.assertIn("a llama.cpp server (model.gguf) on port 8081, pid 6001", text)
        self.assertIn('"Stop the running point"', text)
        self.assertIn(crow_boot.kill_hint("6001"), text)
        self.assertEqual(popen.calls, [])

    def test_a_baseline_point_blocks_an_optional_llama_start(self):
        crow_core.write_active_point("27b", "http://127.0.0.1:8099/v1",
                                     {"serve": 5151, "image": None})
        popen = FakePopen()
        code = self.boot(popen=popen, llama=FakeLlama([LLAMA_27B])).start("qwen35-q4-k-xl")
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_REFUSED)
        self.assertIn("Qwen3.8-27B (27b)", text)
        self.assertIn(crow_boot.kill_hint(5151), text)
        self.assertEqual(popen.calls, [])


class StartingAPointTests(BootCase):
    def test_a_start_spawns_serve_detached_and_writes_the_contract_with_its_pid(self):
        self.layout("27b")
        probes = iter([NOTHING, NOTHING, NOTHING, OK_HEALTH])
        popen = FakePopen()
        with mock.patch.dict(os.environ, {"CROW_HOTSETS": "left-over-from-flash-next"}):
            code = self.boot(popen=popen, get=lambda url, timeout: next(probes),
                             model_path=lambda url, timeout=3.0: "x/Qwen3.8-27B-CNQ4.5.cnq"
                             ).start("27b")
        self.assertEqual(code, crow_boot.EXIT_OK, self.out.getvalue())
        self.assertEqual(len(popen.calls), 1)
        argv, kw = popen.calls[0]
        self.assertEqual(os.path.basename(argv[0]), SERVE)
        self.assertEqual(kw["cwd"], self.install)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["env"]["CROW_CNQ"], os.path.join(
            self.models, "Qwen3.8-27B-CNQ4.5", "Qwen3.8-27B-CNQ4.5.cnq"))
        self.assertNotIn("CROW_HOTSETS", kw["env"], "another point's env leaked in")
        if crow_platform.IS_WINDOWS:
            self.assertTrue(kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(kw["start_new_session"])
        doc = self.contract()
        self.assertEqual(doc["point"], "27b")
        self.assertEqual(doc["base_url"], "http://127.0.0.1:8099/v1")
        self.assertEqual(doc["pids"], {"serve": popen.procs[0].pid, "image": None})
        self.assertIn("Landed!", self.out.getvalue())
        self.assertEqual(self.terminated, [])

    def test_a_timeout_stops_what_it_started_and_writes_no_contract(self):
        self.layout("27b")
        popen = FakePopen(log_lines=["loading", "tensor 1", "CUDA error 2: out of memory"])
        code = self.boot(popen=popen).start("27b")
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("did not answer within 300 s", text)
        self.assertEqual(self.terminated, [popen.procs[0]])
        self.assertIsNone(self.contract())
        self.assertIn("CUDA error 2: out of memory", text, "the log tail is shown")
        self.assertGreaterEqual(self.clock.t, 300.0)

    def test_a_server_that_exits_fails_at_once(self):
        self.layout("27b")
        popen = FakePopen(exit_code=3)
        code = self.boot(popen=popen).start("27b")
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("serve exited with code 3", self.out.getvalue())
        self.assertLess(self.clock.t, 1.0)
        self.assertIsNone(self.contract())

    def test_a_missing_file_is_named_and_nothing_starts(self):
        popen = FakePopen()
        code = self.boot(popen=popen).start("27b")
        self.assertEqual(code, crow_boot.EXIT_SETUP)
        self.assertIn(os.path.join("bin", SERVE), self.out.getvalue())
        self.assertEqual(popen.calls, [])

    def test_the_image_stack_starts_sd_server_only_after_serve_is_ready(self):
        self.layout("image-stack")
        events = []
        health = iter([NOTHING, NOTHING, OK_HEALTH])

        def get(url, timeout):
            events.append(("probe", url.rsplit(":", 1)[1]))
            if url.endswith("/health"):
                answer = next(health)
                if answer is OK_HEALTH:
                    events.append(("serve ready", ""))
                return answer
            return (200, b"{}")
        popen = FakePopen(events=events)
        code = self.boot(popen=popen, get=get).start("image-stack")
        self.assertEqual(code, crow_boot.EXIT_OK, self.out.getvalue())
        names = [e[1] for e in events if e[0] == "popen"]
        self.assertEqual(names, [SERVE, SD_SERVER])
        self.assertLess(events.index(("serve ready", "")),
                        events.index(("popen", SD_SERVER)))
        argv, kw = popen.calls[1]
        self.assertEqual(kw["cwd"], crow_core.image_server_workdir())
        self.assertTrue(os.path.isdir(kw["cwd"]))
        self.assertIn("--listen-port", argv)
        doc = self.contract()
        self.assertEqual(doc["point"], "image-stack")
        self.assertEqual(doc["pids"], {"serve": popen.procs[0].pid,
                                       "image": popen.procs[1].pid})

    def test_no_sd_server_starts_when_serve_never_gets_ready(self):
        self.layout("image-stack")
        popen = FakePopen()
        code = self.boot(popen=popen).start("image-stack")
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertEqual([os.path.basename(c[0][0]) for c in popen.calls], [SERVE])
        self.assertEqual(self.terminated, [popen.procs[0]])
        self.assertIsNone(self.contract())

    def test_an_sd_server_error_answer_is_final_and_both_are_stopped(self):
        self.layout("image-stack")
        popen = FakePopen()
        get = lambda url, timeout: OK_HEALTH if url.endswith("/health") else (500, b"")  # noqa: E731
        code = self.boot(popen=popen, get=get).start("image-stack")
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("answered HTTP 500", self.out.getvalue())
        self.assertEqual(self.terminated, [popen.procs[1], popen.procs[0]])
        self.assertIsNone(self.contract())


class StartCrowTests(BootCase):
    def test_without_a_point_it_says_so_and_starts_nothing(self):
        popen = FakePopen()
        code = self.boot(popen=popen).start_crow()
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("No operating point is running.", text)
        self.assertIn("Start one of the three operating points first", text)
        for point in STACK["points"]:
            self.assertIn(point["menu"]["title"], text)
        self.assertEqual(popen.calls, [])

    def test_with_a_point_it_opens_the_window_on_its_url_with_its_env(self):
        crow_core.write_active_point("image-stack", "http://127.0.0.1:8099/v1",
                                     {"serve": 5151, "image": 6161})
        popen = FakePopen()
        code = self.boot(popen=popen).start_crow()
        self.assertEqual(code, crow_boot.EXIT_OK, self.out.getvalue())
        argv, kw = popen.calls[0]
        self.assertTrue(argv[1].endswith("crow_gui.py"))
        self.assertEqual(argv[2:], ["--base-url", "http://127.0.0.1:8099/v1"])
        self.assertEqual(kw["env"]["CROW_IMAGE_MODEL_DIR"],
                         os.path.join(self.models, "qwen-image-2.1"))

    def test_with_a_llama_server_it_opens_the_window_on_its_port_and_names_it(self):
        scan = lambda: [("6001", "llama-server.exe -m D:/m/Qwen3.8-27B-UD-Q4_K_XL.gguf --port 8082")]  # noqa: E731
        popen = FakePopen()
        code = self.boot(popen=popen, scan=scan).start_crow()
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_OK, text)
        argv, _kw = popen.calls[0]
        self.assertEqual(argv[2:], ["--base-url", "http://127.0.0.1:8082/v1"])
        self.assertIn("a llama.cpp server (Qwen3.8-27B-UD-Q4_K_XL.gguf) on port 8082", text)
        self.assertNotIn("No operating point is running", text)

    def test_an_image_server_alone_still_gets_the_hint(self):
        scan = lambda: [("7002", r"C:\x\bin\sd-server.exe --listen-port 8097")]  # noqa: E731
        popen = FakePopen()
        code = self.boot(popen=popen, scan=scan).start_crow()
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("No operating point is running.", self.out.getvalue())
        self.assertEqual(popen.calls, [])


class TheOptionalLlamaLinesTests(BootCase):
    """The owner, 2026-10-01: the crow-nest points are the baseline; Crow's
    llama.cpp lines are optional, listed below them, tagged and dimmed."""

    def test_they_are_listed_below_the_baseline_tagged_and_dimmed(self):
        out = io.StringIO()
        self.boot(out=out, style=crow_boot.Style(fancy=True, colour=True),
                  llama=FakeLlama([LLAMA_27B])).draw()
        text = out.getvalue()
        self.assertIn("Optional (llama.cpp)", text)
        self.assertLess(text.index("Image Stack"), text.index("Optional (llama.cpp)"))
        self.assertIn("\x1b[2mQwen3.8-27B-UD-Q4_K_XL (optional)", text, "dimmed")
        self.assertIn("llama.cpp, port 8082", text)
        self.assertNotIn("\x1b[2mQwen3.8-27B ", text, "a baseline point is not dimmed")

    def test_the_plain_fallback_keeps_only_the_tag(self):
        out = io.StringIO()
        self.boot(out=out, llama=FakeLlama([LLAMA_27B])).draw()
        text = out.getvalue()
        self.assertIn("Qwen3.8-27B-UD-Q4_K_XL (optional)", text)
        self.assertNotIn("\x1b", text)

    def test_a_line_whose_files_are_missing_is_left_out(self):
        key = "qwen35-q4-k-xl"
        lines = crow_boot.LlamaLines(self.install)
        with mock.patch.dict(os.environ, {"CROW_MODELS": self.models}):
            self.assertNotIn(key, [x["key"] for x in lines.installed()], "nothing on disk yet")
            binary = os.path.join(self.install, "bin", crow_platform.server_binary_name())
            gguf = crow_core.model_candidates(key, None, self.install)[0]
            for path in (binary, gguf):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "wb").close()
            found = {x["key"]: x for x in lines.installed()}
        self.assertIn(key, found)
        self.assertEqual(found[key]["port"], 8082)
        self.assertEqual(found[key]["argv"][:3], [binary, "-m", gguf])
        self.assertNotIn("operating-point", found, "its GGUF is not on disk")
        self.assertNotIn("flash-next-q2-k-xl", found, "its GGUF is not on disk")

    def test_starting_one_uses_the_llama_path_and_writes_no_contract(self):
        popen = FakePopen()
        probes = []

        def get(url, timeout):
            probes.append(url)
            return (200, b"{}") if len(probes) > 2 else (503, b"loading")
        code = self.boot(popen=popen, get=get, llama=FakeLlama([LLAMA_27B], {"CUDA_CACHE_DISABLE": "1"}),
                         model_path=lambda url, timeout=3.0: "D:/m/Qwen3.8-27B-UD-Q4_K_XL.gguf"
                         ).start("qwen35-q4-k-xl")
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_OK, text)
        argv, kw = popen.calls[0]
        self.assertEqual(argv[-len(LLAMA_27B["argv"]):], LLAMA_27B["argv"])
        self.assertEqual(kw["env"]["CUDA_CACHE_DISABLE"], "1", "the line's own env overlay")
        self.assertIsNot(kw["stdout"], kw["stderr"], "out and err logs, as start_server writes them")
        self.assertEqual(set(probes), {"http://127.0.0.1:8082/props"})
        self.assertIn("Landed!", text)
        self.assertIn("llama-server pid %d" % popen.procs[0].pid, text)
        self.assertIsNone(self.contract(), "the contract file is the baseline points' only")
        self.assertTrue(os.path.isfile(os.path.join(self.logs, "llama-server-8082.err.log")))

    def test_a_failed_optional_start_stops_it_and_shows_its_err_log(self):
        popen = FakePopen(exit_code=1)
        code = self.boot(popen=popen, llama=FakeLlama([LLAMA_27B])).start("qwen35-q4-k-xl")
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("llama-server exited with code 1", self.out.getvalue())
        self.assertEqual(self.terminated, [popen.procs[0]])
        self.assertIsNone(self.contract())

    def test_the_menu_numbers_them_after_the_baseline_and_starts_them(self):
        answers = iter(["5", "0"])
        popen = FakePopen()
        code = self.boot(read=lambda prompt="": next(answers), popen=popen,
                         get=lambda url, timeout: (200, b"{}"),
                         llama=FakeLlama([LLAMA_27B])).menu()
        self.assertEqual(code, crow_boot.EXIT_OK)
        self.assertEqual(popen.calls[0][0][-1], "8082")
        rows = self.boot(llama=FakeLlama([LLAMA_27B])).entries()
        self.assertEqual([r[0] for r in rows if r[4] and r[4][0] == "stop"], ["6"])


class TheLandedScreenReturnsToTheMenuTests(BootCase):
    def test_landed_shows_five_seconds_then_the_menu_comes_back_without_a_key(self):
        self.layout("27b")
        answers = iter(["3", "0"])
        asked = []

        def read(prompt=""):
            asked.append(prompt)
            return next(answers)
        code = self.boot(read=read, get=lambda url, timeout: OK_HEALTH).menu()
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_OK)
        self.assertIn("Landed!", text)
        self.assertIn("Back to the menu in 5 s", text)
        self.assertEqual(self.clock.sleeps.count(1), 5, "five one-second ticks")
        self.assertEqual(len(asked), 2, "landing must not wait for Enter")
        after = text.split("Landed!", 1)[1]
        self.assertIn("boot menu", after, "the menu is drawn again after landing")


class StoppingTests(BootCase):
    def test_stop_ends_serve_and_sd_server_and_removes_the_contract(self):
        crow_core.write_active_point("image-stack", "http://127.0.0.1:8099/v1",
                                     {"serve": 7001, "image": 7002})
        killed = []
        listed = [("7001", r"C:\x\bin\serve.exe --port 8099"),
                  ("7002", r"C:\x\bin\sd-server.exe --listen-port 8097")]
        scans = []

        def scan():
            # An ended process stays listed for one more scan (Windows teardown).
            scans.append(list(killed))
            gone = scans[-2] if len(scans) > 1 else []
            return [row for row in listed if row[0] not in gone]
        boot = self.boot(scan=scan, kill=killed.append,
                         alive=lambda pid: str(pid) not in killed)
        code = boot.stop()
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_OK, text)
        self.assertEqual(sorted(killed), ["7001", "7002"])
        self.assertIsNone(self.contract())
        self.assertIn("Stopped Image Stack (image-stack)", text)
        self.assertGreaterEqual(len(scans), 3, "it waits until the scan no longer lists them")

    def test_stop_also_ends_a_llama_server_the_scan_sees(self):
        killed = []
        listed = [("6001", "llama-server.exe -m D:/m/Qwen3.8-27B-UD-Q4_K_XL.gguf --port 8082")]
        code = self.boot(scan=lambda: [r for r in listed if r[0] not in killed],
                         kill=killed.append, alive=lambda pid: str(pid) not in killed).stop()
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_OK, text)
        self.assertEqual(killed, ["6001"])
        self.assertIn("Stopped the llama.cpp server (Qwen3.8-27B-UD-Q4_K_XL.gguf)", text)
        self.assertIn("llama-server pid 6001", text)

    def test_a_process_that_stays_listed_is_reported_not_called_stopped(self):
        killed = []
        listed = [("7001", r"C:\x\bin\serve.exe --port 8099")]
        code = self.boot(scan=lambda: listed, kill=killed.append,
                         alive=lambda pid: False).stop()
        text = self.out.getvalue()
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("still there", text)
        self.assertIn(crow_boot.kill_hint("7001"), text)
        self.assertNotIn("Stopped", text)

    def test_a_pid_only_the_contract_names_is_not_killed(self):
        crow_core.write_active_point("27b", "http://127.0.0.1:8099/v1",
                                     {"serve": 9999, "image": None})
        killed = []
        code = self.boot(kill=killed.append, alive=lambda pid: True).stop()
        self.assertEqual(code, crow_boot.EXIT_OK)
        self.assertEqual(killed, [])
        self.assertIn("left alone", self.out.getvalue())


class TheShortcutTests(BootCase):
    def test_the_shortcut_runs_this_menu_in_windows_terminal_when_there_is_one(self):
        boot = self.boot()
        spec = boot.shortcut_spec(self.tmp, which=lambda name: r"X:\apps\wt.exe")
        self.assertEqual(spec["path"], os.path.join(self.tmp, "Crow.lnk"))
        self.assertEqual(spec["target"], r"X:\apps\wt.exe")
        self.assertIn("--title Crow", spec["arguments"])
        self.assertIn("crow_boot.py", spec["arguments"])
        self.assertEqual(spec["workdir"], self.install)
        self.assertTrue(spec["icon"].endswith("crow.ico"))

    def test_without_windows_terminal_the_interpreter_is_the_target(self):
        spec = self.boot().shortcut_spec(self.tmp, which=lambda name: None)
        self.assertEqual(os.path.basename(spec["target"]).lower().replace("w.exe", ".exe"),
                         os.path.basename(sys.executable).lower().replace("w.exe", ".exe"))
        self.assertTrue(spec["arguments"].strip('"').endswith("crow_boot.py"))

    @unittest.skipUnless(crow_platform.IS_WINDOWS, "a .lnk is Windows")
    def test_the_values_reach_powershell_through_the_environment(self):
        seen = {}

        def run(argv, **kw):
            seen.update(kw["env"])
            open(kw["env"]["CROW_LNK_PATH"], "wb").close()
            return subprocess.CompletedProcess(argv, 0, "", "")
        code = self.boot().create_shortcut(self.tmp, run=run, which=lambda name: None)
        self.assertEqual(code, crow_boot.EXIT_OK)
        self.assertEqual(seen["CROW_LNK_PATH"], os.path.join(self.tmp, "Crow.lnk"))
        self.assertEqual(seen["CROW_LNK_DIR"], self.install)

    @unittest.skipUnless(crow_platform.IS_WINDOWS, "a .lnk is Windows")
    def test_a_cmd_style_variable_in_the_folder_is_expanded(self):
        # PowerShell passes "%USERPROFILE%\Desktop" through unexpanded (2026-10-01)
        seen = {}

        def run(argv, **kw):
            seen.update(kw["env"])
            open(kw["env"]["CROW_LNK_PATH"], "wb").close()
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.dict(os.environ, {"CROW_TEST_SHORTCUT_DIR": self.tmp}):
            code = self.boot().create_shortcut("%CROW_TEST_SHORTCUT_DIR%", run=run,
                                               which=lambda name: None)
        self.assertEqual(code, crow_boot.EXIT_OK)
        self.assertEqual(seen["CROW_LNK_PATH"], os.path.join(self.tmp, "Crow.lnk"))


class StopWaitsForTheTeardownTests(unittest.TestCase):
    def test_a_process_exists_until_it_has_ended_and_stop_asks_that(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL)
        try:
            self.assertTrue(crow_boot.process_exists(child.pid))
        finally:
            child.kill()
            child.wait(timeout=30)
        self.assertFalse(crow_boot.process_exists(child.pid))
        boot = crow_boot.Boot(STACK, "i", "m", out=io.StringIO(), llama=FakeLlama())
        self.assertIs(boot.alive, crow_boot.process_exists,
                      "stop waits on the teardown, not on the exit code")

    @unittest.skipIf(crow_platform.IS_WINDOWS, "a zombie is a POSIX state")
    def test_a_killed_child_nobody_waited_for_is_gone(self):
        # #341: the window is the parent of the serve it started and nothing
        # reaps it after Stop. A zombie answers signal 0, so pid_alive alone
        # kept Stop waiting its 30 s and reporting the server still there.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            child.send_signal(9)
            stat = os.path.join("/proc", str(child.pid), "stat")
            for _ in range(200):
                with open(stat, encoding="utf-8", errors="replace") as fh:
                    if fh.read().rsplit(")", 1)[-1].split()[0] == "Z":
                        break
                time.sleep(0.025)
            self.assertTrue(crow_platform.pid_alive(child.pid), "a zombie still answers signal 0")
            self.assertFalse(crow_boot.process_exists(child.pid))
        finally:
            child.kill()
            child.wait(timeout=30)


class BothPlatformsTests(BootCase):
    """#341: stack.json names the Windows and the Linux binaries, and Boot starts
    serve and sd-server on Linux the way the llama.cpp lines start: behind
    crow_platform.server_scope_prefix(), with stack.json's lib_path in front of
    LD_LIBRARY_PATH. Windows starts exactly as before."""

    SCOPE = ["systemd-run", "--user", "--scope", "--"]

    def platform(self, key):
        p = mock.patch.object(crow_boot, "PLATFORM_KEY", key)
        p.start()
        self.addCleanup(p.stop)

    def test_every_point_plans_on_both_platforms(self):
        for key, exe in (("windows", ".exe"), ("linux", "")):
            libs = {"windows": [],
                    "linux": [os.path.join(self.install, "bin"),
                              os.path.join(self.install, "cuda", "lib")]}[key]
            for point in ("flash-next", "27b", "image-stack"):
                with self.subTest(platform=key, point=point), \
                        mock.patch.object(crow_boot, "PLATFORM_KEY", key):
                    plan = crow_boot.plan_point(STACK, point, self.install, self.models)
                    self.assertEqual(plan["serve"]["argv"][0],
                                     os.path.join(self.install, "bin", "serve" + exe))
                    self.assertEqual(plan["lib_path"], libs)
                    if plan["image"]:
                        argv = plan["image"]["argv"]
                        self.assertEqual(argv[0], os.path.join(self.install, "bin", "sd-server" + exe))
                        self.assertEqual("--mmap" in argv, key == "windows")

    def test_on_linux_only_the_engine_binary_is_missing_from_a_full_model_tree(self):
        self.platform("linux")
        for point in ("27b", "image-stack"):
            with self.subTest(point=point):
                plan = self.layout(point)
                os.remove(plan["serve"]["argv"][0])
                self.assertEqual(crow_boot.missing_files(plan, self.install, self.models),
                                 [os.path.join(self.install, "bin", "serve")])

    def start_image_stack(self, inherited):
        data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(data, "cuda", "lib"))
        self.layout("image-stack")
        popen = FakePopen()
        probes = iter([OK_HEALTH, (200, b"{}")])
        with mock.patch.dict(os.environ, inherited), \
                mock.patch.object(crow_platform, "data_dir", lambda: data), \
                mock.patch.object(crow_platform, "server_scope_prefix", lambda: list(self.SCOPE)):
            code = self.boot(popen=popen, get=lambda url, timeout: next(probes)).start("image-stack")
            expected_image_env = crow_core._image_server_env()
        self.assertEqual(code, crow_boot.EXIT_OK, self.out.getvalue())
        return popen, data, expected_image_env

    def test_on_linux_serve_and_sd_server_start_scoped_with_the_library_path(self):
        self.platform("linux")
        popen, data, _ = self.start_image_stack({"LD_LIBRARY_PATH": "/opt/other"})
        plan = crow_boot.plan_point(STACK, "image-stack", self.install, self.models)
        libs = [os.path.join(self.install, "bin"), os.path.join(self.install, "cuda", "lib")]
        (serve_argv, serve_kw), (image_argv, image_kw) = popen.calls
        self.assertEqual(serve_argv, self.SCOPE + plan["serve"]["argv"])
        self.assertEqual(image_argv, self.SCOPE + plan["image"]["argv"])
        self.assertEqual(serve_kw["env"]["LD_LIBRARY_PATH"], os.pathsep.join(libs + ["/opt/other"]))
        image_libs = libs + ([] if crow_platform.IS_WINDOWS else [os.path.join(data, "cuda", "lib")])
        self.assertEqual(image_kw["env"]["LD_LIBRARY_PATH"],
                         os.pathsep.join(image_libs + ["/opt/other"]))

    def test_on_windows_the_start_is_what_it_was(self):
        # The scope prefix is [] on Windows (crow_platform); lib_path names no
        # Windows folder, so argv and both environments are the pre-#341 ones.
        self.platform("windows")
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/opt/other"}):
            env = {k: v for k, v in os.environ.items()
                   if k not in crow_boot.engine_env_keys(STACK)}
        self.SCOPE = []
        popen, _data, image_env = self.start_image_stack({"LD_LIBRARY_PATH": "/opt/other"})
        plan = crow_boot.plan_point(STACK, "image-stack", self.install, self.models)
        env.update(plan["serve"]["env"])
        (serve_argv, serve_kw), (image_argv, image_kw) = popen.calls
        self.assertEqual(plan["lib_path"], [])
        self.assertEqual(serve_argv, plan["serve"]["argv"])
        self.assertEqual(image_argv, plan["image"]["argv"])
        self.assertEqual(serve_kw["env"], env)
        self.assertEqual(image_kw["env"], image_env)


class TheTerminalTests(unittest.TestCase):
    def test_a_stream_that_is_no_terminal_gets_plain_ascii_output(self):
        style = crow_boot.Style.detect(io.StringIO())
        self.assertFalse(style.fancy or style.colour or style.animate)
        self.assertTrue(all(ord(c) < 128 for key in crow_boot.ICONS
                            for c in style.icon(key)))
        self.assertEqual(style.paint("x", "32"), "x")
        self.assertIn(crow_boot.flight_frame(5, fancy=False), "|/-\\")


class ThePlanAsJsonTests(BootCase):
    """#196 phase 2: `--plan <point> --json` is what CrowSetup's check step reads.
    It must be the menu's own resolution, not a second copy of it."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            code = crow_boot.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_27b_plan_is_plan_point_with_the_point_s_files(self):
        doc = crow_boot.plan_json(STACK, "27b", self.install, self.models)
        plan = crow_boot.plan_point(STACK, "27b", self.install, self.models)
        self.assertEqual(doc["point"], "27b")
        self.assertEqual(doc["serve"]["binary"], plan["serve"]["argv"][0])
        self.assertEqual(doc["serve"]["argv"], plan["serve"]["argv"][1:])
        self.assertEqual(doc["serve"]["cwd"], self.install)
        self.assertEqual(doc["serve"]["port"], 8099)
        self.assertEqual(doc["serve"]["readiness"]["json"], {"status": "ok"})
        point = next(p for p in STACK["points"] if p["id"] == "27b")
        # only the point's own env keys, never the shell's
        self.assertEqual(set(doc["serve"]["env"]), set(point["engine"]["env"]))
        self.assertIsNone(doc["image"])
        self.assertEqual([f["id"] for f in doc["files"]], point["files"])
        by_id = {f["id"]: f for f in STACK["files"]}
        for f in doc["files"]:
            self.assertEqual(f["bytes"], by_id[f["id"]]["bytes"])
            self.assertEqual(f["sha256"], by_id[f["id"]]["sha256"])
            self.assertTrue(f["dest"].startswith(self.models), f["dest"])
        cnq = next(f for f in doc["files"] if f["id"] == "27b-cnq")
        self.assertEqual(cnq["dest"], doc["serve"]["env"]["CROW_CNQ"])
        self.assertEqual(doc["derived"], [])
        self.assertNotIn("${", json.dumps(doc), "a placeholder was left unresolved")

    def test_the_image_stack_carries_sd_server_and_the_derived_encoder(self):
        doc = crow_boot.plan_json(STACK, "image-stack", self.install, self.models)
        plan = crow_boot.plan_point(STACK, "image-stack", self.install, self.models)
        self.assertEqual(doc["image"]["binary"], os.path.join(self.install, "bin", SD_SERVER))
        self.assertEqual(doc["image"]["argv"], plan["image"]["argv"][1:])
        self.assertEqual(doc["image"]["port"], 8097)
        self.assertEqual(doc["crow_env"], plan["crow_env"])
        (derived,) = doc["derived"]
        sdcli = os.path.join(self.models, "qwen-image-2.1", "text_encoder_sdcli")
        self.assertEqual(derived["dest"], sdcli)
        self.assertIn("qi-text-encoder-1", derived["inputs"])
        index = os.path.join(sdcli, "model.safetensors.index.json")
        self.assertIn(index, [o["dest"] for o in derived["outputs"]])
        # sd-server's --llm is one of those outputs: the check verifies what it loads
        llm = doc["image"]["argv"][doc["image"]["argv"].index("--llm") + 1]
        self.assertEqual(llm, index)

    def test_main_prints_only_the_json_and_honours_install_root_and_models(self):
        code, out, err = self.run_main(["--install-root", self.install, "--models", self.models,
                                        "--plan", "27b", "--json"])
        self.assertEqual(code, crow_boot.EXIT_OK, err)
        doc = json.loads(out)
        self.assertEqual(doc, crow_boot.plan_json(STACK, "27b", os.path.abspath(self.install),
                                                  os.path.abspath(self.models)))
        self.assertEqual(doc["install_root"], os.path.abspath(self.install))
        self.assertEqual(doc["models_root"], os.path.abspath(self.models))

    def test_an_unknown_point_is_exit_2_with_nothing_on_stdout(self):
        code, out, err = self.run_main(["--install-root", self.install, "--plan", "nope", "--json"])
        self.assertEqual(code, crow_boot.EXIT_SETUP)
        self.assertEqual(out, "")
        self.assertIn("nope", err)

    def test_plan_and_json_only_go_together(self):
        for argv in (["--plan", "27b"], ["--json"]):
            with self.assertRaises(SystemExit) as cm:
                self.run_main(argv)
            self.assertEqual(cm.exception.code, 2)


class TheModelsFlagLeavesALabRootAloneTests(BootCase):
    """#196 P2-E2E: CrowSetup always installs into <install>/models and hands
    `--models` to every call. A CROW_MODELS the user set (a lab root for the
    llama.cpp lines) stays theirs; without one, `--models` serves both kinds."""

    def run_main(self, argv, crow_models):
        out = io.StringIO()
        with mock.patch.dict(os.environ), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            os.environ.pop("CROW_MODELS", None)
            if crow_models:
                os.environ["CROW_MODELS"] = crow_models
            code = crow_boot.main(argv)
            after = os.environ.get("CROW_MODELS")
        return code, json.loads(out.getvalue()), after

    def argv(self):
        return ["--install-root", self.install, "--models", self.models, "--plan", "27b", "--json"]

    def test_a_lab_root_stays_for_the_llama_lines(self):
        lab = os.path.join(self.tmp, "lab-models")
        code, doc, after = self.run_main(self.argv(), lab)
        self.assertEqual(code, 0)
        self.assertEqual(doc["models_root"], self.models, "the baseline points use --models")
        self.assertEqual(after, lab, "--models overwrote the user's CROW_MODELS")

    def test_without_one_the_models_root_serves_both(self):
        code, doc, after = self.run_main(self.argv(), None)
        self.assertEqual(code, 0)
        self.assertEqual(doc["models_root"], self.models)
        self.assertEqual(after, self.models)


class TheWindowsHooksTests(BootCase):
    """#196, the operating-point window: its Cancel button and its stage line
    reach Boot through `cancelled` and `on_stage`; the menu passes neither."""

    def test_cancelled_ends_a_start_like_ctrl_c_and_stops_what_it_began(self):
        self.layout("27b")
        probes = []

        def get(url, timeout):
            probes.append(url)
            return NOTHING
        popen = FakePopen()
        code = self.boot(popen=popen, get=get, cancelled=lambda: len(probes) >= 2).start("27b")
        self.assertEqual(code, crow_boot.EXIT_FAILED)
        self.assertIn("did not land: cancelled", self.out.getvalue())
        self.assertEqual(self.terminated, [popen.procs[0]])
        self.assertIsNone(self.contract())
        self.assertLess(self.clock.t, 5.0, "it ends at once, not at the timeout")

    def test_on_stage_hears_serve_then_sd_server(self):
        self.layout("image-stack")
        stages = []
        get = lambda url, timeout: OK_HEALTH if url.endswith("/health") else (200, b"{}")  # noqa: E731
        code = self.boot(get=get, on_stage=stages.append).start("image-stack")
        self.assertEqual(code, crow_boot.EXIT_OK, self.out.getvalue())
        self.assertEqual(stages, ["serve", "sd-server"])

    def test_the_menu_never_cancels(self):
        boot = crow_boot.Boot(STACK, "i", "m", out=io.StringIO(), llama=FakeLlama())
        self.assertFalse(boot.cancelled())


class TheShortcutOpensTheWindowTests(BootCase):
    """#196: `--create-shortcut` now opens the operating-point window under
    pythonw.exe (no console); `--terminal` keeps the menu's shortcut."""

    def written(self, boot, **kw):
        seen = {}

        def run(argv, **run_kw):
            seen.update(run_kw["env"])
            open(run_kw["env"]["CROW_LNK_PATH"], "wb").close()
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(crow_platform, "IS_WINDOWS", True):
            code = boot.create_shortcut(self.tmp, run=run, which=lambda name: r"X:\apps\wt.exe", **kw)
        self.assertEqual(code, crow_boot.EXIT_OK, self.out.getvalue())
        return seen

    def test_the_shortcut_runs_the_window_under_pythonw_with_the_roots(self):
        boot = self.boot(forwarded_args=["--install-root", self.install, "--models", self.models])
        seen = self.written(boot)
        with mock.patch.object(crow_platform, "IS_WINDOWS", True):
            self.assertEqual(seen["CROW_LNK_TARGET"], crow_boot.quiet_python(sys.executable))
        self.assertNotIn("wt.exe", seen["CROW_LNK_TARGET"])
        args = seen["CROW_LNK_ARGS"]
        self.assertIn("crow_boot.py", args)
        self.assertIn(" --gui", args)
        self.assertTrue(args.index("--gui") < args.index("--install-root") < args.index("--models"))
        self.assertIn(self.install, args)
        self.assertEqual(seen["CROW_LNK_DIR"], self.install)

    def test_terminal_keeps_the_menu_in_windows_terminal(self):
        seen = self.written(self.boot(), terminal=True)
        self.assertEqual(seen["CROW_LNK_TARGET"], r"X:\apps\wt.exe")
        self.assertNotIn("--gui", seen["CROW_LNK_ARGS"])

    def test_pythonw_is_the_one_beside_the_interpreter(self):
        folder = os.path.join(self.tmp, "py")
        os.makedirs(folder)
        python = os.path.join(folder, "python.exe")
        open(python, "wb").close()
        with mock.patch.object(crow_platform, "IS_WINDOWS", True):
            self.assertEqual(crow_boot.quiet_python(python), python, "no pythonw.exe there")
            open(os.path.join(folder, "pythonw.exe"), "wb").close()
            self.assertEqual(crow_boot.quiet_python(python), os.path.join(folder, "pythonw.exe"))
        with mock.patch.object(crow_platform, "IS_WINDOWS", False):
            self.assertEqual(crow_boot.quiet_python(python), python)

    def test_gui_opens_the_window_over_this_boot(self):
        opened = []
        fake = mock.Mock(open_window=lambda boot: opened.append(boot) or crow_boot.EXIT_OK)
        with mock.patch.dict(sys.modules, {"crow_boot_gui": fake}):
            code = crow_boot.main(["--install-root", self.install, "--gui"])
        self.assertEqual(code, crow_boot.EXIT_OK)
        (boot,) = opened
        self.assertEqual(boot.install, os.path.abspath(self.install))

    def test_terminal_only_goes_with_create_shortcut(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err), self.assertRaises(SystemExit) as cm:
            crow_boot.main(["--terminal"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--terminal goes with --create-shortcut", err.getvalue())


if __name__ == "__main__":
    unittest.main()
