"""MongoDB session helpers using pymongo's native async client (AsyncMongoClient)."""

from __future__ import annotations

from typing import Any

from pymongo import AsyncMongoClient

from autoapply.config import get_settings

_client: AsyncMongoClient | None = None


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        _client = AsyncMongoClient(get_settings().mongo_uri, tz_aware=True)
    return _client


def get_db() -> Any:
    """Returns an AsyncDatabase. Annotated as Any to avoid pinning the import path
    across pymongo minor versions (`pymongo.asynchronous.database.AsyncDatabase`)."""
    return get_client()[get_settings().mongo_db]


async def init_db() -> None:
    """Ensure indexes. Idempotent — safe to call on every process start."""
    from .models import Application, Job, Reply, SourceRun

    db = get_db()

    await db[Job.collection].create_index("dedup_hash", unique=True)
    await db[Job.collection].create_index("jd_url_key")
    await db[Job.collection].create_index([("source", 1), ("source_id", 1)])
    await db[Job.collection].create_index([("company", 1), ("role", 1)])
    await db[Job.collection].create_index([("fit_score", -1)])

    await db[Application.collection].create_index([("status", 1), ("created_at", -1)])
    await db[Application.collection].create_index("job_id")

    await db[Reply.collection].create_index("gmail_message_id", unique=True)
    await db[Reply.collection].create_index("application_id")

    await db[SourceRun.collection].create_index([("source", 1), ("started_at", -1)])


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


__all__ = ["close_db", "get_client", "get_db", "init_db"]
