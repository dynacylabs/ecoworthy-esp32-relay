# hobocamp-bw0f-logger (phase 1)

ESP32-C3 firmware for the hobocamp deployment. Connects to the EcoWorthy
BW0F over BLE, logs everything it can see (advertisements, GATT services/
characteristics, every notify/indicate payload) and makes that log visible
in real time over IP. Supports OTA so phase 2 (real decoding/parsing) can be
pushed wirelessly once the device is out at the hobocamp.

This is a standalone PlatformIO/Arduino project. It deliberately doesn't
try to map known sensors (e.g. via ESPHome's `ble_client` platform, which
needs known characteristic UUIDs) — the BW0F's protocol isn't decoded yet,
so this firmware just dumps raw hex/ASCII for every characteristic so the
protocol can be figured out from real traffic.

## What it does

- Joins WiFi (`ITBurnsWhenIP-CAMS`, hidden SSID, static IP `192.168.2.4` —
  see "Network assumptions" below).
- Scans for BLE devices, picks the best match by name (`ecoworthy`/`bw0f`/
  `bms`/`jbd`) or JBD-like service UUID hints. Falls back to strongest RSSI
  if nothing matches (useful on the bench where the BW0F isn't in range; at
  the hobocamp it should normally be the only/strongest BLE device back
  there).
- Connects, discovers all services/characteristics, reads anything
  readable, subscribes to every notify/indicate characteristic, and logs
  it all with a hex + ASCII dump.
- Runs BLE scanning/connecting on its own FreeRTOS task so a long scan
  never blocks WiFi, OTA, or the HTTP server.
- Serves the log over HTTP:
  - `http://192.168.2.4/` — live-scrolling terminal-style page (uses
    Server-Sent Events, no external JS/CSS, works in any browser).
  - `http://192.168.2.4/stream` — plain-text live tail, e.g. `curl -N
    http://192.168.2.4/stream`.
  - `http://192.168.2.4/status` — JSON health check (uptime, free heap,
    WiFi RSSI, BLE connection state).
  - Also mirrored to USB serial (115200 baud) the whole time.
- ArduinoOTA on port 3232, password-protected, so phase 2 firmware can be
  pushed without physical access.

## Flashing

First flash must be over USB (board is on `COM5` here):

```bash
pio run -t upload
```

If you ever see the board stuck resetting in a fast loop (ROM banner
repeating every ~10ms), that's this specific board's flash chip not
tolerating the default 80MHz DIO read speed — already worked around via
`board_build.flash_mode = dio` / `board_build.f_flash = 40000000L` in
`platformio.ini`. If it recurs after changing those settings, do a full
`pio run -t erase` before reflashing so a stale bootloader/partition table
isn't left behind.

## Deploying updates over OTA (phase 2)

Once the device is running and reachable on the network:

```bash
# PowerShell
$env:OTA_PASSWORD = "<value from secrets.h>"
pio run -e esp32-c3-devkitm-1-ota -t upload
```

```bash
# bash
export OTA_PASSWORD="<value from secrets.h>"
pio run -e esp32-c3-devkitm-1-ota -t upload
```

The OTA password is deliberately alphanumeric-only and separate from the
WiFi password — PlatformIO's `upload_flags` pass through SCons variable
substitution, and a literal `$` in the value gets silently treated as a
(nonexistent) SCons variable and dropped, breaking auth. Keep it that way
if you change it.

## Secrets

`include/secrets.h` is gitignored (see repo-root `.gitignore`). Copy
`include/secrets.h.example` to `include/secrets.h` and fill in real values
before building.

## Network assumptions

The hobocamp WiFi (`ITBurnsWhenIP-CAMS`) is the downstream 2.4GHz side of
the property's HaLow point-to-point bridge. Static IP `192.168.2.4` is
configured with gateway `192.168.2.1` / mask `255.255.255.0` — a guess
from the requested `.4` address in that /24. If the real gateway/subnet
differs, WiFi association will still succeed but routing may be wrong;
update `WIFI_GATEWAY`/`WIFI_SUBNET` in `secrets.h` if so.

## Known limitations (phase 1 scope)

- No persistent storage of log history — only the last ~400 lines (ring
  buffer) are replayed to a newly-connecting log viewer; older lines are
  gone once evicted. Fine for "watch it live," not for after-the-fact
  analysis.
- Up to 4 concurrent log viewers (HTML + `/stream` combined).
- No parsing/decoding of the BW0F's actual protocol yet — that's phase 2,
  once real traffic has been captured from the log to reverse-engineer the
  field layout.
- BLE scan step blocks for ~10s at boot and after any disconnect before
  WiFi/OTA/HTTP become reachable again (each runs in its own task, but a
  cold boot's initial WiFi connect + first BLE scan happen sequentially in
  `setup()`/the BLE task, not in parallel with each other).
