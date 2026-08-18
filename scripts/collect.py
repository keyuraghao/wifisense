#!/usr/bin/env python3
"""Record one labelled sensing session.

    sudo .venv/bin/python scripts/collect.py --label empty     --seconds 120
    sudo .venv/bin/python scripts/collect.py --label walking   --seconds 120
    sudo .venv/bin/python scripts/collect.py --label sitting   --seconds 120

Needs root: creating a monitor vif and opening a raw socket both require
CAP_NET_ADMIN / CAP_NET_RAW.

IMPORTANT experimental hygiene, learned the hard way by everyone in this field:
do NOT record all of class A today and all of class B tomorrow. The model will
learn the day, not the activity. Interleave: empty, walking, empty, walking,
and repeat the whole block on a different day before you believe any number.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense import hw
from wifisense.capture.monitor_rssi import RSSICapture
from wifisense.capture.traffic import PingFlood, default_gateway


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a WiFi sensing session")
    ap.add_argument("--label", required=True,
                    help="ground-truth class, e.g. empty / walking / sitting")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--iface", default="wlan0", help="associated managed interface")
    ap.add_argument("--mon", default="mon0", help="monitor vif name to create")
    ap.add_argument("--out", default="data/sessions")
    ap.add_argument("--no-traffic", action="store_true",
                    help="do not ping-flood; you will only see beacons (~10 Hz)")
    ap.add_argument("--ping-interval", type=float, default=0.002)
    ap.add_argument("--note", default="", help="free-text note stored in meta.json")
    ap.add_argument("--countdown", type=int, default=5,
                    help="seconds to get into position before capture starts")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("error: must run as root (monitor vif + raw socket)", file=sys.stderr)
        return 1

    hw.require_tools("iw", "ip", "ping")

    try:
        link = hw.get_link(args.iface)
    except hw.HardwareError as e:
        print(f"error: {e}", file=sys.stderr)
        print("hint: connect to your router on the interface first.", file=sys.stderr)
        return 1

    phy = hw.phy_for(args.iface)
    gateway = default_gateway()

    print(f"AP        : {link.ssid}  {link.bssid}")
    print(f"Channel   : {link.channel} @ {link.freq_mhz} MHz ({link.band}, {link.width_mhz} MHz)")
    print(f"phy       : {phy}   monitor vif: {args.mon}")
    print(f"Gateway   : {gateway or '(none found)'}")
    print(f"Label     : {args.label}   Duration: {args.seconds:.0f}s")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    sess_dir = Path(args.out) / f"{args.label}__{ts}"
    sess_dir.mkdir(parents=True, exist_ok=True)

    if args.countdown:
        print()
        for i in range(args.countdown, 0, -1):
            print(f"  starting in {i}... ", end="\r", flush=True)
            time.sleep(1)
        print(" " * 40, end="\r")

    flood = None
    try:
        with hw.MonitorInterface(phy, channel=link.channel, width=link.width_mhz,
                                 name=args.mon) as mon:
            actual = mon.current_channel()
            if actual is not None and actual != link.channel:
                print(f"warning: monitor vif landed on channel {actual}, "
                      f"not {link.channel}; the driver overrode us.")

            if not args.no_traffic and gateway:
                flood = PingFlood(gateway, interval=args.ping_interval)
                flood.start()
                print(f"illuminating: ping -i {args.ping_interval} {gateway} "
                      f"(~{flood.expected_rate_hz:.0f} frames/s expected)")

            cap = RSSICapture(iface=mon.name, out_path=sess_dir / "capture.csv",
                              peer=link.bssid)

            def progress(stats, remaining):
                print(f"  RECORDING [{args.label}]  {remaining:5.1f}s left | "
                      f"{stats.kept:7d} frames | {stats.rate_hz:6.1f} Hz | "
                      f"mean {stats.mean_rssi:6.1f} dBm   ",
                      end="\r", flush=True)

            print()
            stats = cap.run_for(args.seconds, progress=progress)
            print()
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except hw.HardwareError as e:
        print(f"\nhardware error: {e}", file=sys.stderr)
        return 1
    finally:
        if flood:
            flood.stop()

    meta = {
        "label": args.label,
        "note": args.note,
        "timestamp": ts,
        "ssid": link.ssid, "bssid": link.bssid,
        "channel": link.channel, "freq_mhz": link.freq_mhz,
        "width_mhz": link.width_mhz, "band": link.band,
        "iface": args.iface, "phy": phy,
        "traffic": None if args.no_traffic else {
            "kind": "icmp_flood", "target": gateway, "interval_s": args.ping_interval,
        },
        "requested_seconds": args.seconds,
        "frames_seen": stats.frames,
        "frames_kept": stats.kept,
        "duration_s": round(stats.duration_s, 3),
        "rate_hz": round(stats.rate_hz, 2),
        "mean_rssi_dbm": round(stats.mean_rssi, 2),
    }
    (sess_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    print(f"\nsaved {stats.kept} frames -> {sess_dir}")
    print(f"      {stats.rate_hz:.1f} Hz effective  (Nyquist limit "
          f"{stats.rate_hz / 2:.1f} Hz of observable motion)")
    if stats.kept == 0:
        print("\nNO FRAMES CAPTURED. Most likely the monitor vif could not "
              "coexist with the associated managed vif on this chipset.\n"
              "See docs/TROUBLESHOOTING.md.")
        return 2
    if stats.rate_hz < 20:
        print("\nwarning: rate below 20 Hz limits you to <10 Hz of motion "
              "bandwidth. Increase traffic or check for channel hopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
