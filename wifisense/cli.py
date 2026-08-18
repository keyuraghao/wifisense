"""Single entry point for the whole project.

    wifisense <group> <command> [options]
    wifisense --help

Commands are grouped by what you are actually doing, because the project spans
three quite different activities and a flat list of seventeen names is not
navigable:

    sense    single-link RSSI sensing: record, inspect, train, watch live
    mesh     the ESP32 node mesh: key, dashboard, simulated nodes
    node     node firmware: configure, build, flash
    study    physics and design studies that need no hardware

Each command's implementation lives in wifisense/commands/ and is imported
lazily. That matters more than it looks: importing matplotlib, sklearn and
scapy up front would put a two-second pause in front of `--help`, and several
commands are unusable without root, so eager imports would also fail noisily
for anyone just looking around.
"""
from __future__ import annotations

import argparse
import importlib
import sys

# group -> command -> (module, one-line help)
COMMANDS: dict[str, dict[str, tuple[str, str]]] = {
    "sense": {
        "check":    ("check_hw",       "report what this radio can and cannot do"),
        "collect":  ("collect",        "record a labelled session (needs root)"),
        "plot":     ("plot_session",   "render a recorded session for inspection"),
        "dataset":  ("build_dataset",  "turn sessions into a windowed feature table"),
        "train":    ("train",          "evaluate against the baseline and chance"),
        "stream":   ("stream",         "continuous capture to a tailable CSV (root)"),
        "view":     ("live_view",      "live dashboard; follow, replay or capture"),
        "detect":   ("live_detect",    "terminal one-line live readout (root)"),
        "selftest": ("selftest",       "validate the pipeline on synthetic data"),
    },
    "mesh": {
        "key":       ("gen_mesh_key",   "generate the mesh master key (run once)"),
        "dashboard": ("rti_dashboard",  "live mesh dashboard and 3D reconstruction"),
        "simulate":  ("rti_fake_nodes", "virtual node mesh, with failure injection"),
    },
    "node": {
        "setup": ("setup_firmware", "write firmware config.h (WiFi, server IP)"),
        "build": ("build_firmware", "build firmware for every ESP32 family"),
        "flash": ("flash_node",     "detect the plugged-in chip and flash it"),
    },
    "study": {
        "physics": ("rf_resolution", "what power, bandwidth and aperture buy you"),
        "rti":     ("rti_sim",       "3D tomography design study for your room"),
    },
}

GROUP_HELP = {
    "sense": "single-link RSSI sensing with this laptop and your router",
    "mesh":  "the ESP32 node mesh and live 3D reconstruction",
    "node":  "node firmware: configure, build, flash",
    "study": "physics and design studies, no hardware needed",
}

# Old script names still in muscle memory and in older notes.
ALIASES = {
    "check_hw": ("sense", "check"), "collect": ("sense", "collect"),
    "plot_session": ("sense", "plot"), "build_dataset": ("sense", "dataset"),
    "train": ("sense", "train"), "stream": ("sense", "stream"),
    "live_view": ("sense", "view"), "live_detect": ("sense", "detect"),
    "selftest": ("sense", "selftest"), "gen_mesh_key": ("mesh", "key"),
    "rti_dashboard": ("mesh", "dashboard"), "rti_fake_nodes": ("mesh", "simulate"),
    "setup_firmware": ("node", "setup"), "build_firmware": ("node", "build"),
    "flash_node": ("node", "flash"), "rf_resolution": ("study", "physics"),
    "rti_sim": ("study", "rti"),
}


def _run(module_name: str, argv: list[str]) -> int:
    mod = importlib.import_module(f".commands.{module_name}", package="wifisense")
    return mod.main(argv)


def _print_overview() -> None:
    print("wifisense - WiFi RF sensing research toolkit\n")
    width = max(len(c) for g in COMMANDS.values() for c in g)
    for group, cmds in COMMANDS.items():
        print(f"  {group}  {GROUP_HELP[group]}")
        for name, (_mod, help_text) in cmds.items():
            print(f"      {name:<{width}}  {help_text}")
        print()
    print("Run 'wifisense <group> <command> --help' for a command's options.")
    print("\nStart here:")
    print("  wifisense sense selftest        validate everything, no hardware")
    print("  wifisense sense check           what your radio supports")
    print("  wifisense study physics         why more power will not help")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_overview()
        return 0
    if argv[0] in ("-V", "--version"):
        print("wifisense 0.4.0")
        return 0

    head = argv[0]

    # Old flat script name, with or without the .py.
    flat = head[:-3] if head.endswith(".py") else head
    if flat in ALIASES:
        group, cmd = ALIASES[flat]
        if head not in COMMANDS:
            print(f"note: '{flat}' is now 'wifisense {group} {cmd}'", file=sys.stderr)
            return _run(COMMANDS[group][cmd][0], argv[1:])

    if head not in COMMANDS:
        print(f"unknown group '{head}'\n", file=sys.stderr)
        _print_overview()
        return 2

    group = head
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(f"wifisense {group} - {GROUP_HELP[group]}\n")
        width = max(len(c) for c in COMMANDS[group])
        for name, (_mod, help_text) in COMMANDS[group].items():
            print(f"  {name:<{width}}  {help_text}")
        return 0

    cmd = argv[1]
    if cmd not in COMMANDS[group]:
        print(f"unknown command '{cmd}' in group '{group}'\n", file=sys.stderr)
        for name, (_mod, help_text) in COMMANDS[group].items():
            print(f"  {name:<12}  {help_text}", file=sys.stderr)
        return 2

    return _run(COMMANDS[group][cmd][0], argv[2:])


def entrypoint() -> None:
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    entrypoint()
