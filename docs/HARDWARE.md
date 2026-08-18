# Hardware: what you have, what it limits, what to buy

Run `wifisense sense check` for the live report.

## What is in this laptop

| | |
|---|---|
| Chipset | MediaTek MT7921 (Filogic 330), 802.11ax |
| Driver | `mt7921e` (mainline `mt76`) |
| Monitor mode | **Yes** |
| CSI export | **No** |
| Router link | 5 GHz, ch 56 (5280 MHz), 80 MHz |

The CSI verdict is not a guess. The mainline `mt76` tree has no CSI path for
MT7921; checking the shipped modules for CSI symbols returns nothing:

```bash
for m in mt76 mt76-connac-lib mt7921-common mt7921e; do
  strings "$(modinfo -n $m)" | grep -i csi
done   # -> no output
```

MediaTek's vendor/OpenWrt trees do expose CSI for some MT7915 AP parts. Porting
that to an MT7921 client part is a driver-reverse-engineering project in its own
right, not a step on the way to a sensing result. Do not start there.

## What each measurement actually buys you

| Measurement | Numbers per frame | Available here | Enables |
|---|---|---|---|
| RSSI | 1 (integer dBm) | **now** | presence, gross motion, coarse activity |
| CSI amplitude | 52-234 per antenna | ESP32 / RPi | fine activity, gesture, respiration |
| CSI amplitude + phase | 2x that, multi-antenna | AX210, RPi4 | AoA, ToF, localisation, imaging |

The jump from 1 number to ~52 is the single largest capability step in this
project. Everything RSSI cannot do, it cannot do because of that ratio: one
scalar is the sum over all propagation paths, so two different room states that
happen to sum to the same power are indistinguishable in principle, not just in
practice.

## Recommended purchases, in order of value per dollar

1. **ESP32 dev boards** (~$5-10 each). **Owned: 1x ESP32-S3.**
   Two distinct uses, and they need different firmware:
   - **CSI capture** (Phase 2): Espressif's `esp-csi`, or the ESP32 CSI Toolkit
     (Hernandez & Bulut). 52 usable subcarriers at up to ~100 Hz over USB
     serial; `wifisense/capture/esp32_csi.py` parses its output. Two boards give
     you a dedicated transmitter and receiver, which removes the
     traffic-generation problem and fixes the link geometry.
   - **3D mesh nodes** (Phase 3b): this project's own firmware, in
     `firmware/esp32_rti_node/`. Any WiFi-capable family works - ESP32, S2, S3,
     C3, C5, C6. **Not H2 or P4: those have no WiFi radio at all.** About 12
     nodes for a room. See `docs/GETTING_STARTED.md`.

2. **Raspberry Pi 4 or 3B+** (~$35-60) with **Nexmon CSI**.
   Broadcom bcm43455c0, 802.11ac, up to 80 MHz -> 234 subcarriers, and it can
   sniff CSI from *your existing router's* traffic rather than needing its own
   transmitter. Higher fidelity than ESP32, meaningfully more setup pain
   (kernel/firmware patching, version-sensitive).

3. **Intel AX210 M.2 card + PicoScenes** (~$25 card, plus a spare M.2 slot or a
   USB/M.2 enclosure). Multi-antenna CSI, 802.11ax, best fidelity of the three.
   Only worth it once you know you need AoA or multi-antenna phase.

4. **SDR (HackRF One ~$150, USRP B210 ~$1500)** - only for Phase 4 passive
   radar. Do not buy this first. It is a much harder project that shares almost
   no code with Phases 1-3.

## A note on the router itself

You do not need to modify or flash the router. It is the *illuminator*: it
transmits, you measure. Its only relevant properties are that it stays on a
fixed channel and transmits often enough. Fix the channel in the router admin
page (disable auto channel selection and DFS) before recording anything you
intend to keep - a mid-session channel change silently invalidates a recording.
