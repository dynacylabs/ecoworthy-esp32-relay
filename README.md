# EcoWorthy ESP32 Relay

This repository is the ESP32 side of a telemetry relay pipeline for a remote EcoWorthy solar charge controller environment.

## Project goal

Create a reliable data path:

EcoWorthy device -> ESP32 collector/cache -> Docker server -> Web interface

Current repository focus:

- Build and operate the ESP32 collector/cache.
- Discover what BLE telemetry the EcoWorthy hardware actually exposes.
- Serve cached values over HTTP for downstream polling.
- Support secure OTA updates over WiFi for remote deployments.

## Current status

Implemented now:

- ESPHome firmware config for BLE polling + cached HTTP-readable entities.
- Broad BMS-style field coverage in cache sensors and consolidated JSON output.
- OTA over WiFi with password, encrypted API transport, and fallback recovery AP.
- Standalone ESP32 BLE discovery tool to enumerate nearby devices and probe GATT.

Not implemented yet in this repository:

- Docker poller/server component.
- Web UI component.

## Repository layout

- [esp32/ecoworthy-relay.yaml](esp32/ecoworthy-relay.yaml): Main ESPHome firmware for runtime telemetry collection and HTTP serving.
- [esp32/secrets.example.yaml](esp32/secrets.example.yaml): Secrets template for WiFi, BLE target, OTA, and API encryption.
- [esp32/README.md](esp32/README.md): ESP32 firmware usage, endpoints, and OTA workflow.
- [esp32/discovery/ecoworthy_ble_discovery.ino](esp32/discovery/ecoworthy_ble_discovery.ino): BLE discovery/probing sketch for unknown MAC/protocol cases.
- [esp32/discovery/README.md](esp32/discovery/README.md): Discovery tool usage and interpretation guidance.

## Recommended operating flow

1. Run discovery first.
- Flash the discovery sketch to an ESP32 near the controller.
- Capture all discovered BLE MACs, service UUIDs, characteristic properties, and verdict.

2. Decide ESPHome integration path.
- If BLE layout looks JBD-compatible: use existing ESPHome relay config as primary path.
- If layout is generic/proprietary: adapt to ble_client/custom parsing or re-evaluate protocol path.

3. Deploy relay firmware.
- Fill secrets from template.
- Flash once locally.
- Validate HTTP endpoints and cache behavior.

4. Switch to OTA-only remote operations.
- Use secure private network access (VPN overlay) to reach device.
- Push updates via ESPHome OTA command.

## Data contract for downstream poller

Primary machine-friendly payload:

- text sensor endpoint for cache_json from the ESPHome web server.

Also available:

- Individual cache entities for pack voltage/current/SOC/capacity, temperatures, cell voltages, and metadata counters.

See detailed endpoint list in [esp32/README.md](esp32/README.md).

## Security and remote deployment notes

- OTA updates are password protected.
- ESPHome API is configured for encryption.
- Fallback AP and captive portal are enabled for local recovery when primary WiFi is unavailable.
- Do not expose ESPHome/API/OTA services directly to the public internet.
- Prefer private connectivity such as site-to-site VPN, Tailscale, or WireGuard.

## AI agent guidance

If you are an AI agent working in this repository:

1. Treat this repository as ESP32-first scope unless explicitly told to start Docker/web work.
2. Preserve existing telemetry field names under cache_* unless a migration is requested.
3. Favor additive changes and avoid removing existing endpoints used by downstream pollers.
4. Validate config edits with diagnostics before finishing.
5. If protocol compatibility is uncertain, run or extend the discovery path before restructuring firmware.

## Known uncertainty

The EcoWorthy hardware is a solar charge controller and may not expose full JBD-style telemetry in all firmware variants. The discovery tool is the source of truth for what is actually available on a given device.

## Next milestones

1. Lock a stable JSON schema for Docker ingestion based on real discovered telemetry.
2. Add Docker poller service in a new folder, with retries, timestamping, and health endpoint.
3. Add web interface consuming Docker-published data.
