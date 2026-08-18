# Mesh security

```bash
.venv/bin/python scripts/gen_mesh_key.py    # once, before flashing anything
```

Writes `config/mesh.key` (server, mode 600) and
`firmware/esp32_rti_node/mesh_key.h` (compiled into every node). Neither is
committed. Then flash the nodes and start the dashboard - security is on by
default and there is nothing else to configure.

## Threat model

What an attacker can do to an **unprotected** mesh:

| # | Attack | Needs | Impact |
|---|---|---|---|
| 1 | Forge UDP reports claiming to be any node | LAN access only | **Total.** The reconstruction input is attacker controlled: place a phantom person anywhere, or erase a real one |
| 2 | Forge ESP-NOW beacons impersonating a node | radio in range | Corrupts peers' RSSI tables |
| 3 | Replay captured traffic | passive capture | Freeze or rewind the picture |
| 4 | Read reports | passive capture | Reveals where people are inside a building |

Attack 1 is the serious one. It needs no radio at all, just network access, and
it hands the attacker direct control of what the system believes.

All four are blocked. What follows is how, and what is *not* covered.

## Design

**Per-node keys from one master.** `K_node = HKDF-SHA256(master, salt=node_mac,
info=purpose)`. Every node holds the master, so any node can derive any peer's
key to verify its beacons, and the server derives a node's key the moment it
first appears. No key exchange, no PKI, no per-device provisioning - one binary,
flashed unchanged. Report keys and beacon keys use different `info` strings, so
recovering one does not yield the other.

**Reports: AES-128-GCM.** The 18-byte header travels in clear because the server
needs the node id to select a key, but it is bound in as additional
authenticated data, so it cannot be altered without breaking the tag. Overhead
is 34 bytes per report.

**Beacons: truncated HMAC-SHA256, no encryption.** There is nothing secret in
`{node, boot_id, seq}`, and ESP-NOW cannot encrypt broadcast frames anyway.
Authenticity is the requirement. 8 bytes of tag on a 40 ms beacon is a
reasonable forgery/overhead trade. Total beacon: 26 bytes.

Critically, the receiver verifies against the MAC **inside the authenticated
header**, not `info->src_addr`, which an attacker controls freely.

**Nonces are counters, never random.** `nonce = 0000 || boot_id || seq`. With a
per-node key that is unique for the life of the key, provided `boot_id`
increases across reboots - which is why the node persists it in NVS and
increments it every boot. GCM nonce reuse is catastrophic, so this is not a
detail to improvise on.

**Replay: strictly increasing `(boot_id, seq)`,** not a sliding window. UDP can
reorder, but accepting an out-of-order frame also accepts a replayed one, and a
stale sensing report is worthless anyway. The counter only advances *after* the
tag verifies, so a forged frame cannot lock a node out.

**Fail closed.** No key configured means nothing is accepted. A mesh that
silently falls back to plaintext when the key file is missing is worse than one
that stops, because nobody notices until the data is already poisoned.
`--insecure` exists for debugging and prints a loud warning.

`KeyStore.from_file` also refuses a world-readable key file rather than quietly
using it.

## Verified

120 hostile datagrams sent at a live server while it tracked a target:

| Attack | Sent | Accepted |
|---|---|---|
| Plaintext injection (no key) | 30 | **0** |
| Forged, sealed with attacker's own key | 30 | **0** |
| Replay of a validly-sealed old frame | 30 | **0** |
| Malformed / garbage flood | 30 | **0** |

Tracking continued undisturbed throughout. Legitimate traffic in the same run
was accepted normally. Fail-closed was confirmed separately: with no key
configured, 10 valid-looking plaintext reports were all rejected.

The firmware's crypto format was validated by compiling its byte-layout and
key-derivation code standalone against OpenSSL and feeding the output to the
real Python verifier - header layout, HKDF salt/info/length, nonce construction
and AAD binding all match.

## What this does NOT protect against

Be clear-eyed about these. They are not oversights; they are outside what
cryptography can do.

**Physical-layer interference.** An attacker with a transmitter can jam the
band, or radiate energy that changes the RSSI legitimate nodes measure on
legitimate links. RSSI is a physical measurement of a shared channel. Crypto
authenticates *who said what*; it cannot authenticate physics. Detecting this is
anomaly detection (`crypto.check_plausibility` is a starting point: it flags
non-physical values and implausible jumps), not a crypto control.

**A compromised node.** Physical possession of one node yields the master key
and therefore the whole mesh, and a node with a valid key can sign whatever it
likes. If that matters for your deployment, enable **ESP32 flash encryption and
secure boot** so the key cannot be read out of a stolen node, and rotate with
`gen_mesh_key.py --rotate` if you suspect compromise. Rotation requires
reflashing every node, which is why the script refuses to overwrite by accident.

**Denial of service.** An attacker who can reach the UDP port can flood it, and
one with a radio can jam ESP-NOW. Forgeries are counted rather than logged per
packet, so a flood cannot become a log-volume DoS, but the traffic itself is not
stopped. Firewall the port to the mesh subnet.

**Traffic analysis.** Packet timing and volume still reveal that a mesh is
running and roughly how many nodes it has, even though the contents are
encrypted.

## Privacy

Worth stating alongside the crypto: this system infers where people are inside a
building. The encryption protects that inference in transit; it does not make
collecting it appropriate. If you record anyone other than yourself and intend
to publish, you need IRB review - see the ethics section of
`docs/RESEARCH_PATHWAY.md`.
