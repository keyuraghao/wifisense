#!/usr/bin/env python3
"""Real-time WiFi sensing dashboard.

Three sources, one display:

    # follow a live capture (scripts/stream.py running as root elsewhere)
    .venv/bin/python scripts/live_view.py --follow data/live/stream.csv

    # replay a recorded session at wall-clock speed -- no radio, no root
    .venv/bin/python scripts/live_view.py --replay data/sessions/walking__... 

    # capture and display in one process (needs root; GUI-as-root can be fussy)
    sudo .venv/bin/python scripts/live_view.py --capture

Panels, top to bottom:
  1. raw RSSI, the thing the radio actually reports
  2. bandpassed motion signal -- drift removed, this is what detection sees
  3. rolling Doppler-ish waterfall, 0-40 Hz; walking lights up 1-5 Hz
  4. motion energy vs the calibrated threshold, with the decision state
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

import matplotlib

from ..models.detector import EnergyDetector
from ..pipeline.stream import CSVTailer, RingBuffer, SessionReplayer
from ..signal import features as ft
from ..signal import preprocess as pp
from ..ui import theme
from ..ui.theme import PALETTE as C

FMAX = 40.0          # top of the displayed motion band


class LiveState:
    """Everything the animation callback needs, kept out of globals."""

    def __init__(self, args):
        self.args = args
        self.ring = RingBuffer(seconds=args.history)
        self.det = EnergyDetector()
        self.calibrating = args.calibrate > 0
        self.calib_vals: list[float] = []
        self.calib_deadline = time.time() + args.calibrate
        self.energy = collections.deque(maxlen=int(args.history / args.hop) + 1)
        self.energy_t = collections.deque(maxlen=int(args.history / args.hop) + 1)
        self.state = False
        self.waterfall: np.ndarray | None = None
        self.freqs: np.ndarray | None = None
        self.clf = None
        self.clf_feats = None
        self.label = ""
        self.last_update = 0.0

        if args.model:
            from wifisense.models.classify import load_model
            self.clf, self.clf_feats = load_model(args.model)

    # -- data in -----------------------------------------------------------
    def ingest(self, pairs) -> None:
        if pairs:
            self.ring.add_many(pairs)
            self.ring.trim(now=pairs[-1][0])

    # -- analysis ----------------------------------------------------------
    def analyse(self):
        """Return (prepared, row) or (None, None) if there is not enough data."""
        fs, win = self.args.fs, self.args.win
        if len(self.ring) < fs * win * 0.4:
            return None, None
        try:
            prep = pp.prepare(self.ring.frame(), fs=fs)
        except ValueError:
            return None, None
        if len(prep["motion"]) < int(fs * win):
            return None, None

        seg = prep["motion"][-int(fs * win):]
        row = ft.window_features(seg, prep["clean"][-int(fs * win):], fs)
        stat = float(row["std"])

        if self.calibrating:
            self.calib_vals.append(stat)
            if time.time() >= self.calib_deadline:
                # The guard must match what calibrate() actually requires, or a
                # short window raises straight through the animation callback
                # and takes the whole dashboard down.
                try:
                    self.det.calibrate(np.asarray(self.calib_vals))
                    self.calibrating = False
                except ValueError:
                    # Too few windows yet: keep going rather than fail. At a low
                    # frame rate or a short --calibrate this is normal, not an
                    # error.
                    self.calib_deadline = time.time() + 5
        else:
            self.state = bool(self.det.predict(np.asarray([stat]))[-1])
            if self.clf is not None:
                x = np.array([[row.get(f, np.nan) for f in self.clf_feats]])
                if not np.isnan(x).any():
                    self.label = str(self.clf.predict(x)[0])

        self.energy.append(stat)
        self.energy_t.append(time.time())
        return prep, row

    def push_spectrum(self, motion: np.ndarray, fs: float, ncols: int) -> None:
        """Append one PSD column to the rolling waterfall."""
        n = int(fs * self.args.win)
        seg = motion[-n:] * np.hanning(n)
        spec = np.abs(np.fft.rfft(seg)) ** 2 / n
        freqs = np.fft.rfftfreq(n, 1 / fs)
        m = freqs <= FMAX
        col = 10 * np.log10(spec[m] + 1e-10)

        if self.waterfall is None:
            self.freqs = freqs[m]
            self.waterfall = np.full((col.size, ncols), col.min())
        self.waterfall = np.roll(self.waterfall, -1, axis=1)
        self.waterfall[:, -1] = col


def make_source(args):
    """Return a callable returning new (t, rssi) pairs, plus a description."""
    if args.replay:
        sess = Path(args.replay)
        csv_path = sess / "capture.csv" if sess.is_dir() else sess
        rep = SessionReplayer(csv_path, speed=args.speed)
        return rep.poll, f"REPLAY {csv_path.parent.name} x{args.speed:g}"

    if args.capture:
        if os.geteuid() != 0:
            print("error: --capture needs root; or run scripts/stream.py as root "
                  "and use --follow", file=sys.stderr)
            raise SystemExit(1)
        return _start_inprocess_capture(args)

    tailer = CSVTailer(args.follow)
    return tailer.poll, f"FOLLOW {args.follow}"


def _start_inprocess_capture(args):
    """Run capture in a background thread and tail its own output file."""
    from wifisense import hw
    from wifisense.capture.monitor_rssi import RSSICapture
    from wifisense.capture.traffic import PingFlood, default_gateway

    link = hw.get_link(args.iface)
    phy = hw.phy_for(args.iface)
    out = Path("data/live/stream.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    mon = hw.MonitorInterface(phy, link.channel, link.width_mhz, args.mon).__enter__()
    gw = default_gateway()
    flood = PingFlood(gw, interval=0.002) if gw else None
    if flood:
        flood.start()

    cap = RSSICapture(iface=mon.name, out_path=out, peer=link.bssid, flush_every=10)
    cap.start()

    def cleanup():
        cap.stop()
        if flood:
            flood.stop()
        mon.__exit__(None, None, None)

    threading.current_thread()  # keep reference clarity
    import atexit
    atexit.register(cleanup)

    tailer = CSVTailer(out)
    return tailer.poll, f"CAPTURE {link.ssid} ch{link.channel}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--follow", metavar="CSV", help="tail a live capture CSV")
    src.add_argument("--replay", metavar="SESSION", help="replay a recorded session")
    src.add_argument("--capture", action="store_true", help="capture in-process (root)")

    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--history", type=float, default=20.0, help="seconds on screen")
    ap.add_argument("--win", type=float, default=2.0, help="analysis window (s)")
    ap.add_argument("--hop", type=float, default=0.2, help="update interval (s)")
    ap.add_argument("--fs", type=float, default=100.0, help="resample rate (Hz)")
    ap.add_argument("--calibrate", type=float, default=15.0,
                    help="seconds of empty-room calibration (0 to skip)")
    ap.add_argument("--model", default=None, help="trained classifier (.joblib)")
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--mon", default="mon0")
    ap.add_argument("--backend", default="TkAgg",
                    help="matplotlib backend; WebAgg serves to a browser")
    ap.add_argument("--save", default=None, help="also write the final frame to PNG")
    ap.add_argument("--headless", type=float, default=0.0,
                    help="run N seconds with no window, then --save the frame "
                         "(for SSH sessions and for testing)")
    args = ap.parse_args(argv)

    matplotlib.use("Agg" if args.headless else args.backend)
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    poll, desc = make_source(args)
    st = LiveState(args)
    ncols = max(40, int(args.history / args.hop))

    theme.apply()
    fig = plt.figure(figsize=(13, 9))
    fig.canvas.manager.set_window_title("wifisense - live")
    gs = fig.add_gridspec(4, 4, height_ratios=[1, 1, 1.5, 1],
                          left=0.055, right=0.985, top=0.862, bottom=0.065,
                          hspace=0.55, wspace=0.22)

    ax = [fig.add_subplot(gs[i, :]) for i in range(3)]
    ax_e = fig.add_subplot(gs[3, 0:3])
    tiles = [fig.add_subplot(gs[3, 3])]
    tile_gs = gs[3, 3].subgridspec(1, 2, wspace=0.2)
    tiles[0].remove()
    tiles = [fig.add_subplot(tile_gs[0, i]) for i in range(2)]

    status = theme.StatusBar(fig)

    ln_raw, = ax[0].plot([], [], lw=0.9, color=C["s2"])
    ax[0].set_ylabel("dBm", fontsize=7.5)

    ln_mot, = ax[1].plot([], [], lw=0.9, color=C["s3"])
    ax[1].axhline(0, color=C["border"], lw=0.7)
    ax[1].set_ylabel("dB", fontsize=7.5)

    im = ax[2].imshow(np.zeros((2, ncols)), aspect="auto", origin="lower",
                      extent=[-args.history, 0, 0, FMAX], cmap=theme.SEQUENTIAL,
                      interpolation="bilinear")
    theme.image_panel(ax[2])
    ax[2].set_ylabel("Hz", fontsize=7.5)

    ln_e, = ax_e.plot([], [], lw=1.5, color=C["s1"])
    hi_line = ax_e.axhline(0, color=C["alert"], ls="--", lw=1.0)
    lo_line = ax_e.axhline(0, color=theme.fade(C["alert"], 0.5), ls=":", lw=1.0)
    ax_e.set_ylabel("dB", fontsize=7.5)
    ax_e.set_xlabel("seconds ago", fontsize=7.5)

    def update(_):
        now = time.time()
        st.ingest(poll())
        if now - st.last_update < args.hop:
            return
        st.last_update = now

        prep, row = st.analyse()
        if prep is None:
            status.set("WAITING FOR FRAMES",
                       f"{len(st.ring)} buffered  ·  {desc}", "muted")
            return

        t = prep["t"] - prep["t"][-1]          # seconds ago, 0 = now
        ln_raw.set_data(t, prep["clean"])
        ax[0].set_xlim(-args.history, 0)
        lo, hi = np.percentile(prep["clean"], [1, 99])
        pad = max(1.0, (hi - lo) * 0.2)
        ax[0].set_ylim(lo - pad, hi + pad)
        theme.panel(ax[0], "received signal", "raw RSSI, 1 dB quantised")

        ln_mot.set_data(t, prep["motion"])
        ax[1].set_xlim(-args.history, 0)
        amp = max(0.5, np.abs(prep["motion"]).max() * 1.1)
        ax[1].set_ylim(-amp, amp)
        theme.panel(ax[1], "motion",
                    f"bandpassed {pp.MOTION_BAND_HZ[0]}-{pp.MOTION_BAND_HZ[1]} Hz")

        st.push_spectrum(prep["motion"], args.fs, ncols)
        im.set_data(st.waterfall)
        vmax = float(st.waterfall.max())
        im.set_clim(vmax - 35, vmax)
        theme.panel(ax[2], "doppler waterfall", "walking lights up 1-5 Hz")

        if st.energy:
            e = np.asarray(st.energy)
            et = np.asarray(st.energy_t) - now
            ln_e.set_data(et, e)
            # ax.collections is an immutable view in modern matplotlib, so the
            # previous frame's fill has to be removed artist by artist. Without
            # this the fills stack up and both memory and redraw time grow.
            for coll in list(ax_e.collections):
                coll.remove()
            ax_e.fill_between(et, 0, e, color=theme.fade(C["s1"], 0.28), lw=0)
            ax_e.set_xlim(-args.history, 0)
            top = max(e.max(), st.det.threshold_hi) * 1.25 + 1e-6
            ax_e.set_ylim(0, top)
            hi_line.set_ydata([st.det.threshold_hi] * 2)
            lo_line.set_ydata([st.det.threshold_lo] * 2)
            theme.panel(ax_e, "motion energy", "against the calibrated threshold")

        rate = st.ring.rate_hz
        theme.tile(tiles[0], "rate", f"{rate:.0f}",
                   C["s2"] if rate >= 200 else C["warn"],
                   f"Hz  ·  nyquist {rate/2:.0f} Hz")
        if st.calibrating:
            left = max(0.0, st.calib_deadline - now)
            theme.tile(tiles[1], "calibrating", f"{left:.0f}s", C["warn"],
                       "keep the space empty")
            status.set("CALIBRATING",
                       f"{left:.0f}s remaining  ·  keep the space empty  ·  {desc}",
                       "warn")
        else:
            moving = st.state
            theme.tile(tiles[1], "energy", f"{float(row['std']):.2f}",
                       C["active"] if moving else C["ok"],
                       f"dB  ·  threshold {st.det.threshold_hi:.2f}")
            extra = f"   ·   class {st.label}" if st.label else ""
            status.set("MOTION" if moving else "IDLE",
                       f"peak {float(row['peak_freq']):.1f} Hz   ·   "
                       f"{rate:.0f} Hz capture{extra}   ·   {desc}",
                       "active" if moving else "ok")

    if args.headless:
        deadline = time.time() + args.headless
        while time.time() < deadline:
            update(None)
            time.sleep(0.02)
        print(f"headless run finished: {len(st.ring)} samples buffered, "
              f"{st.ring.rate_hz:.0f} Hz, "
              f"{'calibrating' if st.calibrating else ('MOTION' if st.state else 'idle')}")
    else:
        anim = FuncAnimation(fig, update, interval=max(30, int(args.hop * 500)),
                             cache_frame_data=False)
        fig._keep_anim = anim  # FuncAnimation dies if not referenced
        try:
            plt.show()
        except KeyboardInterrupt:
            pass
    if args.save:
        fig.savefig(args.save, dpi=130)
        print(f"wrote {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
