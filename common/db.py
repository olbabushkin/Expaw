"""Пул asyncpg и общие операции с БД."""

import json

import asyncpg

from common.config import settings


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=5, init=_init_conn
    )


async def heartbeat(pool: asyncpg.Pool, service: str) -> None:
    await pool.execute(
        """
        INSERT INTO service_heartbeats (service, beat_at) VALUES ($1, now())
        ON CONFLICT (service) DO UPDATE SET beat_at = now()
        """,
        service,
    )


async def system_event(pool: asyncpg.Pool, message: str, level: str = "info") -> None:
    await pool.execute(
        "INSERT INTO system_events (level, message) VALUES ($1, $2)", level, message
    )


async def audit(pool: asyncpg.Pool, actor: int, action: str, payload: dict) -> None:
    await pool.execute(
        "INSERT INTO audit_log (actor, action, payload) VALUES ($1, $2, $3)",
        actor,
        action,
        payload,
    )
