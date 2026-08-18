"""Wire protocol between ESP32 nodes and the reconstruction server.

One UDP datagram per node per reporting interval. UDP, not TCP, on purpose: a
lost report is worth less than a delayed one -- the next one is 200 ms away and
carries fresher data, so retransmission would only ever hand us stale
measurements. Every field the server trusts for timing is stamped on arrival,
not by the node, because ESP32 clocks drift and are not synchronised.

    {
      "v":   1,                         protocol version
      "id":  "aabbccddeeff",            reporting node, MAC without separators
      "seq": 1234,                      monotonic per node; gaps = packet loss
      "up":  45231,                     node uptime in ms (reboot detection)
      "m":   [["aabbcc112233", -55.2, 12], ...]   peer, mean RSSI dBm, samples
    }

Kept as JSON deliberately. At 12 nodes and 5 Hz this is a few kB/s -- nothing --
and being able to read the traffic with tcpdump while debugging a mesh is worth
far more than the bytes a binary format would save.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

PROTOCOL_VERSION = 1
DEFAULT_PORT = 9999
MAX_DATAGRAM = 2048


class ProtocolError(ValueError):
    pass


def normalise_id(raw: str) -> str:
    """Accept aa:bb:cc:dd:ee:ff, AA-BB-..., or bare hex -> canonical lowercase hex."""
    s = "".join(c for c in str(raw).lower() if c in "0123456789abcdef")
    if len(s) != 12:
        raise ProtocolError(f"bad node id {raw!r}: expected 12 hex digits, got {len(s)}")
    return s


def pretty_id(node_id: str) -> str:
    """Canonical hex -> colon form, for humans."""
    return ":".join(node_id[i:i + 2] for i in range(0, 12, 2))


@dataclass
class Measurement:
    peer: str
    rssi_dbm: float
    samples: int = 1


@dataclass
class Report:
    node: str
    seq: int
    uptime_ms: int
    measurements: list[Measurement] = field(default_factory=list)
    version: int = PROTOCOL_VERSION

    def encode(self) -> bytes:
        return json.dumps({
            "v": self.version, "id": self.node, "seq": self.seq,
            "up": self.uptime_ms,
            "m": [[m.peer, round(m.rssi_dbm, 1), m.samples] for m in self.measurements],
        }, separators=(",", ":")).encode()


def decode(payload: bytes) -> Report:
    """Parse a datagram. Raises ProtocolError on anything malformed.

    Strict by design: a mesh is exactly the setting where a half-broken node
    starts emitting garbage, and silently coercing it produces a reconstruction
    that is wrong rather than one that is visibly degraded.
    """
    if len(payload) > MAX_DATAGRAM:
        raise ProtocolError(f"datagram too large: {len(payload)} bytes")
    try:
        d = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"undecodable datagram: {e}") from e
    if not isinstance(d, dict):
        raise ProtocolError("payload is not an object")

    version = int(d.get("v", 0))
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")

    node = normalise_id(d["id"]) if "id" in d else None
    if node is None:
        raise ProtocolError("missing node id")

    out = []
    for entry in d.get("m", []):
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise ProtocolError(f"malformed measurement {entry!r}")
        peer = normalise_id(entry[0])
        if peer == node:
            continue                      # a node hearing itself is meaningless
        rssi = float(entry[1])
        if not (-120.0 <= rssi <= -1.0):  # same physicality guard as the RSSI capture
            continue
        out.append(Measurement(peer=peer, rssi_dbm=rssi,
                               samples=int(entry[2]) if len(entry) > 2 else 1))

    return Report(node=node, seq=int(d.get("seq", 0)),
                  uptime_ms=int(d.get("up", 0)), measurements=out, version=version)


def link_key(a: str, b: str) -> tuple[str, str]:
    """Undirected link identity. RTI does not care who transmitted."""
    return (a, b) if a <= b else (b, a)
