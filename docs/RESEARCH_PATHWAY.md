# Research pathway: detecting objects with router RF

The goal — "detect objects using the radio frequencies my router already emits"
— is the field known as **WiFi sensing** / **device-free passive sensing**.
This document is the staged plan from "nothing" to "defensible result", written
against the hardware you actually own.

The staging is deliberate. Each phase produces a working artefact and an honest
number, and each phase's failure mode is the motivation for the next one. Do not
skip ahead: Phase 3 with no Phase 1 baseline is a result nobody can interpret.

---

## Phase 0 — Frame the question (do this before writing more code)

"Detect objects" is four different research problems wearing one coat. Pick one;
they need different signals, different setups, and different evaluations.

| Question | Difficulty | Minimum hardware |
|---|---|---|
| Is anyone in the room? (presence) | easy | RSSI — **you have this** |
| Is something moving, and how much? (motion) | easy | RSSI — **you have this** |
| What activity is it? (walk / sit / wave) | moderate | CSI strongly preferred |
| Is the person breathing, and how fast? | moderate–hard | CSI required |
| Where is the person? (localisation) | hard | multi-antenna CSI, or many links |
| What shape is the object? (imaging) | very hard | SDR array / mmWave |

**Recommendation for a first project: motion + presence, then coarse activity
classification.** It is achievable on your hardware, it has a clean baseline, and
it makes the case for the CSI upgrade with your own measurements rather than a
citation.

A blunt caveat worth internalising now: WiFi sensing detects *changes in the
propagation channel*. A stationary, non-metallic object that has been in the room
since before you calibrated is nearly invisible. Motion is what you can see.
"Detecting objects" in the sense of seeing a chair through a wall is a different,
much harder problem (Phase 4+, and largely an SDR/mmWave one).

---

## Phase 1 — RSSI motion & presence sensing  ← **start here, works today**

**Hardware:** the laptop's MT7921 in monitor mode + your router. Nothing to buy.

**Physical basis.** Received power is the squared magnitude of the coherent sum
over propagation paths:

```
P(t) = | Σ_k  a_k(t) · exp(−j·2π·f·τ_k(t)) |²
```

A body moving through the room perturbs `a_k` and `τ_k` on the paths it
intersects, so `P(t)` fluctuates. Everything in Phase 1 is extracting structure
from that fluctuation.

**The rate constraint, which trips up everyone.** An idle AP beacons at
~9.8 Hz (102.4 ms TBTT). Nyquist caps you at ~4.9 Hz of observable motion —
below the ~10-30 Hz where limb motion lives. So you must generate traffic.
`wifisense/capture/traffic.py` ping-floods the router to force a dense frame
stream; aim for **>200 Hz** effective capture rate.

**Steps**

1. `python scripts/check_hw.py` — confirm monitor mode.
2. Fix the router to one channel; disable auto-channel and DFS.
3. Record interleaved sessions (`scripts/collect.py`), ≥3 per class, ≥90 s each:
   `empty`, `sitting`, `walking`. **Interleave and repeat on a second day.**
4. `python scripts/plot_session.py <session>` — look at every recording before
   it enters a dataset. You will catch dropouts and channel changes here.
5. `python scripts/build_dataset.py` then `python scripts/train.py`.

**Deliverable.** A session-grouped-CV accuracy for presence/motion, reported
against the unsupervised `EnergyDetector` baseline and against majority-class
chance.

**Expected honest outcome.** Presence and gross motion: strong (>90%). Fine
activity: mediocre. A seated breathing person: essentially undetectable. That
last failure is not a bug in your code — it is the RSSI information limit, and
it is the empirical argument for Phase 2.

---

## Phase 2 — CSI acquisition

**Hardware:** ESP32 + `esp-csi` (~$10), or Raspberry Pi + Nexmon CSI.
See `docs/HARDWARE.md`.

**What changes.** You go from 1 scalar per frame to ~52 complex subcarriers.
Frequency-selective fading becomes visible, so paths that cancel at one
subcarrier survive at another, and the "two different room states, same total
power" ambiguity of RSSI largely dissolves.

**Steps**

1. Flash `esp-csi`; verify `CSI_DATA` lines over serial.
2. Capture with `wifisense/capture/esp32_csi.py`.
3. Sanitise: drop guard/null subcarriers; amplitude outlier removal.
   Phase needs CFO/SFO correction — the practical shortcut is the **CSI ratio**
   between two antennas, which cancels the common phase noise (Zeng et al.,
   FarSense). Do not fight raw single-antenna phase; it is mostly hardware noise.
4. Reduce the subcarrier matrix to a motion series with
   `csi_to_motion_series()` (PCA/SVD) — this feeds the *existing* Phase 1
   feature and model code unchanged, so you get a like-for-like comparison.

**Deliverable.** The same experiment as Phase 1, same protocol, on CSI. The
RSSI-vs-CSI delta on identical activities, measured by you, is a genuine
contribution to your own project's argument.

---

## Phase 3 — Doppler, respiration, and activity recognition

Now use CSI as more than a better RSSI.

- **Doppler / DFS.** Short-time FFT of the CSI stream gives a Doppler
  spectrogram; motion toward/away from the link shifts energy. The CARM model
  (Wang et al., MobiCom 2015) links CSI power-spectrum energy to torso/limb
  speed and is the standard reference.
- **Respiration.** A 0.15–0.6 Hz periodicity (9–36 breaths/min) in CSI
  amplitude or the CSI ratio. Needs a stationary subject and a good link
  geometry; subject placement dominates whether this works at all.
- **Activity recognition.** Doppler spectrograms + a small CNN, or the
  handcrafted features already in `wifisense/signal/features.py` + a forest.
  With <10 sessions per class, the forest will usually beat the CNN — say so
  rather than forcing deep learning.

**Warning that decides whether this phase is publishable:** WiFi sensing models
overfit to *environment* catastrophically. A model trained in one room, one link
geometry, one person, transfers badly. Evaluate cross-environment (move the
laptop, move the router) or cross-person, and report those numbers, not just
within-session CV. Domain-invariant representations (the BVP idea from Widar3.0)
exist precisely because of this.

---

## Phase 3b — 3D spatial reconstruction (multi-node tomography)

**Hardware:** ~12 ESP32s at staggered heights around the room (~$60).

A single link cannot localise in 3D at any power or carrier frequency — one
number per packet, three unknowns. Spatial diversity is the only fix. N nodes
give N(N-1)/2 links; inverting the shadowing pattern yields a 3D voxel field.

Implemented in `wifisense/spatial/`, with the design study and accuracy
simulation in `scripts/rti_sim.py`. Full treatment, including why transmit power
and carrier frequency are the wrong knobs, in `docs/GOING_3D.md`.

**Deliverable.** Localisation error vs node count and link noise, measured in
your actual room against the simulated prediction.

---

## Phase 4 — Passive radar / true "object detection" (optional, hard)

Only if you have a real reason. Two receive chains via SDR (reference channel
pointed at the router, surveillance channel pointed at the scene), cross-ambiguity
function for range-Doppler, CLEAN or adaptive cancellation to suppress the
direct-path signal that will otherwise be ~60 dB above every target.

This shares almost no code with Phases 1-3 and is a much longer project. Budget
months, not weeks, and expect the direct-path cancellation to be where the time
goes.

---

## Evaluation protocol (apply from Phase 1 onward)

These are the things that separate a result from a number.

1. **Group by session in CV.** Sliding windows overlap; a random split puts
   near-duplicate windows in train and test and yields a meaningless ~99%.
   `grouped_cv_report()` enforces `GroupKFold` over sessions.
2. **Always report chance.** Majority-class rate, stated next to your accuracy.
3. **Always report the unsupervised baseline.** If a forest cannot beat a
   calibrated energy threshold, it learned your recording schedule.
4. **Interleave recordings.** All of class A on Monday and class B on Tuesday
   means the model can learn the day. This is the #1 way WiFi sensing papers
   are wrong.
5. **Hold out environment/person, not just windows**, before claiming generality.
6. **Watch the level features.** Absolute RSSI encodes *where the laptop is*.
   `include_level` is off by default for exactly this reason; if you turn it on,
   ablate it and report both.
7. **Log confounds in `meta.json`**: doors open, other people in the flat,
   microwave running, other 5 GHz APs, laptop moved. Most anomalies you will
   chase are one of these.

---

## Reading list

Starting points; verify exact citations before you cite them.

**Surveys / orientation**
- Ma, Zhou, Wang, *WiFi Sensing with Channel State Information: A Survey*,
  ACM Computing Surveys, 2019.
- IEEE **802.11bf** (WLAN Sensing) task group — sensing is being standardised;
  useful for framing why this matters.

**Foundational systems**
- Youssef et al., *Challenges: Device-free Passive Localization*, MobiCom 2007 —
  origin of device-free passive sensing.
- Halperin et al., *Tool release: gathering 802.11n traces with CSI*,
  SIGCOMM CCR 2011 — the Intel 5300 CSI Tool.
- Wang et al., *E-eyes*, MobiCom 2014 — CSI activity fingerprints.
- Wang et al., *CARM: Understanding and Modeling of WiFi Signal Based Human
  Activity Recognition*, MobiCom 2015 — CSI-speed model, the Doppler basis.
- Adib & Katabi, *See Through Walls with WiFi*, SIGCOMM 2013; Adib et al.,
  *WiTrack*, NSDI 2014 — the ambitious end of the field.

**Tooling**
- Gringoli et al., *Free Your CSI* (Nexmon CSI), WiNTECH 2019.
- Jiang et al., *PicoScenes*, IEEE IoT Journal 2022.
- Hernandez & Bulut, *ESP32 CSI Toolkit*, WoWMoM 2020.

**Methods you will need**
- Zeng et al., *FarSense*, IMWUT 2019 — CSI ratio for phase noise cancellation.
- Zheng et al., *Widar3.0*, MobiSys 2019 — cross-domain gesture recognition, BVP.

**Public datasets** (use one to validate your pipeline before trusting your own
recordings): Widar3.0, SignFi, UT-HAR.

---

## Ethics and legality

Worth settling early, not after you have recordings.

- **Capture only your own network.** `collect.py` filters to your router's BSSID.
  Sniffing neighbours' frames is legally murky at best in most jurisdictions;
  the RSSI-only fields here are metadata, but do not widen the filter.
- **Human subjects.** If you record anyone other than yourself and intend to
  publish, you need IRB review. At CMU that is a real process with real lead
  time — start it before you collect, not after.
- **Dual use is genuine here.** Through-wall presence detection is a
  privacy-invasive capability. Say so in your write-up; do not pretend a
  sensing paper is neutral.
