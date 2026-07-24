# hobocamp-bw0f-logger (phase 1)

ESP32-C3 firmware for the hobocamp deployment. Connects to the EcoWorthy
BW0F over BLE, logs everything it can see (GATT services/characteristics,
every notify/indicate payload) and makes that log visible in real time over
IP. Supports OTA so phase 2 (real decoding/parsing) can be pushed
wirelessly without physical access.

This is a standalone PlatformIO/Arduino project. It deliberately doesn't
try to map known sensors (e.g. via ESPHome's `ble_client` platform, which
needs known characteristic UUIDs) — the BW0F's protocol isn't decoded yet,
so this firmware just dumps raw hex/ASCII for every characteristic so the
protocol can be figured out from real traffic.

## What it does

- Joins WiFi (`ITBurnsWhenIP-CAMS`, hidden SSID, static IP `192.168.2.4` —
  see "Network assumptions" below). WiFi modem sleep is explicitly disabled
  (`WiFi.setSleep(false)`) — this device is externally powered, and sleep
  otherwise adds multi-second latency spikes to inbound traffic.
- Scans for BLE devices, connects to the **one target device** (confirmed
  live: `e8:ca:50:42:16:c2`, advertises as `"ECO-WORTHY 0F_16C1"` —
  configured via `TARGET_MAC` in `secrets.h`; falls back to name-hint
  matching if `TARGET_MAC` isn't set), discovers all its
  services/characteristics, reads anything readable, subscribes to every
  notify/indicate characteristic, and **stays connected indefinitely** —
  it does not disconnect and does not rescan on a timer. Notify/indicate
  traffic streams via callback for as long as the connection holds. Only
  rescans if actually disconnected, a rescan is requested, or coming back
  from a paused (OTA in progress) state.
  - This matters a lot for responsiveness: the ESP32-C3 has a single
    2.4GHz radio shared between WiFi and BLE. Active BLE scanning is
    radio-intensive and measurably starves WiFi (confirmed live: WiFi RSSI
    reported as low as -91dBm while continuously scanning, despite the
    device sitting immediately next to the AP — recovered to -22dBm once
    idle). Staying connected once found, instead of continuously
    scanning/reconnecting, leaves WiFi far more airtime.
- Logs a heartbeat line every 15s while connected-but-quiet, both so the
  log clearly shows "still alive, just no data right now" and to keep the
  stall watchdog (below) from false-firing during a legitimate quiet
  stretch.
- Self-recovery watchdog: if the BLE task produces zero log activity for
  40s (the notify-callback library has been observed to hang indefinitely
  inside a single blocking call with no internal timeout — seen stuck on
  a custom/non-standard service's characteristics), the device reboots
  itself rather than requiring a manual `/reboot`.
- Runs BLE scanning/connecting on its own FreeRTOS task so a long scan or
  slow connect attempt never blocks WiFi, OTA, or the HTTP server.
- Serves the log over HTTP:
  - `http://192.168.2.4/` — live-scrolling terminal-style page (uses
    Server-Sent Events, no external JS/CSS, works in any browser). Has
    **Rescan BLE** and **Reboot** buttons in the top bar.
  - `http://192.168.2.4/stream` — plain-text live tail, e.g. `curl -N
    http://192.168.2.4/stream`.
  - `http://192.168.2.4/rescan` — disconnects (if connected) and
    immediately starts a fresh scan. Does not reboot the chip or drop
    WiFi/OTA/log viewers.
  - `http://192.168.2.4/reboot` — full `ESP.restart()`. Drops WiFi/log
    viewers for a few seconds; use `/rescan` instead unless you actually
    need a full restart.
  - `http://192.168.2.4/status` — JSON health check (uptime, free heap,
    WiFi RSSI, BLE connection state, firmware version, whether BLE is
    currently paused for an OTA transfer).
  - Also mirrored to USB serial (115200 baud) the whole time.
- One OTA path, deliberately: **`POST /update`** (push, one-directional,
  MD5-verified). ArduinoOTA (`espota`) and a device-initiated pull-updater
  were both tried and removed — ArduinoOTA needs the device to open a
  fresh outbound connection back to the uploading machine, which
  repeatedly failed in the field (both across VLANs and same-VLAN — the
  real bottleneck is BLE/WiFi radio contention on the device itself, not
  routing); the pull-updater added a meaningfully larger dependency
  (`HTTPClient`/`HTTPUpdate`, ~9% more flash) for a mechanism that was
  never actually verified working in the field. `/update` is the one
  path that's been proven repeatedly today. See "Deploying updates"
  below.
- `pause_ble_for_update()` runs before any transfer starts — stops the
  active BLE connection/scan so the transfer gets clean WiFi airtime
  instead of competing with BLE for the radio. Resumed on every exit path
  except final success (which reboots anyway).

## Flashing

First flash must be over USB (board enumerates as a `USB Serial Device`,
VID `303A` / PID `1001` — port letter isn't stable across replugs, check
Device Manager):

```bash
pio run -e esp32-c3-devkitm-1 -t upload --upload-port COM5
```

If you ever see the board stuck resetting in a fast loop (ROM banner
repeating every ~10ms), that's this specific board's flash chip not
tolerating the default 80MHz DIO read speed — already worked around via
`board_build.flash_mode = dio` / `board_build.f_flash = 40000000L` in
`platformio.ini`. If it recurs after changing those settings, do a full
`pio run -t erase` before reflashing so a stale bootloader/partition table
isn't left behind.

If `pio run -t upload` fails to even open the port
(`PermissionError 13 "device attached to the system is not functioning"`
on Windows) even though Device Manager shows it as healthy, that's usually
a genuinely flaky USB connection (bad cable/port), not a driver/software
issue — try a different cable/port, or a full laptop reboot.

## Deploying updates

```bash
pio run -e esp32-c3-devkitm-1
python tools/push_update.py .pio/build/esp32-c3-devkitm-1/firmware.bin \
    --host 192.168.2.4 --token <OTA_PASSWORD from secrets.h>
```

Computes the MD5, POSTs the binary with an `X-Firmware-MD5` header, prints
the device's response. Only reboots if the whole image was received *and*
the MD5 matched; a failed/interrupted push leaves it running the old
firmware, so it's always safe to retry. In practice, on a congested link
this can take many retries — that's expected, not a sign something's
broken; each attempt is fully safe regardless of how many fail first.

When this machine can't reach `192.168.2.4` directly (e.g. on a cellular
hotspot in the field), relay through `mac-mini`, which is dual-homed onto
the hobocamp's VLAN (`192.168.2.9`) and reachable from anywhere via
Tailscale:

```bash
bash tools/push_via_relay.sh .pio/build/esp32-c3-devkitm-1/firmware.bin <OTA_PASSWORD>
```

Same idea for ad hoc status checks: `ssh austinc@mac-mini curl -s
http://192.168.2.4/status`.

**Bootstrap note:** `/update` only exists in firmware that already has
that code baked in. The very first time it's introduced to an
already-deployed device, it has to arrive via USB — after that, every
future update can go through `/update`.

The OTA password is deliberately alphanumeric-only and separate from the
WiFi password — an earlier ArduinoOTA-based path's `upload_flags` passed
through SCons variable substitution, and a literal `$` in the value got
silently treated as a (nonexistent) SCons variable and dropped, breaking
auth. Keep it that way if you change it. `tools/push_update.py` doesn't
have that specific problem (no SCons involved), but there's no reason to
reintroduce a `$` either.

## Secrets

`include/secrets.h` is gitignored (see repo-root `.gitignore`). Copy
`include/secrets.h.example` to `include/secrets.h` and fill in real values
before building. Includes `TARGET_MAC` — the exact BLE MAC to target;
leave undefined to fall back to name-hint matching instead.

## Network assumptions

The hobocamp WiFi (`ITBurnsWhenIP-CAMS`) is the downstream 2.4GHz side of
the property's HaLow point-to-point bridge, on VLAN 2 (`192.168.2.0/24`).
Static IP `192.168.2.4` is configured with gateway `192.168.2.1` / mask
`255.255.255.0`. `mac-mini` is dual-homed onto both the home LAN
(`192.168.1.100`) and VLAN 2 (`192.168.2.9`), used as the relay/OTA-server
host specifically so it has a direct, same-subnet path to the device
without crossing the VLAN1/VLAN2 firewall boundary (confirmed: that
firewall blocks VLAN2-initiated connections back to VLAN1, which is also
why ArduinoOTA's reverse-connection requirement fails from a laptop that
isn't on VLAN 2).

## Known limitations (phase 1 scope)

- No persistent storage of log history — only the last ~400 lines (ring
  buffer) are replayed to a newly-connecting log viewer; older lines are
  gone once evicted. Fine for "watch it live," not for after-the-fact
  analysis.
- Up to 4 concurrent log viewers (HTML + `/stream` combined).
- No parsing/decoding of the BW0F's actual protocol yet — that's phase 2,
  once real traffic has been captured from the log to reverse-engineer the
  field layout. Relatedly: this firmware never *writes* anything to the
  device besides subscribing to notify/indicate (CCCD writes) — some
  BMS/JBD-style protocols require an explicit "start streaming" command
  write before they'll send periodic telemetry. If the log goes quiet
  after "subscribed... staying connected" with no NOTIFY lines ever
  appearing, that's the likely reason, and figuring out that command is
  part of the phase 2 protocol work.
- BLE scan step blocks for ~10s at boot and after any disconnect before a
  connection is (re)established (runs in its own task, so WiFi/OTA/HTTP
  stay responsive throughout — just the BLE side itself is blocked for
  that window).
