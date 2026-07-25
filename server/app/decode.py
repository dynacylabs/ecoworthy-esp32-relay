"""Decoding for the EcoWorthy BW0F's raw BLE notify payloads.

NOT REVERSE-ENGINEERED YET. This is a stub - every field comes back None
until this is actually filled in. Do not trust any non-None value below
until it's been validated against a real, simultaneously-observed reading
(e.g. the EcoWorthy app open on a phone at the same moment).

What's confirmed so far, from live captures on notify characteristic
0000fff1-0000-1000-8000-00805f9b34fb:

- Two recurring frame shapes, distinguished by the first byte:
  - 0xa1..., 84 bytes total
  - 0xa2..., 102 bytes total
- Both start with a 5-byte header where byte[4] is a constant 0x65 (101)
  in every sample seen - same value in both frame types, so probably not
  a per-type length field. Meaning unconfirmed.
- The last 2 bytes of the 0xa1 frame change whenever anything earlier in
  the frame changes, and don't change when nothing else does - very
  likely a checksum/CRC over the rest of the frame, not itself a value.
- Two single-byte fields (offsets 21 and 27 in the 0xa1 frame, 0-indexed)
  were observed moving together (both decremented by 1 between two
  consecutive samples) while everything else held steady - some kind of
  live counter or status value, single-byte range so unlikely to be a
  precision voltage/current reading directly.
- The 0xa2 frame is mostly a long run of zero bytes in every sample seen
  so far - either fields that are legitimately zero right now (device
  idle, no charge/discharge activity), or fields we haven't triggered
  populated data for yet.

None of this is enough to assign real meaning. Next step for whoever picks
this up: capture frames while deliberately varying one real, known
quantity at a time (e.g. note the EcoWorthy app's displayed pack voltage
at the exact moment a frame arrives) and diff against a baseline capture,
the same way the checksum bytes were identified above.
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


def decode_bw0f_frame(hex_payload: str) -> DecodedReading | None:
    """Attempt to decode one raw notify payload (hex string, no 0x prefix).

    Returns None if the frame isn't recognized at all (wrong length/type
    byte); returns an all-None DecodedReading if recognized but not yet
    decodable (current state - see module docstring).
    """
    data = bytes.fromhex(hex_payload)
    if len(data) not in (84, 102):
        return None
    if data[0] not in (0xA1, 0xA2):
        return None

    # TODO: fill in real field extraction once the protocol is decoded.
    return dict(_EMPTY_READING)
