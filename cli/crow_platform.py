#!/usr/bin/env python3
r"""The one place in Crow that knows which operating system this is.

WHY A MODULE AND NOT TWENTY `if sys.platform == "win32"`. The port to Linux
touched paths, process discovery, spawning, killing, the shell, the browser and
the font store -- seven facts about an OS, each of which was written down where
it happened to be needed. Seven scattered branches are seven places to forget.
This file is the seam: every OS-dependent fact is answered here, the core asks
and does not branch, and a reader who wants to know what changes between the two
platforms reads ONE file.

WHY THE NAME IS `crow_platform` AND NOT `platform`. `python cli/crow.py` puts
cli/ on sys.path[0], so a file called cli/platform.py would shadow the standard
library's `platform` module for every client that starts from this directory --
the same trap crow_core.py's own docstring records for cli/json.py.

STANDARD LIBRARY ONLY, like the core: the terminal client's stdlib-only
invariant runs through here, and `/proc` plus `shutil.which` answer everything
`psutil` would have.

THE LAYOUT, and Windows does not move:

    what                       Windows (unchanged)          Linux
    ------------------------------------------------------------------------
    install root               %LOCALAPPDATA%\Crow          ~/.local/share/crow
    secrets.json, settings,    %LOCALAPPDATA%\Crow\...      ~/.config/crow/...
      roots.json, USER.md,
      skills/, mcp.json,
      providers.json,
      approvals.json
    index.db (session search)  %LOCALAPPDATA%\Crow\...      ~/.local/share/crow/...
    session/, booted.json,     %LOCALAPPDATA%\Crow\...      ~/.local/state/crow/...
      git_events.json
    llama-server boot logs     <cwd>\runs\                  ~/.local/state/crow/log/
    models                     <install>\models             $CROW_MODELS, else
                                                            <install>/models
    llama-server binary        <install>\bin\               <install>/bin/, then
      llama-server.exe                                      PATH, then
                                                            ~/.local/share/crow/bin
    fonts                      %LOCALAPPDATA%\Microsoft\    ~/.local/share/fonts/
                                 Windows\Fonts + winreg       crow/ + fc-cache

Every XDG variable is honoured when it is set and absolute, which is what the
specification asks for; an empty or relative value is treated as unset, because
a relative "base directory" resolves against whatever directory the window was
started from -- exactly the class of bug #177 closed for the working area.

WHAT IS DELIBERATELY NOT HERE: anything that decides product behaviour. This
module answers "where" and "how", never "whether". A refusal, a wording or a
release level is the core's, and a platform file that started answering those
would be a second core.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# The directory name, and it differs by platform on purpose: `Crow` is what
# install.ps1 has always created under %LOCALAPPDATA%, and a lower-case name
# is what every other XDG directory beside it is spelled like.
APP_DIR_WINDOWS = "Crow"
APP_DIR_XDG = "crow"


def _home() -> str:
    return os.path.expanduser("~")


def _windows_base() -> str:
    """`%LOCALAPPDATA%\\Crow` -- install.ps1's own `$InstallTo` default.

    The fallback fires only where LOCALAPPDATA is unset, which on Windows means
    a stripped service environment rather than a user session.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        _home(), "AppData", "Local")
    return os.path.join(base, APP_DIR_WINDOWS)


def _xdg_base(variable: str, default_tail: tuple[str, ...]) -> str:
    """`$<variable>`, or the specification's default below ~.

    A value that is not an ABSOLUTE path is ignored, as the specification says:
    "If an implementation encounters a relative path it should consider it
    invalid". The alternative is a base directory that moves with the cwd.
    """
    raw = os.environ.get(variable) or ""
    if raw and os.path.isabs(raw):
        return raw
    return os.path.join(_home(), *default_tail)


def _xdg(variable: str, default_tail: tuple[str, ...]) -> str:
    """`$<variable>/crow` -- Crow's own directory under one XDG base."""
    return os.path.join(_xdg_base(variable, default_tail), APP_DIR_XDG)


def data_dir() -> str:
    """User data that would hurt to lose: the search index, the install itself."""
    return _windows_base() if IS_WINDOWS else _xdg("XDG_DATA_HOME", (".local", "share"))


def config_dir() -> str:
    """What the user configured: secrets, settings, roots, skills, MCP, providers."""
    return _windows_base() if IS_WINDOWS else _xdg("XDG_CONFIG_HOME", (".config",))


def state_dir() -> str:
    """State that outlives a run and nobody backs up: sessions, logs, boots."""
    return _windows_base() if IS_WINDOWS else _xdg("XDG_STATE_HOME", (".local", "state"))


def cache_dir() -> str:
    """Throwaway: anything here may be deleted between two starts."""
    return _windows_base() if IS_WINDOWS else _xdg("XDG_CACHE_HOME", (".cache",))


def install_dir() -> str:
    """Where the installer puts an installation -- its default, not a guess.

    Windows: `%LOCALAPPDATA%\\Crow`, install.ps1's `$InstallTo` default and the
    path its own documentation names. Linux: install.sh's default is the XDG
    data directory, so the install root IS data_dir(). Nothing is written
    outside the user's own tree on either platform, so no elevation and no sudo
    is involved anywhere in this path.
    """
    return _windows_base() if IS_WINDOWS else data_dir()


def log_dir() -> str:
    """Where a server boot writes its .out.log and .err.log.

    WINDOWS KEEPS `runs\\` UNDER THE CURRENT DIRECTORY, which is robins Ansage
    vom 2026-08-28: the traces of a Crow boot lie beside the ones the B5 runs
    already write, not under a random name in %TEMP% that nobody finds after a
    crash. On Linux the window is started from a desktop entry and the current
    directory is then the user's home or `/` -- a `runs/` folder appearing there
    is litter, and the XDG answer for "logs" is the state directory.
    """
    if IS_WINDOWS:
        return os.path.join(os.getcwd(), "runs")
    return os.path.join(state_dir(), "log")


def models_dir(install: str | None = None) -> str:
    """The root every `models.entries.path` in the manifest resolves under.

    `$CROW_MODELS` FIRST, because a model tree is the one part of an install
    that a user moves: 80-110 GiB do not belong beside the program, and on this
    machine they live under ~/Projects/models while the install is in
    ~/.local/share/crow. The variable is the whole mechanism -- there is no
    second copy of it in a settings file, because a path that two places can
    set is a path nobody can find.

    Absent the variable this is `<install>/models`, which is exactly what
    model_candidates has always used.
    """
    raw = (os.environ.get("CROW_MODELS") or "").strip()
    if raw:
        # expanduser so `CROW_MODELS=~/Projects/models` works from a shell
        # profile that did not expand it; a symlinked tree is followed by the
        # filesystem and needs nothing here.
        return os.path.expanduser(raw)
    return os.path.join(install or install_dir(), "models")


# ------------------------------------------------------------- the server ---

def server_binary_name() -> str:
    return "llama-server.exe" if IS_WINDOWS else "llama-server"


def server_search_dirs(install: str | None = None) -> list[str]:
    """Where a llama-server may sit, in order, WITHOUT looking at PATH.

    `<install>/bin` first, and PATH after it (see find_server_binary): a package
    that ships its own binary must not be overtaken by whatever happens to be on
    a developer's PATH -- that is how a measurement ends up describing a build
    nobody shipped.

    On Linux the XDG install's own bin/ is searched EVEN WHEN `install` points
    somewhere else, because that is where install.sh and the llama.cpp build
    both land the binary while Crow itself is usually run from a checkout.
    """
    out = [os.path.join(install or install_dir(), "bin")]
    if not IS_WINDOWS:
        packaged = os.path.join(data_dir(), "bin")
        if packaged not in out:
            out.append(packaged)
    return out


def find_server_binary(install: str | None = None) -> tuple[str | None, list[str]]:
    """The llama-server this build would run, and everywhere it looked.

    The list is RETURNED rather than reduced to the first hit, for the reason
    model_candidates gives: a failure that says only "not found" cannot tell a
    wrong table from a missing download.
    """
    name = server_binary_name()
    tried: list[str] = []
    for directory in server_search_dirs(install):
        candidate = os.path.join(directory, name)
        tried.append(candidate)
        if os.path.isfile(candidate):
            return candidate, tried
    tried.append("PATH")
    found = shutil.which("llama-server") or shutil.which("llama-server.exe")
    return found, tried


def binary_is_for_this_os(path: str) -> bool:
    """Could this spelling of a path name a program on THIS platform?

    #140 lets a server line name its own binary, and the flash-next line names
    `C:/Users/.../dev/crow-lab/.../llama-server.exe`. On Windows a missing
    binary at that path is ITS OWN error -- falling back would boot a build that
    cannot load the architecture. On Linux the same string is not a statement
    about this machine at all: it is a Windows path, it can never exist here,
    and the honest reading is "this line was written on the other platform", not
    "the binary is missing". So the caller falls back to the search above.

    The question is asked of the SPELLING, not of the disk: a drive letter or a
    backslash is Windows, a leading `/` is POSIX.
    """
    text = (path or "").strip()
    if not text:
        return False
    drive = len(text) > 1 and text[1] == ":" and text[0].isalpha()
    windows_shaped = drive or text.startswith("\\\\") or "\\" in text
    if IS_WINDOWS:
        # A POSIX-absolute path is not a Windows path -- but a bare relative
        # name is fine on both, so only the leading slash disqualifies it.
        return not (text.startswith("/") and not drive)
    return not windows_shaped


# The Windows process list, and it is a PowerShell query because there is no
# /proc: Get-CimInstance is the documented way to a command line, and tasklist
# does not print one. Moved here from crow_core.py with its behaviour intact.
_PROCESS_QUERY = ("Get-CimInstance Win32_Process -Filter \"Name like 'llama-server%'\""
                  " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")

# The POSIX fallback, used only where /proc cannot be read (a container with it
# unmounted, a BSD). `ps` is a subprocess; /proc is four file reads and no fork.
_PS_ARGV = ["ps", "-eo", "pid=,args="]


def _quote(arg: str) -> str:
    """Put an argument back the way a command line would have carried it.

    /proc splits the arguments for us, and the core's `-m` reader expects the
    shape a command LINE has -- so a path with a space goes back in quotes
    rather than being handed over as two tokens.
    """
    return '"%s"' % arg if (" " in arg or "\t" in arg) else arg


def _proc_servers(proc_root: str = "/proc", name: str = "llama-server"
                  ) -> "list[tuple[str, str]] | None":
    """Every llama-server in /proc, or None when /proc cannot be read.

    None IS NOT AN EMPTY LIST HERE. "No /proc" and "no server" are different
    answers and the caller does different things with them: the first falls back
    to `ps`, the second refuses to start a second server. Collapsing them would
    make an unreadable /proc look like a free card.
    """
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    mine = str(os.getpid())
    out: list[tuple[str, str]] = []
    for pid in entries:
        if not pid.isdigit() or pid == mine:
            continue
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue                      # the process ended, or is not ours
        if not raw:
            continue                      # a kernel thread has no command line
        args = [a.decode("utf-8", "replace")
                for a in raw.split(b"\0") if a]
        if not args:
            continue
        # THE NAME IS READ OFF argv[0] AND NOT OFF THE WHOLE LINE, which is the
        # same narrowing the Windows query gets from `Name like 'llama-server%'`:
        # a text editor with llama-server.log open is not a server, and a
        # measurement script that mentions one is not one either.
        if not os.path.basename(args[0]).startswith(name):
            continue
        out.append((pid, " ".join(_quote(a) for a in args)))
    return out


def _run_query(argv: list[str]) -> str:
    # stdin=DEVNULL, AND IT IS NOT TIDINESS. Without it the child inherits this
    # process's stdin, and `powershell -Command` READS it: measured 2026-08-21,
    # a picker that asked which model to start got EOF instead of the answer,
    # because listing the processes had already swallowed it.
    done = subprocess.run(argv, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace", timeout=60)
    return done.stdout if done.returncode == 0 else ""


def _parse_process_table(text: str) -> list[tuple[str, str]]:
    """`<pid>\\t<command line>` (or `<pid> <command line>`) into pairs."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or "llama-server" not in line:
            continue
        pid, _, rest = line.partition("\t")
        if not rest:
            pid, _, rest = line.partition(" ")
        out.append((pid.strip(), rest.strip()))
    return out


def find_servers(query=None, proc_root: str = "/proc") -> list[tuple[str, str]]:
    """Every llama-server on this machine, as (pid, command line).

    An unreadable process list comes back EMPTY rather than raising: the caller
    runs this on the path that starts a server, and refusing to boot because a
    query failed would trade a rare risk for a certain one.

    `query` is the injection point for a test and for `running_servers`: any
    callable returning the text a process listing prints.
    """
    if query is None:
        if not IS_WINDOWS:
            found = _proc_servers(proc_root)
            if found is not None:
                return found
            argv = _PS_ARGV
        else:
            argv = ["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", _PROCESS_QUERY]

        def query():
            return _run_query(argv)
    try:
        return _parse_process_table(query())
    except Exception:
        return []


# ---------------------------------------------------- spawning and killing ---

def spawn_kwargs(detached: bool = True) -> dict:
    r"""The Popen keywords that keep a server out of the console that bore it.

    #158, AND IT WAS THE ROOT OF THE SILENT DEATHS. Without these a llama-server
    inherits the console AND the process group of the window, and a Ctrl+C in
    the terminal the window was started from -- an aborted `Get-Content -Wait`,
    a finished measurement run -- hits the server too. It then shuts down
    cleanly with exit code 1, no error line, no dump: the "silent exit-1 class"
    that was blamed on the driver, the compute cache and the PR build in turn.

    Windows: CREATE_NEW_PROCESS_GROUP takes it out of the signal group,
    CREATE_NO_WINDOW out of the console (the second half is for CTRL_CLOSE_EVENT,
    which a closing console sends to everyone attached).

    Linux: `start_new_session=True` is setsid(), which is both halves at once --
    a new session has its own process group, so SIGINT from the terminal does
    not reach it, and it has no controlling terminal to be hung up by.
    """
    if not detached:
        return {}
    if IS_WINDOWS:
        return {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                  | getattr(subprocess, "CREATE_NO_WINDOW", 0))}
    return {"start_new_session": True}


def kill_pid(pid) -> bool:
    """Ask the process with this pid to end. True when the ask went out.

    Windows: `taskkill /PID <pid> /F`, which reaches a process in its own group
    unchanged -- it is not a signal.

    Linux: the GROUP, not the pid, because spawn_kwargs put the server in a
    session of its own and llama-server's own children would otherwise outlive
    it. SIGTERM first: llama.cpp's signal handler shuts down cleanly, and a
    server killed mid-write leaves a slot file nobody can load.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(number), "/F"],
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=30)
        return True
    import signal
    try:
        os.killpg(os.getpgid(number), signal.SIGTERM)
    except ProcessLookupError:
        return False                       # it was already gone
    except (OSError, PermissionError):
        try:
            os.kill(number, signal.SIGTERM)
        except OSError:
            return False
    return True


def terminate_tree(proc, grace: float = 5.0) -> None:
    """End a child this process started, and whatever it started in turn.

    THE HANDLE, NEVER A NAME. #158 was paid once: a measurement script swept
    "every llama-server" and took robins running test server with it. What dies
    here is the process this caller spawned.

    Windows: killing the handle is the whole story (TerminateProcess). Linux:
    SIGTERM to the session's group, then SIGKILL to whatever is still there
    after `grace` -- a browser leaves grandchildren, and a killed leader does
    not take them along.
    """
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    if IS_WINDOWS:
        return
    import signal
    import time
    try:
        group = os.getpgid(proc.pid)
    except (OSError, AttributeError):
        return
    if group == os.getpgrp():
        return                             # not detached: killing it is enough
    # DER ANFUEHRER WIRD ABGEHOLT, BEVOR DIE GRUPPE GEZAEHLT WIRD. `proc.kill()`
    # oben hinterlaesst eine Leiche, bis jemand auf sie wartet -- und eine
    # Leiche ist weiterhin Mitglied der Gruppe: `killpg(group, 0)` unten gelingt
    # auf ihr. Ohne dieses Warten lief die Schleife DESHALB immer die vollen
    # `grace` Sekunden und eskalierte immer auf SIGKILL, auch wenn das SIGTERM
    # die Gruppe laengst geleert hatte (gemessen 2026-09-16: 5,01 s bei
    # grace=5,0, und tool_render_page zahlt das auf dem Zeitlimit-Weg).
    try:
        proc.wait(timeout=grace)
    except Exception:                      # noqa: BLE001 - die Schleife zaehlt
        pass
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, OSError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                os.killpg(group, 0)
            except (ProcessLookupError, OSError):
                return
            time.sleep(0.05)


# --------------------------------------------------------------- the shell ---

def shell_name() -> str:
    """What `run_command` runs a command line through, as the model reads it."""
    return "cmd.exe" if IS_WINDOWS else "bash"


def shell_executable() -> "str | None":
    """The shell binary for `subprocess.run(..., shell=True)`, or None.

    None on Windows means COMSPEC, which is what shell=True has always used --
    handing it an explicit argv instead would change how a space or an ampersand
    in a path is quoted, and that is the one thing a shell call may not change.

    On Linux shell=True is `/bin/sh`, and `sh` is not what the model is told it
    is talking to: it writes bash (`[[`, `$'...'`, process substitution). So the
    shell is named, and only if it is really there.
    """
    if IS_WINDOWS:
        return None
    return shutil.which("bash")


def shell_argv(command: str) -> list[str]:
    """The same command as an explicit argv, for a caller that wants no shell=True."""
    if IS_WINDOWS:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", command]
    return [shell_executable() or "/bin/sh", "-c", command]


# ------------------------------------------------------------- the browser ---

# #175. WHERE A BROWSER LIES, AND IT IS SEARCHED FOR RATHER THAN GUESSED.
# Windows: Chrome first, Edge as the fallback -- both are Chromium and take the
# same switches, but Edge is on every Windows, and a tool that presupposes an
# installation the user does not have is a tool that fails once and is never
# called again.
_BROWSERS_WINDOWS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
)

# Linux has no fixed installation path worth guessing -- a browser may come from
# the distribution, from flatpak or from a tarball -- so the names are looked up
# on PATH. Chromium first for the same reason Chrome is first on Windows:
# `--headless=new --screenshot` is a Chromium switch set. Firefox is last and is
# NOT equivalent (its headless screenshot mode takes different flags); it stands
# here so that `find_browser` can say "there is a browser" on a machine that has
# only Firefox, and the render call then fails with the browser's own words.
_BROWSERS_LINUX = ("chromium", "chromium-browser", "google-chrome-stable",
                   "google-chrome", "brave", "brave-browser", "microsoft-edge",
                   "firefox")


def browser_candidates() -> tuple[str, ...]:
    """Every place a Chromium could be, in order. Nothing is started."""
    return _BROWSERS_WINDOWS if IS_WINDOWS else _BROWSERS_LINUX


def find_browser_path() -> "str | None":
    """The first browser on this machine, or None. Path only, nothing started."""
    for raw in browser_candidates():
        if IS_WINDOWS:
            path = os.path.expandvars(raw)
            if "%" not in path and os.path.isfile(path):
                return path
        else:
            found = shutil.which(raw)
            if found:
                return found
    return None


# --------------------------------------------------------------- the fonts ---

def font_store() -> str:
    """The per-user font directory this platform reads faces from.

    Per-user on both, and on purpose: the machine-wide store needs elevation on
    Windows and root here, and a chat client has no business asking for either.
    """
    if IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Microsoft", "Windows", "Fonts")
    # `$XDG_DATA_HOME/fonts` is the directory fontconfig scans per user; the
    # `crow/` below it is so that an uninstall can remove exactly what Crow put
    # there and nothing the user installed by hand.
    return os.path.join(_xdg_base("XDG_DATA_HOME", (".local", "share")),
                        "fonts", APP_DIR_XDG)


def install_fonts(source_dir: str, names: "list[str]", family: str = "") -> int:
    """Copy the bundled faces into the per-user store. 0 installed, 1 nothing new.

    WINDOWS REGISTERS, LINUX INDEXES, and that is the whole difference. The
    Windows store wants an entry under HKCU\\...\\Fonts with the FULL PATH as the
    value (a bare filename registers a font Windows then cannot find, and it
    fails silently); fontconfig wants no registry at all, only a directory it
    already scans and a cache refresh to see it before the next login.

    `fc-cache` IS BEST EFFORT AND NEVER FATAL: without it the faces are still
    installed, they are simply not visible to a program that started earlier --
    and a missing typeface may never keep a client from starting.
    """
    target = font_store()
    if not names:
        return 2
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return 2
    done = 0
    for name in names:
        dst = os.path.join(target, name)
        if not os.path.isfile(dst):
            try:
                shutil.copyfile(os.path.join(source_dir, name), dst)
            except OSError:
                continue
            done += 1
    if IS_WINDOWS:
        import winreg
        key = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                            winreg.KEY_SET_VALUE) as k:
            for name in names:
                dst = os.path.join(target, name)
                winreg.SetValueEx(k, "%s (%s)" % (family, name), 0,
                                  winreg.REG_SZ, dst)
        return 0 if done else 1
    if done and shutil.which("fc-cache"):
        try:
            subprocess.run(["fc-cache", "-f", target], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0 if done else 1


# ------------------------------------------------------------- the updater ---

def updater_command(script: str, install: str | None = None) -> list[str]:
    """How this platform runs the installer, as data. Nothing is executed here.

    WINDOWS: PowerShell, the installer as a FILE, and told not to wait. Two
    facts, each fatal on its own -- `irm <url> | iex` cannot take parameters
    (install.ps1 says so in its own comment), so the script is fetched to a file
    and run with `-File`; and the installer waits for ENTER at the end so the
    last screen can be read, which behind a window with no console is a wait
    that never ends. `-NoProfile` so a profile that prints or prompts cannot
    join in, `-ExecutionPolicy Bypass` because a downloaded file is exactly what
    the default policy refuses.

    LINUX: `bash install.sh`, no flags. install.sh's contract is
    `install.sh [--to DIR]` and its default target is install_dir(), so an
    update in place is the bare call; bash is named rather than relying on the
    executable bit, because a script fetched over HTTP arrives without one.
    """
    if IS_WINDOWS:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", script, "-NoPause"]
    return ["bash", script]
