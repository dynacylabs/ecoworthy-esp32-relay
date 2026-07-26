from datetime import datetime

from pydantic import BaseModel


class ReadingPoint(BaseModel):
    time: datetime
    battery_voltage_v: float | None = None
    battery_current_a: float | None = None
    pv_power_w: float | None = None
    yield_today_kwh: float | None = None
    load_current_a: float | None = None
    device_state: str | None = None
    charger_error: str | None = None


class DeviceStatus(BaseModel):
    mac: str
    name: str | None
    last_seen: datetime | None
    latest: ReadingPoint | None
