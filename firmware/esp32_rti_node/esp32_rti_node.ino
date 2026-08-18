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

// ------------------------------------------------------------------ state ---
typedef struct { uint8_t magic; uint8_t mac[6]; uint32_t seq; } beacon_t;
static const uint8_t BEACON_MAGIC = 0xA7;

struct PeerStat {
  uint8_t  mac[6];
  int32_t  rssi_sum;
  uint16_t samples;
  uint32_t last_ms;
  bool     used;
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
  memcpy(freeSlot->mac, mac, 6);
  freeSlot->rssi_sum = 0; freeSlot->samples = 0;
  freeSlot->last_ms = millis(); freeSlot->used = true;
  return freeSlot;
}

// Core 3.x hands us rx_ctrl, which carries the RSSI. This callback IS the
// measurement - everything else in this sketch is transport.
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len < (int)sizeof(beacon_t)) return;
  const beacon_t *b = (const beacon_t *)data;
  if (b->magic != BEACON_MAGIC) return;

  PeerStat *p = findOrAdd(info->src_addr);
  if (!p) return;
  p->rssi_sum += info->rx_ctrl->rssi;
  p->samples++;
  p->last_ms = millis();
}

static void sendBeacon() {
  beacon_t b;
  b.magic = BEACON_MAGIC;
  WiFi.macAddress(b.mac);
  b.seq = beaconSeq++;
  esp_now_send(BROADCAST, (const uint8_t *)&b, sizeof(b));
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
  udp.beginPacket(SERVER_IP, SERVER_PORT);
  udp.write((const uint8_t *)buf, n);
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
