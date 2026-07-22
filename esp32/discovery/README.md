# ESP32 BLE Discovery Tool (EcoWorthy)

This tool runs on an ESP32 and discovers what BLE data your EcoWorthy device exposes.

By default, it scans all BLE MAC addresses in range, so you do not need to know the target MAC ahead of time.

It does three things:

1. Scans BLE advertisements for all devices in range.
2. Connects and enumerates GATT services and characteristics.
3. Prints a compatibility verdict for ESPHome:
   - YES (HIGH CONFIDENCE)
   - YES (CUSTOM/GENERIC)
   - MAYBE
   - UNLIKELY
   - UNKNOWN

## Files

- `ecoworthy_ble_discovery.ino`: Arduino sketch for ESP32.

## Configure target

Edit these constants in the sketch before flashing:

- `SCAN_ALL_DEVICES`: keep `true` to discover every BLE MAC in range.
- `TARGET_MAC`: optional target BLE MAC override.
- `TARGET_NAME_SUBSTRING`: partial BLE name to match if MAC is not known.
- `SCAN_SECONDS`: scan time.

Selection behavior:

- In all-device mode, the tool prints every unique BLE MAC it sees.
- It then auto-selects one device for full GATT probing using this priority:
   - explicit `TARGET_MAC` match (if set)
   - `TARGET_NAME_SUBSTRING` match (if set)
   - EcoWorthy/BMS name hints
   - JBD-like service UUID hints
   - strongest RSSI fallback

## Run

1. Open the sketch in Arduino IDE or PlatformIO.
2. Select your ESP32 board and serial port.
3. Flash the firmware.
4. Open serial monitor at `115200` baud.
5. Wait for:
   - advertisement dump
   - service and characteristic dump
   - final compatibility verdict

## How to use the verdict

- `YES (HIGH CONFIDENCE)`: usually a direct fit for known ESPHome BLE integrations.
- `YES (CUSTOM/GENERIC)`: ESPHome is still viable via `ble_client` and custom parsing.
- `MAYBE`: device likely needs pairing/auth or vendor-specific sequence.
- `UNLIKELY`: ESPHome probably not a good direct target for BLE telemetry.
- `UNKNOWN`: discovery failed or was incomplete; repeat with better signal and longer scan.

## Notes

- This tool does not modify your EcoWorthy device.
- Some devices only expose full data after proprietary auth; in that case output may be partial.
- If you share serial output, we can produce an exact ESPHome integration profile from it.
