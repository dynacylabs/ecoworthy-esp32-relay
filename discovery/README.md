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
- Scans for BLE devices (10s), then connects to **every** device it found,
  one at a time — not just a best guess. For each: discovers all
  services/characteristics, reads anything readable, subscribes to every
  notify/indicate characteristic, listens for ~8s to catch some live
  traffic, then disconnects and moves to the next device. Once it's been
  through all of them, it scans again — forever. Useful since we don't
  know for certain what the BW0F identifies as; this way nothing gets
  missed just because a name/service heuristic didn't match it.
- Runs BLE scanning/connecting on its own FreeRTOS task so a long scan or
  slow connect attempt never blocks WiFi, OTA, or the HTTP server.
- Serves the log over HTTP:
  - `http://192.168.2.4/` — live-scrolling terminal-style page (uses
    Server-Sent Events, no external JS/CSS, works in any browser). Has
    **Rescan BLE** and **Reboot** buttons in the top bar.
  - `http://192.168.2.4/stream` — plain-text live tail, e.g. `curl -N
    http://192.168.2.4/stream`.
  - `http://192.168.2.4/rescan` — abandons whatever device it's currently
    exploring and immediately starts a fresh scan cycle over all devices.
    Does not reboot the chip or drop WiFi/OTA/log viewers.
  - `http://192.168.2.4/reboot` — full `ESP.restart()`. Drops WiFi/log
    viewers for a few seconds; use `/rescan` instead unless you actually
    need a full restart.
  - `http://192.168.2.4/status` — JSON health check (uptime, free heap,
    WiFi RSSI, BLE connection state).
  - Also mirrored to USB serial (115200 baud) the whole time.
- ArduinoOTA on port 3232, password-protected. Kept in the code, but in
  practice it was unreliable once the device was deployed across the
  hobocamp's HaLow bridge (`espota`'s handshake needs the device to open a
  fresh outbound connection back to the uploading machine, which doesn't
  survive a lossy/asymmetric link well). Use the HTTP push method below
  instead for anything beyond a same-LAN update.
- `POST /update?token=<OTA_PASSWORD>` — the reliable update path. A single
  one-directional TCP push (client -> device, no callback connection
  needed), streamed straight to flash via the ESP32 `Update` library.
  Verifies an `X-Firmware-MD5` header against the actual bytes received
  and only reboots into the new image if the write completes *and* the
  MD5 matches; any failure leaves the currently-running firmware
  untouched. See "Deploying updates" below.

**Bootstrap note:** `/update` only exists in firmware that already has it
baked in. The very first time it's introduced to an already-deployed
device, it has to arrive via USB (or a successful ArduinoOTA push) — after
that, every future update can go through `/update`.

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

## Deploying updates

**Preferred: HTTP push (`tools/push_update.py`).** Reliable over the
hobocamp's bridge link since it's a single one-directional TCP connection,
unlike ArduinoOTA below.

```bash
pio run -e esp32-c3-devkitm-1
python tools/push_update.py .pio/build/esp32-c3-devkitm-1/firmware.bin \
    --host 192.168.2.4 --token <OTA_PASSWORD from secrets.h>
```

It computes the MD5, POSTs the binary with an `X-Firmware-MD5` header, and
prints the device's response. The device only reboots if the whole image
was received and the MD5 matched; a failed/interrupted push leaves it
running the old firmware, so it's safe to retry.

**When working from the field on a cellular hotspot** (no direct route to
`192.168.2.4`, only Tailscale): relay the push through `mac-mini`, which
sits on the home LAN and has a direct route to the device.

```bash
bash tools/push_via_relay.sh .pio/build/esp32-c3-devkitm-1/firmware.bin <OTA_PASSWORD>
```

This scp's the built binary to `mac-mini` over Tailscale, `git pull`s
`~/ecoworthy-esp32-relay` there to pick up the latest `push_update.py`,
and runs it from `mac-mini` against `192.168.2.4`. `mac-mini` also has
this laptop's SSH key installed for passwordless access, and a clone of
this repo — set up once, reusable for every future deploy. Same idea works
for ad hoc validation while in the field:
`ssh austinc@mac-mini curl -s http://192.168.2.4/status`.

**Fallback: ArduinoOTA (`espota`, same-LAN only).**

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
if you change it. This same password also gates `/update`'s `?token=`.

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
