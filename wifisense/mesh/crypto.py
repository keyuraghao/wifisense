"""Authentication and encryption for the mesh.

Threat model, stated plainly because it decides the design.

  What an attacker on your LAN or within radio range can do to an UNPROTECTED
  mesh:
    1. Forge UDP reports claiming to be any node, with fabricated RSSI values.
       This is the worst one by far: the reconstruction input is attacker
       controlled, so they can place a phantom person anywhere or erase a real
       one. It needs no radio at all, just network access.
    2. Forge ESP-NOW beacons impersonating a node, corrupting peers' RSSI tables.
    3. Replay captured traffic to freeze or rewind the picture.
    4. Read the reports, which reveal where people are inside a building.

  What this module stops: all four.

  What it CANNOT stop, and no cryptography can: an attacker with a transmitter
  can jam the band, or radiate energy that changes the RSSI legitimate nodes
  measure on legitimate links. RSSI is a physical measurement of the channel,
  and the channel is shared. Crypto authenticates *who said what*; it cannot
  authenticate physics. Detecting that kind of interference is an anomaly
  detection problem (see check_plausibility), not a crypto one.

Design:

  * Per-node keys derived from one master key: K_node = HKDF(master, salt=node).
    Every node holds the master, so any node can derive any peer's key to verify
    its beacons, and the server can derive a node's key the moment it first
    appears. No key exchange, no PKI, no provisioning per device.

  * Reports use AES-128-GCM. The header travels in clear (the server needs the
    node id to pick a key) but is bound in as additional authenticated data, so
    it cannot be tampered with.

  * Nonces are (boot_id, seq), never random. With a per-node key, that pair is
    unique for the life of the key provided boot_id increases across reboots -
    which is why the node persists it in NVS. Nonce reuse in GCM is
    catastrophic, so this is not a detail to improvise on.

  * Beacons carry a truncated HMAC tag rather than encryption: there is nothing
    secret in {node, boot_id, seq}, and ESP-NOW cannot encrypt broadcast frames
    anyway. Authenticity is what is needed, and 8 bytes of tag on a 40 ms beacon
    is a sensible forgery/overhead trade.
"""
from __future__ import annotations

import hmac
import os
import secrets
import struct
from dataclasses import dataclass, field
from hashlib import sha256

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MASTER_KEY_BYTES = 16
NODE_KEY_BYTES = 16
BEACON_TAG_BYTES = 8
GCM_TAG_BYTES = 16

MAGIC = b"RT"
SECURE_VERSION = 2

# magic(2) version(1) flags(1) node(6) boot_id(4) seq(4)
HEADER_FMT = "<2sBB6sII"
HEADER_LEN = struct.calcsize(HEADER_FMT)

INFO_REPORT = b"rti-mesh-report-v2"
INFO_BEACON = b"rti-mesh-beacon-v2"


class SecurityError(Exception):
    """Authentication failed, replay detected, or the frame was malformed."""


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def generate_master_key() -> bytes:
    return secrets.token_bytes(MASTER_KEY_BYTES)


def _node_bytes(node_id: str) -> bytes:
    return bytes.fromhex(node_id)


def derive_node_key(master: bytes, node_id: str, info: bytes = INFO_REPORT) -> bytes:
    """K_node = HKDF-SHA256(master, salt=node_mac, info=purpose).

    Separate `info` per purpose means the report key and the beacon key are
    independent: recovering one does not yield the other.
    """
    if len(master) < 16:
        raise SecurityError("master key must be at least 16 bytes")
    return HKDF(algorithm=hashes.SHA256(), length=NODE_KEY_BYTES,
                salt=_node_bytes(node_id), info=info).derive(master)


class KeyStore:
    """Master key plus a cache of derived per-node keys."""

    def __init__(self, master: bytes):
        if len(master) < MASTER_KEY_BYTES:
            raise SecurityError(f"master key must be >= {MASTER_KEY_BYTES} bytes")
        self._master = master
        self._report: dict[str, bytes] = {}
        self._beacon: dict[str, bytes] = {}

    def report_key(self, node_id: str) -> bytes:
        k = self._report.get(node_id)
        if k is None:
            k = self._report[node_id] = derive_node_key(self._master, node_id,
                                                        INFO_REPORT)
        return k

    def beacon_key(self, node_id: str) -> bytes:
        k = self._beacon.get(node_id)
        if k is None:
            k = self._beacon[node_id] = derive_node_key(self._master, node_id,
                                                        INFO_BEACON)
        return k

    @classmethod
    def from_hex(cls, hexkey: str) -> "KeyStore":
        return cls(bytes.fromhex(hexkey.strip()))

    @classmethod
    def from_file(cls, path) -> "KeyStore":
        from pathlib import Path
        raw = Path(path).read_text().strip()
        # Refuse a world-readable key file rather than quietly using it.
        mode = os.stat(path).st_mode & 0o077
        if mode:
            raise SecurityError(
                f"{path} is readable by others (mode {oct(mode)}); "
                f"run: chmod 600 {path}")
        return cls.from_hex(raw)


# --------------------------------------------------------------------------
# Replay protection
# --------------------------------------------------------------------------

@dataclass
class ReplayWindow:
    """Highest (boot_id, seq) accepted per node.

    Strictly increasing, not a sliding window: UDP can reorder, but accepting an
    out-of-order frame would also accept a replayed one, and for sensing a
    slightly stale report is worthless anyway. Rejecting reorders costs us
    almost nothing and removes a whole class of attack.
    """

    highest: dict[str, tuple[int, int]] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)

    def check(self, node_id: str, boot_id: int, seq: int) -> None:
        prev = self.highest.get(node_id)
        cur = (boot_id, seq)
        if prev is not None and cur <= prev:
            self.rejected[node_id] = self.rejected.get(node_id, 0) + 1
            raise SecurityError(
                f"replay from {node_id}: got boot={boot_id} seq={seq}, "
                f"already saw boot={prev[0]} seq={prev[1]}")
        self.highest[node_id] = cur

    @property
    def total_rejected(self) -> int:
        return sum(self.rejected.values())


# --------------------------------------------------------------------------
# Report framing: AES-128-GCM
# --------------------------------------------------------------------------

def _nonce(boot_id: int, seq: int) -> bytes:
    # 12 bytes: 4 zero || boot_id || seq. Unique per (key, boot, seq).
    return b"\x00\x00\x00\x00" + struct.pack("<II", boot_id, seq)


def seal_report(keys: KeyStore, node_id: str, boot_id: int, seq: int,
                plaintext: bytes, flags: int = 0) -> bytes:
    """Encrypt+authenticate a report. Header is cleartext but authenticated."""
    header = struct.pack(HEADER_FMT, MAGIC, SECURE_VERSION, flags,
                         _node_bytes(node_id), boot_id, seq)
    ct = AESGCM(keys.report_key(node_id)).encrypt(
        _nonce(boot_id, seq), plaintext, header)
    return header + ct


def open_report(keys: KeyStore, frame: bytes,
                replay: ReplayWindow | None = None) -> tuple[str, int, int, bytes]:
    """Verify and decrypt. Raises SecurityError on anything suspicious."""
    if len(frame) < HEADER_LEN + GCM_TAG_BYTES:
        raise SecurityError(f"frame too short: {len(frame)} bytes")

    header = frame[:HEADER_LEN]
    magic, version, flags, node_raw, boot_id, seq = struct.unpack(HEADER_FMT, header)
    if magic != MAGIC:
        raise SecurityError("bad magic")
    if version != SECURE_VERSION:
        raise SecurityError(f"unsupported secure version {version}")

    node_id = node_raw.hex()
    try:
        pt = AESGCM(keys.report_key(node_id)).decrypt(
            _nonce(boot_id, seq), frame[HEADER_LEN:], header)
    except InvalidTag as e:
        raise SecurityError(f"authentication failed for {node_id}") from e

    # Only after the tag verifies: an unauthenticated frame must never be able
    # to advance the replay counter, or an attacker could lock a node out.
    if replay is not None:
        replay.check(node_id, boot_id, seq)
    return node_id, boot_id, seq, pt


# --------------------------------------------------------------------------
# Beacon authentication: truncated HMAC-SHA256
# --------------------------------------------------------------------------

BEACON_FMT = "<2sBB6sII"
BEACON_HEADER_LEN = struct.calcsize(BEACON_FMT)
BEACON_LEN = BEACON_HEADER_LEN + BEACON_TAG_BYTES


def make_beacon(keys: KeyStore, node_id: str, boot_id: int, seq: int) -> bytes:
    header = struct.pack(BEACON_FMT, MAGIC, SECURE_VERSION, 0x01,
                         _node_bytes(node_id), boot_id, seq)
    tag = hmac.new(keys.beacon_key(node_id), header, sha256).digest()
    return header + tag[:BEACON_TAG_BYTES]


def verify_beacon(keys: KeyStore, frame: bytes,
                  replay: ReplayWindow | None = None) -> tuple[str, int, int]:
    if len(frame) != BEACON_LEN:
        raise SecurityError(f"beacon wrong length: {len(frame)} != {BEACON_LEN}")
    header, tag = frame[:BEACON_HEADER_LEN], frame[BEACON_HEADER_LEN:]
    magic, version, kind, node_raw, boot_id, seq = struct.unpack(BEACON_FMT, header)
    if magic != MAGIC or version != SECURE_VERSION or kind != 0x01:
        raise SecurityError("bad beacon header")

    node_id = node_raw.hex()
    expect = hmac.new(keys.beacon_key(node_id), header, sha256).digest()
    # Constant time: a timing oracle on tag comparison is a real forgery path.
    if not hmac.compare_digest(tag, expect[:BEACON_TAG_BYTES]):
        raise SecurityError(f"beacon authentication failed for {node_id}")

    if replay is not None:
        replay.check(node_id, boot_id, seq)
    return node_id, boot_id, seq


# --------------------------------------------------------------------------
# Defence in depth: crypto cannot catch a compromised but authentic node
# --------------------------------------------------------------------------

def check_plausibility(rssi_dbm: float, history_mean: float | None,
                       max_jump_db: float = 25.0) -> str | None:
    """Flag values a genuine link is unlikely to produce.

    A node whose key has been extracted can sign whatever it likes, and an
    attacker radiating into the band changes real measurements without touching
    a single packet. Neither is a cryptographic failure, so neither is caught by
    a cryptographic control. Returns a reason string, or None if plausible.
    """
    if not (-120.0 <= rssi_dbm <= -1.0):
        return f"non-physical RSSI {rssi_dbm:.1f} dBm"
    if history_mean is not None and abs(rssi_dbm - history_mean) > max_jump_db:
        return (f"implausible jump: {rssi_dbm:.1f} dBm vs "
                f"{history_mean:.1f} dBm mean")
    return None
