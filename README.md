# WiFi RF sensing - detecting motion and objects with an ordinary router

Research scaffold for device-free sensing using the RF your existing WiFi router
already transmits. No router modification, no extra transmitter.

**Have ESP32 hardware in hand?** [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md)
- toolchain, flashing, placement, first run.

**Start here:** [`docs/RESEARCH_PATHWAY.md`](docs/RESEARCH_PATHWAY.md) - the
staged plan from "nothing" to "defensible result".
Then [`docs/HARDWARE.md`](docs/HARDWARE.md) - what your radio can and cannot do.
For 3D: [`docs/GOING_3D.md`](docs/GOING_3D.md) - why power and carrier frequency
are the wrong knobs, and what to do instead.
For the mesh: [`docs/MESH.md`](docs/MESH.md) - adding ESP32 nodes, and how the
reconstruction survives node failure.
Security: [`docs/SECURITY.md`](docs/SECURITY.md) - threat model, AES-128-GCM
transport, and what crypto cannot protect.

---

## Where this machine stands

`scripts/check_hw.py` reports it live. Today:

- **MT7921 (mt7921e), monitor mode: yes, CSI: no.**
  You can do per-frame **RSSI** sensing right now - presence, motion, coarse
  activity. Fine activity, gesture, and respiration need **CSI**, which this
  chipset does not export. The fix is a ~$10 ESP32, not a driver rewrite.

The code is built so the CSI upgrade costs you nothing: `csi_to_motion_series()`
reduces a CSI subcarrier matrix to a scalar series that feeds the *same*
preprocessing, features, and models as RSSI.

---

## Quick start

```bash
source .venv/bin/activate

# 0. Validate the whole pipeline with synthetic data - no radio needed
python scripts/selftest.py

# 1. What can this radio do?
python scripts/check_hw.py

# 2. Record. Interleave classes; repeat the block on a second day.
sudo .venv/bin/python scripts/collect.py --label empty   --seconds 120
sudo .venv/bin/python scripts/collect.py --label walking --seconds 120
sudo .venv/bin/python scripts/collect.py --label sitting --seconds 120

# 3. Look at every recording before trusting it
python scripts/plot_session.py data/sessions/walking__20260818-011500

# 4. Features -> evaluation
python scripts/build_dataset.py
python scripts/train.py

# 5. Live
sudo .venv/bin/python scripts/live_detect.py --calibrate 30 --model models/rf.joblib
```

---

## Watching it in real time

Capture needs root; the GUI does not. They are separate processes so the display
never runs as root, and so you can watch a session live *while* it records.

```bash
# Terminal 1 (root) -- continuous capture to a tailable CSV
sudo .venv/bin/python scripts/stream.py

# Terminal 2 (you) -- live dashboard
.venv/bin/python scripts/live_view.py --follow data/live/stream.csv
```

Four panels update ~5x/s:

| Panel | Shows |
|---|---|
| RSSI (dBm) | what the radio actually reports, 1 dB quantised |
| motion (dB) | bandpassed 0.3-40 Hz - drift removed, this is what detection sees |
| waterfall | rolling 0-40 Hz spectrum; walking lights up 1-5 Hz |
| energy | windowed energy vs the calibrated threshold, with the decision |

The banner turns **orange** while calibrating, **green** for idle, **red** for
MOTION, and shows peak frequency, capture rate, and the predicted class if you
pass `--model models/rf.joblib`.

Spend the first 15 s out of the room - that is the calibration window
(`--calibrate 0` to skip, `--history 40` for a longer view).

**No radio, no root - replay a recording at wall-clock speed:**

```bash
.venv/bin/python scripts/live_view.py --replay data/sessions/walking__20260818-011500
```

This runs the identical display and detection code, so it is the right way to
demo the system, and to debug the live path without fighting the radio.
`--speed 4` to fast-forward.

**Over SSH / no display:** `--backend WebAgg` serves the dashboard to a browser,
or `--headless 30 --save frame.png` runs blind and writes one frame.
`scripts/live_detect.py` is the plain single-line terminal readout.

`sudo` is needed for the monitor vif and the raw socket. Use the venv
interpreter explicitly under sudo, as shown, or you will get the system python.

---

## Layout

```
wifisense/
  hw.py                      radio/interface control, monitor vif lifecycle
  capture/
    monitor_rssi.py          per-frame RSSI from monitor mode  (Phase 1)
    traffic.py               ICMP illuminator - beacons alone are only ~10 Hz
    esp32_csi.py             ESP32 CSI reader + PCA reduction   (Phase 2)
  signal/
    preprocess.py            resample, hampel, bandpass, drift removal
    features.py              27 windowed time/spectral features
  models/
    detector.py              unsupervised energy threshold - the honest baseline
    classify.py              RandomForest + session-grouped CV
  pipeline/
    dataset.py               sessions -> labelled feature table
    stream.py                CSV tailer, ring buffer, session replayer
  spatial/                   3D reconstruction  (Phase 3)
    physics.py               resolution limits: power, bandwidth, aperture
    geometry.py              voxel grids, node layouts
    rti.py                   radio tomography: links -> 3D voxel field
    simulate.py              forward model for validating reconstruction
    adaptive.py              rebuilds the inverse when nodes fail or rejoin
  mesh/                      ESP32 mesh  (Phase 3b)
    crypto.py                AES-128-GCM, HKDF per-node keys, replay window
    protocol.py              UDP wire format
    registry.py              auto-enrolment, liveness, per-link baselines
    server.py                collector thread + live reconstruction session

scripts/
  check_hw.py  collect.py  plot_session.py  selftest.py
  build_dataset.py  train.py
  stream.py                  continuous capture   (root)
  live_view.py               real-time dashboard  (no root)
  live_detect.py             terminal one-liner   (root)
  rf_resolution.py           what power/frequency/bandwidth actually buy
  rti_sim.py                 3D tomography design study
  rti_dashboard.py           live mesh dashboard + reconstruction
  rti_fake_nodes.py          virtual ESP32 mesh, with failure injection
  gen_mesh_key.py            generate the mesh master key (run once)
  setup_firmware.py          generate firmware config.h (WiFi, server IP)
  build_firmware.py          build for every ESP32 family, with a manifest
  flash_node.py              detect the plugged-in chip and flash it

firmware/
  esp32_rti_node/            one sketch, model-independent
  build/                     per-family binaries + manifest.json (generated)

docs/
  RESEARCH_PATHWAY.md  HARDWARE.md  TROUBLESHOOTING.md
```

---

## Three things that decide whether your results mean anything

1. **Generate traffic.** An idle AP beacons at ~9.8 Hz, which Nyquist-limits you
   to ~4.9 Hz of observable motion. Human limb motion goes to ~30 Hz. Aim for
   >200 Hz capture.
2. **Group by session in cross-validation.** Sliding windows overlap; a random
   split reports ~99% and means nothing. `train.py` enforces `GroupKFold`.
3. **Beat the unsupervised baseline.** `train.py` reports a calibrated energy
   threshold alongside the model. If the forest does not clearly beat it, the
   forest learned your recording schedule, not the physics.

Expect RSSI to nail presence and gross motion, and to fail on a seated breathing
person. That failure is the information limit of one scalar per frame - and it
is the measurement that justifies Phase 2.

---

## Thinking about 3D?

```bash
.venv/bin/python scripts/rf_resolution.py   # the physics, with numbers
.venv/bin/python scripts/rti_sim.py         # the 3D design study
```

Short answer: **more transmit power buys 0.000 dB** - your floor is the 1 dB
RSSI quantiser, not thermal noise, and you are already 45 dB above the noise.
A higher carrier helps micro-motion only. Range resolution is bandwidth
(ΔR = c/2B; at your 80 MHz that is 1.9 m, worse than the room) and angle is
antenna count (1.77/N rad - 3 antennas is 34°).

And one link gives one number per packet, so 3D from it is a rank deficiency,
not an engineering shortfall. The fix is spatial diversity: ~12 ESP32s around
the room at **staggered heights** → radio tomography. See
[`docs/GOING_3D.md`](docs/GOING_3D.md).
