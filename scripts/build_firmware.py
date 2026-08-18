#!/usr/bin/env python3
"""Build the node firmware for every ESP32 family, into a flashable bundle.

    .venv/bin/python scripts/build_firmware.py
    .venv/bin/python scripts/build_firmware.py --targets esp32s3 esp32c3

Produces firmware/build/<family>/ plus firmware/build/manifest.json, which
scripts/flash_node.py reads to flash whatever board you plug in without you
having to know or care which one it is.

Support is determined by COMPILING, not by a hard-coded list. Espressif ships
new parts and the Arduino core changes what it supports; a list in this file
would be wrong within a year, whereas "did it build?" stays true. Families
without a WiFi radio (ESP32-H2, ESP32-P4) fail here for a real reason and are
recorded with that reason rather than silently omitted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKETCH = ROOT / "firmware" / "esp32_rti_node"
BUILD = ROOT / "firmware" / "build"
CORE_VER = "3.3.11"

# One generic "Dev Module" per silicon family. Vendor boards (Feather, QT Py,
# ...) are all the same silicon and the same binary works, so the family is the
# right granularity. Native-USB parts need CDCOnBoot so Serial reaches USB.
TARGETS = {
    "esp32":   {"fqbn": "esp32:esp32:esp32",   "opts": "",                "usb": "bridge"},
    "esp32s2": {"fqbn": "esp32:esp32:esp32s2", "opts": "CDCOnBoot=cdc",   "usb": "native"},
    "esp32s3": {"fqbn": "esp32:esp32:esp32s3", "opts": "CDCOnBoot=cdc",   "usb": "native"},
    "esp32c3": {"fqbn": "esp32:esp32:esp32c3", "opts": "CDCOnBoot=cdc",   "usb": "native"},
    "esp32c6": {"fqbn": "esp32:esp32:esp32c6", "opts": "CDCOnBoot=cdc",   "usb": "native"},
    "esp32c5": {"fqbn": "esp32:esp32:esp32c5", "opts": "CDCOnBoot=cdc",   "usb": "native"},
    "esp32c2": {"fqbn": "esp32:esp32:esp32c2", "opts": "",                "usb": "bridge"},
    "esp32h2": {"fqbn": "esp32:esp32:esp32h2", "opts": "CDCOnBoot=cdc",   "usb": "native"},
    "esp32p4": {"fqbn": "esp32:esp32:esp32p4", "opts": "CDCOnBoot=cdc",   "usb": "native"},
}

# Why a family can fail to build. Classifying this matters: "no WiFi radio" is
# permanent and physical, while "SDK package missing" is a toolchain gap that
# may be fixed by an Espressif release. Lumping both into "unsupported" would
# hide that difference from anyone reading the manifest later.
def classify_failure(log: str) -> tuple[str, str]:
    # arduino-cli wraps some errors in a box-drawing frame, which splits phrases
    # across lines mid-sentence. Strip the frame and collapse whitespace before
    # matching, or "does not exist" is invisible to a substring test.
    flat = re.sub(r"[\u2500-\u257F|]", " ", log)
    flat = re.sub(r"\s+", " ", flat)

    if "does not exist" in flat and "runtime.tools" in flat:
        m = re.search(r"runtime\.tools\.([a-z0-9_-]+)\.path", flat)
        pkg = m.group(1) if m else "an SDK package"
        return ("missing_sdk",
                f"Arduino core ships no '{pkg}' package, so this family cannot "
                f"be built even though the source compiles. Not a code problem.")
    if ("'WiFi' was not declared" in flat
            or "undefined reference to `esp_now_init'" in flat
            or "incomplete type 'wifi_pkt_rx_ctrl_t'" in flat):
        return ("no_wifi_radio",
                "This chip has no WiFi radio, so ESP-NOW and RSSI measurement "
                "are physically impossible. It cannot be a mesh node.")
    first = next((l.strip() for l in log.splitlines() if "error:" in l.lower()), "")
    return ("build_error", first[:200] or "unknown build failure")


# esptool reports these names; map them to our family keys.
CHIP_TO_FAMILY = {
    "ESP32": "esp32", "ESP32-S2": "esp32s2", "ESP32-S3": "esp32s3",
    "ESP32-C2": "esp32c2", "ESP32-C3": "esp32c3", "ESP32-C5": "esp32c5",
    "ESP32-C6": "esp32c6", "ESP32-H2": "esp32h2", "ESP32-P4": "esp32p4",
}


def arduino_cli() -> str:
    exe = shutil.which("arduino-cli") or str(Path.home() / ".local/bin/arduino-cli")
    if not Path(exe).exists():
        sys.exit("arduino-cli not found; see docs/GETTING_STARTED.md")
    return exe


def core_dir() -> Path:
    return Path.home() / ".arduino15/packages/esp32/hardware/esp32" / CORE_VER


def bootloader_addr(family: str) -> str:
    """Read the offset from the core rather than hardcoding it.

    These genuinely differ: 0x0 for S3/C3/C6/H2/C2, 0x1000 for ESP32/S2, and
    0x2000 for C5/P4. Flashing a bootloader to the wrong offset produces a board
    that will not boot, so this is not a detail to guess at.
    """
    boards = core_dir() / "boards.txt"
    if boards.exists():
        m = re.search(rf"^{family}\.build\.bootloader_addr=(\S+)",
                      boards.read_text(), re.M)
        if m:
            return m.group(1)
    return "0x0"


def build_one(family: str, spec: dict, cli: str) -> dict:
    fqbn = spec["fqbn"] + (":" + spec["opts"] if spec["opts"] else "")
    outdir = BUILD / family
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    proc = subprocess.run(
        [cli, "compile", "--fqbn", fqbn, "--output-dir", str(outdir), str(SKETCH)],
        capture_output=True, text=True)
    elapsed = time.time() - t0

    if proc.returncode != 0:
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        kind, reason = classify_failure(log)
        shutil.rmtree(outdir, ignore_errors=True)
        return {"family": family, "ok": False, "fqbn": fqbn,
                "kind": kind, "reason": reason, "seconds": round(elapsed, 1)}

    app = next((p for p in outdir.glob("*.ino.bin")), None)
    boot = next((p for p in outdir.glob("*.bootloader.bin")), None)
    parts = next((p for p in outdir.glob("*.partitions.bin")), None)
    if not (app and boot and parts):
        return {"family": family, "ok": False, "fqbn": fqbn,
                "reason": "build produced no .bin artefacts", "seconds": round(elapsed, 1)}

    size = 0
    m = re.search(r"Sketch uses (\d+) bytes", proc.stdout + proc.stderr)
    if m:
        size = int(m.group(1))

    return {
        "family": family, "ok": True, "fqbn": fqbn,
        "usb": spec["usb"],
        "bootloader_addr": bootloader_addr(family),
        "app": app.name, "bootloader": boot.name, "partitions": parts.name,
        "app_bytes": size, "seconds": round(elapsed, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", nargs="*", default=None,
                    help="families to build (default: all)")
    ap.add_argument("--out", default=str(BUILD))
    args = ap.parse_args()

    globals()["BUILD"] = Path(args.out)

    for required in ("config.h", "mesh_key.h"):
        if not (SKETCH / required).exists():
            sys.exit(f"missing {SKETCH / required}\n"
                     f"run scripts/gen_mesh_key.py and scripts/setup_firmware.py first")

    cli = arduino_cli()
    families = args.targets or list(TARGETS)
    BUILD.mkdir(parents=True, exist_ok=True)

    print(f"building {len(families)} targets with arduino-esp32 {CORE_VER}\n")
    results = []
    for fam in families:
        if fam not in TARGETS:
            print(f"  {fam:<9} SKIP     unknown family")
            continue
        print(f"  {fam:<9} building...", end="\r", flush=True)
        r = build_one(fam, TARGETS[fam], cli)
        results.append(r)
        if r["ok"]:
            print(f"  {fam:<9} OK       {r['app_bytes']:>7,} B  "
                  f"boot@{r['bootloader_addr']:<6} {r['seconds']:>5.1f}s")
        else:
            print(f"  {fam:<9} {r['kind'].upper():<14} {r['reason'][:64]}")

    ok = [r for r in results if r["ok"]]
    manifest = {
        "core_version": CORE_VER,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "chip_to_family": CHIP_TO_FAMILY,
        "targets": {r["family"]: r for r in ok},
        "unsupported": {r["family"]: {"kind": r["kind"], "reason": r["reason"]}
                        for r in results if not r["ok"]},
    }
    (BUILD / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\n{len(ok)}/{len(results)} families built -> {BUILD}/manifest.json")
    if manifest["unsupported"]:
        print("\nnot buildable (the reason is stored in the manifest, so "
              "plugging one in gives an explanation, not a mystery):")
        for fam, info in manifest["unsupported"].items():
            print(f"  {fam:<9} [{info['kind']}]\n             {info['reason']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
