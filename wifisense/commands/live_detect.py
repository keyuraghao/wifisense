#!/usr/bin/env python3
"""Real-time detection from a live monitor capture.

    sudo .venv/bin/python scripts/live_detect.py --calibrate 30
    sudo .venv/bin/python scripts/live_detect.py --model models/rf.joblib

Calibration mode records N seconds you must spend OUT of the room (or at least
motionless and far from the link), then streams live decisions.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

from .. import hw
from ..capture.monitor_rssi import RSSICapture
from ..capture.traffic import PingFlood, default_gateway
from ..models.classify import load_model
from ..models.detector import EnergyDetector
from ..pipeline.stream import RingBuffer
from ..signal import features as ft
from ..signal import preprocess as pp

BAR = "#"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--mon", default="mon0")
    ap.add_argument("--win", type=float, default=2.0, help="analysis window (s)")
    ap.add_argument("--hop", type=float, default=0.5, help="decision interval (s)")
    ap.add_argument("--fs", type=float, default=100.0)
    ap.add_argument("--calibrate", type=float, default=30.0,
                    help="seconds of empty-room calibration (0 to skip)")
    ap.add_argument("--model", default=None, help="optional trained classifier")
    ap.add_argument("--ping-interval", type=float, default=0.002)
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("error: must run as root", file=sys.stderr)
        return 1

    link = hw.get_link(args.iface)
    phy = hw.phy_for(args.iface)
    gateway = default_gateway()

    clf = feats_needed = None
    if args.model:
        clf, feats_needed = load_model(args.model)
        print(f"classifier: {args.model}  classes={list(clf.classes_)}")

    ring = RingBuffer(seconds=args.win + 1.0)
    flood = None
    print(f"AP {link.ssid} {link.bssid}  ch{link.channel}  ({link.band})")

    try:
        with hw.MonitorInterface(phy, link.channel, link.width_mhz, args.mon) as mon:
            if gateway:
                flood = PingFlood(gateway, interval=args.ping_interval)
                flood.start()

            cap = RSSICapture(iface=mon.name, out_path="/dev/null", peer=link.bssid)
            original_handle = cap._handle

            def tap(pkt):
                original_handle(pkt)
                try:
                    from scapy.layers.dot11 import Dot11
                    if pkt.haslayer(Dot11):
                        rssi = pkt.getfieldval("dBm_AntSignal")
                        if rssi is not None:
                            ring.add(float(pkt.time), float(rssi))
                            ring.trim()
                except Exception:
                    pass

            cap._handle = tap
            cap.start()

            det = EnergyDetector()
            calib_vals: list[float] = []
            calibrating = args.calibrate > 0
            t_end_cal = time.time() + args.calibrate

            if calibrating:
                print(f"\nCALIBRATING {args.calibrate:.0f}s -- keep the space "
                      f"empty and still.")

            while True:
                time.sleep(args.hop)
                df = ring.frame()
                if len(df) < args.fs * args.win * 0.3:
                    print("  waiting for frames...          ", end="\r", flush=True)
                    continue

                try:
                    prep = pp.prepare(df, fs=args.fs)
                    win = ft.sliding_windows(prep, win_s=args.win, hop_s=args.hop)
                except ValueError:
                    continue
                if win.empty:
                    continue
                row = win.iloc[-1]
                stat = float(row["std"])

                if calibrating:
                    calib_vals.append(stat)
                    left = t_end_cal - time.time()
                    print(f"  calibrating... {left:4.1f}s  std={stat:.3f}   ",
                          end="\r", flush=True)
                    if left <= 0:
                        if len(calib_vals) < 20:
                            print("\ntoo few calibration windows; extend --calibrate")
                            return 1
                        det.calibrate(np.array(calib_vals))
                        calibrating = False
                        print(f"\ncalibrated: hi={det.threshold_hi:.3f} "
                              f"lo={det.threshold_lo:.3f} dB "
                              f"(n={len(calib_vals)})\n")
                    continue

                moving = bool(det.predict(np.array([stat]))[-1])
                level = min(30, int(stat / max(det.threshold_hi, 1e-6) * 10))
                label = ""
                if clf is not None:
                    x = row.reindex(feats_needed).to_numpy(dtype=float)[None, :]
                    if not np.isnan(x).any():
                        label = f"  class={clf.predict(x)[0]:<10s}"

                state = "MOTION " if moving else "  --   "
                print(f"  [{state}] std={stat:6.3f} dB {BAR * level:<30s}"
                      f"{label}  {len(df):5d} smp", end="\r", flush=True)

    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    except hw.HardwareError as e:
        print(f"\nhardware error: {e}", file=sys.stderr)
        return 1
    finally:
        if flood:
            flood.stop()


if __name__ == "__main__":
    raise SystemExit(main())
