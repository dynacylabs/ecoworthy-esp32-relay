# victron-dashboard

A dashboard for a Victron SmartSolar MPPT solar charge controller, read
over its encrypted BLE "Instant Readout" broadcast via an ESP32 running
the community [esphome-victron_ble](https://github.com/Fabian-Schmidt/esphome-victron_ble)
external component:

```
Victron SmartSolar (BLE advertisement) -> ESP32-C3 (ESPHome + esphome-victron_ble) -> server (subscribes, stores, displays)
```

Unlike a raw BLE proxy, the ESP32 here does real work: it scans for the
SmartSolar's manufacturer-data advertisements, decrypts them (AES-128-CTR
with a per-device bindkey pulled from the VictronConnect app), and
exposes the decoded fields as plain ESPHome sensors/text sensors. The
server just connects to the ESP32's native API and subscribes to those
sensor states - no BLE protocol knowledge lives on the server side at
all. This keeps the server simple and means any future protocol fixes
land upstream in `esphome-victron_ble`, not in this repo.

## Repository layout

- [esphome-victron-ble/](esphome-victron-ble/victron-mppt.yaml): the
  ESPHome config for the ESP32-C3. Pulls in `esphome-victron_ble` via
  `external_components` and declares a `victron_ble:` device (MAC +
  bindkey, both from secrets) plus a handful of `sensor:`/`text_sensor:`
  entries for the fields this dashboard cares about. The server always
  initiates the connection to the device (over the native API, port
  6053); the device never has to reach out anywhere, which matters given
  the network's VLAN separation (see below).
- [app/](app/main.py): the dashboard/collector server. A background
  poller (`aioesphomeapi`) connects to the ESP32's native API, subscribes
  to the decoded Victron sensor states, and stores every update in
  TimescaleDB. A FastAPI app serves a themed dashboard (styling matches
  the [heltec-wifi-optimization](https://github.com/dynacylabs/heltec-wifi-optimization)
  project) with charts for battery/solar/load state.
- [db/migrations/](db/migrations/001_init.sql): TimescaleDB schema,
  applied automatically on startup (see `app/migrate.py`).
- [docker-compose.yml](docker-compose.yml): runs the `app` and
  `timescaledb` services - see "Running" below.

## Layout

- `app/ble_poller.py` - connects out to the ESPHome device's native API,
  lists its entities, subscribes to state updates for the Victron
  sensors/text sensors, and stores every update.
- `app/alerts.py` - sends ntfy.sh push notifications when a reading
  crosses a configured threshold (low battery voltage), the charge
  controller reports an error, or the ESP32 stops reporting (offline),
  and again once each condition clears. See "Alerts" below.
- `app/settings.py` - the runtime-configurable settings backing the
  Settings tab (ntfy config, alert thresholds, target MAC, stall
  timeout). Falls back to env-var defaults from `config.py` until
  overridden via the web UI; overrides are persisted in the `settings` DB
  table and take effect immediately, no restart needed.
- `app/firmware.py` - backs the Firmware tab: reads/writes
  `esphome-victron-ble/secrets.yaml`, and runs `esphome compile`/`upload`
  as a subprocess with output streamed live to the browser over SSE. See
  "Firmware updates" below.
- `app/main.py` - FastAPI app: JSON API + the dashboard/settings pages.
- `app/static/app.html` - the whole UI: status cards, history charts, the
  Settings tab (ntfy/alerts/device config, plus a read-only view of the
  env-var-only infrastructure settings), and the Firmware tab (device
  credential form + compile/upload log panel). `/dashboard`, `/settings`,
  and `/firmware` all serve this one page; switching tabs is client-side
  (no full page reload) - see its `showTab()`.
- `db/migrations/001_init.sql` - TimescaleDB schema. `readings` holds one
  row per decoded sensor update: battery voltage/current, PV power,
  today's yield, load current, and the controller's device-state/error
  text.
- `db/migrations/002_settings.sql` - the `settings` table backing
  `app/settings.py`.
- `app/migrate.py` - applies any not-yet-applied file in `db/migrations/`
  on every startup (tracked in a `schema_migrations` table), so upgrading
  to a new migration is just a container restart - no manual `psql` step,
  even for deployments that already exist. Postgres's own
  `docker-entrypoint-initdb.d` (which `docker-compose.yml` also points at
  `db/migrations/`) only runs on a brand new volume; this is what covers
  everyone else.

## ESPHome setup

1. In the VictronConnect app, open the SmartSolar's **Settings -> Product
   info -> Instant readout details -> SHOW** to get its BLE MAC address
   and 32-hex-character bindkey.
2. Copy `esphome-victron-ble/secrets.yaml.example` to
   `esphome-victron-ble/secrets.yaml` and fill in WiFi/API/OTA secrets
   plus `victron_mac_address`/`victron_bindkey`.
3. Flash `victron-mppt.yaml` with `esphome run` (first flash over USB
   only - it has to be reachable on the network before OTA is possible).
   It declares
   `external_components: source: github://Fabian-Schmidt/esphome-victron_ble`,
   so ESPHome pulls the component automatically at build time - no local
   checkout needed.

Every flash after that first one - new firmware, WiFi/API/OTA credential
changes, or a different Victron MAC/bindkey - can be done over the air
from the dashboard's **Firmware** tab instead of the command line; see
"Firmware updates" below.

## Firmware updates (web UI)

The **Firmware** tab (`/firmware`) edits `esphome-victron-ble/secrets.yaml`
directly and runs the real `esphome` CLI (`esphome compile` then
`esphome upload`) as a subprocess from within the `app` container - see
`app/firmware.py`. No separate ESPHome dashboard/device-builder container
is involved:

- **Device credentials** panel: WiFi SSID/password, static IP/gateway/
  subnet, API encryption key, OTA password, and the SmartSolar's
  `victron_mac_address`/`victron_bindkey`. Saving rewrites
  `esphome-victron-ble/secrets.yaml` (any comments in that file are lost
  the first time it's saved from here - only the values matter to
  ESPHome).
- **Compile & upload** button: runs `esphome compile victron-mppt.yaml`
  then `esphome upload victron-mppt.yaml --device $ESPHOME_HOST`,
  streaming combined stdout/stderr live into the log panel below it over
  Server-Sent Events. Only one build can run at a time; reloading the
  page while one's in progress reconnects to the same live log instead of
  starting another.

This was chosen over embedding the official ESPHome dashboard/device
builder (which was prototyped first): that image is ~1.4GB and would run
as a second always-on container, plus needed an nginx sidecar just to
reskin its SPA to match this app's theme - not worth it for a feature
used only occasionally against one already-known device.

One cost doesn't go away either way: ESPHome's `esp-idf` framework needs
to download a large (~1GB) PlatformIO/ESP-IDF toolchain on the very first
compile, which needs internet access and takes a while. That download is
cached in the `firmware-cache` volume (see `PLATFORMIO_CORE_DIR` /
`ESPHOME_ESP_IDF_PREFIX` in `docker-compose.yml`), so every build after
the first is much faster. The first-ever flash of a blank ESP32 still
needs a USB cable regardless (it has to be reachable on the network
before OTA is possible) - see "ESPHome setup" above.

## Running

```bash
cp docker-compose.yml docker-compose.override.yml   # optional, or just edit in place
docker compose up -d --build
```

Before starting, edit the `app` service's environment in
`docker-compose.yml`:

- `API_TOKEN` / the `timescaledb` password - pick real secrets. These,
  plus `ESPHOME_HOST`/`ESPHOME_API_ENCRYPTION_KEY`/`DATABASE_URL`, are
  infrastructure config and stay environment-only (see "Settings" below)
  - getting one of these wrong via a web form could break connectivity or
    lock you out, so they require editing `docker-compose.yml` and
    restarting.
- `ESPHOME_HOST` - IP of the ESP32 running ESPHome (matches its
  `static_ip` in `esphome-victron-ble/victron-mppt.yaml`).
- `ESPHOME_API_ENCRYPTION_KEY` - must match that device's
  `api.encryption.key` (see `esphome-victron-ble/secrets.yaml`).
- `TARGET_BLE_MAC` - the SmartSolar's BLE MAC (label only - see
  "ESPHome setup" above for where the actual decode key lives); can also
  be changed later from the Settings tab.

Then open `http://<host>:8081/` (redirects to `/dashboard`, token is
injected server-side). Use the tabs at the top to switch between the
dashboard, Settings, and Firmware.

> **Note:** this project's schema changed completely when it moved from
> an earlier EcoWorthy prototype to Victron support. If you have an
> existing deployment with data under the old schema, you'll need to
> recreate the database volume (`docker compose down -v`) before
> starting - there's no migration path between the two.

## Settings

The **Settings** tab (`/settings`) exposes every runtime-tunable option
as a web form - changes are saved to the database and take effect on the
next reading/reconnect, no restart required:

- **Device** - the target MAC (label), the stall timeout (how long
  without a sensor update before the device is considered offline - see
  `app/ble_poller.py`), and telemetry retention.
- **Alerts (ntfy)** - everything described below.

### Telemetry retention

`telemetry_retention_days` (default **0 = keep forever**, editable from
the Settings tab's Device group) is applied as a TimescaleDB retention
policy on the `readings` hypertable both at startup and immediately
whenever it's changed (`app/db.py`'s `ensure_retention_policy`) - no
restart needed. Matches heltec-wifi-optimization's
`telemetry_retention_days` default/semantics. Setting it to a positive
number uses `if_not_exists => true`, so it's safe to save repeatedly -
but that also means changing a *nonzero* value on a deployment that
already has the policy won't take effect on its own; remove the old one
by hand first:
`docker compose exec timescaledb psql -U victron -d victron -c "SELECT remove_retention_policy('readings');"`
then save the new value from the Settings tab again. Setting it *back to
0*, though, is handled automatically - the app removes any existing
policy whenever the value is 0.

Each field falls back to its `docker-compose.yml` env var as a default
(shown as the input's placeholder) until you save an override; clearing a
field reverts it to that default. A read-only **Environment** section at
the bottom shows the env-var-only infrastructure settings (ESPHome
host/port, whether the encryption key/API token are set) for reference -
those aren't editable here, see "Running" above.

## Alerts

`app/alerts.py` can push notifications to a phone/desktop via
[ntfy.sh](https://ntfy.sh) (or a self-hosted ntfy instance) when a reading
crosses a threshold, and again once it clears. Configure everything below
from the **Settings** tab (or via the matching env vars as initial
defaults - see `docker-compose.yml`):

- **ntfy topic** - **required to enable alerting at all**; left blank,
  nothing is sent. Pick something hard to guess (anyone who knows the
  topic name can subscribe to it on a public ntfy.sh server), or use a
  private/self-hosted instance.
- **ntfy server URL** - defaults to `https://ntfy.sh`; point at your own
  instance if you're self-hosting.
- **ntfy auth token** - only needed if the topic requires auth.
- **Low battery voltage** - threshold in volts; optional, leave blank to
  skip that check. There's no sane default since it depends on the
  pack's nominal voltage (12V/24V/48V).
- **Alert cooldown** - minimum time between repeat notifications for the
  same still-ongoing condition (default 1800s = 30min), so one bad
  reading doesn't turn into a notification storm.

Two more conditions are always alerted on when ntfy is configured, no
threshold needed:

- **Charger error** - fires whenever the SmartSolar's `charger_error`
  text sensor reports anything other than "No error".
- **Device offline** - fires when no sensor update has arrived from the
  ESP32 within the stall timeout (BLE out of range, or the ESP32 itself
  unreachable), and clears once updates resume.

Use the **Send test notification** button on the Settings page to verify
your ntfy setup. Subscribe to the topic in the
[ntfy app](https://ntfy.sh/#subscribe) or at
`https://ntfy.sh/<your-topic>` in a browser to receive the pushes.

## Network context

The ESP32 lives at the hobocamp on VLAN 2 (internet access only, cannot
reach the server's VLAN). The server must always be the one to initiate
the connection to the ESP32 over the native ESPHome API - the device only
ever responds to connections the server opens toward it, satisfying that
directionality requirement.

## History

This repository originally targeted an EcoWorthy BW0F solar/battery
device whose BLE telemetry protocol was never fully reverse-engineered
(a hand-rolled ESP32 BLE proxy fed raw, undecoded payloads to the
server). That device has been abandoned in favor of a Victron SmartSolar
MPPT controller, whose BLE "Instant Readout" protocol is already decoded
by the community-maintained
[esphome-victron_ble](https://github.com/Fabian-Schmidt/esphome-victron_ble)
component - so decoding now happens on the ESP32 itself, and the server
only ever deals with named, typed sensor values. Before that, this
repository contained a hand-written PlatformIO/Arduino firmware for the
ESP32-C3; both the Arduino firmware's and the EcoWorthy-era code's git
history remain available if ever needed for reference.
