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
  configs for the ESP32-C3. Just WiFi, the native API, OTA, and
  `bluetooth_proxy` - no custom code. The server always initiates the
  connection to the device (over the native API, port 6053); the device
  never has to reach out anywhere, which matters given the network's VLAN
  separation (see below).
- [server/](server/README.md): the actual dashboard. A background poller
  (`aioesphomeapi`) connects to the ESP32's proxy, connects to the BW0F by
  MAC, subscribes to its data characteristic, and stores every raw payload
  in TimescaleDB. A FastAPI app serves a themed dashboard (styling matches
  the [heltec-wifi-optimization](https://github.com/dynacylabs/heltec-wifi-optimization)
  project) with charts for battery/power state. Runs via Docker Compose.

## Current status

The BW0F's actual telemetry protocol isn't reverse-engineered yet - we
have raw packet captures and some structural observations (a trailing
checksum, a couple of fields that change over time) but no confirmed
mapping from bytes to real voltage/current/SOC values. The full pipeline
(ESP32 proxy -> poller -> DB -> dashboard) is built and wired up end to
end; the decode step (`server/app/decode.py`) is a clearly-marked stub
until the protocol is actually cracked, so charts exist but won't show
meaningful numbers until that's filled in.

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
