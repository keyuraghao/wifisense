#!/usr/bin/env python3
"""Detect whatever ESP32 is plugged in and flash the matching firmware.

    .venv/bin/python scripts/flash_node.py            # auto-detect, flash, done
    .venv/bin/python scripts/flash_node.py --monitor  # then watch serial
    .venv/bin/python scripts/flash_node.py --list     # what is supported

You do not tell it which board you have. It asks the chip, looks the family up
in firmware/build/manifest.json, and flashes the binary built for that silicon
at the offsets that family needs. Plug in any supported ESP32 and run it.

Flashing uses esptool against prebuilt binaries, so the machine doing the
flashing needs neither arduino-cli nor the 5.6 GB toolchain - only the build
machine does. Copy firmware/build/ to a laptop and it can flash nodes.
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "firmware" / "build"

# Offsets that never vary by family. bootloader_addr DOES vary and comes from
# the manifest, which reads it out of the core's boards.txt at build time.
PARTITIONS_ADDR = "0x8000"
BOOT_APP0_ADDR = "0xe000"
APP_ADDR = "0x10000"


def load_manifest() -> dict:
    m = BUILD / "manifest.json"
    if not m.exists():
        sys.exit(f"no build found at {m}\n"
                 f"run: .venv/bin/python scripts/build_firmware.py")
    return json.loads(m.read_text())


def find_ports() -> list[str]:
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))


def detect_chip(port: str) -> str | None:
    """Ask the silicon what it is. Never guess from the port name."""
    try:
        out = subprocess.run([sys.executable, "-m", "esptool", "--port", port,
                              "chip-id"], capture_output=True, text=True,
                             timeout=45).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in out.splitlines():
        if "Detecting chip type" in line or "Chip type:" in line:
            for token in line.replace(",", " ").split():
                t = token.strip("().")
                if t.upper().startswith("ESP32"):
                    return t.upper()
    return None


def boot_app0() -> Path | None:
    hits = sorted(Path.home().glob(
        ".arduino15/packages/esp32/hardware/esp32/*/tools/partitions/boot_app0.bin"))
    return hits[-1] if hits else None


def flash(port: str, family: str, target: dict, erase: bool) -> int:
    d = BUILD / family
    app, boot, parts = d / target["app"], d / target["bootloader"], d / target["partitions"]
    for f in (app, boot, parts):
        if not f.exists():
            sys.exit(f"missing artefact {f}; rebuild with scripts/build_firmware.py")

    args = [sys.executable, "-m", "esptool", "--port", port, "--chip",
            family.replace("esp32", "esp32-") if family != "esp32" else "esp32"]
    if erase:
        print("erasing flash (this also clears NVS, so boot_id restarts at 1)...")
        subprocess.run(args + ["erase-flash"], check=False)

    cmd = args + ["write-flash", "-z",
                  target["bootloader_addr"], str(boot),
                  PARTITIONS_ADDR, str(parts),
                  APP_ADDR, str(app)]
    b0 = boot_app0()
    if b0:
        cmd[-6:-6] = [BOOT_APP0_ADDR, str(b0)]

    print(f"flashing {family}: bootloader@{target['bootloader_addr']} "
          f"partitions@{PARTITIONS_ADDR} app@{APP_ADDR}")
    return subprocess.run(cmd).returncode


def monitor(port: str, seconds: float) -> None:
    try:
        import serial
    except ImportError:
        print("pyserial not installed; skipping monitor")
        return
    print(f"\n--- serial {port} for {seconds:.0f}s ---")
    with serial.Serial(port, 115200, timeout=1) as s:
        time.sleep(0.3); s.setDTR(False); s.setRTS(True)
        time.sleep(0.15); s.setRTS(False)
        end = time.time() + seconds
        while time.time() < end:
            line = s.readline()
            if line:
                t = line.decode("utf-8", "replace").rstrip()
                if "return error" not in t and "send fail" not in t:
                    print(t)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--family", default=None, help="skip detection (rarely needed)")
    ap.add_argument("--erase", action="store_true",
                    help="erase flash first; also wipes NVS, resetting boot_id")
    ap.add_argument("--monitor", action="store_true")
    ap.add_argument("--monitor-seconds", type=float, default=25.0)
    ap.add_argument("--list", action="store_true", help="show supported families")
    args = ap.parse_args(argv)

    man = load_manifest()

    if args.list:
        print(f"built with arduino-esp32 {man['core_version']} on {man['built_at']}\n")
        print("SUPPORTED - plug one in and this script handles it:")
        for fam, t in sorted(man["targets"].items()):
            print(f"  {fam:<9} {t['app_bytes']:>9,} B   bootloader@{t['bootloader_addr']:<7}"
                  f" {t['usb']} USB")
        if man["unsupported"]:
            print("\nNOT SUPPORTED:")
            for fam, info in sorted(man["unsupported"].items()):
                print(f"  {fam:<9} [{info['kind']}] {info['reason']}")
        return 0

    port = args.port
    if not port:
        ports = find_ports()
        if not ports:
            sys.exit("no serial device found. Plug a board in.")
        if len(ports) > 1:
            print(f"multiple ports: {', '.join(ports)}; using {ports[0]} "
                  f"(pass --port to choose)")
        port = ports[0]

    family = args.family
    if not family:
        print(f"detecting chip on {port}...")
        chip = detect_chip(port)
        if not chip:
            sys.exit(f"could not identify the chip on {port}.\n"
                     f"Is it in download mode? Some boards need BOOT held while "
                     f"tapping RESET.")
        family = man["chip_to_family"].get(chip)
        print(f"chip: {chip}" + (f" -> {family}" if family else ""))
        if not family:
            sys.exit(f"{chip} is not a family this project knows about.")

    if family in man.get("unsupported", {}):
        info = man["unsupported"][family]
        sys.exit(f"\n{family} cannot be used as a mesh node.\n  {info['reason']}\n"
                 + ("\nThis is a property of the silicon, not something a "
                    "firmware change can fix. Use an ESP32, S2, S3, C3, C5 or "
                    "C6 instead." if info["kind"] == "no_wifi_radio" else ""))

    target = man["targets"].get(family)
    if not target:
        sys.exit(f"no firmware built for {family}; run scripts/build_firmware.py")

    rc = flash(port, family, target, args.erase)
    if rc != 0:
        return rc

    print("\nflashed. The node prints its MAC on boot - that is its mesh id.")
    print("Add it to config/nodes.json with its measured x/y/z position.")
    if args.monitor:
        monitor(port, args.monitor_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
