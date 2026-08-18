# Troubleshooting

## `collect.py` captures 0 frames

Most likely: the driver refused to run a monitor vif alongside the associated
managed vif. MT7921 is a single-radio part, so both vifs must share a channel;
some driver versions allow it, some silently drop all monitor frames.

Diagnose:

```bash
sudo iw phy phy0 interface add mon0 type monitor
sudo ip link set mon0 up
sudo iw dev mon0 info            # does it report the right channel?
sudo tcpdump -i mon0 -c 20 -e    # any frames at all?
sudo iw dev mon0 del
```

If `tcpdump` sees nothing, use the **disconnected capture** workaround: drop the
managed connection, put the interface itself into monitor mode, and illuminate
from a *second* device (phone, another laptop) that stays connected and pings
the router.

```bash
sudo systemctl stop NetworkManager
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set channel 56 80MHz
# then run collect.py with --mon wlan0 --no-traffic
sudo .venv/bin/python scripts/collect.py --label empty --mon wlan0 --no-traffic --seconds 120
```

Restore afterwards:

```bash
sudo ip link set wlan0 down && sudo iw dev wlan0 set type managed
sudo ip link set wlan0 up && sudo systemctl start NetworkManager
```

This is the more reliable configuration overall, and a second device as the
traffic source is better practice anyway — it fixes the link geometry instead of
letting your own NIC be both illuminator and sensor.

## Capture rate is low (< 50 Hz)

- Confirm the ping flood is running: `ping -i 0.002` needs root.
- The AP may be rate-limiting ICMP. Use a TCP/UDP stream instead
  (`iperf3 -c <router>` if the router runs a server, or a large HTTP download).
- Check for DFS radar events forcing a channel change: `dmesg | grep -i dfs`.
  Move to a non-DFS channel (36-48 in the US) and disable auto-channel.
- `plot_session.py` panel 4 shows frame rate over time — look for dropouts.

## Rate is fine but RSSI is flat / never varies

- The link may be too strong. If the laptop is 1 m from the router the direct
  path dominates every reflected path and motion is invisible. Move to 3-8 m,
  ideally with the person able to pass between the two.
- Check `iw dev wlan0 link` for a fixed high MCS — some drivers report a
  smoothed RSSI. Per-frame radiotap values from monitor mode should be noisy;
  if yours are suspiciously constant, the driver may be reporting an EWMA.

## `scapy` permission errors

Raw sockets need root. Either `sudo .venv/bin/python ...` or grant capabilities:
`sudo setcap cap_net_raw,cap_net_admin+eip $(readlink -f .venv/bin/python)`
(the setcap route affects every script that interpreter runs — prefer sudo).

## Channel keeps changing mid-session

Disable auto channel selection and DFS in the router admin page. Any session
where `meta.json`'s channel does not match the frames is garbage; delete it
rather than trying to salvage it.

## Results look too good (>99%)

Almost certainly leakage. Check, in order:
1. Are you using `grouped_cv_report` (session-grouped), not a random split?
2. Did you record all of one class in one sitting, and another class later?
3. Is `include_level` on? Absolute RSSI can encode "which session is this".

## Live dashboard shows "waiting for frames"

The viewer is tailing a file nothing is writing to. Check `scripts/stream.py` is
actually running as root in the other terminal and that its frame counter is
climbing. Confirm the path matches: `stream.py --out` and `live_view.py --follow`
must point at the same file.

To check the display path independently of the radio, replay a recording. If you
have none yet, `scripts/selftest.py` generates synthetic ones:

```bash
.venv/bin/python scripts/selftest.py          # creates data/sessions_synthetic/
.venv/bin/python scripts/live_view.py --replay data/sessions_synthetic/walking__synth0 --speed 4
```

If that renders, the problem is capture, not the viewer.

## Live dashboard: no window appears

`live_view.py` defaults to the TkAgg backend. Over SSH or on a headless box use
`--backend WebAgg` (opens in a browser) or `--headless 30 --save frame.png`.

Avoid `--capture`, which runs the GUI as root; root often cannot reach your
Wayland/X session. The two-terminal split (`stream.py` as root, `live_view.py`
as you) exists precisely to avoid this.

## Everything reads MOTION, or nothing ever does

You calibrated wrong. Calibration must happen with the space **empty and still** —
if you were sitting at the laptop during it, the threshold is set above your own
motion and nothing will ever trigger. Restart the viewer and leave the room for
the calibration window.

`--calibrate 0` skips calibration entirely, which leaves the threshold at 0 and
makes everything read MOTION. That mode is only useful with a trained `--model`.
