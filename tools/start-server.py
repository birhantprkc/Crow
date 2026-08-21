#!/usr/bin/env python3
"""Pick an operating point, then BECOME llama-server.

The picker asks which model. After that this script gets out of the way: the
server runs in the foreground, its log goes to this console, and Ctrl+C stops it
-- exactly what starting llama-server by hand does. Nothing waits for /health,
nothing prints a summary, no client and no window is opened.

The command line comes from manifests/operating-point.json, the same source
README.md and install.ps1 are held to, so it cannot drift from the documented
line without tools/check_operating_point.py going red.

A server that is already running is stopped first when a DIFFERENT model is
picked: two at once overbook the card, and the driver moves what does not fit
into host memory without printing a word.

Usage:  python tools/start-server.py            pick from the list
        python tools/start-server.py <key>      no question asked

Exit code is llama-server's own.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "cli"))

import crow_core  # noqa: E402


def main(argv: list[str]) -> int:
    keys = crow_core.bootable_models()
    if not keys:
        print("manifests/operating-point.json declares no server lines.")
        return 1

    running = crow_core.running_servers()

    if len(argv) > 1:
        key = argv[1]
        if key not in keys:
            print("no model %r. There is: %s" % (key, ", ".join(keys)))
            return 1
    else:
        for pid, line in running:
            print("running (pid %s): %s"
                  % (pid, crow_core.served_model(line) or "unreadable command line"))
        for i, k in enumerate(keys, 1):
            print("  %d) %s" % (i, crow_core.model_label(k)))
        try:
            # The BOM is stripped because PowerShell puts one at the head of a
            # pipe: the first line a script is fed arrives as '﻿1', which
            # is not a digit. Typing is unaffected.
            answer = input("> ").strip().lstrip("﻿").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 1
        if not answer.isdigit() or not 1 <= int(answer) <= len(keys):
            return 1
        key = keys[int(answer) - 1]

    if running:
        if all(crow_core.served_model_matches(key, crow_core.served_model(l))
               for _p, l in running):
            print("%s is already running." % crow_core.model_label(key))
            return 0
        crow_core.stop_servers(print)

    try:
        command = crow_core.server_command(key)
    except crow_core.ServerBootError as exc:
        print(exc)
        return 1

    # `--slot-save-path` REFUSES a directory that does not exist -- exit 1
    # before a single tensor is read, and the message is about an argument.
    slot = command.index("--slot-save-path") + 1 if "--slot-save-path" in command else 0
    if slot:
        try:
            os.makedirs(command[slot], exist_ok=True)
        except OSError:
            pass

    # AND NOW GET OUT OF THE WAY. No pipes, no capture: the child inherits this
    # console, so the log lands where a hand-started server's log lands and
    # Ctrl+C reaches it the same way. This process just waits and hands back
    # whatever the server exited with.
    return subprocess.call(command)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
