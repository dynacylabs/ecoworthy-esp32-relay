import logging

import asyncpg

from config import DATABASE_URL

logger = logging.getLogger("ecoworthy.db")

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
