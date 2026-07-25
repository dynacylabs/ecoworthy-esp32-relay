# server

Dashboard + collector for the EcoWorthy BW0F, pulled over BLE via an
ESP32 running ESPHome's `bluetooth_proxy` (see `../esphome-bluetooth-proxy/`).

```
EcoWorthy BW0F (BLE) -> ESP32 (ESPHome, native API :6053) -> this server (pulls, decodes, stores, displays)
```

The server always initiates the connection to the ESP32's native API, and
from there to the BLE device. The device side never has to reach out
anywhere - this is required by the VLAN/firewall split between the
hobocamp (internet-only VLAN) and wherever this server runs.

## Layout

- `app/ble_poller.py` - connects out to the ESPHome device, connects to
  the BW0F over it, subscribes to its notify characteristics, stores
  every payload.
- `app/decode.py` - turns a raw BLE payload into named fields (battery
  voltage, PV power, etc). **Currently a stub** - the BW0F's protocol
  hasn't been reverse-engineered yet, so every reading comes back with
  all fields `None`. See the module docstring for what's been confirmed
  so far and what's needed to finish it.
- `app/main.py` - FastAPI app: JSON API + the dashboard page.
- `app/static/dashboard.html` - the dashboard itself (status cards,
  history charts, raw event log).
- `db/migrations/001_init.sql` - TimescaleDB schema. `raw_events` is the
  source of truth (every payload, undecoded, so nothing is ever lost
  while `decode.py` is incomplete); `readings` holds decoded values once
  `decode.py` can produce them.

## Running

```bash
cp docker-compose.yml docker-compose.override.yml   # optional, or just edit in place
docker compose up -d --build
```

Before starting, edit the `app` service's environment in
`docker-compose.yml`:

- `API_TOKEN` / the `timescaledb` password - pick real secrets.
- `ESPHOME_HOST` - IP of the ESP32 running ESPHome (matches its
  `static_ip` in `../esphome-bluetooth-proxy/*.yaml`).
- `ESPHOME_API_ENCRYPTION_KEY` - must match that device's
  `api.encryption.key` (see `../esphome-bluetooth-proxy/secrets.yaml`).
- `TARGET_BLE_MAC` - defaults to the known BW0F MAC (`e8:ca:50:42:16:c2`).

Then open `http://<host>:8080/` (redirects to `/dashboard`, token is
injected server-side).

## Status

The pipeline (ESPHome proxy -> poller -> TimescaleDB -> dashboard) is
built end-to-end, but the BW0F's telemetry protocol is not yet decoded -
see `app/decode.py`. Until it is, the dashboard's charts will be flat and
the "Raw Events" table is the only place to see real data. That table is
also the intended tool for reverse-engineering the protocol: capture
payloads while a known value changes (e.g. the EcoWorthy app's displayed
pack voltage) and diff against a baseline.
