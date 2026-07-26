"""ntfy.sh push notifications for battery/charge-controller alert conditions.

Called from ble_poller.py right after each reading is decoded and stored
(check_reading) and around BLE connection loss (check_offline). Disabled
entirely unless the ntfy_topic setting is non-blank.

All thresholds and ntfy connection details are read fresh from
settings.current() on every call (not imported once at startup), so
changes made on the Settings page take effect on the very next reading -
no restart required.

Each alert condition (low voltage, low charge, high temp, device offline)
is tracked independently per device, with:

- A cooldown, so a persistently-bad reading doesn't turn into a
  notification storm - only one push per condition per alert_cooldown_seconds
  while it stays active.
- Hysteresis on the numeric thresholds, so a value oscillating right at
  the threshold doesn't fire/clear/fire repeatedly.
- A "resolved" push once the condition clears, so you know when things
  are back to normal without having to check the dashboard.
"""

import logging
import time

import httpx

import settings

logger = logging.getLogger("ecoworthy.alerts")

_VOLTAGE_HYSTERESIS_V = 0.2
_SOC_HYSTERESIS_PCT = 3.0
_TEMP_HYSTERESIS_C = 3.0

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
    """Run every threshold check against one decoded reading."""
    current = settings.current()
    low_voltage = current["alert_low_voltage_v"]
    low_soc = current["alert_low_soc_pct"]
    high_temp = current["alert_high_temp_c"]

    voltage = decoded.get("battery_voltage_v")
    soc = decoded.get("battery_soc_pct")
    temp = decoded.get("temperature_c")

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

    if soc is not None and low_soc is not None:
        active = soc < low_soc
        cleared = soc > low_soc + _SOC_HYSTERESIS_PCT
        if active or cleared:
            await _fire(
                mac, "low_soc", active,
                title=f"\u26a0\ufe0f Low battery charge ({mac})",
                message=f"Battery charge is {soc:.0f}% (threshold {low_soc:.0f}%).",
                resolved_message=f"Battery charge is back up to {soc:.0f}%.",
                priority="high", tags="battery,warning",
            )

    if temp is not None and high_temp is not None:
        active = temp > high_temp
        cleared = temp < high_temp - _TEMP_HYSTERESIS_C
        if active or cleared:
            await _fire(
                mac, "high_temp", active,
                title=f"\U0001f321\ufe0f High temperature ({mac})",
                message=f"Temperature is {temp:.1f}\u00b0C (threshold {high_temp:.1f}\u00b0C).",
                resolved_message=f"Temperature is back down to {temp:.1f}\u00b0C.",
                priority="high", tags="thermometer,warning",
            )


async def check_offline(mac: str, offline: bool) -> None:
    """Alert when the BLE connection is lost/stalled, and when it recovers."""
    await _fire(
        mac, "offline", offline,
        title=f"\U0001f50c Device offline ({mac})",
        message="No data received over BLE - connection lost or out of range.",
        resolved_message="BLE connection re-established, data is flowing again.",
        priority="high", tags="satellite,warning",
    )
