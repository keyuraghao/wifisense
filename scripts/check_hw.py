#!/usr/bin/env python3
"""Report what this machine's radio can and cannot do for sensing.

    .venv/bin/python scripts/check_hw.py
"""
from __future__ import annotations

import glob
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense import hw

# Chipsets with a public, working CSI extraction path.
# Looked up longest-key-first so 'mt7921e' matches 'mt7921' before 'mt76'.
CSI_PLATFORMS = {
    "iwlwifi": "Intel 5300 only (Linux 802.11n CSI Tool, old kernel); "
               "AX200/AX210 via PicoScenes",
    "ath9k": "Atheros AR9300-series: Atheros CSI Tool / PicoScenes",
    "brcmfmac": "Broadcom bcm43455c0 (RPi 3B+/4), bcm4366c0: Nexmon CSI",
    "mt7921": "NO public CSI export in the mainline mt76 driver",
    "mt7915": "vendor/OpenWrt trees expose CSI; mainline does not",
    "mt76": "no public CSI export in the mainline driver",
    "rtw": "Realtek: no public CSI path",
}


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    except FileNotFoundError:
        return ""


def main() -> int:
    print("=" * 68)
    print("WiFi sensing hardware capability report")
    print("=" * 68)

    ifaces = [Path(p).parent.name for p in glob.glob("/sys/class/net/*/wireless")]
    if not ifaces:
        print("no wireless interfaces found")
        return 1

    for iface in ifaces:
        drv = Path(f"/sys/class/net/{iface}/device/driver").resolve().name \
            if Path(f"/sys/class/net/{iface}/device/driver").exists() else "?"
        print(f"\ninterface : {iface}")
        print(f"driver    : {drv}")

        try:
            phy = hw.phy_for(iface)
            print(f"phy       : {phy}")
            print(f"monitor   : {'YES' if hw.supports_monitor(phy) else 'NO'}")
        except hw.HardwareError as e:
            print(f"phy       : ({e})")
            phy = None

        try:
            link = hw.get_link(iface)
            print(f"associated: {link.ssid}  {link.bssid}")
            print(f"            ch {link.channel} / {link.freq_mhz} MHz "
                  f"/ {link.width_mhz} MHz / {link.band}")
        except hw.HardwareError:
            print("associated: no")

        csi = next((CSI_PLATFORMS[k] for k in sorted(CSI_PLATFORMS, key=len, reverse=True)
                    if k in drv), None)
        print(f"CSI       : {csi or 'unknown for this driver -- check upstream'}")

    print("\n" + "-" * 68)
    print("Legend")
    print("  monitor=YES  -> Phase 1 (per-frame RSSI) works on this machine now.")
    print("  CSI          -> per-subcarrier complex gain. Needed for Phase 2+.")
    print("                  If your driver has no CSI path, the cheapest fix is")
    print("                  an ESP32 running esp-csi (see docs/HARDWARE.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
