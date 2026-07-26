# ecoworthy-dashboard

A dashboard for an EcoWorthy BW0F solar/battery device, read over BLE via
an ESP32 acting as a dumb Bluetooth-to-network relay:

```
EcoWorthy BW0F (BLE) -> ESP32-C3 (ESPHome bluetooth_proxy) -> server (pulls, decodes, stores, displays)
```

The ESP32 does no protocol interpretation at all - it just proxies raw
GATT operations (connect, discover services, read, subscribe to
notify/indicate) over ESPHome's native API. All the "brains" - deciding
what to connect to, what characteristics matter, and what the raw bytes
actually mean - live in the server. This keeps the device side minimal and
means fixing or extending the decoding logic never requires touching
firmware on hardware that's remote and inconvenient to reach.

## Repository layout

- [esphome-bluetooth-proxy/](esphome-bluetooth-proxy/bench.yaml): ESPHome
  configs for the ESP32-C3 - `bench.yaml` for the desk unit, `field.yaml`
  for the actual hobocamp deployment. Just WiFi, the native API, OTA, and
  `bluetooth_proxy` - no custom code. The server always initiates the
  connection to the device (over the native API, port 6053); the device
  never has to reach out anywhere, which matters given the network's VLAN
  separation (see below). `field.yaml`'s first flash has to happen over
  USB on site - the device is still running the old custom firmware with
  a different partition layout, so there's no OTA path until that first
  flash lands. Every flash after that (including on the bench unit) can
  go over OTA - confirmed working this session.
- [app/](app/main.py): the dashboard/collector server. A background
  poller (`aioesphomeapi`) connects to the ESP32's proxy, connects to the
  BW0F by MAC, subscribes to its data characteristic, and stores every
  raw payload in TimescaleDB. A FastAPI app serves a themed dashboard
  (styling matches the
  [heltec-wifi-optimization](https://github.com/dynacylabs/heltec-wifi-optimization)
  project) with charts for battery/power state.
- [db/migrations/](db/migrations/001_init.sql): TimescaleDB schema,
  applied automatically on startup (see `app/migrate.py`).
- [docker-compose.yml](docker-compose.yml): runs the `app` and
  `timescaledb` services - see "Running" below.

## Current status

The BW0F's actual telemetry protocol isn't reverse-engineered yet - we
have raw packet captures and some structural observations (a trailing
checksum, a couple of fields that change over time) but no confirmed
mapping from bytes to real voltage/current/SOC values. The full pipeline
(ESP32 proxy -> poller -> DB -> dashboard) is built and wired up end to
end; the decode step (`app/decode.py`) is a clearly-marked stub until the
protocol is actually cracked, so charts exist but won't show meaningful
numbers until that's filled in.

## Layout

- `app/ble_poller.py` - connects out to the ESPHome device, connects to
  the BW0F over it, subscribes to its notify characteristics, stores
  every payload.
- `app/decode.py` - turns a raw BLE payload into named fields (battery
  voltage, PV power, etc). **Currently a stub** - the BW0F's protocol
  hasn't been reverse-engineered yet, so every reading comes back with
  all fields `None`. See the module docstring for what's been confirmed
  so far and what's needed to finish it.
- `app/alerts.py` - sends ntfy.sh push notifications when a reading
  crosses a configured threshold (low voltage, low charge, high temp) or
  the BLE connection drops, and again once the condition clears. See
  "Alerts" below.
- `app/settings.py` - the runtime-configurable settings backing the
  Settings tab (ntfy config, alert thresholds, BLE timing/target MAC).
  Falls back to env-var defaults from `config.py` until overridden via
  the web UI; overrides are persisted in the `settings` DB table and take
  effect immediately, no restart needed.
- `app/main.py` - FastAPI app: JSON API + the dashboard/settings pages.
- `app/static/app.html` - the whole UI: status cards, history charts, raw
  event log, and the Settings tab (ntfy/alerts/BLE config, plus a
  read-only view of the env-var-only infrastructure settings). `/dashboard`
  and `/settings` both serve this one page; switching tabs is client-side
  (no full page reload) - see its `showTab()`.
- `db/migrations/001_init.sql` - TimescaleDB schema. `raw_events` is the
  source of truth (every payload, undecoded, so nothing is ever lost
  while `decode.py` is incomplete); `readings` holds decoded values once
  `decode.py` can produce them.
- `db/migrations/002_settings.sql` - the `settings` table backing
  `app/settings.py`.
- `app/migrate.py` - applies any not-yet-applied file in `db/migrations/`
  on every startup (tracked in a `schema_migrations` table), so upgrading
  to a new migration (like the one above) is just a container restart -
  no manual `psql` step, even for deployments that already exist.
  Postgres's own `docker-entrypoint-initdb.d` (which `docker-compose.yml`
  also points at `db/migrations/`) only runs on a brand new volume; this
  is what covers everyone else.

## Running

```bash
cp docker-compose.yml docker-compose.override.yml   # optional, or just edit in place
docker compose up -d --build
```

Before starting, edit the `app` service's environment in
`docker-compose.yml`:

- `API_TOKEN` / the `timescaledb` password - pick real secrets. These, plus
  `ESPHOME_HOST`/`ESPHOME_API_ENCRYPTION_KEY`/`DATABASE_URL`, are
  infrastructure config and stay environment-only (see "Settings" below)
  - getting one of these wrong via a web form could break connectivity or
    lock you out, so they require editing `docker-compose.yml` and
    restarting.
- `ESPHOME_HOST` - IP of the ESP32 running ESPHome (matches its
  `static_ip` in `esphome-bluetooth-proxy/*.yaml`).
- `ESPHOME_API_ENCRYPTION_KEY` - must match that device's
  `api.encryption.key` (see `esphome-bluetooth-proxy/secrets.yaml`).
- `TARGET_BLE_MAC` - defaults to the known BW0F MAC (`e8:ca:50:42:16:c2`);
  can also be changed later from the Settings tab.

Then open `http://<host>:8080/` (redirects to `/dashboard`, token is
injected server-side). Use the tabs at the top to switch between the
dashboard and Settings.

## Settings

The **Settings** tab (`/settings`) exposes every runtime-tunable option
as a web form - changes are saved to the database and take effect on the
next reading/reconnect, no restart required:

- **Device / BLE** - the target MAC, BLE connect timeout, and stall
  timeout (see `app/ble_poller.py`).
- **Alerts (ntfy)** - everything described below.

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
- **Low battery voltage / Low battery charge / High temperature** -
  thresholds for battery voltage (V), state of charge (%), and
  temperature (°C). Each is independently optional - leave blank to skip
  that check. There's no sane default for voltage since it depends on the
  pack's nominal voltage (12V/24V/48V).
- **Alert cooldown** - minimum time between repeat notifications for the
  same still-ongoing condition (default 1800s = 30min), so one bad
  reading doesn't turn into a notification storm.

A "device offline" alert also fires automatically (no configuration
needed beyond the ntfy topic) whenever the BLE connection to the BW0F is
lost or stalls (see the BLE stall timeout setting), and clears once it
reconnects.

Use the **Send test notification** button on the Settings page to verify
your ntfy setup. Subscribe to the topic in the
[ntfy app](https://ntfy.sh/#subscribe) or at
`https://ntfy.sh/<your-topic>` in a browser to receive the pushes.

## Server status

The pipeline (ESPHome proxy -> poller -> TimescaleDB -> dashboard) is
built end-to-end, but the BW0F's telemetry protocol is not yet decoded -
see `app/decode.py`. Until it is, the dashboard's charts will be flat and
the "Raw Events" table is the only place to see real data. That table is
also the intended tool for reverse-engineering the protocol: capture
payloads while a known value changes (e.g. the EcoWorthy app's displayed
pack voltage) and diff against a baseline.

## Network context

The ESP32 lives at the hobocamp on VLAN 2 (internet access only, cannot
reach the server's VLAN). The server must always be the one to initiate
the connection to the ESP32 - both the native ESPHome API and this
project's earlier hand-rolled HTTP endpoints satisfy that same
directionality requirement, since the device only ever responds to
connections the server opens toward it.

## History

This repository originally contained a hand-written PlatformIO/Arduino
firmware for the ESP32-C3 (BLE discovery, custom HTTP endpoints, custom
OTA). It's been replaced by ESPHome's `bluetooth_proxy`, which already
solves the same problems (OTA reliability, WiFi/BLE coexistence
handling, generic GATT client operations) with a much smaller,
better-tested surface area. The old firmware's git history remains
available if ever needed for reference.
