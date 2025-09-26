from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional, Sequence

import asyncpg


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")
        return self._pool

    async def fetch(self, query: str, *args: Any) -> Sequence[asyncpg.Record]:
        pool = self._require_pool()
        return await pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        pool = self._require_pool()
        return await pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        pool = self._require_pool()
        return await pool.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        pool = self._require_pool()
        return await pool.execute(query, *args)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        pool = self._require_pool()
        conn = await pool.acquire()
        try:
            yield conn
        finally:
            await pool.release(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                yield conn
            except Exception:
                await tx.rollback()
                raise
            else:
                await tx.commit()
