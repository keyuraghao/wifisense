# Getting started with real ESP32 hardware

Verified on an **ESP32-S3** (QFN56 rev v0.1, 8 MB PSRAM, native USB-Serial/JTAG),
arduino-esp32 core **3.3.11**, arduino-cli 1.5.1.

## 0. Toolchain (once)

```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=$HOME/.local/bin sh
export PATH=$HOME/.local/bin:$PATH        # add to ~/.zshrc to make it stick

arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32@3.3.11     # ~5.6 GB, takes a while
```

Core **3.x is required**. The RSSI-bearing ESP-NOW receive callback
(`esp_now_recv_info_t`) does not exist in 2.x, and RSSI is the entire
measurement.

You also need to be in the `dialout` group for serial access:

```bash
groups | grep -q dialout || { sudo usermod -aG dialout "$USER"; echo "log out and back in"; }
```

## 1. Identify your board

```bash
ls /dev/ttyACM* /dev/ttyUSB*
.venv/bin/python -m esptool --port /dev/ttyACM0 chip-id
```

- `/dev/ttyACM*` means **native USB** (S3, C3, C6, S2)
- `/dev/ttyUSB*` means an **external bridge** (CP210x / CH340), i.e. classic ESP32

The chip decides the FQBN. `scripts/flash_node.sh` detects this for you.

| Chip | FQBN |
|---|---|
| ESP32-S3 | `esp32:esp32:esp32s3:CDCOnBoot=cdc` |
| ESP32-C3 | `esp32:esp32:esp32c3:CDCOnBoot=cdc` |
| classic ESP32 | `esp32:esp32:esp32da` |

`CDCOnBoot=cdc` matters on native-USB parts: without it `Serial` goes to the
UART pins and you see nothing over USB.

## 2. Generate the mesh key (once, before flashing anything)

```bash
.venv/bin/python scripts/gen_mesh_key.py
```

Writes `config/mesh.key` (server, mode 600) and
`firmware/esp32_rti_node/mesh_key.h` (compiled into every node). Every node
shares this key. Neither file is committed. See `docs/SECURITY.md`.

## 3. Configure WiFi

```bash
.venv/bin/python scripts/setup_firmware.py \
  --ssid 'YOUR_2.4GHz_SSID' --password 'YOUR_PASSWORD' --led-pin -1
```

Three things to get right:

- **ESP32 is 2.4 GHz only.** If 5 GHz has its own SSID you must use the 2.4 GHz
  one. If your router band-steers under one SSID, that is fine.
- **Server IP** defaults to this machine's address. Give it a **DHCP
  reservation** on your router, or the mesh breaks when the lease changes.
- **`--led-pin -1`** on ESP32-S3 DevKits: they usually carry an addressable RGB
  LED on GPIO48, which `digitalWrite` cannot drive. Classic ESP32 boards can use
  `--led-pin 2`.

## 4. Flash

```bash
./scripts/flash_node.sh -m        # auto-detects port and chip, -m to watch serial
```

One board at a time: plug in, flash, note the MAC it prints, unplug, **label the
board with the last 4 hex digits**, next.

A healthy boot looks like:

```
connecting to MyNet_2G....
ip 192.168.1.52  rssi -47 dBm
channel 6 (from AP)
node id 348518926798
boot id 2
security: AES-128-GCM reports, HMAC-SHA256 beacons
key fingerprint 7fe94604 (must match on every node)
reporting to 192.168.1.187:9999
--- running ---
[     5s] peers 3  reports 25  rejected 0  ch 6  wifi up
```

`node id` is the mesh identity. `key fingerprint` must be **identical on every
node** - if one differs, that node was flashed with a stale `mesh_key.h` and the
server will reject everything it sends.

## 5. Place the nodes

Positions go in `config/nodes.json`, keyed by MAC:

```json
{
  "nodes": {
    "34:85:18:92:67:98": {"name": "n00", "x": 0.83, "y": 0.0, "z": 0.4}
  }
}
```

A node appears in the dashboard as **UNPLACED** until you add it here.
Enrolment is automatic; placement is deliberate, because a wrong position
corrupts the geometry worse than a missing node does.

- **Measure with a tape measure.** Placement error goes straight into
  localisation error; the simulation assumes 5 cm.
- **Stagger heights: 0.4 / 1.2 / 2.0 m.** Coplanar nodes cannot resolve height
  at all - they return the same z every time. See `docs/GOING_3D.md`.
- Spread them around the perimeter, not clustered on one wall.
- Origin and axes are yours to choose; just be consistent.

## 6. Run

```bash
.venv/bin/python scripts/rti_dashboard.py --room 5 4 2.4
```

Keep the room **empty** for the calibration window (default 20 s). It starts
automatically once 4 or more placed nodes are alive.

## Troubleshooting

**Node never appears in the dashboard.** Check the serial heartbeat. `wifi DOWN`
means it never associated - wrong SSID/password, or a 5 GHz-only SSID.

**Node appears but `peers 0`.** All nodes must be on the same channel. That
happens automatically once they all associate with the same AP, so `peers 0`
almost always means the others have not associated. While unassociated a node
scans, which hops channels, and ESP-NOW needs a shared channel - so a node that
cannot reach the AP drops out of the mesh entirely, not just out of reporting.

**Dashboard shows `auth failures` climbing.** A node was flashed with a
different `mesh_key.h`. Compare key fingerprints across nodes and reflash the
odd one out.

**`Sketch uses ... 67%` but the board reboots repeatedly.** Usually a PSRAM
setting mismatch on S3 boards. The default (PSRAM disabled) is safe; this
firmware does not need it.

**Serial prints nothing on an S3/C3.** Missing `CDCOnBoot=cdc` in the FQBN.

**`Peer channel is not equal to the home channel`.** Fixed in this firmware (the
broadcast peer uses channel 0, meaning "follow the interface"). If you see it,
you are running an older build - recompile.

## Scaling to a full mesh

12 nodes is the sweet spot for a 5x4 m room; returns flatten past that. Flash
them one at a time, label each with its MAC suffix, measure positions, and add
them all to `config/nodes.json`.

You do not need all 12 before testing. The reconstruction runs on whatever is
alive above `--min-links` (default 10, i.e. 5 nodes), and degrades gracefully.
Start with 5-6 and add more.
