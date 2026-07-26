import os

DATABASE_URL = os.environ["DATABASE_URL"]

# Shared secret required on the dashboard/API routes (as a ?token= query
# param, injected server-side into the dashboard HTML - see main.py).
API_TOKEN = os.environ["API_TOKEN"]

# The ESP32 running ESPHome's bluetooth_proxy. The server always initiates
# this connection (native API, default port 6053) - the device never
# reaches out on its own, which is what makes this work across the VLAN
# separation between the hobocamp and wherever this server runs.
ESPHOME_HOST = os.environ["ESPHOME_HOST"]
ESPHOME_PORT = int(os.environ.get("ESPHOME_PORT", "6053"))
ESPHOME_API_ENCRYPTION_KEY = os.environ["ESPHOME_API_ENCRYPTION_KEY"]

# The BW0F's BLE MAC, confirmed live: e8:ca:50:42:16:c2. Server-side
# config, not firmware - see README for why (any number of target devices
# can be added here without ever touching the ESP32).
TARGET_BLE_MAC = os.environ["TARGET_BLE_MAC"]

# How long to wait for a BLE connection/service-discovery attempt before
# giving up and retrying. The BW0F is usually quick once in range, but the
# proxy's own connection setup can occasionally take a while over a
# marginal link.
BLE_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("BLE_CONNECT_TIMEOUT_SECONDS", "20"))

# If nothing's been received from the device in this long while connected,
# assume the connection is stale (silently dead, not properly
# disconnected) and reconnect rather than waiting forever.
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


# Alert thresholds. Each is optional - leave unset (blank/absent) to skip
# that check entirely, since "low" depends on the battery pack's nominal
# voltage (12V/24V/48V) which varies by installation.
ALERT_LOW_VOLTAGE_V = _float_env("ALERT_LOW_VOLTAGE_V")
ALERT_LOW_SOC_PCT = _float_env("ALERT_LOW_SOC_PCT")
ALERT_HIGH_TEMP_C = _float_env("ALERT_HIGH_TEMP_C")

# Minimum time between repeat notifications for the same still-ongoing
# alert, so one bad-but-persistent reading doesn't turn into a
# notification storm (readings can arrive every second or so).
ALERT_COOLDOWN_SECONDS = float(os.environ.get("ALERT_COOLDOWN_SECONDS", "1800"))
