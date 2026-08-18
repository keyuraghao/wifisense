// Types and constants shared across the sketch.
//
// This lives in a header rather than the .ino for a specific reason: the
// Arduino builder auto-generates function prototypes and inserts them directly
// after the include block. Any struct declared later in the .ino is therefore
// unknown to those prototypes, and every function taking a PeerStat* fails to
// compile with "does not name a type". Declaring it in an included header puts
// it ahead of the generated prototypes.
#pragma once
#include <stdint.h>
#include <stddef.h>

// Wire format is defined by wifisense/mesh/crypto.py. Header bytes are laid out
// by hand rather than with a packed struct: struct padding differs between the
// ESP32 toolchain and the server, and a silent layout mismatch would show up as
// "authentication failed" with no clue why.
//
//   header (18 bytes, little endian):
//     magic[2]="RT" | version=2 | flags | node_mac[6] | boot_id[4] | seq[4]
//   report : header || AES-128-GCM(ciphertext) || tag[16]   AAD = header
//   beacon : header(flags=1) || HMAC-SHA256(header)[0..7]
//
// Nonce is 4 zero bytes || boot_id || seq. Unique per key because boot_id is
// persisted in NVS and incremented every boot. GCM nonce reuse is catastrophic,
// so boot_id must never go backwards.
static const uint8_t  SECURE_VERSION = 2;
static const size_t   HEADER_LEN = 18;
static const size_t   GCM_TAG_LEN = 16;
static const size_t   BEACON_TAG_LEN = 8;
static const size_t   NODE_KEY_LEN = 16;
static const size_t   BEACON_LEN = HEADER_LEN + BEACON_TAG_LEN;
static const uint8_t  MAX_PEERS = 32;

static const char *INFO_REPORT = "rti-mesh-report-v2";
static const char *INFO_BEACON = "rti-mesh-beacon-v2";

struct PeerStat {
  uint8_t  mac[6];
  int32_t  rssi_sum;
  uint16_t samples;
  uint32_t last_ms;
  bool     used;
  uint8_t  beaconKey[NODE_KEY_LEN];  // derived once, on first sight
  bool     keyReady;
  uint32_t lastBoot;                 // per-peer replay state
  uint32_t lastSeq;
  bool     seenOnce;
  uint32_t rejected;                 // failed auth or replay, for diagnostics
};
