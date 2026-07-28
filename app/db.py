import logging

import asyncpg

from config import DATABASE_URL

logger = logging.getLogger("victron.db")

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ensure_retention_policy(pool: asyncpg.Pool, telemetry_retention_days: float):
    # telemetry_retention_days <= 0 means "keep everything forever" - remove
    # any existing policy rather than just skipping adding one, so flipping
    # this back to 0 on a deployment that already has a real policy actually
    # takes effect immediately instead of silently doing nothing. Mirrors
    # heltec-wifi-optimization's db.ensure_retention_policies, just against
    # this app's single `readings` hypertable instead of two. Called both
    # at startup and again immediately whenever it's changed from the
    # Settings tab (see main.py) - if_not_exists=True makes this safe to
    # call repeatedly, but won't pick up a *changed* nonzero value once the
    # policy already exists at a different interval (remove it by hand
    # first: SELECT remove_retention_policy('readings');).
    async with pool.acquire() as conn:
        try:
            if telemetry_retention_days > 0:
                await conn.execute(
                    "SELECT add_retention_policy('readings'::regclass, make_interval(days => $1), if_not_exists => true)",
                    int(telemetry_retention_days),
                )
            else:
                await conn.execute(
                    "SELECT remove_retention_policy('readings'::regclass, if_exists => true)",
                )
        except Exception:
            logger.exception("failed to ensure retention policy on readings")


async def get_or_create_device(pool: asyncpg.Pool, mac: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM devices WHERE mac = $1", mac)
        if row:
            await conn.execute("UPDATE devices SET last_seen = now() WHERE id = $1", row["id"])
            return row["id"]
        row = await conn.fetchrow(
            "INSERT INTO devices (mac, last_seen) VALUES ($1, now()) RETURNING id", mac,
        )
        return row["id"]
