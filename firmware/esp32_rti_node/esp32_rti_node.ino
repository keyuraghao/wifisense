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
#include <esp_log.h>
#include <Preferences.h>
#include <mbedtls/gcm.h>
#include <mbedtls/hkdf.h>
#include <mbedtls/md.h>

#include "rti_types.h"  // types first: see the note in that file
#include "config.h"     // copy from config.h.example, or setup_firmware.py
#include "mesh_key.h"   // generated: scripts/gen_mesh_key.py

// ---------------------------------------------------------------- config ---
// Values come from config.h so the sketch itself carries no secrets.
static const char *WIFI_SSID = CFG_WIFI_SSID;
static const char *WIFI_PASS = CFG_WIFI_PASS;
static const char *SERVER_IP = CFG_SERVER_IP;
static const uint16_t SERVER_PORT = CFG_SERVER_PORT;

static const uint32_t BEACON_INTERVAL_MS = CFG_BEACON_INTERVAL_MS;
static const uint32_t REPORT_INTERVAL_MS = CFG_REPORT_INTERVAL_MS;
static const uint32_t PEER_STALE_MS = CFG_PEER_STALE_MS;

#define LED_PIN CFG_LED_PIN

// ------------------------------------------------------------- security ---
// Constants and the PeerStat type live in rti_types.h.

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
  if (LED_PIN >= 0) pinMode(LED_PIN, OUTPUT);
  esp_log_level_set("wifi", ESP_LOG_WARN);
  esp_log_level_set("ESPNOW", ESP_LOG_WARN);
  memset(peers, 0, sizeof(peers));

  // AP_STA so ESP-NOW keeps working while the station interface is associated.
  WiFi.mode(WIFI_AP_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("connecting to %s", WIFI_SSID);
  // Bounded, not infinite. A node that cannot reach the AP should still boot
  // and still beacon: its peers keep measuring the links to it, and loop()
  // retries the association. Blocking here would take the node out of the mesh
  // entirely for a fault that only affects reporting.
  uint32_t deadline = millis() + 20000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(300); Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nip %s  rssi %d dBm\n", WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
  } else {
    Serial.println("\nWiFi TIMEOUT - will keep retrying. Until it associates "
                   "this node cannot mesh: scanning hops channels, and ESP-NOW "
                   "needs a shared channel.");
  }

  // ESP-NOW must sit on the AP's channel or peers on other channels go deaf.
  uint8_t primary; wifi_second_chan_t second;
  esp_wifi_get_channel(&primary, &second);
  if (WiFi.status() != WL_CONNECTED) {
    // Unassociated: park on a known channel so nodes can still hear each other.
    primary = 1;
    esp_wifi_set_channel(primary, WIFI_SECOND_CHAN_NONE);
  }
  // Associated: the AP owns the channel. Do NOT override it - and this is also
  // what silently puts every node on the same channel, since they all join the
  // same AP. That shared channel is a hard requirement for ESP-NOW.
  Serial.printf("channel %u (%s)\n", primary,
                WiFi.status() == WL_CONNECTED ? "from AP" : "fallback, not associated");

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
  // Fingerprint, not the key: enough to confirm every node derived the same
  // material, useless to anyone reading the serial log.
  uint8_t fp[32];
  const mbedtls_md_info_t *mdi = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md(mdi, selfReportKey, NODE_KEY_LEN, fp);
  Serial.printf("security: AES-128-GCM reports, HMAC-SHA256 beacons\n");
  Serial.printf("key fingerprint %02x%02x%02x%02x (must match on every node)\n",
                fp[0], fp[1], fp[2], fp[3]);
  Serial.printf("reporting to %s:%u\n", SERVER_IP, SERVER_PORT);
  Serial.println("--- running ---");

  if (esp_now_init() != ESP_OK) { Serial.println("esp_now_init failed"); ESP.restart(); }
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t bc = {};
  memcpy(bc.peer_addr, BROADCAST, 6);
  // channel 0 means "whatever channel the interface is currently on". Pinning a
  // number here breaks the moment the STA associates and the radio follows the
  // AP: every send then fails with "Peer channel is not equal to the home
  // channel". Channel 0 makes ESP-NOW track the interface instead.
  bc.channel = 0;
  bc.ifidx = WIFI_IF_STA;
  bc.encrypt = false;
  esp_now_add_peer(&bc);

  udp.begin(SERVER_PORT + 1);
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

    static uint32_t lastLog = 0;
    if (now - lastLog >= 5000) {
      lastLog = now;
      int live = 0, rej = 0;
      for (int i = 0; i < MAX_PEERS; i++)
        if (peers[i].used) { live++; rej += peers[i].rejected; }
      uint8_t ch; wifi_second_chan_t sec;
      esp_wifi_get_channel(&ch, &sec);
      Serial.printf("[%6lus] peers %d  reports %lu  rejected %d  ch %u  wifi %s\n",
                    (unsigned long)(now / 1000), live, (unsigned long)reportSeq,
                    rej, ch, WiFi.status() == WL_CONNECTED ? "up" : "DOWN");
    }
    if (LED_PIN >= 0) digitalWrite(LED_PIN, !digitalRead(LED_PIN));
  }

  // Rejoin if the AP drops. Note what actually happens while unassociated: the
  // STA scans, which hops channels, and ESP-NOW only works between radios on
  // the SAME channel. So a node that cannot reach the AP does not merely stop
  // reporting - it drops out of the mesh until it re-associates. Association is
  // what pins every node to one channel; there is no meshing without it.
  // Rate limited: calling reconnect() every loop iteration floods the WiFi task
  // with "sta is connecting, return error" and makes association slower, not
  // faster.
  static uint32_t lastReconnect = 0;
  if (WiFi.status() != WL_CONNECTED && now - lastReconnect >= 5000) {
    lastReconnect = now;
    WiFi.reconnect();
  }

  delay(1);
}
