# Crow's tasks. `just` alone lists them. Nothing here is required to use Crow --
# the README's start lines are, and these only save typing the long ones.
#
# EVERYTHING RUNS UNDER THE RUNTIME VENV, not the system python: the GUI suite
# imports `webview`, and one of its cases needs PyGObject beside it. That venv
# is made with --system-site-packages by install.sh, which is what lets a pip
# package and a distribution package be in the same interpreter.

crow_home := env_var_or_default("CROW_HOME", env_var("HOME") + "/.local/share/crow")
py        := crow_home + "/venv/bin/python"

default:
    @just --list

# Everything a change has to survive: lint, the three suites, the three checkers and the install selftest.
check: lint test
    {{py}} tools/check_shared_core.py
    {{py}} tools/check_operating_point.py
    {{py}} tools/check_gui_prereqs.py
    bash install.sh --selftest

# 2,620 cases (2026-09-24: 1,386 core, 452 terminal, 782 window) over the core, the terminal client and the window.
test:
    cd cli && {{py}} -m unittest test_crow_core test_crow test_crow_gui

# E9/F63/F7/F82 only -- see the reasoning in pyproject.toml.
lint:
    @{{py}} -c "import ruff" 2>/dev/null || {{py}} -m pip install --quiet ruff
    {{py}} -m ruff check .

# The window, from this checkout.
run *ARGS:
    {{py}} cli/crow_gui.py {{ARGS}}

# Built from manifests/operating-point.json -- never a second copy of the line.
# Without a name it lists what is bootable.

# A server, in the foreground, with its log on this console.
serve MODEL="":
    {{py}} tools/start-server.py {{MODEL}}

# llama.cpp pin 6c84c7d5d + PR #27880 + PR #28040. No root, about 20 minutes,
# every byte of it under $CROW_HOME.

# Build the CUDA llama-server.
engine:
    CROW_HOME={{crow_home}} bash tools/build-llama-server.sh

# Into $CROW_HOME, with the .desktop entry, the icons and the Hyprland rule.
install *ARGS:
    bash install.sh {{ARGS}}
