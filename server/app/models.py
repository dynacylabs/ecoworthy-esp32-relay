from datetime import datetime

from pydantic import BaseModel


class ReadingPoint(BaseModel):
    time: datetime
    battery_voltage_v: float | None = None
    battery_current_a: float | None = None
    battery_soc_pct: float | None = None
    pv_voltage_v: float | None = None
    pv_current_a: float | None = None
    pv_power_w: float | None = None
    load_voltage_v: float | None = None
    load_current_a: float | None = None
    load_power_w: float | None = None
    temperature_c: float | None = None


class DeviceStatus(BaseModel):
    mac: str
    name: str | None
    last_seen: datetime | None
    latest: ReadingPoint | None
    events_last_5min: int


class RawEventPoint(BaseModel):
    time: datetime
    characteristic: str
    hex: str
    len: int
