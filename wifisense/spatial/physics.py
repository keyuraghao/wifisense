"""What actually sets sensing resolution -- and what does not.

Three levers get proposed for "better accuracy". Only one of them is real:

  transmit power   almost useless. See power_analysis(). Your measurement floor
                   is the 1 dB radiotap quantiser, not thermal noise. You are
                   already ~45 dB above the noise floor, so +10 dB of transmit
                   power moves the quantiser not at all -- and raises the direct
                   path and the reflected path together, leaving the ratio that
                   carries the information unchanged. It is also capped by
                   regulation (FCC Part 15: 1 W conducted / 4 W EIRP at 2.4 GHz,
                   and consumer radios sit at 20 dBm).

  carrier freq     real but narrow. Halving the wavelength doubles the phase
                   shift per millimetre of target displacement, which genuinely
                   helps micro-motion (breathing). It does nothing for range
                   resolution by itself -- that is bandwidth.

  bandwidth        the real lever for range. dR = c/2B, full stop. 20 MHz gives
                   you 7.5 m of range resolution, which is worse than the room.

  aperture         the real lever for angle, and the one nobody has: angular
                   resolution needs many antennas spread over many wavelengths.

The reason higher-frequency systems look so much better is not the carrier. It
is that mmWave hardware ships with GHz of bandwidth and 12-64 element arrays.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

C = 299_792_458.0  # m/s


# --------------------------------------------------------------------------
# Resolution limits
# --------------------------------------------------------------------------

def wavelength(freq_hz: float) -> float:
    return C / freq_hz


def range_resolution(bandwidth_hz: float) -> float:
    """Two-way range resolution, dR = c / (2B). Independent of carrier."""
    return C / (2.0 * bandwidth_hz)


def angular_resolution(n_antennas: int, spacing_wavelengths: float = 0.5) -> float:
    """Half-power beamwidth of a uniform linear array, in radians.

    ~0.886 * lambda / (N*d). At the standard d = lambda/2 this collapses to
    1.772/N -- note it depends only on element *count*, not on frequency. Three
    antennas give ~34 degrees no matter what band you are in.
    """
    if n_antennas < 2:
        return math.pi  # a single element has no angular discrimination at all
    return 0.886 / (n_antennas * spacing_wavelengths)


def cross_range_resolution(range_m: float, n_antennas: int,
                           spacing_wavelengths: float = 0.5) -> float:
    """Lateral resolution at a given range: R * beamwidth."""
    return range_m * angular_resolution(n_antennas, spacing_wavelengths)


def phase_per_mm(freq_hz: float) -> float:
    """Two-way phase shift (radians) per millimetre of target displacement.

    dphi = 4*pi*dd/lambda. This is the one place a higher carrier directly wins:
    at 5 GHz a 1 mm chest displacement turns 0.21 rad; at 60 GHz, 2.5 rad.
    """
    return 4.0 * math.pi * 1e-3 / wavelength(freq_hz)


def velocity_resolution(freq_hz: float, observation_s: float) -> float:
    """Doppler velocity resolution: dv = lambda / (2*T)."""
    return wavelength(freq_hz) / (2.0 * observation_s)


# --------------------------------------------------------------------------
# Why transmit power is the wrong knob
# --------------------------------------------------------------------------

@dataclass
class PowerAnalysis:
    rssi_dbm: float
    noise_floor_dbm: float
    snr_db: float
    thermal_precision_db: float
    quantiser_precision_db: float
    limiting_factor: str
    gain_from_10db_more_power_db: float


def power_analysis(rssi_dbm: float = -50.0, noise_floor_dbm: float = -95.0,
                   quantisation_db: float = 1.0) -> PowerAnalysis:
    """Compare the two things that limit how finely you can measure the channel.

    Thermal precision: with SNR this high, amplitude estimation error is roughly
    1/sqrt(SNR) in linear terms, which in dB is tiny.
    Quantiser precision: a uniform 1 dB quantiser has RMS error q/sqrt(12).

    Whichever is larger is your floor. For any normal indoor link it is the
    quantiser by two orders of magnitude, and transmit power cannot touch it.
    """
    snr_db = rssi_dbm - noise_floor_dbm
    snr_lin = 10 ** (snr_db / 10.0)
    thermal = 8.686 / math.sqrt(2 * snr_lin)        # dB, ~ 20*log10(e)/sqrt(2*SNR)
    quant = quantisation_db / math.sqrt(12.0)

    limiting = "quantiser" if quant > thermal else "thermal noise"
    # +10 dB power lifts SNR by 10 dB, improving only the thermal term.
    thermal_after = 8.686 / math.sqrt(2 * snr_lin * 10)
    before = max(thermal, quant)
    after = max(thermal_after, quant)
    gain = 20 * math.log10(before / after) if after > 0 else 0.0

    return PowerAnalysis(
        rssi_dbm=rssi_dbm, noise_floor_dbm=noise_floor_dbm, snr_db=snr_db,
        thermal_precision_db=thermal, quantiser_precision_db=quant,
        limiting_factor=limiting, gain_from_10db_more_power_db=gain,
    )


# --------------------------------------------------------------------------
# Platform comparison
# --------------------------------------------------------------------------

@dataclass
class Platform:
    name: str
    freq_hz: float
    bandwidth_hz: float
    n_rx_antennas: int
    cost_usd: str
    notes: str

    @property
    def range_res_m(self) -> float:
        return range_resolution(self.bandwidth_hz)

    @property
    def angle_res_deg(self) -> float:
        return math.degrees(angular_resolution(self.n_rx_antennas))

    @property
    def cross_range_at_5m(self) -> float:
        return cross_range_resolution(5.0, self.n_rx_antennas)

    @property
    def phase_per_mm_rad(self) -> float:
        return phase_per_mm(self.freq_hz)


PLATFORMS = [
    Platform("MT7921 RSSI (yours)", 5.28e9, 80e6, 1, "$0",
             "scalar only -- no ranging, no angle, at any bandwidth"),
    Platform("ESP32 CSI", 2.437e9, 20e6, 1, "$5",
             "52 subcarriers; per-link, not spatial"),
    Platform("RPi 4 + Nexmon CSI", 5.24e9, 80e6, 1, "$45",
             "234 subcarriers; sniffs your existing router"),
    Platform("Intel AX210 + PicoScenes", 5.24e9, 160e6, 2, "$25",
             "first real multi-antenna phase; 2 antennas is barely an array"),
    Platform("WiFi 7 BE200", 6.1e9, 320e6, 2, "$35",
             "widest WiFi bandwidth available"),
    Platform("802.11ad 60 GHz", 60.48e9, 2.16e9, 8, "$100+",
             "huge bandwidth, but CSI access on consumer gear is impractical"),
    Platform("TI IWR6843 mmWave radar", 60e9, 4e9, 12, "$300",
             "3TX x 4RX MIMO -> 3D point cloud out of the box; not WiFi"),
]
