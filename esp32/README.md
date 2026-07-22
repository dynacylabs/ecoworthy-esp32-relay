# ESP32 EcoWorthy Relay Component

This folder contains the ESP32 firmware configuration for the first stage in the flow:

EcoWorthy device -> ESP32 -> Docker server -> Web page

## What this firmware does

- Connects to an EcoWorthy battery BMS over BLE using the JBD protocol component.
- Polls the BMS every 5 seconds.
- Caches a broad BMS field set (pack, SOC, capacity, min/max/avg cell voltage, cell 1-8, temperature 1-5, status text) so stale network periods still expose the latest known values.
- Serves data over HTTP using ESPHome web server.

## Files

- `ecoworthy-relay.yaml`: Main ESPHome firmware config.
- `secrets.example.yaml`: Copy to `secrets.yaml` and fill your real values.

## OTA updates over WiFi

This firmware is configured for OTA updates using ESPHome with:

- OTA password protection.
- API transport encryption.
- Fallback recovery AP and captive portal if primary WiFi fails.

### Required secrets

Add these to `secrets.yaml`:

- `ota_password`
- `api_encryption_key` (32-byte base64 key)
- `fallback_ap_ssid`
- `fallback_ap_password`

Generate a strong API key with:

```bash
openssl rand -base64 32
```

### Typical OTA update command

```bash
esphome run esp32/ecoworthy-relay.yaml --device <esp32-ip>
```

If the binary is already built, upload only:

```bash
esphome upload esp32/ecoworthy-relay.yaml --device <esp32-ip>
```

### Remote-site recommendation

For a remote install, keep the ESP32 reachable only through a private network path (site-to-site VPN, Tailscale, or WireGuard). Avoid exposing OTA or API ports directly to the public internet.

### Recovery behavior

If the ESP32 cannot join its configured WiFi, it brings up the fallback AP (`fallback_ap_ssid`). You can connect locally to recover network settings and restore OTA reachability.

## HTTP surface for the Docker poller

After flashing, open the ESP32 web server at:

- `http://<esp32-ip>/`

For machine polling, use web server entity endpoints such as:

- `http://<esp32-ip>/sensor/cache_battery_strings`
- `http://<esp32-ip>/sensor/cache_total_voltage`
- `http://<esp32-ip>/sensor/cache_current`
- `http://<esp32-ip>/sensor/cache_soc`
- `http://<esp32-ip>/sensor/cache_capacity_remaining`
- `http://<esp32-ip>/sensor/cache_nominal_capacity`
- `http://<esp32-ip>/sensor/cache_min_cell_voltage`
- `http://<esp32-ip>/sensor/cache_max_cell_voltage`
- `http://<esp32-ip>/sensor/cache_avg_cell_voltage`
- `http://<esp32-ip>/sensor/cache_temp_1`
- `http://<esp32-ip>/sensor/cache_temp_2`
- `http://<esp32-ip>/sensor/cache_temp_3`
- `http://<esp32-ip>/sensor/cache_temp_4`
- `http://<esp32-ip>/sensor/cache_temp_5`
- `http://<esp32-ip>/sensor/cache_cell_voltage_1`
- `http://<esp32-ip>/sensor/cache_cell_voltage_2`
- `http://<esp32-ip>/sensor/cache_cell_voltage_3`
- `http://<esp32-ip>/sensor/cache_cell_voltage_4`
- `http://<esp32-ip>/sensor/cache_cell_voltage_5`
- `http://<esp32-ip>/sensor/cache_cell_voltage_6`
- `http://<esp32-ip>/sensor/cache_cell_voltage_7`
- `http://<esp32-ip>/sensor/cache_cell_voltage_8`
- `http://<esp32-ip>/sensor/cache_successful_polls`
- `http://<esp32-ip>/sensor/cache_last_update_epoch`
- `http://<esp32-ip>/text_sensor/cache_device_model`
- `http://<esp32-ip>/text_sensor/cache_operation_status`
- `http://<esp32-ip>/text_sensor/cache_json`

The `cache_json` text sensor gives one compact JSON payload that includes all cached numeric fields above, plus `device_model` and `operation_status`.

## Bring-up steps

1. Install ESPHome locally or use the ESPHome dashboard.
2. Copy `secrets.example.yaml` to `secrets.yaml` and set WiFi, BMS BLE MAC, OTA password, API key, and fallback AP values.
3. Flash `ecoworthy-relay.yaml` to your ESP32.
4. Verify values appear on the web endpoint.

## Notes

- If BLE connection does not come up, confirm the BMS BLE MAC with a BLE scanner app.
- WiFi power-save is disabled and reboot timeout is disabled for better link stability.
