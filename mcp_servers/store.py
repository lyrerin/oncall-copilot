from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.base import SessionLocal


async def fetch_logs(
    service: str, pattern: str | None, minutes: int, limit: int, level: str | None
) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    # 可选条件靠"拼 WHERE"实现，不用 (:level IS NULL OR level = :level)。
    # 原因：传 None 时 Postgres 推断不出 $n 的类型，asyncpg 直接报
    # AmbiguousParameterError: could not determine data type of parameter $3
    where = ["service = :service", "ts >= :since"]
    params: dict = {"service": service, "since": since, "limit": limit}
    if level:
        where.append("level = :level")
        params["level"] = level
    if pattern:
        where.append("message ILIKE :pattern")
        params["pattern"] = f"%{pattern}%"

    sql = text(
        "SELECT ts, level, service, message, trace_id FROM mock_logs "
        f"WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT :limit"
    )
    async with SessionLocal() as s:
        rows = await s.execute(sql, params)
        return [dict(r._mapping) for r in rows]


async def aggregate_log_stats(service: str, minutes: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    sql = text("""
        SELECT level, count(*) AS cnt
        FROM mock_logs
        WHERE service = :service AND ts >= :since
        GROUP BY level
    """)
    async with SessionLocal() as s:
        rows = (await s.execute(sql, {"service": service, "since": since})).all()

    by_level = {r.level: r.cnt for r in rows}
    total = sum(by_level.values())
    errors = by_level.get("ERROR", 0) + by_level.get("FATAL", 0)
    return {
        "service": service,
        "window_minutes": minutes,
        "total": total,
        "errors": errors,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "by_level": by_level,
    }


async def fetch_metric_points(
    service: str,
    metric: str,
    minutes: int,
    step: int | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """取一个时间窗内的指标点。

    until 是窗口右边界（默认 now）。detect_anomaly 用它把**基线窗口**和
    **对比窗口**切开：基线必须是故障发生之前的数据，否则故障期被算进基线里，
    标准差被撑大，z-score 反而变小（实测同一份数据 18 → 1.6）。
    """
    end = until or datetime.now(timezone.utc)
    since = end - timedelta(minutes=minutes)

    sql = text(
        "SELECT ts, value FROM mock_metrics "
        "WHERE service = :service AND metric = :metric "
        "AND ts >= :since AND ts <= :until ORDER BY ts"
    )
    async with SessionLocal() as s:
        rows = (await s.execute(sql, {
            "service": service, "metric": metric, "since": since, "until": end,
        })).all()

    # step 是抽稀目标（秒）。mock 数据一般 1 分钟一个点，step=300 就每 5 个取 1 个，
    # 免得 24 小时基线拉回几千个点把上下文撑爆。
    if step and len(rows) > 1:
        gap = (rows[-1].ts - rows[0].ts).total_seconds() / (len(rows) - 1)
        stride = max(1, round(step / gap)) if gap > 0 else 1
        if stride > 1:
            rows = rows[::stride]

    return [{"ts": r.ts.isoformat(), "value": float(r.value)} for r in rows]


async def fetch_recent_changes(service: str, hours: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    sql = text("""
        SELECT id, kind, operator, at, detail, "commit"
        FROM mock_changes
        WHERE service = :service AND at >= :since
        ORDER BY at DESC
    """)
    async with SessionLocal() as s:
        rows = await s.execute(sql, {"service": service, "since": since})
        return [
            {**dict(r._mapping), "at": r.at.isoformat(), "commit": r.commit}
            for r in rows
        ]