#!/usr/bin/env python3
"""Continuous capture to a tailable CSV, for the live viewer to follow.

Terminal 1 (root):
    sudo .venv/bin/python scripts/stream.py

Terminal 2 (you):
    .venv/bin/python scripts/live_view.py --follow data/live/stream.csv

Runs until Ctrl-C. The file is truncated on start, which the viewer detects and
handles, so restarting capture does not require restarting the viewer.
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--mon", default="mon0")
    ap.add_argument("--out", default="data/live/stream.csv")
    ap.add_argument("--no-traffic", action="store_true")
    ap.add_argument("--ping-interval", type=float, default=0.002)
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = run forever")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("error: must run as root", file=sys.stderr)
        return 1

    hw.require_tools("iw", "ip", "ping")
    try:
        link = hw.get_link(args.iface)
    except hw.HardwareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    phy = hw.phy_for(args.iface)
    gateway = default_gateway()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"AP      : {link.ssid} {link.bssid}  ch{link.channel} ({link.band})")
    print(f"stream  : {out}")
    print(f"view    : .venv/bin/python scripts/live_view.py --follow {out}")

    # Side-car metadata so the viewer can label its display without root.
    (out.with_suffix(".meta.json")).write_text(json.dumps({
        "started": datetime.now().isoformat(timespec="seconds"),
        "ssid": link.ssid, "bssid": link.bssid, "channel": link.channel,
        "freq_mhz": link.freq_mhz, "band": link.band,
    }, indent=2))

    flood = None
    try:
        with hw.MonitorInterface(phy, link.channel, link.width_mhz, args.mon) as mon:
            if not args.no_traffic and gateway:
                flood = PingFlood(gateway, interval=args.ping_interval)
                flood.start()
                print(f"traffic : ping {gateway} @ {args.ping_interval}s")

            cap = RSSICapture(iface=mon.name, out_path=out, peer=link.bssid,
                              flush_every=10)
            cap.start()
            print("\nstreaming (Ctrl-C to stop)\n")

            deadline = time.time() + args.seconds if args.seconds else None
            while deadline is None or time.time() < deadline:
                time.sleep(0.5)
                s = cap.stats
                print(f"  {s.kept:9d} frames | {s.rate_hz:7.1f} Hz | "
                      f"mean {s.mean_rssi:6.1f} dBm   ", end="\r", flush=True)
    except KeyboardInterrupt:
        print("\nstopping")
    except hw.HardwareError as e:
        print(f"\nhardware error: {e}", file=sys.stderr)
        return 1
    finally:
        if flood:
            flood.stop()
        try:
            cap.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
