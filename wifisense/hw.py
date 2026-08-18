"""Radio and interface control for WiFi sensing capture.

Everything here shells out to `iw`/`ip` because the nl80211 python bindings add a
dependency without buying us anything: we touch the radio exactly twice per
session (up, down).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass


class HardwareError(RuntimeError):
    pass


def _run(cmd: list[str], check: bool = True) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise HardwareError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout


def require_tools(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise HardwareError(f"missing required tools: {', '.join(missing)}")


# --------------------------------------------------------------------------
# Link introspection (works while you are still associated / online)
# --------------------------------------------------------------------------

@dataclass
class Link:
    iface: str
    ssid: str
    bssid: str
    freq_mhz: int
    channel: int
    width_mhz: int

    @property
    def band(self) -> str:
        return "2.4GHz" if self.freq_mhz < 3000 else "5GHz"


def freq_to_channel(freq_mhz: int) -> int:
    if 2412 <= freq_mhz <= 2484:
        return 14 if freq_mhz == 2484 else (freq_mhz - 2407) // 5
    if 5000 <= freq_mhz <= 5900:
        return (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:  # 6 GHz / WiFi 6E
        return (freq_mhz - 5950) // 5
    raise ValueError(f"cannot map {freq_mhz} MHz to a channel")


def get_link(iface: str) -> Link:
    """Parse `iw dev <iface> link` into a Link. Raises if not associated."""
    out = _run(["iw", "dev", iface, "link"])
    if "Not connected" in out:
        raise HardwareError(f"{iface} is not associated with an AP")

    bssid = re.search(r"Connected to ([0-9a-f:]{17})", out, re.I)
    ssid = re.search(r"SSID: (.+)", out)
    freq = re.search(r"freq: (\d+)", out)
    if not (bssid and freq):
        raise HardwareError(f"could not parse link state:\n{out}")

    # `iw dev <iface> info` carries the operating width, `link` does not.
    info = _run(["iw", "dev", iface, "info"], check=False)
    width = re.search(r"width: (\d+) MHz", info)

    freq_mhz = int(freq.group(1))
    return Link(
        iface=iface,
        ssid=ssid.group(1).strip() if ssid else "<unknown>",
        bssid=bssid.group(1).lower(),
        freq_mhz=freq_mhz,
        channel=freq_to_channel(freq_mhz),
        width_mhz=int(width.group(1)) if width else 20,
    )


def phy_for(iface: str) -> str:
    """Return the phy name (e.g. 'phy0') backing an interface."""
    out = _run(["iw", "dev"])
    current_phy = None
    for line in out.splitlines():
        if line.startswith("phy#"):
            current_phy = "phy" + line.strip()[4:]
        elif line.strip().startswith("Interface ") and line.split()[-1] == iface:
            if current_phy:
                return current_phy
    raise HardwareError(f"no phy found for {iface}")


def supports_monitor(phy: str) -> bool:
    out = _run(["iw", "phy", phy, "info"])
    block = out.split("Supported interface modes:", 1)
    return len(block) > 1 and "* monitor" in block[1].split("Band")[0]


# --------------------------------------------------------------------------
# Monitor interface lifecycle
# --------------------------------------------------------------------------

class MonitorInterface:
    """Create a monitor vif on `phy`, park it on a channel, tear it down after.

    Usage:
        with MonitorInterface("phy0", channel=56, width=80) as mon:
            sniff(iface=mon.name, ...)

    NOTE: on most single-radio chipsets (including MT7921) adding a monitor vif
    alongside an associated managed vif either fails outright or silently forces
    both onto the same channel. This class does NOT try to hide that: it asks
    the driver, and if the driver refuses you get an exception, not bad data.
    """

    def __init__(self, phy: str, channel: int, width: int = 20, name: str = "mon0"):
        self.phy = phy
        self.channel = channel
        self.width = width
        self.name = name
        self._created = False

    def __enter__(self) -> "MonitorInterface":
        require_tools("iw", "ip")
        if not supports_monitor(self.phy):
            raise HardwareError(f"{self.phy} does not advertise monitor mode")

        # Remove a stale vif from a crashed previous run.
        _run(["ip", "link", "set", self.name, "down"], check=False)
        _run(["iw", "dev", self.name, "del"], check=False)

        _run(["iw", "phy", self.phy, "interface", "add", self.name, "type", "monitor"])
        self._created = True
        _run(["ip", "link", "set", self.name, "up"])

        width_arg = {20: ["HT20"], 40: ["HT40+"], 80: ["80MHz"], 160: ["160MHz"]}
        _run(["iw", "dev", self.name, "set", "channel", str(self.channel),
              *width_arg.get(self.width, [])], check=False)
        time.sleep(0.3)  # let the firmware settle on the new channel
        return self

    def __exit__(self, *exc) -> None:
        if self._created:
            _run(["ip", "link", "set", self.name, "down"], check=False)
            _run(["iw", "dev", self.name, "del"], check=False)
            self._created = False

    def current_channel(self) -> int | None:
        out = _run(["iw", "dev", self.name, "info"], check=False)
        m = re.search(r"channel (\d+)", out)
        return int(m.group(1)) if m else None
