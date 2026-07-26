"""Background task: connects to the ESP32's ESPHome bluetooth_proxy,
connects to the BW0F over it, subscribes to its notify characteristics,
and stores every raw payload (plus a best-effort decode - see decode.py).

The server always initiates this connection (to the ESP32's native API,
and from there to the BLE device) - the device side never has to reach
out anywhere. That's what makes this work across the VLAN separation
between the hobocamp and wherever this server runs.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from aioesphomeapi import APIClient, BluetoothLEAdvertisement

import alerts
import settings
from config import ESPHOME_API_ENCRYPTION_KEY, ESPHOME_HOST, ESPHOME_PORT
from db import get_or_create_device, get_pool
from decode import decode_bw0f_frame

logger = logging.getLogger("ecoworthy.ble_poller")

# Standard "Service Changed" characteristic (GATT-cache-invalidation
# housekeeping, not telemetry). Skipped for the same reason the earlier
# hand-rolled ESP32 firmware skipped it: subscribing has been observed to
# hang indefinitely on this specific peripheral.
SKIP_CHARACTERISTIC_UUID = "00002a05-0000-1000-8000-00805f9b34fb"

# Standard BLE characteristic properties bitmask (Bluetooth Core spec).
PROP_NOTIFY = 0x10
PROP_INDICATE = 0x20


class BLEPoller:
    def __init__(self):
        self._client: APIClient | None = None
        self._last_activity = 0.0
        self._stopping = False

    async def run_forever(self):
        while not self._stopping:
            try:
                await self._run_once()
            except Exception:
                logger.exception("BLE poller iteration failed, retrying in 10s")
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
        logger.info("Connected to ESPHome proxy at %s:%s", ESPHOME_HOST, ESPHOME_PORT)
        try:
            await self._connect_and_stream(client)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _discover_address_type(self, client: APIClient, target_address: int, connect_timeout: float) -> int | None:
        """aioesphomeapi (45.x) requires the BLE address type (public/random)
        up front when connecting - it no longer defaults or infers it. The
        only way to learn it is from an advertisement, so listen for one
        from our target MAC before attempting to connect.
        """
        found: asyncio.Future[int] = asyncio.get_event_loop().create_future()

        def on_adv(adv: BluetoothLEAdvertisement):
            if adv.address == target_address and not found.done():
                found.set_result(adv.address_type)

        unsub = client.subscribe_bluetooth_le_advertisements(on_adv)
        try:
            return await asyncio.wait_for(found, timeout=connect_timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsub()

    async def _connect_and_stream(self, client: APIClient):
        # Read fresh at the start of every connection attempt (rather than
        # once at import time) so changes made on the Settings page take
        # effect on the very next reconnect, no restart required.
        current = settings.current()
        target_mac: str = current["target_ble_mac"]
        target_address = int(target_mac.replace(":", ""), 16)
        connect_timeout: float = current["ble_connect_timeout_seconds"]
        stall_seconds: float = current["ble_stall_seconds"]

        connected_event = asyncio.Event()
        disconnected_event = asyncio.Event()
        state = {"connected": False}

        def on_state(connected: bool, mtu: int, error: int):
            state["connected"] = connected
            if connected:
                connected_event.set()
            else:
                disconnected_event.set()

        device_info = await client.device_info()
        feature_flags = device_info.bluetooth_proxy_feature_flags_compat(client.api_version)

        logger.info("Waiting for an advertisement from %s to learn its address type ...", target_mac)
        address_type = await self._discover_address_type(client, target_address, connect_timeout)
        if address_type is None:
            logger.warning("Never saw an advertisement from %s, can't connect yet", target_mac)
            return

        logger.info("Connecting to BLE device %s ...", target_mac)
        remove_listener = await client.bluetooth_device_connect(
            target_address, on_state,
            timeout=connect_timeout,
            feature_flags=feature_flags,
            address_type=address_type,
        )

        try:
            try:
                await asyncio.wait_for(
                    connected_event.wait(), timeout=connect_timeout + 5,
                )
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for BLE connection state")
                return

            if not state["connected"]:
                logger.warning("BLE connect to %s failed", target_mac)
                return

            services = await client.bluetooth_gatt_get_services(target_address)
            notify_targets = []
            for service in services.services:
                for ch in service.characteristics:
                    if ch.uuid.lower() == SKIP_CHARACTERISTIC_UUID:
                        logger.info("Skipping %s (known hang risk, not telemetry)", ch.uuid)
                        continue
                    if ch.properties & (PROP_NOTIFY | PROP_INDICATE):
                        notify_targets.append((ch.uuid, ch.handle))

            if not notify_targets:
                logger.warning("No notify/indicate characteristics found on %s", target_mac)
                return

            pool = await get_pool()
            device_id = await get_or_create_device(pool, target_mac)

            self._last_activity = time.monotonic()
            stop_notifies = []

            def make_notify_handler(char_uuid: str):
                def on_notify(handle: int, data: bytearray):
                    self._last_activity = time.monotonic()
                    asyncio.create_task(self._store_reading(pool, device_id, target_mac, char_uuid, bytes(data)))
                return on_notify

            for uuid, handle in notify_targets:
                stop_notify, _cleanup = await client.bluetooth_gatt_start_notify(
                    target_address, handle, make_notify_handler(uuid),
                )
                stop_notifies.append(stop_notify)
                logger.info("Subscribed to %s (handle %s)", uuid, handle)

            await alerts.check_offline(target_mac, offline=False)
            try:
                while True:
                    try:
                        await asyncio.wait_for(disconnected_event.wait(), timeout=5)
                        logger.warning("BLE device disconnected unexpectedly")
                        return
                    except asyncio.TimeoutError:
                        pass
                    if time.monotonic() - self._last_activity > stall_seconds:
                        logger.warning("No data for %.0fs, reconnecting", stall_seconds)
                        return
            finally:
                if not self._stopping:
                    await alerts.check_offline(target_mac, offline=True)
                for stop_notify in stop_notifies:
                    try:
                        await stop_notify()
                    except Exception:
                        pass
        finally:
            try:
                await client.bluetooth_device_disconnect(target_address)
            except Exception:
                pass
            remove_listener()

    async def _store_reading(self, pool, device_id: int, mac: str, characteristic: str, data: bytes):
        hex_payload = data.hex()
        now = datetime.now(timezone.utc)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO raw_events (time, device_id, characteristic, hex, len) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    now, device_id, characteristic, hex_payload, len(data),
                )
                decoded = decode_bw0f_frame(hex_payload)
                if decoded is not None:
                    await conn.execute(
                        """
                        INSERT INTO readings (
                            time, device_id, battery_voltage_v, battery_current_a, battery_soc_pct,
                            pv_voltage_v, pv_current_a, pv_power_w,
                            load_voltage_v, load_current_a, load_power_w, temperature_c
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        """,
                        now, device_id,
                        decoded["battery_voltage_v"], decoded["battery_current_a"], decoded["battery_soc_pct"],
                        decoded["pv_voltage_v"], decoded["pv_current_a"], decoded["pv_power_w"],
                        decoded["load_voltage_v"], decoded["load_current_a"], decoded["load_power_w"],
                        decoded["temperature_c"],
                    )
        except Exception:
            logger.exception("Failed to store reading")
            return

        if decoded is not None:
            await alerts.check_reading(mac, decoded)
