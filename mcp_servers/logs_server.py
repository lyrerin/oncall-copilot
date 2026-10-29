"""mcp_servers/logs_server.py —— mcp-logs :8001。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 566 行起（§5.2 日志 MCP Server）。
职责：暴露查日志的工具（按服务/时间窗/关键字检索）。
"""
import logging
from collections import Counter
from datetime import datetime
from fastmcp import FastMCP
from app.config import settings
from mcp_servers import store

logging.basicConfig(level=settings.log_level)
log = logging.getLogger('mcp-logs')

mcp = FastMCP(
    name = 'logs',
    instructions = ("提供生产环境日志检索能力。查询时优先用聚合类工具"
        "（error_rate / top_error_patterns）定位方向，再用 search_logs 看细节，"
        "避免一次拉取大量原始日志。"),
)

@mcp.tool
async def error_rate(service: str, minutes: int = 30) -> dict:
    """查询某服务最近 N 分钟的日志总量、错误数和错误率。

    Args:
        service: 服务名，例如 api-gateway
        minutes: 回溯窗口（分钟），默认 30
    """
    return await store.aggregate_log_stats(service, minutes)

@mcp.tool
async def search_logs(
    service: str,
    pattern: str | None = None,
    minutes: int = 30,
    level: str | None = None,
    limit: int = 50,
) -> dict:
    """按关键字或级别检索原始日志。

        Args:
            service: 服务名
            pattern: 关键字（大小写不敏感的模糊匹配），如 "timeout"
            minutes: 回溯窗口（分钟）
            level: 日志级别过滤，ERROR / WARN / INFO / FATAL
            limit: 最多返回条数，上限 200
        """
    limit = min(limit, 200)
    rows = await store.fetch_logs(
        service, pattern, minutes, limit,level
    )
    for r in rows:
        r['message'] = (r['message'] or '')[:500]
        r['ts'] = r['ts'].isoformat() if isinstance(r['ts'], datetime) else r['ts']
    return {
        "service": service,
        "pattern": pattern,
        "window_minutes": minutes,
        "count": len(rows),
        "truncated": len(rows) >= limit,
        "lines": rows,
    }

@mcp.tool
async def top_error_patterns(service: str, minutes: int = 30, top: int = 5) -> dict:
    """把错误日志按模板粗聚类，返回最高频的几种错误模式。

    这是定位根因最高效的工具：先看"哪种错误最多"，
    而不是逐条读日志。

    Args:
        service: 服务名
        minutes: 回溯窗口（分钟）
        top: 返回前 N 种模式
    """
    rows = await store.fetch_logs(service, None, minutes, 2000, "ERROR")
    # 粗聚类：把数字、UUID、IP、十六进制串替换成占位符，
    # 这样 "timeout after 3s to 10.1.2.3" 和 "timeout after 7s to 10.9.9.9"
    # 会归到同一个模板
    import re

    def templatize(msg: str) -> str:
        msg = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<UUID>", msg, flags=re.I)
        msg = re.sub(r"\b\d+\.\d+\.\d+\.\d+\b", "<IP>", msg)
        msg = re.sub(r"\b0x[0-9a-f]+\b", "<HEX>", msg, flags=re.I)
        # 不能用 \b\d+\b：数字后面紧跟字母时（"3s" / "8726ms"）\b 不成立，
        # 数字换不掉，每条都成一个独立模板，聚类就废了。
        msg = re.sub(r"\d+", "<N>", msg)
        return msg[:200]

    counter: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for r in rows:
        tpl = templatize(r["message"] or "")
        counter[tpl] += 1
        sample.setdefault(tpl, r["message"])

    return {
        "service": service,
        "window_minutes": minutes,
        "scanned": len(rows),
        "patterns": [
            {"pattern": p, "count": c, "sample": sample[p]}
            for p, c in counter.most_common(top)
        ],
    }

@mcp.tool
async def get_trace(trace_id: str) -> dict:
    """按 trace_id 取出一次完整请求链路的日志，用于跨服务追踪。

    Args:
        trace_id: 链路 ID
    """
    from sqlalchemy import text
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT ts, level, service, message FROM mock_logs "
                "WHERE trace_id = :tid ORDER BY ts"
            ),
            {"tid": trace_id},
        )
        lines = [
            {'ts': r.ts.isoformat(), 'level': r.level, 'service': r.service, 'message': (r.message or '')[:500]}
            for r in rows
        ]
    return {'trace_id': trace_id, 'count': len(lines), 'lines': lines}

if __name__ == "__main__":
    log.info("starting logs MCP server on :8001")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)