# WiFi RF sensing - detecting motion and objects with radio

Research scaffold for device-free sensing: detecting people and motion using the
RF that ordinary WiFi hardware already transmits. No router modification.

Two tracks, sharing one signal-processing and modelling pipeline:

| Track | Hardware | Gives you |
|---|---|---|
| **A. Single link** | this laptop + your router | presence, motion, coarse activity |
| **B. Node mesh** | ~12 ESP32s | 3D position, live, fault tolerant |

**New here?** Read [`docs/RESEARCH_PATHWAY.md`](docs/RESEARCH_PATHWAY.md) first -
it is the staged plan from nothing to a defensible result, and it explains why
the tracks are ordered this way.

---

## Status

| Component | State |
|---|---|
| RSSI capture, features, models | works; validated end to end |
| Real capture on this laptop's MT7921 | **confirmed**, 82,886 frames at 1041 Hz |
| Live dashboard and session replay | works |
| 3D tomography maths, node-failure recovery | validated in simulation |
| Mesh protocol, registry, encryption | works; attack-tested |
| Node firmware | **builds for 6 ESP32 families; flashed and run on an ESP32-S3** |
| Multi-node mesh on real hardware | **not tested** - needs more than one board |
| Localisation accuracy in a real room | **not measured** - simulation only |

Simulated numbers are an upper bound. The forward model has no walls or
furniture, so expect real RTI at 0.3-0.5 m, matching the published literature.

---

## Track A: single-link RSSI sensing

Works today on this laptop. Your MT7921 has **monitor mode but no CSI**, so the
measurement is one integer dBm per frame. That is enough for presence and gross
motion, and it will fail on a seated breathing person. See
[`docs/HARDWARE.md`](docs/HARDWARE.md).

```bash
source .venv/bin/activate

python scripts/selftest.py          # validate the pipeline, no radio needed
python scripts/check_hw.py          # what can this radio actually do?

# record. Interleave classes; repeat the whole block on a second day.
sudo .venv/bin/python scripts/collect.py --label empty   --seconds 120
sudo .venv/bin/python scripts/collect.py --label walking --seconds 120
sudo .venv/bin/python scripts/collect.py --label sitting --seconds 120

python scripts/plot_session.py data/sessions/walking__<timestamp>  # look before trusting
python scripts/build_dataset.py
python scripts/train.py
```

`sudo` is needed for the monitor vif and the raw socket. Use the venv
interpreter explicitly under sudo, as shown, or you get the system python.

### Watching it live

Capture needs root, the GUI does not, so they are separate processes.

```bash
sudo .venv/bin/python scripts/stream.py                              # terminal 1
python scripts/live_view.py --follow data/live/stream.csv            # terminal 2
```

Four panels: raw RSSI, bandpassed motion, a rolling 0-40 Hz waterfall, and
energy against the calibrated threshold. Spend the first 15 s out of the room -
that is the calibration window.

No radio and no root? Replay a recording through the identical display and
detection code:

```bash
python scripts/live_view.py --replay data/sessions/walking__20260818-011500
```

---

## Track B: 3D mesh

One link gives one number per packet, so 3D from it is a rank deficiency, not an
engineering shortfall. The fix is spatial diversity: ~12 nodes around the room at
**staggered heights**, inverted into a voxel field (radio tomography).

Try the whole stack with no hardware:

```bash
python scripts/rti_dashboard.py                                       # terminal 1
python scripts/rti_fake_nodes.py --nodes 12 --fail-after 28 --fail-count 4 \
                                 --revive-after 12                    # terminal 2
```

The virtual nodes speak the real protocol over a real UDP socket, so enrolment,
liveness, baselines, encryption and topology rebuilds are all genuinely
exercised. Only the radio is simulated.

With real ESP32s, follow [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md):

```bash
python scripts/gen_mesh_key.py                        # once, before flashing
python scripts/setup_firmware.py --ssid ... --password ...
python scripts/build_firmware.py                      # builds all families
python scripts/flash_node.py --monitor                # plug in ANY ESP32
```

The firmware is **model-independent**: one sketch, built for ESP32, S2, S3, C3,
C5 and C6, and the flasher detects which you plugged in. ESP32-H2 and ESP32-P4
have no WiFi radio and cannot be nodes.

### Design study before you buy

```bash
python scripts/rf_resolution.py    # what power/frequency/bandwidth actually buy
python scripts/rti_sim.py          # how many nodes, placed where
```

---

## Three things that decide whether your results mean anything

1. **Generate traffic.** An idle AP beacons at ~9.8 Hz, which Nyquist-limits you
   to ~4.9 Hz of observable motion, while human limb motion goes to ~30 Hz. Aim
   for >200 Hz capture. `capture/traffic.py` does this.
2. **Group by session in cross-validation.** Sliding windows overlap; a random
   split reports ~99% and means nothing. `train.py` enforces `GroupKFold`.
3. **Beat the unsupervised baseline.** `train.py` reports a calibrated energy
   threshold alongside the model. If the forest does not clearly beat it, the
   forest learned your recording schedule, not the physics.

A fourth, for the mesh: **stagger node heights.** Coplanar nodes return the same
z every single time, pinned to the node plane. The z estimate looks plausible
and carries no information.

---

## Documentation

| Doc | Covers |
|---|---|
| [RESEARCH_PATHWAY](docs/RESEARCH_PATHWAY.md) | the staged plan, evaluation protocol, reading list, ethics |
| [HARDWARE](docs/HARDWARE.md) | what your radio can do, what to buy next |
| [GOING_3D](docs/GOING_3D.md) | why power and carrier frequency are the wrong knobs |
| [MESH](docs/MESH.md) | mesh architecture, adding nodes, surviving failures |
| [SECURITY](docs/SECURITY.md) | threat model, AES-128-GCM transport, what crypto cannot do |
| [GETTING_STARTED](docs/GETTING_STARTED.md) | ESP32 toolchain, flashing, placement, first run |
| [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | symptoms and fixes for Track A |

---

## Layout

```
wifisense/
  hw.py                      radio/interface control, monitor vif lifecycle
  capture/
    monitor_rssi.py          per-frame RSSI from monitor mode
    traffic.py               ICMP illuminator; beacons alone are only ~10 Hz
    esp32_csi.py             ESP32 CSI reader + PCA reduction
  signal/
    preprocess.py            resample, hampel, bandpass, drift removal
    features.py              27 windowed time/spectral features
  models/
    detector.py              unsupervised energy threshold, the honest baseline
    classify.py              RandomForest + session-grouped CV
  pipeline/
    dataset.py               sessions -> labelled feature table
    stream.py                CSV tailer, ring buffer, session replayer
  spatial/
    physics.py               resolution limits: power, bandwidth, aperture
    geometry.py              voxel grids, node layouts
    rti.py                   radio tomography: links -> 3D voxel field
    simulate.py              forward model for validating reconstruction
    adaptive.py              rebuilds the inverse when nodes fail or rejoin
  mesh/
    crypto.py                AES-128-GCM, HKDF per-node keys, replay window
    protocol.py              UDP wire format
    registry.py              auto-enrolment, liveness, per-link baselines
    server.py                collector thread + live reconstruction session

scripts/
  selftest.py                validate the whole Track A pipeline, no radio
  check_hw.py                what this radio supports
  collect.py                 record a labelled session          (root)
  plot_session.py            inspect a recording before using it
  build_dataset.py           sessions -> features
  train.py                   evaluate against baseline and chance
  stream.py                  continuous capture                 (root)
  live_view.py               real-time dashboard                (no root)
  live_detect.py             terminal one-liner                 (root)
  rf_resolution.py           what power/frequency/bandwidth buy you
  rti_sim.py                 3D tomography design study
  rti_dashboard.py           live mesh dashboard + reconstruction
  rti_fake_nodes.py          virtual ESP32 mesh, with failure injection
  gen_mesh_key.py            generate the mesh master key       (once)
  setup_firmware.py          generate firmware config.h
  build_firmware.py          build for every ESP32 family
  flash_node.py              detect the plugged-in chip and flash it

firmware/
  esp32_rti_node/            one sketch, model-independent
  build/                     per-family binaries + manifest.json (generated)
```

Generated and secret files are gitignored: `config/mesh.key`,
`firmware/esp32_rti_node/{config.h,mesh_key.h}`, `firmware/build/`,
`data/`, `models/*.joblib`.
