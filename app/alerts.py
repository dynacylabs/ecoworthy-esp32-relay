"""ntfy.sh push notifications for battery/charge-controller alert conditions.

Called from ble_poller.py right after each reading is stored
(check_reading) and around ESPHome/BLE connection loss (check_offline).
Disabled entirely unless the ntfy_topic setting is non-blank.

All thresholds and ntfy connection details are read fresh from
settings.current() on every call (not imported once at startup), so
changes made on the Settings page take effect on the very next reading -
no restart required.

Each alert condition (low voltage, charger error, device offline) is
tracked independently per device, with:

- A cooldown, so a persistently-bad reading doesn't turn into a
  notification storm - only one push per condition per alert_cooldown_seconds
  while it stays active.
- Hysteresis on the numeric threshold, so a value oscillating right at
  the threshold doesn't fire/clear/fire repeatedly.
- A "resolved" push once the condition clears, so you know when things
  are back to normal without having to check the dashboard.
"""

import logging
import time

import httpx

import settings

logger = logging.getLogger("victron.alerts")

_VOLTAGE_HYSTERESIS_V = 0.2

# ntfy alerts are ANY charger_error value that isn't one of these -
# "No error" is what the ESPHome component reports during normal
# operation, empty/None means no reading has arrived yet.
_NO_ERROR_VALUES = {None, "", "No error"}

# One entry per (mac, alert_key): {"active": bool, "last_sent": monotonic}
_state: dict[tuple[str, str], dict] = {}


def _enabled() -> bool:
    return bool(settings.current()["ntfy_topic"])


async def _send(title: str, message: str, priority: str = "default", tags: str = "") -> None:
    if not _enabled():
        return
    current = settings.current()
    url = f"{current['ntfy_url'].rstrip('/')}/{current['ntfy_topic']}"
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if current["ntfy_auth_token"]:
        headers["Authorization"] = f"Bearer {current['ntfy_auth_token']}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, content=message.encode("utf-8"), headers=headers)
    except Exception:
        logger.exception("Failed to send ntfy notification")


async def send_test_notification() -> bool:
    """Used by the Settings page's "Send test notification" button. Returns
    False without sending anything if ntfy isn't configured."""
    if not _enabled():
        return False
    await _send(
        "\u2705 Test notification", "ntfy alerts are configured correctly.",
        priority="default", tags="white_check_mark",
    )
    return True


async def _fire(
    mac: str,
    key: str,
    active: bool,
    title: str,
    message: str,
    resolved_message: str,
    priority: str,
    tags: str,
) -> None:
    if not _enabled():
        return

    state_key = (mac, key)
    state = _state.setdefault(state_key, {"active": False, "last_sent": 0.0})
    now = time.monotonic()

    if active:
        cooldown_elapsed = (now - state["last_sent"]) > settings.current()["alert_cooldown_seconds"]
        if not state["active"] or cooldown_elapsed:
            await _send(title, message, priority=priority, tags=tags)
            state["last_sent"] = now
        state["active"] = True
    else:
        if state["active"]:
            await _send(f"{title} - resolved", resolved_message, priority="default", tags="white_check_mark")
        state["active"] = False
        state["last_sent"] = 0.0


async def check_reading(mac: str, decoded: dict) -> None:
    """Run every threshold check against one stored reading."""
    current = settings.current()
    low_voltage = current["alert_low_voltage_v"]

    voltage = decoded.get("battery_voltage_v")
    charger_error = decoded.get("charger_error")

    if voltage is not None and low_voltage is not None:
        active = voltage < low_voltage
        cleared = voltage > low_voltage + _VOLTAGE_HYSTERESIS_V
        if active or cleared:
            await _fire(
                mac, "low_voltage", active,
                title=f"\u26a0\ufe0f Low battery voltage ({mac})",
                message=f"Battery voltage is {voltage:.2f}V (threshold {low_voltage:.2f}V).",
                resolved_message=f"Battery voltage is back up to {voltage:.2f}V.",
                priority="high", tags="battery,warning",
            )

    if charger_error is not None:
        active = charger_error not in _NO_ERROR_VALUES
        await _fire(
            mac, "charger_error", active,
            title=f"\u26a0\ufe0f Charge controller error ({mac})",
            message=f"Charger error: {charger_error}.",
            resolved_message="Charger error has cleared - back to normal operation.",
            priority="high", tags="warning",
        )


async def check_offline(mac: str, offline: bool) -> None:
    """Alert when the ESP32 stops reporting sensor updates (out of BLE
    range, or the ESP32 itself unreachable), and when it recovers."""
    await _fire(
        mac, "offline", offline,
        title=f"\U0001f50c Device offline ({mac})",
        message="No data received from the ESP32 - BLE out of range, or the ESP32 is unreachable.",
        resolved_message="Data is flowing again.",
        priority="high", tags="satellite,warning",
    )
