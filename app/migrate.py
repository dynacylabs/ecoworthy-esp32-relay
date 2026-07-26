"""Tiny SQL migration runner - applies any not-yet-applied .sql file in
db/migrations/, in filename order, tracked in a `schema_migrations`
table. Run once at startup (see main.py's lifespan).

This exists because Postgres's docker-entrypoint-initdb.d (which
docker-compose.yml also points at db/migrations/) only runs on a
completely fresh volume - it does nothing for an already-initialized
database. Without this, adding a new migration (e.g. 002_settings.sql for
the Settings tab) would require every existing deployment to apply it by
hand. With this, it's just a container restart.

Idempotent for both fresh and pre-existing installs: if a migration's
objects already exist (because Postgres's initdb bootstrap already ran
every file present at first startup), the resulting DuplicateTableError
is treated as "already applied" rather than a fatal error.
"""

import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger("victron.migrate")

MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", "db/migrations"))


async def run(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}

        if not MIGRATIONS_DIR.is_dir():
            logger.warning("Migrations directory %s not found, skipping", MIGRATIONS_DIR)
            return

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue

            sql = path.read_text(encoding="utf-8")
            try:
                async with conn.transaction():
                    await conn.execute(sql)
            except asyncpg.exceptions.DuplicateTableError:
                logger.info(
                    "%s: objects already exist (likely created by Postgres's initdb "
                    "bootstrap on first start) - marking as applied without re-running",
                    path.name,
                )
            else:
                logger.info("Applied migration %s", path.name)

            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
                path.name,
            )
