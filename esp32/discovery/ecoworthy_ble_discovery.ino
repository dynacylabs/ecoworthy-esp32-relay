#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEAdvertisedDevice.h>
#include <BLEScan.h>
#include <BLEUtils.h>
#include <map>
#include <set>
#include <string>
#include <vector>

// ===== User settings =====
// By default this scans all BLE MAC addresses in range.
// Optionally set target filters before flashing.
static const bool SCAN_ALL_DEVICES = true;
static const char *TARGET_MAC = "AA:BB:CC:DD:EE:FF";
static const char *TARGET_NAME_SUBSTRING = "";
static const uint32_t SCAN_SECONDS = 20;
// =========================

struct DiscoverySummary {
  bool saw_advertisement = false;
  bool connected = false;
  bool discovered_services = false;
  bool has_readable_chars = false;
  bool has_notifiable_chars = false;
  bool has_writeable_chars = false;
  bool maybe_encrypted_only = false;
  uint32_t service_count = 0;
  uint32_t characteristic_count = 0;
  std::set<std::string> service_uuids;
};

static DiscoverySummary g_summary;
static BLEAdvertisedDevice *g_target_device = nullptr;
static std::vector<BLEAdvertisedDevice *> g_discovered_devices;
static std::set<std::string> g_seen_macs;

static std::string lower_copy(const std::string &input) {
  std::string out = input;
  for (char &c : out) {
    if (c >= 'A' && c <= 'Z') {
      c = static_cast<char>(c + ('a' - 'A'));
    }
  }
  return out;
}

static String bytes_to_hex(const uint8_t *data, size_t len) {
  static const char *hex = "0123456789ABCDEF";
  String out;
  out.reserve(len * 2);
  for (size_t i = 0; i < len; i++) {
    out += hex[(data[i] >> 4) & 0x0F];
    out += hex[data[i] & 0x0F];
  }
  return out;
}

static bool looks_like_target(const BLEAdvertisedDevice &dev) {
  if (SCAN_ALL_DEVICES) {
    return true;
  }

  const std::string wanted_mac = lower_copy(std::string(TARGET_MAC));
  const std::string dev_mac = lower_copy(dev.getAddress().toString());

  if (wanted_mac != "aa:bb:cc:dd:ee:ff" && dev_mac == wanted_mac) {
    return true;
  }

  if (strlen(TARGET_NAME_SUBSTRING) > 0 && dev.haveName()) {
    const std::string needle = lower_copy(std::string(TARGET_NAME_SUBSTRING));
    const std::string name = lower_copy(dev.getName());
    if (name.find(needle) != std::string::npos) {
      return true;
    }
  }

  return false;
}

static bool has_explicit_mac_target() {
  const std::string wanted_mac = lower_copy(std::string(TARGET_MAC));
  return wanted_mac != "aa:bb:cc:dd:ee:ff";
}

static bool is_service_hint_match(const std::string &uuid_lower) {
  return uuid_lower.find("0000ffe0") != std::string::npos ||
         uuid_lower.find("0000ff00") != std::string::npos ||
         uuid_lower.find("0000fff0") != std::string::npos;
}

static bool is_name_hint_match(const BLEAdvertisedDevice &dev) {
  if (!dev.haveName()) {
    return false;
  }
  const std::string name = lower_copy(dev.getName());
  return name.find("ecoworthy") != std::string::npos ||
         name.find("eco worthy") != std::string::npos ||
         name.find("bw0f") != std::string::npos ||
         name.find("jbd") != std::string::npos ||
         name.find("bms") != std::string::npos;
}

static BLEAdvertisedDevice *pick_best_target_device() {
  if (g_discovered_devices.empty()) {
    return nullptr;
  }

  if (has_explicit_mac_target()) {
    const std::string wanted_mac = lower_copy(std::string(TARGET_MAC));
    for (BLEAdvertisedDevice *dev : g_discovered_devices) {
      if (lower_copy(dev->getAddress().toString()) == wanted_mac) {
        Serial.println("Target selection: explicit TARGET_MAC match.");
        return dev;
      }
    }
  }

  if (strlen(TARGET_NAME_SUBSTRING) > 0) {
    const std::string wanted_name = lower_copy(std::string(TARGET_NAME_SUBSTRING));
    for (BLEAdvertisedDevice *dev : g_discovered_devices) {
      if (!dev->haveName()) {
        continue;
      }
      const std::string name = lower_copy(dev->getName());
      if (name.find(wanted_name) != std::string::npos) {
        Serial.println("Target selection: TARGET_NAME_SUBSTRING match.");
        return dev;
      }
    }
  }

  BLEAdvertisedDevice *best_name_hint = nullptr;
  BLEAdvertisedDevice *best_service_hint = nullptr;
  BLEAdvertisedDevice *best_rssi = nullptr;

  for (BLEAdvertisedDevice *dev : g_discovered_devices) {
    if (best_rssi == nullptr || dev->getRSSI() > best_rssi->getRSSI()) {
      best_rssi = dev;
    }

    if (is_name_hint_match(*dev)) {
      if (best_name_hint == nullptr || dev->getRSSI() > best_name_hint->getRSSI()) {
        best_name_hint = dev;
      }
    }

    if (dev->haveServiceUUID()) {
      const std::string uuid = lower_copy(dev->getServiceUUID().toString());
      if (is_service_hint_match(uuid)) {
        if (best_service_hint == nullptr || dev->getRSSI() > best_service_hint->getRSSI()) {
          best_service_hint = dev;
        }
      }
    }
  }

  if (best_name_hint != nullptr) {
    Serial.println("Target selection: EcoWorthy/BMS name hint.");
    return best_name_hint;
  }

  if (best_service_hint != nullptr) {
    Serial.println("Target selection: JBD-like service UUID hint.");
    return best_service_hint;
  }

  Serial.println("Target selection: strongest RSSI fallback.");
  return best_rssi;
}

class TargetCallbacks : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice advertisedDevice) override {
    if (!looks_like_target(advertisedDevice)) {
      return;
    }

    g_summary.saw_advertisement = true;

    const std::string mac = lower_copy(advertisedDevice.getAddress().toString());
    if (g_seen_macs.find(mac) != g_seen_macs.end()) {
      return;
    }
    g_seen_macs.insert(mac);

    g_discovered_devices.push_back(new BLEAdvertisedDevice(advertisedDevice));

    Serial.println("=== ADVERTISEMENT FOUND ===");
    Serial.print("Address: ");
    Serial.println(advertisedDevice.getAddress().toString().c_str());
    Serial.print("RSSI: ");
    Serial.println(advertisedDevice.getRSSI());

    if (advertisedDevice.haveName()) {
      Serial.print("Name: ");
      Serial.println(advertisedDevice.getName().c_str());
    } else {
      Serial.println("Name: <none>");
    }

    if (advertisedDevice.haveServiceUUID()) {
      Serial.print("Primary Service UUID: ");
      Serial.println(advertisedDevice.getServiceUUID().toString().c_str());
    }

    if (advertisedDevice.haveManufacturerData()) {
      std::string mfg = advertisedDevice.getManufacturerData();
      Serial.print("Manufacturer Data (hex): ");
      Serial.println(bytes_to_hex(reinterpret_cast<const uint8_t *>(mfg.data()), mfg.size()));
    }

    if (advertisedDevice.haveServiceData()) {
      std::string svc = advertisedDevice.getServiceData();
      Serial.print("Service Data (hex): ");
      Serial.println(bytes_to_hex(reinterpret_cast<const uint8_t *>(svc.data()), svc.size()));
    }

    Serial.println();
  }
};

static void print_properties(BLERemoteCharacteristic *ch, std::string *out_flags) {
  std::vector<std::string> flags;

  if (ch->canRead()) {
    flags.push_back("read");
    g_summary.has_readable_chars = true;
  }
  if (ch->canNotify()) {
    flags.push_back("notify");
    g_summary.has_notifiable_chars = true;
  }
  if (ch->canIndicate()) {
    flags.push_back("indicate");
    g_summary.has_notifiable_chars = true;
  }
  if (ch->canWrite()) {
    flags.push_back("write");
    g_summary.has_writeable_chars = true;
  }
  if (ch->canWriteNoResponse()) {
    flags.push_back("write_no_response");
    g_summary.has_writeable_chars = true;
  }
  if (ch->canBroadcast()) {
    flags.push_back("broadcast");
  }

  std::string merged;
  for (size_t i = 0; i < flags.size(); i++) {
    if (i > 0) {
      merged += ",";
    }
    merged += flags[i];
  }

  *out_flags = merged;
}

static void explore_gatt() {
  if (g_target_device == nullptr) {
    g_target_device = pick_best_target_device();
  }

  if (g_target_device == nullptr) {
    Serial.println("No BLE devices found to probe.");
    return;
  }

  BLEClient *client = BLEDevice::createClient();
  Serial.print("Connecting to selected target: ");
  Serial.println(g_target_device->getAddress().toString().c_str());

  if (!client->connect(g_target_device)) {
    Serial.println("Connect failed.");
    return;
  }

  g_summary.connected = true;
  Serial.println("Connected.");

  std::map<std::string, BLERemoteService *> *services = client->getServices();
  if (services == nullptr || services->empty()) {
    Serial.println("No services discovered.");
    client->disconnect();
    return;
  }

  g_summary.discovered_services = true;
  g_summary.service_count = static_cast<uint32_t>(services->size());

  Serial.println("=== GATT SERVICES ===");

  for (const auto &service_entry : *services) {
    const std::string svc_uuid = service_entry.first;
    BLERemoteService *svc = service_entry.second;

    g_summary.service_uuids.insert(lower_copy(svc_uuid));

    Serial.print("Service: ");
    Serial.println(svc_uuid.c_str());

    std::map<std::string, BLERemoteCharacteristic *> *chars = svc->getCharacteristics();
    if (chars == nullptr || chars->empty()) {
      Serial.println("  Characteristics: <none>");
      continue;
    }

    for (const auto &char_entry : *chars) {
      BLERemoteCharacteristic *ch = char_entry.second;
      g_summary.characteristic_count += 1;

      std::string flags;
      print_properties(ch, &flags);

      Serial.print("  Characteristic: ");
      Serial.println(ch->getUUID().toString().c_str());
      Serial.print("    Properties: ");
      Serial.println(flags.empty() ? "<none>" : flags.c_str());

      if (ch->canRead()) {
        std::string value = ch->readValue();
        Serial.print("    Read Length: ");
        Serial.println(static_cast<int>(value.size()));
        if (!value.empty()) {
          const size_t preview_len = value.size() > 24 ? 24 : value.size();
          Serial.print("    Read Preview Hex: ");
          Serial.println(bytes_to_hex(
              reinterpret_cast<const uint8_t *>(value.data()), preview_len));
        }
      }
    }
  }

  if (!g_summary.has_readable_chars && !g_summary.has_notifiable_chars &&
      !g_summary.has_writeable_chars) {
    g_summary.maybe_encrypted_only = true;
  }

  client->disconnect();
  Serial.println("Disconnected.");
}

static bool has_known_jbd_shape() {
  // Common JBD BLE devices often expose FFE0/FFE1 or FF00-like custom service UUIDs.
  // This heuristic is intentionally broad because vendors vary by firmware.
  for (const auto &uuid : g_summary.service_uuids) {
    if (uuid.find("0000ffe0") != std::string::npos ||
        uuid.find("0000ff00") != std::string::npos ||
        uuid.find("0000fff0") != std::string::npos) {
      return true;
    }
  }
  return false;
}

static void print_verdict() {
  Serial.println();
  Serial.println("=== ESPHOME COMPATIBILITY VERDICT ===");

  if (!g_summary.saw_advertisement) {
    Serial.println("Result: UNKNOWN");
    Serial.println("Reason: No BLE advertisements were captured.");
    Serial.println("Action: Re-run with longer scan window and verify power/range.");
    return;
  }

  if (!g_summary.connected) {
    Serial.println("Result: UNKNOWN");
    Serial.println("Reason: BLE devices were seen but selected target connection failed.");
    Serial.println("Action: Device may be busy, out of range, or connection-limited.");
    return;
  }

  if (!g_summary.discovered_services || g_summary.service_count == 0) {
    Serial.println("Result: UNLIKELY");
    Serial.println("Reason: Connected but no GATT services were discovered.");
    return;
  }

  const bool jbd_like = has_known_jbd_shape();

  if (jbd_like) {
    Serial.println("Result: YES (HIGH CONFIDENCE)");
    Serial.println("Reason: Service UUIDs look like known JBD/BMS-style layouts.");
    Serial.println("Action: ESPHome with jbd_bms_ble is likely a good target.");
    return;
  }

  if (g_summary.has_readable_chars || g_summary.has_notifiable_chars) {
    Serial.println("Result: YES (CUSTOM/GENERIC)");
    Serial.println("Reason: Readable/notifiable characteristics are available.");
    Serial.println("Action: ESPHome can likely integrate via ble_client + custom parsing.");
    return;
  }

  if (g_summary.maybe_encrypted_only) {
    Serial.println("Result: MAYBE");
    Serial.println("Reason: Services exist but no obvious readable/notifiable properties.");
    Serial.println("Action: Device may require auth/pairing or proprietary handshake.");
    return;
  }

  Serial.println("Result: UNKNOWN");
  Serial.println("Reason: Partial GATT data discovered but no clear access path.");
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("EcoWorthy BLE Discovery Tool");
  Serial.println("Starting BLE scanner...");

  BLEDevice::init("esp32-bms-discovery");

  BLEScan *scanner = BLEDevice::getScan();
  scanner->setAdvertisedDeviceCallbacks(new TargetCallbacks());
  scanner->setActiveScan(true);
  scanner->setInterval(100);
  scanner->setWindow(99);

  scanner->start(SCAN_SECONDS, false);
  scanner->clearResults();

  Serial.print("Unique BLE devices found: ");
  Serial.println(static_cast<int>(g_discovered_devices.size()));

  explore_gatt();
  print_verdict();

  Serial.println();
  Serial.println("Discovery complete. Device will stay idle.");
}

void loop() {
  delay(1000);
}
