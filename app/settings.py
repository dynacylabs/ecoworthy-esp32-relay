"""Runtime-configurable settings, editable from the web UI's Settings tab
and persisted in the `settings` DB table (see
db/migrations/002_settings.sql). Every key falls back to its env-var
default from config.py until a user overrides it via POST /api/settings;
clearing a value (blank string) removes the override and reverts to that
default.

The effective values are cached in memory after every load()/save(),
since ble_poller.py and alerts.py consult them on every single sensor
update from the ESP32 - a DB round trip per reading would be wasteful.
current() just reads that cache and is safe to call from a hot path.
"""

import logging

import config

logger = logging.getLogger("victron.settings")


class Field:
    __slots__ = ("key", "value_type", "env_default", "label", "help", "group", "secret")

    def __init__(self, key, value_type, env_default, label, help, group, secret=False):
        self.key = key
        self.value_type = value_type
        self.env_default = env_default
        self.label = label
        self.help = help
        self.group = group
        self.secret = secret


# Everything a human might reasonably want to tune without rebuilding the
# container. Deliberately excludes real infrastructure config (DB
# credentials, the ESPHome host/port/encryption key, the dashboard's own
# API_TOKEN) - those stay env-var-only, since changing them wrong can lock
# you out or break connectivity in ways a web form can't safely recover
# from. See /api/environment for a read-only view of those.
SCHEMA: list[Field] = [
    Field(
        "target_ble_mac", str, config.TARGET_BLE_MAC,
        "Target BLE MAC", "MAC address of the Victron SmartSolar being tracked (label only - the ESP32 does the actual BLE decoding).", "device",
    ),
    Field(
        "ble_stall_seconds", float, config.BLE_STALL_SECONDS,
        "BLE stall timeout (s)",
        "Fire the offline alert if no sensor update has arrived from the ESP32 in this long.", "device",
    ),
    Field(
        "ntfy_url", str, config.NTFY_URL,
        "ntfy server URL", "Use https://ntfy.sh, or your own self-hosted instance.", "alerts",
    ),
    Field(
        "ntfy_topic", str, config.NTFY_TOPIC,
        "ntfy topic",
        "Required to enable alerts at all - blank disables every alert below. Pick something hard to guess.",
        "alerts",
    ),
    Field(
        "ntfy_auth_token", str, config.NTFY_AUTH_TOKEN,
        "ntfy auth token", "Only needed if your topic requires authentication.", "alerts", secret=True,
    ),
    Field(
        "alert_low_voltage_v", float, config.ALERT_LOW_VOLTAGE_V,
        "Low battery voltage (V)",
        "Notify when battery voltage drops below this. Blank disables the check.", "alerts",
    ),
    Field(
        "alert_cooldown_seconds", float, config.ALERT_COOLDOWN_SECONDS,
        "Alert cooldown (s)",
        "Minimum time between repeat notifications for the same still-ongoing alert.", "alerts",
    ),
]

BY_KEY = {f.key: f for f in SCHEMA}

_cache: dict[str, object] = {f.key: f.env_default for f in SCHEMA}


def current() -> dict:
    """In-memory cached effective settings (env default, or DB override if
    one's been saved). Safe to call frequently/from a hot path."""
    return _cache


def _coerce(field: Field, raw: str):
    return float(raw) if field.value_type is float else raw


async def load(pool) -> dict:
    """(Re)load the cache from the DB. Call once at startup, and again
    after every save()."""
    global _cache
    rows = await pool.fetch("SELECT key, value FROM settings")
    stored = {r["key"]: r["value"] for r in rows}
    effective = {}
    for field in SCHEMA:
        raw = stored.get(field.key)
        effective[field.key] = _coerce(field, raw) if raw else field.env_default
    _cache = effective
    return effective


async def save(pool, updates: dict) -> dict:
    """Upsert the given {key: value} pairs. A value that's None or blank
    clears the override, reverting that key to its env default. Returns
    the freshly-reloaded effective settings."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in updates.items():
                field = BY_KEY.get(key)
                if field is None:
                    continue
                if value is None or (isinstance(value, str) and value.strip() == ""):
                    await conn.execute("DELETE FROM settings WHERE key = $1", key)
                    continue
                if field.value_type is float:
                    float(value)  # raises ValueError before we store garbage
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    key, str(value),
                )
    return await load(pool)


async def describe(pool) -> list[dict]:
    """Schema metadata + current effective value + override status, for
    the Settings page."""
    rows = await pool.fetch("SELECT key FROM settings")
    overridden = {r["key"] for r in rows}
    return [
        {
            "key": field.key,
            "label": field.label,
            "help": field.help,
            "group": field.group,
            "type": "float" if field.value_type is float else "str",
            "secret": field.secret,
            "value": _cache.get(field.key, field.env_default),
            "env_default": field.env_default,
            "is_override": field.key in overridden,
        }
        for field in SCHEMA
    ]
