// Phase 1: EcoWorthy BW0F BLE logger for ESP32-C3.
//
// Connects to WiFi, auto-discovers/connects to the BW0F over BLE, logs every
// advertisement/service/characteristic/notification it sees, and fans that
// log out over IP (HTML live view + plain-text stream) plus OTA for phase 2.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <Update.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEClient.h>
#include <BLEUtils.h>
#include <vector>
#include <string>
#include <set>
#include <map>
#include "secrets.h"

// Bump manually whenever a new build is published, so /status makes it
// easy to confirm a push actually landed.
#define FIRMWARE_VERSION "2026-07-24.3"

// ===================== Log fan-out =====================

static const size_t LOG_RING_SIZE = 400;
static const size_t MAX_LOG_CLIENTS = 4;

struct LogClient {
  WiFiClient client;
  bool sse = false;
  bool in_use = false;
};

static String g_ring[LOG_RING_SIZE];
static size_t g_ring_head = 0;
static size_t g_ring_count = 0;
static LogClient g_log_clients[MAX_LOG_CLIENTS];
static SemaphoreHandle_t g_log_mutex;

static String sse_escape(const String &line) {
  // Single log lines never contain \n, but guard anyway since SSE frames
  // break on blank lines.
  String out = line;
  out.replace("\r", " ");
  out.replace("\n", " ");
  return out;
}

static volatile uint32_t g_last_activity_ms = 0;

static void log_line(const String &msg) {
  g_last_activity_ms = millis();
  String stamped = "[" + String(millis() / 1000.0, 3) + "] " + msg;
  Serial.println(stamped);

  // Copy out client handles under the lock, then write outside it - a slow
  // or half-dead client's write shouldn't stall everyone else waiting on
  // this mutex (HTTP handler adding new clients, /status, etc.). Actually
  // pruning dead clients stays in loop(), which already does it every
  // iteration; a dead client is just skipped here for this one line.
  WiFiClient targets[MAX_LOG_CLIENTS];
  bool targetSse[MAX_LOG_CLIENTS];
  bool targetValid[MAX_LOG_CLIENTS];

  xSemaphoreTake(g_log_mutex, portMAX_DELAY);
  g_ring[g_ring_head] = stamped;
  g_ring_head = (g_ring_head + 1) % LOG_RING_SIZE;
  if (g_ring_count < LOG_RING_SIZE) g_ring_count++;

  for (size_t i = 0; i < MAX_LOG_CLIENTS; i++) {
    LogClient &lc = g_log_clients[i];
    targetValid[i] = lc.in_use;
    if (lc.in_use) {
      targets[i] = lc.client;
      targetSse[i] = lc.sse;
    }
  }
  xSemaphoreGive(g_log_mutex);

  for (size_t i = 0; i < MAX_LOG_CLIENTS; i++) {
    if (!targetValid[i] || !targets[i].connected()) continue;
    if (targetSse[i]) {
      targets[i].print("data: ");
      targets[i].print(sse_escape(stamped));
      targets[i].print("\n\n");
    } else {
      targets[i].print(stamped);
      targets[i].print("\r\n");
    }
  }
}

static String bytes_to_hex(const uint8_t *data, size_t len) {
  static const char *hex = "0123456789abcdef";
  String out;
  out.reserve(len * 2);
  for (size_t i = 0; i < len; i++) {
    out += hex[(data[i] >> 4) & 0x0F];
    out += hex[data[i] & 0x0F];
  }
  return out;
}

static String bytes_to_ascii(const uint8_t *data, size_t len) {
  String out;
  out.reserve(len);
  for (size_t i = 0; i < len; i++) {
    char c = static_cast<char>(data[i]);
    out += (c >= 32 && c < 127) ? c : '.';
  }
  return out;
}

// ===================== BLE scan bookkeeping =====================

static std::string lower_copy(const std::string &input) {
  std::string out = input;
  for (char &c : out) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
  }
  return out;
}

static std::vector<BLEAdvertisedDevice *> g_discovered;
static std::set<std::string> g_seen_macs;

class ScanCallbacks : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice advertisedDevice) override {
    const std::string mac = lower_copy(advertisedDevice.getAddress().toString());
    if (g_seen_macs.find(mac) != g_seen_macs.end()) return;
    g_seen_macs.insert(mac);
    g_discovered.push_back(new BLEAdvertisedDevice(advertisedDevice));

    String line = "[SCAN] addr=" + String(advertisedDevice.getAddress().toString().c_str()) +
                  " rssi=" + String(advertisedDevice.getRSSI());
    if (advertisedDevice.haveName()) {
      line += " name=\"" + String(advertisedDevice.getName().c_str()) + "\"";
    }
    if (advertisedDevice.haveServiceUUID()) {
      line += " service=" + String(advertisedDevice.getServiceUUID().toString().c_str());
    }
    if (advertisedDevice.haveManufacturerData()) {
      std::string mfg = advertisedDevice.getManufacturerData();
      line += " mfg=" + bytes_to_hex(reinterpret_cast<const uint8_t *>(mfg.data()), mfg.size());
    }
    log_line(line);
  }
};

#ifdef TARGET_MAC
static bool is_target_mac(BLEAdvertisedDevice &dev) {
  return lower_copy(dev.getAddress().toString()) == lower_copy(std::string(TARGET_MAC));
}
#endif

// Fallback for when TARGET_MAC isn't set. The real device advertises as
// "ECO-WORTHY 0F_16C1" - note the hyphen, which an earlier version of this
// check didn't handle (only "ecoworthy"/"eco worthy" - matched nothing on
// the real hardware, silently fell through to RSSI-fallback the whole time).
static bool is_name_hint_match(BLEAdvertisedDevice &dev) {
  if (!dev.haveName()) return false;
  const std::string name = lower_copy(dev.getName());
  return name.find("ecoworthy") != std::string::npos ||
         name.find("eco-worthy") != std::string::npos ||
         name.find("eco worthy") != std::string::npos ||
         name.find("bw0f") != std::string::npos ||
         name.find("jbd") != std::string::npos ||
         name.find("bms") != std::string::npos;
}

// Only returns a device we actually want to connect to - no RSSI-fallback
// to "whatever's strongest," since that just wastes a connection+GATT
// discovery cycle (and BLE radio time that WiFi needs) on someone's phone
// or watch. Returns nullptr if the target isn't in this scan's results.
static BLEAdvertisedDevice *pick_target() {
#ifdef TARGET_MAC
  for (BLEAdvertisedDevice *dev : g_discovered) {
    if (is_target_mac(*dev)) return dev;
  }
  return nullptr;
#else
  BLEAdvertisedDevice *best = nullptr;
  for (BLEAdvertisedDevice *dev : g_discovered) {
    if (is_name_hint_match(*dev)) {
      if (best == nullptr || dev->getRSSI() > best->getRSSI()) best = dev;
    }
  }
  return best;
#endif
}

// ===================== BLE connect / subscribe / log =====================

static bool g_ble_connected = false;
static BLEClient *g_ble_client = nullptr;
static volatile bool g_force_rescan = false;
// ESP32-C3 has a single 2.4GHz radio shared between WiFi and BLE. Heavy BLE
// scan/connect activity measurably starves WiFi (seen as WiFi.RSSI()
// reporting garbage and dropped packets even sitting right next to the AP).
// Set while an OTA transfer is in progress so it gets clean airtime instead
// of competing with BLE for the radio.
static volatile bool g_ble_paused = false;

static void pause_ble_for_update() {
  g_ble_paused = true;
  BLEDevice::getScan()->stop();
  if (g_ble_client != nullptr && g_ble_client->isConnected()) {
    g_ble_client->disconnect();
  }
  log_line("[BLE] paused for firmware update");
}

static void resume_ble_after_update() {
  g_ble_paused = false;
  log_line("[BLE] resumed after firmware update");
}

static void on_ble_notify(BLERemoteCharacteristic *ch, uint8_t *data, size_t length, bool isNotify) {
  String line = "[" + String(isNotify ? "NOTIFY" : "INDICATE") + "] char=" +
                String(ch->getUUID().toString().c_str()) +
                " len=" + String(static_cast<int>(length)) +
                " hex=" + bytes_to_hex(data, length) +
                " ascii=\"" + bytes_to_ascii(data, length) + "\"";
  log_line(line);
}

class ClientCallbacks : public BLEClientCallbacks {
  void onConnect(BLEClient *client) override {
    log_line("[BLE] connected");
  }
  void onDisconnect(BLEClient *client) override {
    log_line("[BLE] disconnected");
    g_ble_connected = false;
  }
};

// Connects to the target device, logs every service/characteristic/read,
// and subscribes to every notify/indicate characteristic. Deliberately
// does NOT disconnect afterward - stays connected indefinitely so
// on_ble_notify() keeps streaming for as long as the peripheral sends
// data, and so the radio sits idle (cheap) instead of actively scanning
// (expensive, competes with WiFi) once it's found what it's looking for.
static void explore_and_subscribe(BLEAdvertisedDevice *target) {
  if (g_ble_client == nullptr) {
    g_ble_client = BLEDevice::createClient();
    g_ble_client->setClientCallbacks(new ClientCallbacks());
  }

  String addr = String(target->getAddress().toString().c_str());
  log_line("[BLE] connecting to " + addr);

  if (!g_ble_client->connect(target)) {
    log_line("[BLE] connect failed: " + addr);
    return;
  }
  g_ble_connected = true;

  std::map<std::string, BLERemoteService *> *services = g_ble_client->getServices();

  if (services == nullptr || services->empty()) {
    log_line("[BLE] no GATT services discovered on " + addr);
    if (g_ble_client->isConnected()) g_ble_client->disconnect();
    g_ble_connected = false;
    return;
  }

  log_line("[BLE] " + addr + " has " + String(static_cast<int>(services->size())) + " service(s)");

  for (const auto &service_entry : *services) {
    if (g_force_rescan || g_ble_paused) break;
    BLERemoteService *svc = service_entry.second;
    log_line("[SERVICE] " + addr + " " + String(service_entry.first.c_str()));

    std::map<std::string, BLERemoteCharacteristic *> *chars = svc->getCharacteristics();
    if (chars == nullptr || chars->empty()) continue;

    for (const auto &char_entry : *chars) {
      BLERemoteCharacteristic *ch = char_entry.second;

      String flags;
      if (ch->canRead()) flags += "read,";
      if (ch->canNotify()) flags += "notify,";
      if (ch->canIndicate()) flags += "indicate,";
      if (ch->canWrite()) flags += "write,";
      if (ch->canWriteNoResponse()) flags += "write_no_response,";

      log_line("[CHARACTERISTIC] " + addr + " " + String(char_entry.first.c_str()) + " flags=" + flags);

      if (ch->canRead()) {
        std::string value = ch->readValue();
        if (!value.empty()) {
          log_line("[READ] " + addr + " char=" + String(ch->getUUID().toString().c_str()) +
                    " len=" + String(static_cast<int>(value.size())) +
                    " hex=" + bytes_to_hex(reinterpret_cast<const uint8_t *>(value.data()), value.size()) +
                    " ascii=\"" + bytes_to_ascii(reinterpret_cast<const uint8_t *>(value.data()), value.size()) + "\"");
        }
      }

      if (ch->canNotify() || ch->canIndicate()) {
        std::string uuidLower = lower_copy(ch->getUUID().toString());
        if (uuidLower.find("00002a05") != std::string::npos) {
          // Standard "Service Changed" characteristic (Generic Attribute
          // service, GATT-cache-invalidation housekeeping, not
          // telemetry). Confirmed live: registerForNotify() on this
          // specific indicate-only characteristic hangs indefinitely -
          // the CCCD write's response never arrives, and the BLE library
          // has no internal timeout on that wait. Costs nothing to skip.
          log_line("[SKIP] " + addr + " char=" + String(ch->getUUID().toString().c_str()) +
                   " (known to hang registerForNotify(), not telemetry)");
        } else {
          ch->registerForNotify(on_ble_notify);
          log_line("[SUBSCRIBE] " + addr + " char=" + String(ch->getUUID().toString().c_str()));
        }
      }
    }
  }

  log_line("[BLE] subscribed to " + addr + ", staying connected and streaming");
}

// Scans for the target device, connects and subscribes once, then just
// idles while already connected (on_ble_notify does the actual streaming
// from a different callback context) - only rescans if actually
// disconnected, paused, or a rescan is explicitly requested. This keeps
// the radio doing active BLE scanning only when it actually needs to,
// leaving WiFi far more airtime than continuously exploring every nearby
// device would.
static const uint32_t HEARTBEAT_INTERVAL_MS = 15000;

static void ble_task(void *param) {
  static ScanCallbacks scan_callbacks;
  BLEScan *scanner = BLEDevice::getScan();
  scanner->setAdvertisedDeviceCallbacks(&scan_callbacks);

  uint32_t lastHeartbeat = 0;

  for (;;) {
    if (g_ble_paused) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }

    if (g_ble_connected && g_ble_client != nullptr && g_ble_client->isConnected()) {
      if (g_force_rescan) {
        log_line("[RESCAN] rescan requested, disconnecting");
        g_ble_client->disconnect();
        g_force_rescan = false;
        continue;
      }
      // Idle - streaming happens via on_ble_notify(). Heartbeat keeps the
      // watchdog satisfied and confirms in the log that we're still
      // connected even during a stretch with no notify traffic.
      if (millis() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
        lastHeartbeat = millis();
        log_line("[BLE] still connected, waiting for data");
      }
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }

    g_force_rescan = false;

    for (BLEAdvertisedDevice *d : g_discovered) delete d;
    g_discovered.clear();
    g_seen_macs.clear();

    log_line("[SCAN] starting 10s BLE scan for target device...");
    scanner->setActiveScan(true);
    scanner->setInterval(100);
    scanner->setWindow(99);
    scanner->start(10, false);
    scanner->clearResults();

    if (g_ble_paused) continue;

    BLEAdvertisedDevice *target = pick_target();
    if (target == nullptr) {
      log_line("[BLE] target not found this scan (" + String(static_cast<int>(g_discovered.size())) +
               " other device(s) seen), retrying in 5s");
      vTaskDelay(pdMS_TO_TICKS(5000));
      continue;
    }

    explore_and_subscribe(target);
  }
}

// The BLE stack occasionally hangs indefinitely inside a single blocking
// call (observed: stuck on a custom/non-standard service's characteristics,
// likely a read or notify-subscribe waiting on a GATT response that never
// arrives) with no internal timeout. Rather than chase every possible
// blocking call inside the library, watch for the symptom directly: no log
// activity at all for too long means the BLE task is stuck somewhere, so
// force a clean reboot to recover instead of requiring a manual /reboot.
// The steady-state heartbeat above (every 15s) keeps this from
// false-firing during normal idle-but-connected stretches.
static const uint32_t WATCHDOG_STALL_MS = 40000;

static void watchdog_task(void *param) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(5000));
    uint32_t sinceActivity = millis() - g_last_activity_ms;
    if (sinceActivity > WATCHDOG_STALL_MS) {
      Serial.println("[WATCHDOG] no activity for " + String(sinceActivity / 1000) +
                      "s, BLE task appears stuck - rebooting");
      Serial.flush();
      delay(200);
      ESP.restart();
    }
  }
}

// ===================== WiFi / HTTP =====================

static WiFiServer g_http_server(80);

static void connect_wifi() {
  WiFi.mode(WIFI_STA);
  // Deliberately NOT calling WiFi.setSleep(false) here. It was added to
  // fix WiFi responsiveness, but confirmed via addr2line against a real
  // crash dump: with sleep disabled, BLEDevice::init() crashes 100% of
  // the time inside Espressif's precompiled coex_core_enable() (WiFi/BLE
  // coexistence library) - not our code, no workaround found besides not
  // disabling sleep. The stay-connected BLE redesign (not continuously
  // scanning) already addresses most of the responsiveness problem this
  // was meant to fix, so leaving sleep at its default is the safer trade.
  WiFi.setHostname(DEVICE_HOSTNAME);

#ifdef WIFI_STATIC_IP
  IPAddress ip, gw, mask, dns;
  ip.fromString(WIFI_STATIC_IP);
  gw.fromString(WIFI_GATEWAY);
  mask.fromString(WIFI_SUBNET);
  dns.fromString(WIFI_DNS);
  if (!WiFi.config(ip, gw, mask, dns)) {
    Serial.println("[WIFI] static IP config failed, falling back to DHCP");
  }
#endif

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[WIFI] connecting to " WIFI_SSID " ");
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[WIFI] connected, IP=");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[WIFI] not connected yet, will keep retrying in background");
  }
}

static const char PAGE_HTML[] PROGMEM = R"HTML(<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hobocamp BW0F Logger</title>
<style>
body{background:#0b0f14;color:#c9d1d9;font-family:ui-monospace,Consolas,Menlo,monospace;margin:0}
#bar{padding:10px 14px;background:#161b22;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;gap:10px;flex-wrap:wrap}
#status{color:#8b949e;font-size:13px}
#log{padding:12px 14px;white-space:pre-wrap;word-break:break-all;font-size:12.5px;line-height:1.45}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.ok{background:#3fb950}.bad{background:#f85149}
button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 12px;font-family:inherit;font-size:12.5px;cursor:pointer}
button:hover{background:#30363d}
button:disabled{opacity:0.5;cursor:default}
.controls{display:flex;gap:8px;align-items:center}
</style></head>
<body>
<div id="bar">
  <div><span class="dot bad" id="dot"></span><b>Hobocamp EcoWorthy BW0F Logger</b></div>
  <div class="controls">
    <div id="status">connecting...</div>
    <button id="rescanBtn" onclick="rescan()">Rescan BLE</button>
    <button id="rebootBtn" onclick="reboot()">Reboot</button>
  </div>
</div>
<div id="log"></div>
<script>
const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const dot = document.getElementById('dot');
let lines = 0;
const es = new EventSource('/log');
es.onopen = () => { statusEl.textContent = 'live'; dot.className = 'dot ok'; };
es.onerror = () => { statusEl.textContent = 'disconnected - retrying...'; dot.className = 'dot bad'; };
es.onmessage = (e) => {
  const div = document.createElement('div');
  div.textContent = e.data;
  logEl.appendChild(div);
  lines++;
  if (lines > 1500) { logEl.removeChild(logEl.firstChild); lines--; }
  window.scrollTo(0, document.body.scrollHeight);
};
function rescan() {
  fetch('/rescan').then(() => {
    statusEl.textContent = 'rescan requested';
  });
}
function reboot() {
  if (!confirm('Reboot the device? It will drop off WiFi for a few seconds.')) return;
  const btn = document.getElementById('rebootBtn');
  btn.disabled = true;
  btn.textContent = 'Rebooting...';
  fetch('/reboot').catch(() => {});
}
</script>
</body></html>
)HTML";

static void send_backlog(LogClient &lc) {
  xSemaphoreTake(g_log_mutex, portMAX_DELAY);
  size_t start = (g_ring_count < LOG_RING_SIZE) ? 0 : g_ring_head;
  for (size_t i = 0; i < g_ring_count; i++) {
    size_t idx = (start + i) % LOG_RING_SIZE;
    if (lc.sse) {
      lc.client.print("data: ");
      lc.client.print(sse_escape(g_ring[idx]));
      lc.client.print("\n\n");
    } else {
      lc.client.print(g_ring[idx]);
      lc.client.print("\r\n");
    }
  }
  xSemaphoreGive(g_log_mutex);
}

static bool add_log_client(WiFiClient client, bool sse) {
  xSemaphoreTake(g_log_mutex, portMAX_DELAY);
  for (size_t i = 0; i < MAX_LOG_CLIENTS; i++) {
    if (!g_log_clients[i].in_use) {
      g_log_clients[i].client = client;
      g_log_clients[i].sse = sse;
      g_log_clients[i].in_use = true;
      xSemaphoreGive(g_log_mutex);
      return true;
    }
  }
  xSemaphoreGive(g_log_mutex);
  return false;
}

static String url_query_param(const String &requestLine, const String &key) {
  int qIdx = requestLine.indexOf('?');
  if (qIdx < 0) return "";
  int spaceIdx = requestLine.indexOf(' ', qIdx);
  String query = requestLine.substring(qIdx + 1, spaceIdx < 0 ? requestLine.length() : spaceIdx);
  String needle = key + "=";
  int pos = query.indexOf(needle);
  if (pos < 0) return "";
  int start = pos + needle.length();
  int end = query.indexOf('&', start);
  return end < 0 ? query.substring(start) : query.substring(start, end);
}

// Custom HTTP-push OTA: the client (our own upload script) POSTs the raw
// firmware binary in one direction over a single TCP connection. Avoids
// ArduinoOTA's UDP invitation + callback-connection handshake, which
// needs a route back from the device to the uploading machine and turned
// out to be unreliable over the HaLow bridge. Only reboots on a fully
// verified write; any failure leaves the currently-running firmware alone.
static void handle_update_upload(WiFiClient client, const String &requestLine, long contentLength,
                                  String expectedMd5) {
  String token = url_query_param(requestLine, "token");
  if (token != OTA_PASSWORD) {
    log_line("[UPDATE] rejected: bad/missing token");
    client.println("HTTP/1.1 401 Unauthorized");
    client.println("Connection: close");
    client.println();
    client.print("bad token\r\n");
    client.stop();
    return;
  }

  if (contentLength <= 0) {
    client.println("HTTP/1.1 400 Bad Request");
    client.println("Connection: close");
    client.println();
    client.print("missing/invalid Content-Length\r\n");
    client.stop();
    return;
  }

  // Stop competing with WiFi for the shared 2.4GHz radio for the duration
  // of the transfer - resumed on every exit path below except the final
  // success path, which reboots anyway.
  pause_ble_for_update();

  log_line("[UPDATE] starting: " + String(contentLength) + " bytes, expected md5=" +
           (expectedMd5.length() ? expectedMd5 : String("(none)")));

  if (!Update.begin(contentLength)) {
    log_line("[UPDATE] begin failed: " + String(Update.errorString()));
    client.println("HTTP/1.1 500 Internal Server Error");
    client.println("Connection: close");
    client.println();
    client.print("Update.begin failed: " + String(Update.errorString()) + "\r\n");
    client.stop();
    resume_ble_after_update();
    return;
  }

  if (expectedMd5.length() == 32) {
    Update.setMD5(expectedMd5.c_str());
  }

  static uint8_t buf[1024];
  long remaining = contentLength;
  uint32_t lastProgressPct = 0;
  client.setTimeout(10);
  uint32_t stallStart = millis();

  while (remaining > 0) {
    size_t want = remaining < (long)sizeof(buf) ? (size_t)remaining : sizeof(buf);
    size_t n = client.readBytes(buf, want);
    if (n == 0) {
      if (!client.connected() || millis() - stallStart > 30000) {
        log_line("[UPDATE] aborted: connection stalled/closed with " + String(remaining) + " bytes remaining");
        Update.abort();
        client.stop();
        resume_ble_after_update();
        return;
      }
      continue;
    }
    stallStart = millis();

    if (Update.write(buf, n) != n) {
      log_line("[UPDATE] write error: " + String(Update.errorString()));
      Update.abort();
      client.println("HTTP/1.1 500 Internal Server Error");
      client.println("Connection: close");
      client.println();
      client.print("write failed: " + String(Update.errorString()) + "\r\n");
      client.stop();
      resume_ble_after_update();
      return;
    }
    remaining -= n;

    uint32_t pct = static_cast<uint32_t>(100 * (contentLength - remaining) / contentLength);
    if (pct >= lastProgressPct + 10) {
      lastProgressPct = pct - (pct % 10);
      log_line("[UPDATE] progress " + String(pct) + "%");
    }
  }

  if (!Update.end(true)) {
    log_line("[UPDATE] failed: " + String(Update.errorString()));
    client.println("HTTP/1.1 500 Internal Server Error");
    client.println("Connection: close");
    client.println();
    client.print("Update.end failed: " + String(Update.errorString()) + "\r\n");
    client.stop();
    resume_ble_after_update();
    return;
  }

  log_line("[UPDATE] success, rebooting");
  client.println("HTTP/1.1 200 OK");
  client.println("Connection: close");
  client.println();
  client.print("update ok, rebooting\r\n");
  client.flush();
  client.stop();
  delay(300);
  ESP.restart();
}

static void handle_http_client(WiFiClient client) {
  client.setTimeout(2);
  String requestLine = client.readStringUntil('\n');

  long contentLength = -1;
  String firmwareMd5;

  while (client.available()) {
    String h = client.readStringUntil('\n');
    if (h == "\r") break;
    String hLower = h;
    hLower.toLowerCase();
    if (hLower.startsWith("content-length:")) {
      contentLength = h.substring(h.indexOf(':') + 1).toInt();
    } else if (hLower.startsWith("x-firmware-md5:")) {
      firmwareMd5 = h.substring(h.indexOf(':') + 1);
      firmwareMd5.trim();
    }
  }

  if (requestLine.startsWith("POST /update")) {
    handle_update_upload(client, requestLine, contentLength, firmwareMd5);
  } else if (requestLine.startsWith("GET / ") || requestLine.startsWith("GET / HTTP")) {
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/html");
    client.println("Connection: close");
    client.println();
    client.print(PAGE_HTML);
    client.stop();
  } else if (requestLine.startsWith("GET /log")) {
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/event-stream");
    client.println("Cache-Control: no-cache");
    client.println("Connection: keep-alive");
    client.println("Access-Control-Allow-Origin: *");
    client.println();
    LogClient tmp;
    tmp.client = client;
    tmp.sse = true;
    send_backlog(tmp);
    if (!add_log_client(client, true)) {
      client.print("data: log client slots full, try again shortly\n\n");
      client.stop();
    }
  } else if (requestLine.startsWith("GET /stream")) {
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/plain");
    client.println("Cache-Control: no-cache");
    client.println("Connection: keep-alive");
    client.println();
    LogClient tmp;
    tmp.client = client;
    tmp.sse = false;
    send_backlog(tmp);
    if (!add_log_client(client, false)) {
      client.print("log client slots full, try again shortly\r\n");
      client.stop();
    }
  } else if (requestLine.startsWith("GET /rescan")) {
    g_force_rescan = true;
    log_line("[RESCAN] requested via HTTP");
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/plain");
    client.println("Connection: close");
    client.println();
    client.print("rescan requested\r\n");
    client.stop();
  } else if (requestLine.startsWith("GET /reboot")) {
    log_line("[REBOOT] requested via HTTP, restarting now");
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/plain");
    client.println("Connection: close");
    client.println();
    client.print("rebooting\r\n");
    client.flush();
    client.stop();
    delay(200);
    ESP.restart();
  } else if (requestLine.startsWith("GET /status")) {
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: application/json");
    client.println("Connection: close");
    client.println();
    client.print("{\"uptime_s\":");
    client.print(millis() / 1000);
    client.print(",\"heap_free\":");
    client.print(ESP.getFreeHeap());
    client.print(",\"wifi_rssi\":");
    client.print(WiFi.RSSI());
    client.print(",\"ble_connected\":");
    client.print(g_ble_connected ? "true" : "false");
    client.print(",\"wifi_mac\":\"");
    client.print(WiFi.macAddress());
    client.print("\",\"ble_paused\":");
    client.print(g_ble_paused ? "true" : "false");
    client.print(",\"firmware_version\":\"" FIRMWARE_VERSION "\"");
    client.println("}");
    client.stop();
  } else {
    client.println("HTTP/1.1 404 Not Found");
    client.println("Connection: close");
    client.println();
    client.stop();
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("Hobocamp EcoWorthy BW0F Logger - phase 1");

  g_log_mutex = xSemaphoreCreateMutex();
  g_last_activity_ms = millis();

  connect_wifi();
  if (WiFi.status() == WL_CONNECTED) {
    MDNS.begin(DEVICE_HOSTNAME);
    g_http_server.begin();
    log_line("[WIFI] connected, ip=" + WiFi.localIP().toString() + " hostname=" DEVICE_HOSTNAME ".local");
    log_line("[HTTP] live log page at http://" + WiFi.localIP().toString() + "/");
    log_line("[HTTP] plain-text stream at http://" + WiFi.localIP().toString() + "/stream");
  }

  // Initializing BLE immediately after WiFi connects crashes reproducibly
  // in Espressif's precompiled coexistence library (coex_core_enable,
  // confirmed via addr2line against a real crash - not our code). Some
  // settling time avoids it; this was likely masked before by ArduinoOTA's
  // setup work incidentally providing that delay.
  delay(1000);
  BLEDevice::init("hobocamp-bw0f-logger");
  xTaskCreate(ble_task, "ble_task", 8192, nullptr, 1, nullptr);
  xTaskCreate(watchdog_task, "watchdog_task", 2048, nullptr, 1, nullptr);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    static uint32_t last_retry = 0;
    if (millis() - last_retry > 10000) {
      last_retry = millis();
      Serial.println("[WIFI] reconnecting...");
      WiFi.reconnect();
    }
  }

  WiFiClient client = g_http_server.available();
  if (client) {
    handle_http_client(client);
  }

  // Prune dead log clients.
  xSemaphoreTake(g_log_mutex, portMAX_DELAY);
  for (size_t i = 0; i < MAX_LOG_CLIENTS; i++) {
    if (g_log_clients[i].in_use && !g_log_clients[i].client.connected()) {
      g_log_clients[i].client.stop();
      g_log_clients[i].in_use = false;
    }
  }
  xSemaphoreGive(g_log_mutex);

  delay(10);
}
