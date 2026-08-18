# The ESP32 mesh: adding nodes, surviving failures

Run it with no hardware at all:

```bash
# terminal 1 - server, reconstruction, dashboard
.venv/bin/python scripts/rti_dashboard.py

# terminal 2 - 12 virtual nodes, 4 of which die at t=28s and return at t=40s
.venv/bin/python scripts/rti_fake_nodes.py --nodes 12 --fail-after 28 --fail-count 4 --revive-after 12
```

The virtual nodes speak the real protocol over a real UDP socket, so enrolment,
liveness, baselines and topology rebuilds are all genuinely exercised. Only the
radio is simulated.

## Architecture

```
  ESP32 ---ESP-NOW broadcast---> every other ESP32      (measurement)
  ESP32 ---WiFi UDP-----------> server                  (reporting)
  server: registry -> active link set -> adaptive reconstruct -> dashboard
```

**ESP-NOW for measuring, WiFi/UDP for reporting.** ESP-NOW is connectionless and
needs no AP, so N nodes yield all N(N-1)/2 links with no association storm and
no router involvement, and it exposes per-frame RSSI in the receive callback -
which is the entire measurement. It has no route to the server though, so each
node also joins the normal WiFi network and sends its table over UDP. The two
coexist only on the same channel, so the firmware locks itself to the AP's
channel after associating.

**UDP, not TCP.** A lost report is worth less than a delayed one: the next one
is 200 ms away and carries fresher data, so retransmission would only ever
deliver stale measurements.

## Adding a node

Run `scripts/gen_mesh_key.py` once before flashing anything - it generates the
mesh key that every node shares. See `docs/SECURITY.md`.

Three steps, and the same binary goes on every node:

1. Flash `firmware/esp32_rti_node/esp32_rti_node.ino` (set SSID, password,
   server IP at the top). No per-node ID, no peer list, no per-node build.
2. Power it on. It appears in the dashboard within seconds under **UNPLACED**,
   with its MAC shown.
3. Add that MAC to `config/nodes.json` with its position:

```json
{
  "nodes": {
    "aa:bb:cc:00:00:01": {"name": "n00", "x": 0.83, "y": 0.0, "z": 0.4}
  }
}
```

Enrolment is automatic, **placement is deliberate**. A node that has never been
seen is registered the instant its first packet arrives, but it does not enter
the reconstruction until you say where it physically is. Guessing a position
would silently corrupt the geometry, and a wrong position is far worse than a
missing node.

Measure positions with a tape measure. Placement error goes straight into
localisation error.

**Stagger the heights.** Use 0.4 / 1.2 / 2.0 m around the walls. Coplanar nodes
cannot resolve height at all - see `docs/GOING_3D.md`.

## Surviving node failure

The inverse operator `Pi = C W^T (W C W^T + sigma^2 I)^-1` is built for one
specific link set. When a node dies its links vanish, so Pi is no longer the
right operator. Feeding it a short vector is a shape error; feeding it zeros is
worse, because zero attenuation on a dead link is a positive claim that nothing
is there.

So the operator is **rebuilt whenever the topology changes**. That is affordable
because the expensive part - the N x N spatial prior - depends only on the voxel
grid and is computed once at startup. A rebuild is then one N x M product and
one M x M inverse: **1-6 ms** at mesh scale. Recent operators are cached, so a
node flapping up and down does not force a rebuild each time.

Measured on the virtual mesh, killing 4 of 12 nodes:

| t | nodes | links | coverage | tracking |
|---|---|---|---|---|
| 27 s | 12 | 66 | 0.99 | (1.17, 2.71, 1.06) |
| **31 s - 4 nodes killed** | **8** | **28** | **0.69** | (1.51, 1.92, 1.23) |
| 37 s | 8 | 28 | 0.69 | (2.38, 0.73, 1.57) |
| **39 s - nodes revived** | **12** | **66** | **0.99** | (2.40, 0.92, 0.93) |

Tracking continues throughout. Note the **z estimate degrades** during the
outage (1.23-1.68 m against a true 0.875 m) while x and y stay usable: the four
lost nodes took height diversity with them. That is the coplanar trap appearing
dynamically, and it is why `coverage` is the number to watch rather than the
node count. Losing one corner node hurts far more than losing one of two nodes
on the same wall.

Below `--min-links` (default 10) the server refuses to reconstruct rather than
emitting a confident wrong answer, and the dashboard shows **DEGRADED** with the
reason.

## Calibration

RTI measures *change*, so every link needs an empty-room baseline. The dashboard
calibrates automatically once 4 or more placed nodes are alive: keep the room
empty for `--calibrate` seconds.

Baselines are stored **per link**, not per run, so a node can drop out and rejoin
without recalibrating the whole mesh. That is what makes failure recovery
seamless.

Calibration also measures per-link variance and feeds it to the reconstructor as
`noise_var`. Do not skip this: an under-estimated `noise_var` makes the inverse
over-trust the data, and accuracy then gets *worse* as you add nodes (0.61 m
with a guessed prior vs 0.16 m with a matched one, at 16 nodes).

## Dashboard panels

| Panel | Shows |
|---|---|
| 3D room | node positions, green alive / red dead, active links, target estimate |
| link matrix | which node pairs are currently exchanging packets |
| mesh health | nodes alive and links active over time |
| reconstruction | top-down attenuation field with the estimate marked |
| per node loss | packet loss per node, from sequence-number gaps |
| status | counts, unplaced MACs, rebuild timing, degradation reason |

## Status

**Verified:** protocol encode/decode including malformed input, registry
enrolment and liveness, per-link baselines, adaptive rebuild under node loss,
the full server + dashboard + virtual mesh loop, and the firmware's exact
output bytes round-tripping through the real Python parser (compiled the report
builder standalone and fed its output to `protocol.decode`).

**Now verified on hardware:** the firmware compiles (883 KB, 67% of flash;
49.5 KB RAM) and runs on an ESP32-S3 under arduino-esp32 3.3.11. Boot, key
derivation, NVS-persisted boot_id, and the heartbeat are all confirmed. See
`docs/GETTING_STARTED.md`.

**Still unverified:** multi-node behaviour. ESP-NOW at 12 nodes, real RSSI
quality, and reconstruction from live measurements are untested - that needs
more than one board.

Requires **arduino-esp32 core 3.x** (ESP-IDF 5.x): the RSSI-bearing ESP-NOW
receive callback does not exist in core 2.x.
