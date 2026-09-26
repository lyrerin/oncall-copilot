"""app/db/base.py —— async engine 与 session factory。

状态：骨架，未实现。
实现位置：docs/02-数据库设计.md 第 604 行起（§ app/db/base.py）。
职责：创建 async engine（走 asyncpg 驱动）、AsyncSessionLocal、
      DeclarativeBase 子类，以及一个 FastAPI 依赖用的 get_session()。
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker,create_async_engine
from app.config import settings

engine = create_async_engine(
      settings.pg_dsn,
      echo = settings.debug,
      pool_size = 10,
      max_overflow = 20,
      pool_pre_ping = True,
      pool_recycle = 1800,
)

SessionLocal = async_sessionmaker(
      engine,class_ = AsyncSession,expire_on_commit = False,autoflush = False 
)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        