"""Background task: connects to the ESP32's native ESPHome API and
subscribes to the sensor/text_sensor states exposed by its
Fabian-Schmidt/esphome-victron_ble component (see
esphome-victron-ble/*.yaml), storing every update.

Unlike the earlier EcoWorthy setup, all BLE work (scanning for the
SmartSolar's encrypted "Instant Readout" advertisements, AES-128-CTR
decryption, field decoding) happens on the ESP32 itself, inside that
component - not here. This server only ever talks to the ESP32's native
API and never touches BLE directly, which is what makes this work across
the VLAN separation between the hobocamp and wherever this server runs
(the server always initiates the connection to the ESP32, never the
other way around).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from aioesphomeapi import APIClient, EntityState

import alerts
import settings
from config import ESPHOME_API_ENCRYPTION_KEY, ESPHOME_HOST, ESPHOME_PORT
from db import get_or_create_device, get_pool

logger = logging.getLogger("victron.ble_poller")

# Maps the ESPHome-derived object_id of each sensor/text_sensor (name,
# lowercased with spaces turned into underscores) to the DB column it
# feeds. Must be kept in sync with the `name:` given to each entity in
# esphome-victron-ble/*.yaml.
NUMERIC_FIELDS = {
    "battery_voltage": "battery_voltage_v",
    "battery_current": "battery_current_a",
    "pv_power": "pv_power_w",
    "yield_today": "yield_today_kwh",
    "load_current": "load_current_a",
}
TEXT_FIELDS = {
    "device_state": "device_state",
    "charger_error": "charger_error",
}
ALL_FIELDS = [*NUMERIC_FIELDS.values(), *TEXT_FIELDS.values()]


class BLEPoller:
    def __init__(self):
        self._client: APIClient | None = None
        self._last_activity = 0.0
        self._stopping = False
        self._latest: dict[str, float | str | None] = {}

    async def run_forever(self):
        while not self._stopping:
            try:
                await self._run_once()
            except Exception:
                logger.exception("ESPHome poller iteration failed, retrying in 10s")
            if not self._stopping:
                await asyncio.sleep(10)

    async def stop(self):
        self._stopping = True
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    async def _run_once(self):
        client = APIClient(
            ESPHOME_HOST, ESPHOME_PORT, password="",
            noise_psk=ESPHOME_API_ENCRYPTION_KEY,
        )
        self._client = client
        await client.connect(login=True)
        logger.info("Connected to ESPHome device at %s:%s", ESPHOME_HOST, ESPHOME_PORT)
        try:
            await self._stream_states(client)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _stream_states(self, client: APIClient):
        # Read fresh at the start of every connection attempt (rather than
        # once at import time) so changes made on the Settings page take
        # effect on the very next reconnect, no restart required.
        current = settings.current()
        target_mac: str = current["target_ble_mac"]
        stall_seconds: float = current["ble_stall_seconds"]

        entities, _services = await client.list_entities_services()
        key_to_field: dict[int, tuple[str, str]] = {}
        for entity in entities:
            if entity.object_id in NUMERIC_FIELDS:
                key_to_field[entity.key] = ("numeric", NUMERIC_FIELDS[entity.object_id])
            elif entity.object_id in TEXT_FIELDS:
                key_to_field[entity.key] = ("text", TEXT_FIELDS[entity.object_id])

        if not key_to_field:
            logger.warning(
                "No matching victron_ble sensors found on the ESPHome device - "
                "check the sensor/text_sensor names in esphome-victron-ble/*.yaml "
                "against NUMERIC_FIELDS/TEXT_FIELDS in this file"
            )
            return

        pool = await get_pool()
        device_id = await get_or_create_device(pool, target_mac)
        self._latest = {}
        self._last_activity = time.monotonic()

        state_event = asyncio.Event()

        def on_state(state: EntityState):
            mapping = key_to_field.get(state.key)
            if mapping is None:
                return
            if getattr(state, "missing_state", False):
                return
            _kind, field = mapping
            self._latest[field] = state.state
            self._last_activity = time.monotonic()
            state_event.set()

        client.subscribe_states(on_state)
        await alerts.check_offline(target_mac, offline=False)

        try:
            while True:
                try:
                    await asyncio.wait_for(state_event.wait(), timeout=5)
                    state_event.clear()
                    await self._store_reading(pool, device_id, target_mac)
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - self._last_activity > stall_seconds:
                    logger.warning("No sensor update for %.0fs, reconnecting", stall_seconds)
                    return
        finally:
            if not self._stopping:
                await alerts.check_offline(target_mac, offline=True)

    async def _store_reading(self, pool, device_id: int, mac: str):
        now = datetime.now(timezone.utc)
        reading = {field: self._latest.get(field) for field in ALL_FIELDS}
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO readings (
                        time, device_id, battery_voltage_v, battery_current_a,
                        pv_power_w, yield_today_kwh, load_current_a,
                        device_state, charger_error
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    now, device_id,
                    reading["battery_voltage_v"], reading["battery_current_a"],
                    reading["pv_power_w"], reading["yield_today_kwh"], reading["load_current_a"],
                    reading["device_state"], reading["charger_error"],
                )
        except Exception:
            logger.exception("Failed to store reading")
            return

        await alerts.check_reading(mac, reading)

