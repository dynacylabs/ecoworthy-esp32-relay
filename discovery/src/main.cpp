// Phase 1: EcoWorthy BW0F BLE logger for ESP32-C3.
//
// Connects to WiFi, auto-discovers/connects to the BW0F over BLE, logs every
// advertisement/service/characteristic/notification it sees, and fans that
// log out over IP (HTML live view + plain-text stream) plus OTA for phase 2.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
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

static void log_line(const String &msg) {
  String stamped = "[" + String(millis() / 1000.0, 3) + "] " + msg;
  Serial.println(stamped);

  xSemaphoreTake(g_log_mutex, portMAX_DELAY);
  g_ring[g_ring_head] = stamped;
  g_ring_head = (g_ring_head + 1) % LOG_RING_SIZE;
  if (g_ring_count < LOG_RING_SIZE) g_ring_count++;

  for (size_t i = 0; i < MAX_LOG_CLIENTS; i++) {
    LogClient &lc = g_log_clients[i];
    if (!lc.in_use) continue;
    if (!lc.client.connected()) {
      lc.client.stop();
      lc.in_use = false;
      continue;
    }
    if (lc.sse) {
      lc.client.print("data: ");
      lc.client.print(sse_escape(stamped));
      lc.client.print("\n\n");
    } else {
      lc.client.print(stamped);
      lc.client.print("\r\n");
    }
  }
  xSemaphoreGive(g_log_mutex);
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

// ===================== BLE target selection (mirrors discovery.ino) =====================

static std::string lower_copy(const std::string &input) {
  std::string out = input;
  for (char &c : out) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
  }
  return out;
}

static bool is_service_hint_match(const std::string &uuid_lower) {
  return uuid_lower.find("0000ffe0") != std::string::npos ||
         uuid_lower.find("0000ff00") != std::string::npos ||
         uuid_lower.find("0000fff0") != std::string::npos;
}

static bool is_name_hint_match(BLEAdvertisedDevice &dev) {
  if (!dev.haveName()) return false;
  const std::string name = lower_copy(dev.getName());
  return name.find("ecoworthy") != std::string::npos ||
         name.find("eco worthy") != std::string::npos ||
         name.find("bw0f") != std::string::npos ||
         name.find("jbd") != std::string::npos ||
         name.find("bms") != std::string::npos;
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

static BLEAdvertisedDevice *pick_best_target() {
  if (g_discovered.empty()) return nullptr;

  BLEAdvertisedDevice *best_name_hint = nullptr;
  BLEAdvertisedDevice *best_service_hint = nullptr;
  BLEAdvertisedDevice *best_rssi = nullptr;

  for (BLEAdvertisedDevice *dev : g_discovered) {
    if (best_rssi == nullptr || dev->getRSSI() > best_rssi->getRSSI()) best_rssi = dev;

    if (is_name_hint_match(*dev)) {
      if (best_name_hint == nullptr || dev->getRSSI() > best_name_hint->getRSSI())
        best_name_hint = dev;
    }

    if (dev->haveServiceUUID()) {
      const std::string uuid = lower_copy(dev->getServiceUUID().toString());
      if (is_service_hint_match(uuid)) {
        if (best_service_hint == nullptr || dev->getRSSI() > best_service_hint->getRSSI())
          best_service_hint = dev;
      }
    }
  }

  if (best_name_hint != nullptr) {
    log_line("[TARGET] selected via name hint (ecoworthy/bw0f/bms/jbd)");
    return best_name_hint;
  }
  if (best_service_hint != nullptr) {
    log_line("[TARGET] selected via JBD-like service UUID hint");
    return best_service_hint;
  }
  log_line("[TARGET] no name/service hint match; falling back to strongest RSSI");
  return best_rssi;
}

// ===================== BLE connect / subscribe / log =====================

static bool g_ble_connected = false;
static BLEClient *g_ble_client = nullptr;

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

static void explore_and_subscribe(BLEAdvertisedDevice *target) {
  if (g_ble_client == nullptr) {
    g_ble_client = BLEDevice::createClient();
    g_ble_client->setClientCallbacks(new ClientCallbacks());
  }

  log_line("[BLE] connecting to " + String(target->getAddress().toString().c_str()));
  if (!g_ble_client->connect(target)) {
    log_line("[BLE] connect failed");
    return;
  }
  g_ble_connected = true;

  std::map<std::string, BLERemoteService *> *services = g_ble_client->getServices();
  if (services == nullptr || services->empty()) {
    log_line("[BLE] no GATT services discovered");
    return;
  }

  log_line("[BLE] discovered " + String(static_cast<int>(services->size())) + " service(s)");

  for (const auto &service_entry : *services) {
    BLERemoteService *svc = service_entry.second;
    log_line("[SERVICE] " + String(service_entry.first.c_str()));

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

      log_line("[CHARACTERISTIC] " + String(char_entry.first.c_str()) + " flags=" + flags);

      if (ch->canRead()) {
        std::string value = ch->readValue();
        if (!value.empty()) {
          log_line("[READ] char=" + String(ch->getUUID().toString().c_str()) +
                    " len=" + String(static_cast<int>(value.size())) +
                    " hex=" + bytes_to_hex(reinterpret_cast<const uint8_t *>(value.data()), value.size()) +
                    " ascii=\"" + bytes_to_ascii(reinterpret_cast<const uint8_t *>(value.data()), value.size()) + "\"");
        }
      }

      if (ch->canNotify() || ch->canIndicate()) {
        ch->registerForNotify(on_ble_notify);
        log_line("[SUBSCRIBE] char=" + String(ch->getUUID().toString().c_str()));
      }
    }
  }
}

// One scan+connect attempt. Runs on its own task so it never blocks WiFi/OTA/HTTP.
static void ble_task(void *param) {
  for (;;) {
    if (g_ble_connected && g_ble_client != nullptr && g_ble_client->isConnected()) {
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }

    for (BLEAdvertisedDevice *d : g_discovered) delete d;
    g_discovered.clear();
    g_seen_macs.clear();

    log_line("[SCAN] starting 10s BLE scan...");
    BLEScan *scanner = BLEDevice::getScan();
    scanner->setAdvertisedDeviceCallbacks(new ScanCallbacks());
    scanner->setActiveScan(true);
    scanner->setInterval(100);
    scanner->setWindow(99);
    scanner->start(10, false);
    scanner->clearResults();

    log_line("[SCAN] found " + String(static_cast<int>(g_discovered.size())) + " unique device(s)");

    BLEAdvertisedDevice *target = pick_best_target();
    if (target != nullptr) {
      explore_and_subscribe(target);
    } else {
      log_line("[BLE] no candidate device found this round");
    }

    if (!g_ble_connected) {
      log_line("[BLE] will retry scan in 15s");
      vTaskDelay(pdMS_TO_TICKS(15000));
    }
  }
}

// ===================== WiFi / OTA / HTTP =====================

static WiFiServer g_http_server(80);

static void connect_wifi() {
  WiFi.mode(WIFI_STA);
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

static void setup_ota() {
  ArduinoOTA.setHostname(DEVICE_HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() { log_line("[OTA] update starting"); });
  ArduinoOTA.onEnd([]() { log_line("[OTA] update complete, rebooting"); });
  ArduinoOTA.onError([](ota_error_t error) {
    log_line("[OTA] error code=" + String(static_cast<int>(error)));
  });
  ArduinoOTA.begin();
}

static const char PAGE_HTML[] PROGMEM = R"HTML(<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hobocamp BW0F Logger</title>
<style>
body{background:#0b0f14;color:#c9d1d9;font-family:ui-monospace,Consolas,Menlo,monospace;margin:0}
#bar{padding:10px 14px;background:#161b22;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0}
#status{color:#8b949e;font-size:13px}
#log{padding:12px 14px;white-space:pre-wrap;word-break:break-all;font-size:12.5px;line-height:1.45}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.ok{background:#3fb950}.bad{background:#f85149}
</style></head>
<body>
<div id="bar"><div><span class="dot bad" id="dot"></span><b>Hobocamp EcoWorthy BW0F Logger</b></div><div id="status">connecting...</div></div>
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

static void handle_http_client(WiFiClient client) {
  client.setTimeout(2);
  String requestLine = client.readStringUntil('\n');
  while (client.available()) {
    String h = client.readStringUntil('\n');
    if (h == "\r") break;
  }

  if (requestLine.startsWith("GET / ") || requestLine.startsWith("GET / HTTP")) {
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

  connect_wifi();
  if (WiFi.status() == WL_CONNECTED) {
    MDNS.begin(DEVICE_HOSTNAME);
    setup_ota();
    g_http_server.begin();
    log_line("[WIFI] connected, ip=" + WiFi.localIP().toString() + " hostname=" DEVICE_HOSTNAME ".local");
    log_line("[HTTP] live log page at http://" + WiFi.localIP().toString() + "/");
    log_line("[HTTP] plain-text stream at http://" + WiFi.localIP().toString() + "/stream");
  }

  BLEDevice::init("hobocamp-bw0f-logger");
  xTaskCreate(ble_task, "ble_task", 8192, nullptr, 1, nullptr);
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

  ArduinoOTA.handle();

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
