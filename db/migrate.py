"""Применяет SQL-миграции из db/migrations по порядку имён файлов.

Запуск: python -m db.migrate
"""

import asyncio
import pathlib
import sys

import asyncpg

from common.config import settings

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

CONNECT_ATTEMPTS = 15
CONNECT_RETRY_SEC = 2


async def _connect() -> asyncpg.Connection:
    """При первом старте Postgres перезапускается после initdb — ждём его."""
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return await asyncpg.connect(settings.database_url)
        except (OSError, asyncpg.PostgresError) as exc:
            if attempt == CONNECT_ATTEMPTS:
                raise
            print(f"db not ready ({exc}), retry {attempt}/{CONNECT_ATTEMPTS}")
            await asyncio.sleep(CONNECT_RETRY_SEC)
    raise AssertionError("unreachable")


async def main() -> None:
    conn = await _connect()
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            r["filename"]
            for r in await conn.fetch("SELECT filename FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                )
            print(f"applied {path.name}")
        print("migrations up to date")
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"migration failed: {exc}", file=sys.stderr)
        sys.exit(1)
