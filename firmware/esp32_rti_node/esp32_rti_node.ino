/*
 * RTI mesh node - ESP32
 *
 * Measures RSSI to every peer over ESP-NOW, reports the table to the server
 * over UDP. Every node runs this SAME binary: there is no per-node build, no
 * node ID to assign, no peer list to maintain. Adding a node to the mesh is
 * flashing this sketch and giving the server its position.
 *
 * Why ESP-NOW for the measurement and WiFi/UDP for the reporting:
 *   - ESP-NOW is connectionless and needs no AP, so N nodes yield all
 *     N(N-1)/2 links with no association storm and no router involvement.
 *   - It exposes per-frame RSSI in the receive callback, which is the entire
 *     measurement.
 *   - But it has no route to the server, so the node also joins the normal
 *     WiFi network and sends its table over UDP. ESP-NOW and station mode
 *     coexist only on the SAME channel, which is why the sketch locks itself
 *     to the AP's channel after associating.
 *
 * REQUIRES arduino-esp32 core 3.x (ESP-IDF 5.x). The RSSI-bearing receive
 * callback signature (esp_now_recv_info_t) does not exist in core 2.x; on 2.x
 * you would have to run promiscuous mode and correlate frames by hand.
 *
 * NOT YET TESTED ON HARDWARE. The protocol side is verified against the Python
 * server by scripts/rti_fake_nodes.py, but this sketch has never been flashed.
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <mbedtls/gcm.h>
#include <mbedtls/hkdf.h>
#include <mbedtls/md.h>

#include "mesh_key.h"   // generated: scripts/gen_mesh_key.py

// ---------------------------------------------------------------- config ---
static const char *WIFI_SSID = "YOUR_SSID";
static const char *WIFI_PASS = "YOUR_PASSWORD";
static const char *SERVER_IP = "192.168.1.100";   // machine running the dashboard
static const uint16_t SERVER_PORT = 9999;

static const uint32_t BEACON_INTERVAL_MS = 40;    // ~25 beacons/s per node
static const uint32_t REPORT_INTERVAL_MS = 200;   // 5 reports/s to the server
static const uint8_t  MAX_PEERS = 32;
static const uint32_t PEER_STALE_MS = 3000;       // forget a silent peer

#define LED_PIN 2

// ------------------------------------------------------------- security ---
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

static const char *INFO_REPORT = "rti-mesh-report-v2";
static const char *INFO_BEACON = "rti-mesh-beacon-v2";

static uint8_t selfReportKey[NODE_KEY_LEN];
static uint8_t selfBeaconKey[NODE_KEY_LEN];
static uint32_t bootId = 0;
static Preferences prefs;

// K_node = HKDF-SHA256(ikm=master, salt=node_mac, info=purpose)
static bool deriveKey(const uint8_t *mac, const char *info, uint8_t *out) {
  const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  return mbedtls_hkdf(md, mac, 6, MESH_MASTER_KEY, MESH_MASTER_KEY_LEN,
                      (const uint8_t *)info, strlen(info),
                      out, NODE_KEY_LEN) == 0;
}

static void putU32(uint8_t *p, uint32_t v) {
  p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF;
  p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}

static void buildHeader(uint8_t *hdr, const uint8_t *mac, uint8_t flags,
                        uint32_t boot, uint32_t seq) {
  hdr[0] = 'R'; hdr[1] = 'T'; hdr[2] = SECURE_VERSION; hdr[3] = flags;
  memcpy(hdr + 4, mac, 6);
  putU32(hdr + 10, boot);
  putU32(hdr + 14, seq);
}

static void buildNonce(uint8_t *nonce, uint32_t boot, uint32_t seq) {
  memset(nonce, 0, 4);
  putU32(nonce + 4, boot);
  putU32(nonce + 8, seq);
}

// ------------------------------------------------------------------ state ---
// 18-byte header + 8-byte tag. Broadcast ESP-NOW cannot be encrypted, and the
// beacon carries nothing secret anyway - authenticity is the whole requirement.
static const size_t BEACON_LEN = HEADER_LEN + BEACON_TAG_LEN;

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

static PeerStat peers[MAX_PEERS];
static WiFiUDP udp;
static uint32_t beaconSeq = 0, reportSeq = 0;
static uint32_t lastBeacon = 0, lastReport = 0;
static char selfId[13];
static const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static void macToHex(const uint8_t *mac, char *out) {
  static const char *H = "0123456789abcdef";
  for (int i = 0; i < 6; i++) { out[i*2] = H[mac[i] >> 4]; out[i*2+1] = H[mac[i] & 0xF]; }
  out[12] = '\0';
}

static PeerStat *findOrAdd(const uint8_t *mac) {
  PeerStat *freeSlot = nullptr;
  for (int i = 0; i < MAX_PEERS; i++) {
    if (peers[i].used && memcmp(peers[i].mac, mac, 6) == 0) return &peers[i];
    if (!peers[i].used && !freeSlot) freeSlot = &peers[i];
  }
  if (!freeSlot) return nullptr;            // mesh larger than MAX_PEERS
  memset(freeSlot, 0, sizeof(*freeSlot));
  memcpy(freeSlot->mac, mac, 6);
  freeSlot->last_ms = millis();
  freeSlot->used = true;
  // Every node holds the master key, so any node can derive any peer's beacon
  // key. That is what makes enrolment zero-config: no key exchange, no PKI.
  freeSlot->keyReady = deriveKey(mac, INFO_BEACON, freeSlot->beaconKey);
  return freeSlot;
}

// Core 3.x hands us rx_ctrl, which carries the RSSI. This callback IS the
// measurement - everything else in this sketch is transport.
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != (int)BEACON_LEN) return;
  if (data[0] != 'R' || data[1] != 'T' || data[2] != SECURE_VERSION ||
      data[3] != 0x01) return;

  // The claimed MAC lives inside the authenticated header. Verify against that,
  // not against info->src_addr, which an attacker controls freely.
  const uint8_t *claimed = data + 4;

  PeerStat *p = findOrAdd(claimed);
  if (!p || !p->keyReady) return;

  uint8_t expect[32];
  const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (mbedtls_md_hmac(md, p->beaconKey, NODE_KEY_LEN, data, HEADER_LEN, expect) != 0)
    return;

  // Constant-time compare: a timing oracle here is a forgery path.
  uint8_t diff = 0;
  for (size_t i = 0; i < BEACON_TAG_LEN; i++) diff |= expect[i] ^ data[HEADER_LEN + i];
  if (diff != 0) { p->rejected++; return; }

  uint32_t boot, seq;
  memcpy(&boot, data + 10, 4);
  memcpy(&seq, data + 14, 4);

  // Strictly increasing (boot_id, seq) kills replay of captured beacons.
  if (p->seenOnce && (boot < p->lastBoot ||
                      (boot == p->lastBoot && seq <= p->lastSeq))) {
    p->rejected++;
    return;
  }
  p->lastBoot = boot; p->lastSeq = seq; p->seenOnce = true;

  p->rssi_sum += info->rx_ctrl->rssi;
  p->samples++;
  p->last_ms = millis();
}

static void sendBeacon() {
  uint8_t mac[6]; WiFi.macAddress(mac);
  uint8_t frame[HEADER_LEN + BEACON_TAG_LEN];
  buildHeader(frame, mac, 0x01, bootId, beaconSeq++);

  uint8_t tag[32];
  const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (mbedtls_md_hmac(md, selfBeaconKey, NODE_KEY_LEN, frame, HEADER_LEN, tag) != 0)
    return;
  memcpy(frame + HEADER_LEN, tag, BEACON_TAG_LEN);

  esp_now_send(BROADCAST, frame, sizeof(frame));
}

// Build and send the JSON report. Hand-rolled rather than using ArduinoJson so
// the sketch has no library dependency; the format is fixed and tiny.
static void sendReport() {
  uint32_t now = millis();
  char buf[1400];
  int n = snprintf(buf, sizeof(buf),
                   "{\"v\":1,\"id\":\"%s\",\"seq\":%lu,\"up\":%lu,\"m\":[",
                   selfId, (unsigned long)reportSeq, (unsigned long)now);

  bool first = true;
  for (int i = 0; i < MAX_PEERS; i++) {
    PeerStat *p = &peers[i];
    if (!p->used) continue;
    if (now - p->last_ms > PEER_STALE_MS) { p->used = false; continue; }
    if (p->samples == 0) continue;

    char hex[13];
    macToHex(p->mac, hex);
    float mean = (float)p->rssi_sum / (float)p->samples;
    int written = snprintf(buf + n, sizeof(buf) - n, "%s[\"%s\",%.1f,%u]",
                           first ? "" : ",", hex, mean, p->samples);
    // Never emit a truncated datagram: the server rejects malformed JSON, so a
    // partial report would cost the whole interval rather than a few peers.
    if (written < 0 || n + written >= (int)sizeof(buf) - 4) break;
    n += written;
    first = false;

    p->rssi_sum = 0; p->samples = 0;   // reset the accumulator each report
  }

  n += snprintf(buf + n, sizeof(buf) - n, "]}");

  // Seal: header in clear (the server needs the id to pick a key) but bound in
  // as AAD, so it cannot be altered without breaking the tag.
  uint8_t mac[6]; WiFi.macAddress(mac);
  static uint8_t frame[HEADER_LEN + sizeof(buf) + GCM_TAG_LEN];
  buildHeader(frame, mac, 0x00, bootId, reportSeq);

  uint8_t nonce[12];
  buildNonce(nonce, bootId, reportSeq);

  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);
  if (mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, selfReportKey,
                         NODE_KEY_LEN * 8) != 0) {
    mbedtls_gcm_free(&gcm); return;
  }
  int rc = mbedtls_gcm_crypt_and_tag(
      &gcm, MBEDTLS_GCM_ENCRYPT, n, nonce, sizeof(nonce),
      frame, HEADER_LEN,                       // AAD = header
      (const uint8_t *)buf, frame + HEADER_LEN,
      GCM_TAG_LEN, frame + HEADER_LEN + n);    // tag trails the ciphertext
  mbedtls_gcm_free(&gcm);
  if (rc != 0) return;

  udp.beginPacket(SERVER_IP, SERVER_PORT);
  udp.write(frame, HEADER_LEN + n + GCM_TAG_LEN);
  udp.endPacket();
  reportSeq++;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  memset(peers, 0, sizeof(peers));

  // AP_STA so ESP-NOW keeps working while the station interface is associated.
  WiFi.mode(WIFI_AP_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("connecting");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.printf("\nip %s\n", WiFi.localIP().toString().c_str());

  // ESP-NOW must sit on the AP's channel or peers on other channels go deaf.
  uint8_t primary; wifi_second_chan_t second;
  esp_wifi_get_channel(&primary, &second);
  esp_wifi_set_channel(primary, WIFI_SECOND_CHAN_NONE);
  Serial.printf("channel %u\n", primary);

  uint8_t mac[6]; WiFi.macAddress(mac); macToHex(mac, selfId);
  Serial.printf("node id %s\n", selfId);

  // boot_id is the high half of the GCM nonce. It MUST increase every boot or
  // nonces repeat across reboots, which would be a total loss of confidentiality
  // and authenticity. NVS survives power loss and reflashing of the app
  // partition, which is exactly the durability this needs.
  prefs.begin("rti", false);
  bootId = prefs.getUInt("boot", 0) + 1;
  prefs.putUInt("boot", bootId);
  prefs.end();
  Serial.printf("boot id %lu\n", (unsigned long)bootId);

  if (!deriveKey(mac, INFO_REPORT, selfReportKey) ||
      !deriveKey(mac, INFO_BEACON, selfBeaconKey)) {
    Serial.println("key derivation failed"); ESP.restart();
  }
  Serial.println("security: AES-128-GCM reports, HMAC-SHA256 beacons");

  if (esp_now_init() != ESP_OK) { Serial.println("esp_now_init failed"); ESP.restart(); }
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t bc = {};
  memcpy(bc.peer_addr, BROADCAST, 6);
  bc.channel = primary;
  bc.encrypt = false;
  esp_now_add_peer(&bc);

  udp.begin(SERVER_PORT + 1);
  Serial.printf("reporting to %s:%u\n", SERVER_IP, SERVER_PORT);
}

void loop() {
  uint32_t now = millis();

  // Jitter the beacon so N nodes do not lock into a repeating collision pattern.
  if (now - lastBeacon >= BEACON_INTERVAL_MS) {
    lastBeacon = now + (esp_random() % 8);
    sendBeacon();
  }

  if (now - lastReport >= REPORT_INTERVAL_MS) {
    lastReport = now;
    sendReport();
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
  }

  // Rejoin if the AP drops; ESP-NOW keeps measuring throughout, so the node
  // only stops contributing for as long as it cannot reach the server.
  if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();

  delay(1);
}
