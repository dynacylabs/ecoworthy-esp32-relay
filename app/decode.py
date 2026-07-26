"""Decoding for the EcoWorthy BW0F's raw BLE notify payloads.

Partially decoded, sourced from prior art rather than our own captures -
see below. Fields not listed as decoded still come back None until
validated against a real, simultaneously-observed reading (e.g. the
EcoWorthy app open on a phone at the same moment).

Sources (all describe EcoWorthy's "BW0x" Bluetooth module family - BW02,
used on plain LiFePO4 batteries, and presumably BW0F, used on this
all-in-one solar/battery/load unit - as sharing one wire protocol):

- patman15/aiobmsble (Apache-2.0), a maintained/tested Home Assistant BMS
  library with a real "ECO-WORTHY BW 02/0B" plugin:
  https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/ecoworthy_bms.py
  This is the primary source for everything below - GATT UUIDs, frame
  head bytes, checksum algorithm, and the battery_level/voltage/current
  field offsets.
- A Home Assistant community thread reverse-engineering the same BW02
  protocol independently, including a raw hex capture that cross-checks
  against aiobmsble's offsets:
  https://community.home-assistant.io/t/eco-worthy-100ah-iot-battery-integration/792000

This lines up with everything we'd already confirmed from our own BW0F
captures before finding this: notify characteristic
0000fff1-0000-1000-8000-00805f9b34fb (aiobmsble: service fff0, notify
fff1, write fff2), frames starting with 0xa1/0xa2, and a trailing
checksum that changes whenever the rest of the frame does (now known to
be CRC-16/MODBUS, little-endian, over everything but the last 2 bytes).
Byte 4 being a constant 0x65 across every sample is also consistent -
it falls in a byte range aiobmsble's plugin never assigns a field to.
As a further sanity check, decoding the community thread's sample bytes
with these offsets yields battery_level=92%, voltage=13.32V,
current=-0.49A, and design_capacity=100Ah - the last one an exact match
for the "100Ah" battery that thread was specifically about.

What's still NOT covered by aiobmsble, because it's a plain BMS library
with no concept of a solar controller: PV (panel) voltage/current/power
and load voltage/current/power. BW0F almost certainly carries additional
fields for these beyond what BW02 needs, most likely somewhere in the
84-byte 0xa1 frame's still-unassigned bytes or the 102-byte 0xa2 frame
(which aiobmsble only uses for cell voltages/temperatures - fields not
meaningful for this non-battery-pack product and not decoded here).
Finding those still requires real captures correlated against known
values (e.g. the EcoWorthy app's displayed PV/load readings at the
moment a frame arrives), same as originally planned.
"""

from typing import TypedDict


class DecodedReading(TypedDict):
    battery_voltage_v: float | None
    battery_current_a: float | None
    battery_soc_pct: float | None
    pv_voltage_v: float | None
    pv_current_a: float | None
    pv_power_w: float | None
    load_voltage_v: float | None
    load_current_a: float | None
    load_power_w: float | None
    temperature_c: float | None


_EMPTY_READING: DecodedReading = {
    "battery_voltage_v": None,
    "battery_current_a": None,
    "battery_soc_pct": None,
    "pv_voltage_v": None,
    "pv_current_a": None,
    "pv_power_w": None,
    "load_voltage_v": None,
    "load_current_a": None,
    "load_power_w": None,
    "temperature_c": None,
}


def _crc_modbus(data: bytes) -> int:
    """CRC-16/MODBUS - matches aiobmsble's crc_modbus() exactly."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else (crc >> 1)
    return crc & 0xFFFF


def decode_bw0f_frame(hex_payload: str) -> DecodedReading | None:
    """Attempt to decode one raw notify payload (hex string, no 0x prefix).

    Returns None if the frame isn't recognized at all (wrong length/type
    byte, or fails its checksum); returns a DecodedReading (fields still
    None where undecoded) if recognized and passes its checksum.
    """
    data = bytes.fromhex(hex_payload)
    if len(data) not in (84, 102):
        return None
    if data[0] not in (0xA1, 0xA2):
        return None

    body, trailer = data[:-2], data[-2:]
    if _crc_modbus(body) != int.from_bytes(trailer, "little"):
        return None

    reading = dict(_EMPTY_READING)
    if data[0] == 0xA1:
        reading["battery_soc_pct"] = float(int.from_bytes(data[16:18], "big"))
        reading["battery_voltage_v"] = int.from_bytes(data[20:22], "big") / 100
        reading["battery_current_a"] = (
            int.from_bytes(data[22:24], "big", signed=True) / 100
        )
    return reading
