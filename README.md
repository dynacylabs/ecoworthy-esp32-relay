# ecoworthy-esp32-relay

ESP32-C3 firmware that connects to an EcoWorthy BW0F over BLE and relays
everything it sees to a laptop over IP in real time, as the first phase of
a hobocamp telemetry pipeline: `EcoWorthy BW0F -> ESP32-C3 -> IP (browser /
plain-text stream)`.

## Repository layout

- [discovery/](discovery/README.md): PlatformIO/Arduino firmware for the
  ESP32-C3. Scans for the BW0F over BLE, discovers its GATT services and
  characteristics, subscribes to every notify/indicate characteristic, and
  logs everything (hex + ASCII) to USB serial and over HTTP (live page +
  plain-text stream). Supports OTA updates over WiFi.

## Current status

This is phase 1: raw discovery and live logging, not yet protocol
decoding. The BW0F's actual telemetry format isn't reverse-engineered yet
— that's the point of this phase, capturing real traffic to work from.

Not implemented yet:

- Parsing/decoding the BW0F's actual protocol into named fields.
- Any server/database component beyond the ESP32 itself.
- A persistent web UI (the live log page is real-time only, no history).

## Deployment context

The ESP32-C3 lives at the hobocamp, at the back of the property, joined to
the property's hobocam WiFi network (the downstream 2.4GHz side of a HaLow
point-to-point bridge back to the house). See
[discovery/README.md](discovery/README.md) for network assumptions,
flashing instructions, and the OTA deployment workflow.

## Next milestones

1. Capture real BW0F traffic via the discovery firmware's log output.
2. Reverse-engineer the field layout from that traffic.
3. Build phase 2 firmware (or extend this one) to decode and expose named
   telemetry fields, and push it via OTA.
