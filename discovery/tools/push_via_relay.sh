#!/usr/bin/env bash
# Push firmware to the deployed ESP32 by relaying through mac-mini over
# Tailscale. Use this instead of push_update.py directly when this machine
# can't reach the device's LAN on its own (e.g. on a cellular hotspot in
# the field, while mac-mini sits on the home LAN with a route to it).
set -euo pipefail

FIRMWARE="${1:?Usage: push_via_relay.sh <firmware.bin> <OTA_PASSWORD>}"
TOKEN="${2:?Usage: push_via_relay.sh <firmware.bin> <OTA_PASSWORD>}"
RELAY_HOST="${RELAY_HOST:-mac-mini}"
RELAY_USER="${RELAY_USER:-austinc}"
DEVICE_HOST="${DEVICE_HOST:-192.168.2.4}"

REMOTE_TMP="/tmp/$(basename "$FIRMWARE")"

echo "Copying $FIRMWARE to ${RELAY_HOST}:${REMOTE_TMP} ..."
scp "$FIRMWARE" "${RELAY_USER}@${RELAY_HOST}:${REMOTE_TMP}"

echo "Syncing tools on ${RELAY_HOST} and pushing to ${DEVICE_HOST} ..."
ssh "${RELAY_USER}@${RELAY_HOST}" \
  "git -C ~/ecoworthy-esp32-relay pull --quiet && \
   python3 ~/ecoworthy-esp32-relay/discovery/tools/push_update.py '$REMOTE_TMP' --host '$DEVICE_HOST' --token '$TOKEN'"
