import os

DATABASE_URL = os.environ["DATABASE_URL"]

# Shared secret required on the dashboard/API routes (as a ?token= query
# param, injected server-side into the dashboard HTML - see main.py).
API_TOKEN = os.environ["API_TOKEN"]

# The ESP32 running the Fabian-Schmidt/esphome-victron_ble component (see
# esphome-victron-ble/). It does its own BLE scanning/decryption on-device
# and exposes the decoded values as plain ESPHome sensors; the server just
# subscribes to those over the native API. The server always initiates
# this connection (default port 6053) - the device never reaches out on
# its own, which is what makes this work across the VLAN separation
# between the hobocamp and wherever this server runs.
ESPHOME_HOST = os.environ["ESPHOME_HOST"]
ESPHOME_PORT = int(os.environ.get("ESPHOME_PORT", "6053"))
ESPHOME_API_ENCRYPTION_KEY = os.environ["ESPHOME_API_ENCRYPTION_KEY"]

# The Victron SmartSolar's BLE MAC. Purely a label used to tag stored
# readings/alerts here - the actual BLE MAC/bindkey pairing that decrypts
# its advertisements lives on the ESP32 (esphome-victron-ble/*.yaml),
# since decoding happens there now, not on the server.
TARGET_BLE_MAC = os.environ["TARGET_BLE_MAC"]

# If no sensor update has arrived from the ESP32 in this long, assume the
# SmartSolar is out of range/offline (or the ESP32 itself is unreachable)
# and fire the offline alert rather than waiting forever.
BLE_STALL_SECONDS = float(os.environ.get("BLE_STALL_SECONDS", "60"))

# --- ntfy.sh push notifications (see app/alerts.py) --------------------
#
# Alerting is disabled entirely unless NTFY_TOPIC is set. Point NTFY_URL
# at self-hosted ntfy instead of ntfy.sh if you're running your own, and
# set NTFY_AUTH_TOKEN if that topic requires auth.
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_AUTH_TOKEN = os.environ.get("NTFY_AUTH_TOKEN", "")


def _float_env(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


# Low battery voltage alert threshold. Optional - leave unset (blank/absent)
# to skip that check entirely, since "low" depends on the battery pack's
# nominal voltage (12V/24V/48V) which varies by installation. The
# SmartSolar's charger_error text ("No error" vs anything else) is always
# alerted on when ntfy is configured - no threshold needed for that one.
ALERT_LOW_VOLTAGE_V = _float_env("ALERT_LOW_VOLTAGE_V")

# Minimum time between repeat notifications for the same still-ongoing
# alert, so one bad-but-persistent reading doesn't turn into a
# notification storm (readings can arrive every second or so).
ALERT_COOLDOWN_SECONDS = float(os.environ.get("ALERT_COOLDOWN_SECONDS", "1800"))
