"""app/db/base.py —— async engine 与 session factory。

状态：骨架，未实现。
实现位置：docs/02-数据库设计.md 第 604 行起（§ app/db/base.py）。
职责：创建 async engine（走 asyncpg 驱动）、AsyncSessionLocal、
      DeclarativeBase 子类，以及一个 FastAPI 依赖用的 get_session()。
"""
