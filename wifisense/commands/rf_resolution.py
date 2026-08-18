#!/usr/bin/env python3
"""What power, frequency, bandwidth and aperture actually buy you.

    .venv/bin/python scripts/rf_resolution.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from ..spatial import physics as ph


def main(argv=None) -> int:
    print("=" * 78)
    print("1. TRANSMIT POWER")
    print("=" * 78)
    a = ph.power_analysis()
    print(f"  typical link: {a.rssi_dbm:.0f} dBm received, "
          f"noise floor {a.noise_floor_dbm:.0f} dBm  ->  SNR {a.snr_db:.0f} dB")
    print(f"  precision limit from thermal noise : {a.thermal_precision_db:.4f} dB")
    print(f"  precision limit from 1 dB quantiser: {a.quantiser_precision_db:.4f} dB")
    print(f"  you are limited by: {a.limiting_factor.upper()} "
          f"({a.quantiser_precision_db / a.thermal_precision_db:.0f}x the thermal term)")
    print(f"\n  improvement from +10 dB transmit power: "
          f"{a.gain_from_10db_more_power_db:.3f} dB")
    print("  -> Power raises the direct path and the reflected path together, so")
    print("     the ratio carrying the information is unchanged, and the quantiser")
    print("     floor does not move. It is also capped by regulation (FCC Part 15).")
    print("     The way to break this floor is CSI, which is not quantised to 1 dB.")

    print()
    print("=" * 78)
    print("2. BANDWIDTH  -- the real lever for range")
    print("=" * 78)
    print("     dR = c / 2B, independent of carrier frequency\n")
    for b, label in [(20e6, "802.11n 20 MHz"), (80e6, "your link, 80 MHz"),
                     (160e6, "802.11ax 160 MHz"), (320e6, "802.11be 320 MHz"),
                     (2.16e9, "802.11ad 2.16 GHz"), (4e9, "TI mmWave FMCW 4 GHz")]:
        print(f"  {label:<26} {b/1e6:>8.0f} MHz  ->  {ph.range_resolution(b):>7.2f} m")
    print("\n  -> At 80 MHz your range resolution is worse than the room. You cannot")
    print("     separate two people by range at any transmit power.")

    print()
    print("=" * 78)
    print("3. APERTURE  -- the real lever for angle")
    print("=" * 78)
    print("     beamwidth ~ 0.886*lambda/(N*d); at d=lambda/2 this depends ONLY on N,")
    print("     not on frequency\n")
    for n in [1, 2, 3, 4, 8, 16, 64]:
        r = ph.angular_resolution(n)
        print(f"  {n:>3} antennas  ->  {math.degrees(r):>6.1f} deg  "
              f"->  {ph.cross_range_resolution(5.0, n):>6.2f} m across, at 5 m range")
    print("\n  -> 3 antennas is not an array. This is why single-device WiFi")
    print("     localisation is so poor, and why RTI uses many nodes instead.")

    print()
    print("=" * 78)
    print("4. CARRIER FREQUENCY  -- real, but only for micro-motion")
    print("=" * 78)
    print("     two-way phase shift per 1 mm of target displacement\n")
    for f, label in [(2.437e9, "2.4 GHz"), (5.28e9, "5 GHz (yours)"),
                     (6.1e9, "6 GHz"), (60e9, "60 GHz mmWave")]:
        print(f"  {label:<16} lambda {ph.wavelength(f)*100:>5.2f} cm  ->  "
              f"{ph.phase_per_mm(f):>5.2f} rad/mm")
    print("\n  -> Going 5 -> 60 GHz makes you ~11x more sensitive to breathing-scale")
    print("     motion. It does NOT improve range resolution -- that came along for")
    print("     the ride because mmWave hardware ships with GHz of bandwidth.")

    print()
    print("=" * 78)
    print("5. PLATFORMS")
    print("=" * 78)
    print(f"  {'platform':<28}{'range':>8}{'angle':>8}{'@5m':>8}{'rad/mm':>8}  {'cost':>6}")
    print("  " + "-" * 74)
    for p in ph.PLATFORMS:
        ang = "n/a" if p.n_rx_antennas < 2 else f"{p.angle_res_deg:.0f}d"
        cross = "n/a" if p.n_rx_antennas < 2 else f"{p.cross_range_at_5m:.1f}m"
        print(f"  {p.name:<28}{p.range_res_m:>7.2f}m{ang:>8}{cross:>8}"
              f"{p.phase_per_mm_rad:>8.2f}  {p.cost_usd:>6}")
    print()
    for p in ph.PLATFORMS:
        print(f"  {p.name:<28} {p.notes}")

    print()
    print("=" * 78)
    print("BOTTOM LINE")
    print("=" * 78)
    print("  A single link gives one number per packet. No amount of power or")
    print("  carrier frequency turns one number into a 3D position -- that is a")
    print("  rank argument, not an engineering limitation.")
    print()
    print("  To get 3D you need spatial diversity. Two honest options:")
    print("    (a) MANY CHEAP NODES  ~12 ESP32s around the room -> tomography.")
    print("        ~$60, keeps the WiFi framing, ~0.2-0.5 m. scripts/rti_sim.py")
    print("    (b) ONE mmWave RADAR  TI IWR6843: 4 GHz bandwidth + 12 virtual")
    print("        antennas -> 3D point cloud. ~$300, but it is no longer WiFi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
