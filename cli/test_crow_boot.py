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
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import crow_boot  # noqa: E402
import crow_core  # noqa: E402
import crow_platform  # noqa: E402

STACK = crow_boot.load_stack(crow_boot.DEFAULT_STACK)
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
                   mock.patch.object(crow_platform, "pid_alive", lambda pid: True)]
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
            os.path.join(self.install, "bin", "serve.exe")))
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
        self.assertEqual(argv[0], os.path.join(self.install, "bin", "sd-server.exe"))
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

    def test_the_menu_text_is_the_stack_s_and_only_the_image_stack_has_no_200k(self):
        # owner decision 2026-10-01: the 27B alone runs at 200k; the image stack keeps 65,536
        rows = self.boot().entries()
        titles = [r[2] for r in rows]
        for point in STACK["points"]:
            self.assertIn(point["menu"]["title"], titles)
            self.assertIn(point["menu"]["line"], [r[3] for r in rows])
        line_27b = next(r[3] for r in rows if r[4] == ("point", "27b"))
        line_image = next(r[3] for r in rows if r[4] == ("point", "image-stack"))
        self.assertIn("200k", line_27b)
        self.assertNotIn("200k", line_image)
        env = crow_boot.plan_point(STACK, "image-stack", self.install, self.models)
        self.assertNotIn("CROW_CONTEXT", str(env), "the image stack inherits no 200k")
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
        self.assertTrue(argv[0].endswith("serve.exe"))
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
        self.assertIn("serve.exe", self.out.getvalue())
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
        self.assertEqual(names, ["serve.exe", "sd-server.exe"])
        self.assertLess(events.index(("serve ready", "")),
                        events.index(("popen", "sd-server.exe")))
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
        self.assertEqual([os.path.basename(c[0][0]) for c in popen.calls], ["serve.exe"])
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


class TheTerminalTests(unittest.TestCase):
    def test_a_stream_that_is_no_terminal_gets_plain_ascii_output(self):
        style = crow_boot.Style.detect(io.StringIO())
        self.assertFalse(style.fancy or style.colour or style.animate)
        self.assertTrue(all(ord(c) < 128 for key in crow_boot.ICONS
                            for c in style.icon(key)))
        self.assertEqual(style.paint("x", "32"), "x")
        self.assertIn(crow_boot.flight_frame(5, fancy=False), "|/-\\")


if __name__ == "__main__":
    unittest.main()
