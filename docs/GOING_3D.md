# Going 3D: what power, frequency and geometry actually buy

Run `.venv/bin/python scripts/rf_resolution.py` for the live numbers, and
`.venv/bin/python scripts/rti_sim.py` for the 3D design study.

## The short version

Of the three levers, **two do not do what they sound like they do.**

| Lever | Verdict |
|---|---|
| More transmit power | **Does essentially nothing.** |
| Higher carrier frequency | Real, but only for micro-motion — not for 3D. |
| More bandwidth / more aperture | The actual levers. Neither is a power setting. |

### Why power does not help

Your link sits around −50 dBm with a −95 dBm noise floor: **45 dB of SNR**. At
that SNR, amplitude-estimation error from thermal noise is ~0.034 dB. But
radiotap reports RSSI as an **integer dBm**, so quantisation alone contributes
1/√12 = **0.289 dB** — 8× larger. Your measurement floor is the quantiser.

Adding 10 dB of transmit power improves the thermal term and leaves the
quantiser untouched, for a net precision gain of **0.000 dB**. Worse, power
raises the direct path and the reflected path *together*, so the ratio that
actually carries target information is unchanged.

It is also capped by regulation (FCC Part 15: 1 W conducted / 4 W EIRP at
2.4 GHz; consumer radios ship at ~20 dBm), and unlicensed amplifiers are not
legal to operate. But the regulatory point is almost beside the point — even
with unlimited power the physics gives you nothing.

**The way to break that floor is CSI, not watts.** CSI is not quantised to 1 dB
and gives ~52 numbers per frame instead of 1.

### What frequency actually buys

Two-way phase shift per millimetre of target displacement is 4π·Δd/λ:

| Carrier | λ | rad per mm |
|---|---|---|
| 2.4 GHz | 12.3 cm | 0.10 |
| 5 GHz (yours) | 5.7 cm | 0.22 |
| 60 GHz | 5.0 mm | 2.51 |

So 60 GHz is ~11× more sensitive to breathing-scale motion. That is genuine.
But it does **nothing** for range resolution, which depends only on bandwidth:

    ΔR = c / 2B

| Bandwidth | Range resolution |
|---|---|
| 20 MHz | 7.5 m |
| **80 MHz (your link)** | **1.9 m** |
| 160 MHz | 0.94 m |
| 320 MHz (WiFi 7) | 0.47 m |
| 2.16 GHz (802.11ad) | 0.07 m |

At 80 MHz your range resolution is worse than your room. mmWave systems look
transformative because they ship with GHz of bandwidth and 12–64 element arrays
— not because of the carrier itself.

### Why one link can never give 3D

Angular resolution is ~0.886·λ/(N·d); at the standard λ/2 spacing that is
**1.77/N radians — a function of antenna count only, not frequency**. Three
antennas give ~34°, which at 5 m range is ±3 m of cross-range error.

More fundamentally: one link yields **one number per packet**. Recovering three
coordinates from one measurement is a rank deficiency, not an engineering
shortfall. No power level and no carrier fixes it.

---

## Two honest routes to 3D

### (a) Many cheap nodes → radio tomography  ← recommended

Surround the room with ~12 ESP32s. N nodes give N(N−1)/2 links, and inverting
the shadowing pattern gives a 3D voxel field (Wilson & Patwari, RTI, IEEE TMC
2010). Keeps the WiFi framing, extends the existing code, costs ~$60.

Implemented in `wifisense/spatial/`, validated by `scripts/rti_sim.py`.

### (b) One mmWave radar

TI IWR6843: 4 GHz bandwidth (3.7 cm range resolution) and 3TX×4RX MIMO for real
3D point clouds, ~$300. Far better data — but it is a radar, not your router,
and it shares no code with Phases 1–3.

---

## RTI design rules (each one from a simulation in `rti_sim.py`)

**1. About 12 nodes. More stops helping.**
Median XY error vs node count and link noise, 5×4×2.4 m room:

| nodes | links | 0.5 dB | 1.0 dB | 1.5 dB | 2.0 dB | 3.0 dB |
|---|---|---|---|---|---|---|
| 8 | 28 | 0.15 | 0.17 | 0.22 | 0.29 | 0.42 |
| **12** | **66** | **0.08** | **0.10** | **0.13** | **0.15** | **0.27** |
| 16 | 120 | 0.07 | 0.09 | 0.14 | 0.16 | 0.21 |
| 24 | 276 | 0.10 | 0.13 | 0.14 | 0.18 | 0.24 |

Past ~12 nodes, model error and node-placement error dominate, and adding links
fixes neither.

**2. Stagger node heights. This is not optional.**
With every node at one height, all link paths lie in one plane, illuminating a
thin horizontal slab. Probing with a compact spherical target at random heights:

| layout | z coverage | z corr | est. spread |
|---|---|---|---|
| all at 1.2 m | 0.67 | **0.28** | **0.00 m** |
| two heights 0.4/2.0 m | 1.00 | 0.97 | 0.48 m |
| three heights 0.4/1.2/2.0 m | 1.00 | 0.97 | 0.43 m |

Read **est. spread**: coplanar nodes return the *same* height every single time,
pinned to the node plane. Their z error looks survivable only because that plane
happens to sit mid-room — an artefact that collapses the moment you move them.
The correlation is the honest number.

**3. Calibrate `noise_var` against real empty-room link variance.**
This is not cosmetic tuning. Under-estimating it makes the inverse over-trust
the data, and accuracy then gets *worse* as you add nodes — at 16 nodes,
0.61 m with a guessed prior vs 0.16 m with a matched one. Record a few minutes
of empty room and call `rti.estimate_noise_var()`.

**4. RTI measures *change*.** Everything is differenced against an empty-room
baseline. Rearranged furniture invalidates the baseline; re-record it.

---

## What is and is not validated

**Validated in simulation:** the reconstruction maths, the node-count curve, the
coplanar-geometry failure, and the noise-prior sensitivity. The forward model is
deliberately *not* the one being inverted — finer grid, soft Fresnel taper
instead of a hard ellipse, correlated fading, node-position jitter, and the 1 dB
quantiser — so the numbers are not self-fulfilling.

**Not validated:** any of it on real hardware. And the simulation still omits
walls, furniture, body orientation, and the fact that a person perturbs
multipath on links they never block. **Expect real deployments at the pessimistic
end of those tables, 0.3–0.5 m, in line with the published RTI literature.**
Treat the simulated figures as an upper bound on what you will measure.

## Bill of materials (~$70)

| Item | Qty | ~Cost |
|---|---|---|
| ESP32-WROOM-32 dev board | 12 | $60 |
| USB power supplies / battery packs | 12 | varies |
| Tape measure, for node positions | 1 | — |

Node position error goes straight into localisation error — the simulation
assumes 5 cm placement jitter. Measure carefully; do not eyeball it.

## Reading

- Wilson & Patwari, *Radio Tomographic Imaging with Wireless Networks*,
  IEEE TMC 2010 — the RTI formulation implemented here.
- Wilson & Patwari, *See-Through Walls: Motion Tracking Using Variance-Based
  Radio Tomography*, IEEE TMC 2011 — variance-based RTI, better for moving targets.
- Kaltiokallio, Bocca & Patwari — RTI calibration and fade-level effects.
