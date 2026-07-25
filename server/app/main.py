import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from ble_poller import BLEPoller
from config import API_TOKEN, TARGET_BLE_MAC
from db import close_pool, get_pool
from models import DeviceStatus, RawEventPoint, ReadingPoint

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ecoworthy")

poller = BLEPoller()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    poller_task = asyncio.create_task(poller.run_forever())
    yield
    await poller.stop()
    poller_task.cancel()
    await close_pool()


app = FastAPI(title="ecoworthy-dashboard", lifespan=lifespan)


async def require_token(token: str):
    if token != API_TOKEN:
        raise HTTPException(401, "invalid token")


# Every chart-data endpoint downsamples to roughly this many points
# regardless of the selected range, via TimescaleDB's time_bucket() - same
# reasoning as heltec-wifi-optimization: without this, a 12mo range at a
# reading every second or so is far too much to query/ship/render.
TARGET_CHART_POINTS = 600
MAX_HOURS = 24 * 400


def _bucket_seconds(hours: float) -> int:
    return max(5, int((hours * 3600) / TARGET_CHART_POINTS))


@app.get("/api/status", response_model=list[DeviceStatus], dependencies=[Depends(require_token)])
async def get_status():
    pool = await get_pool()
    async with pool.acquire() as conn:
        devices = await conn.fetch("SELECT id, mac, name, last_seen FROM devices ORDER BY mac")
        result = []
        for d in devices:
            latest = await conn.fetchrow(
                """
                SELECT time, battery_voltage_v, battery_current_a, battery_soc_pct,
                       pv_voltage_v, pv_current_a, pv_power_w,
                       load_voltage_v, load_current_a, load_power_w, temperature_c
                FROM readings WHERE device_id = $1 ORDER BY time DESC LIMIT 1
                """,
                d["id"],
            )
            event_count = await conn.fetchval(
                "SELECT count(*) FROM raw_events WHERE device_id = $1 AND time > now() - interval '5 minutes'",
                d["id"],
            )
            result.append(DeviceStatus(
                mac=d["mac"],
                name=d["name"],
                last_seen=d["last_seen"],
                latest=ReadingPoint(**dict(latest)) if latest else None,
                events_last_5min=event_count or 0,
            ))
        return result


@app.get("/api/readings/{mac}", response_model=list[ReadingPoint], dependencies=[Depends(require_token)])
async def get_readings_history(mac: str, hours: float = Query(default=6, gt=0, le=MAX_HOURS)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                time_bucket(make_interval(secs => $3), r.time) AS time,
                avg(r.battery_voltage_v) AS battery_voltage_v,
                avg(r.battery_current_a) AS battery_current_a,
                avg(r.battery_soc_pct) AS battery_soc_pct,
                avg(r.pv_voltage_v) AS pv_voltage_v,
                avg(r.pv_current_a) AS pv_current_a,
                avg(r.pv_power_w) AS pv_power_w,
                avg(r.load_voltage_v) AS load_voltage_v,
                avg(r.load_current_a) AS load_current_a,
                avg(r.load_power_w) AS load_power_w,
                avg(r.temperature_c) AS temperature_c
            FROM readings r JOIN devices d ON d.id = r.device_id
            WHERE d.mac = $1 AND r.time > now() - make_interval(secs => $2)
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            mac, hours * 3600, _bucket_seconds(hours),
        )
        return [ReadingPoint(**dict(r)) for r in rows]


@app.get("/api/raw-events/{mac}", response_model=list[RawEventPoint], dependencies=[Depends(require_token)])
async def get_raw_events(mac: str, limit: int = Query(default=200, gt=0, le=2000)):
    # Not downsampled - meant for spot-checking real payloads (e.g. while
    # reverse-engineering decode.py), not for charting.
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.time, e.characteristic, e.hex, e.len
            FROM raw_events e JOIN devices d ON d.id = e.device_id
            WHERE d.mac = $1
            ORDER BY e.time DESC LIMIT $2
            """,
            mac, limit,
        )
        return [RawEventPoint(**dict(r)) for r in rows]


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard_page():
    # Token injected server-side, same pattern as heltec-wifi-optimization -
    # access control for a human is expected to happen in front of this
    # (reverse proxy), not here. The token stays required on the API routes
    # regardless, as defense in depth.
    with open("static/dashboard.html", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__API_TOKEN__", json.dumps(API_TOKEN))
    html = html.replace("__TARGET_MAC__", json.dumps(TARGET_BLE_MAC))
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok"}
