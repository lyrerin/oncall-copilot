import logging
import statistics
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP
from app.config import settings
from mcp_servers import store

logging.basicConfig(level=settings.log_level)
mcp = FastMCP(
    name="metrics",
    instructions ="提供时序指标查询与异常检测。先用 detect_anomaly 找异常指标，再用 query_range 看曲线细节。"
)
# 允许查询的指标白名单 —— 防止 LLM 编造指标名
ALLOWED_METRICS = {
    "cpu_usage", "memory_usage", "request_rate", "error_rate",
    "p99_latency_ms", "p50_latency_ms", "connections", "qps",
    "db_pool_used", "db_pool_size", "cache_hit_rate", "gc_pause_ms",
    "restart_count", "disk_io_wait",
}
@mcp.tool
async def list_metrics(service: str) -> dict:
    """列出该服务可查询的指标名。不确定指标名时先调这个，不要猜。"""
    return {"service": service, "metrics": sorted(ALLOWED_METRICS)}

@mcp.tool
async def query_range(service:str, metric:str, minutes:int = 60,step: int = 60):
    """查询指标在时间范围内的数据点。

    Args:
        service: 服务名
        metric: 指标名，必须来自 list_metrics
        minutes: 回溯窗口（分钟）
        step: 采样步长（秒）
    """
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"unknown metric: {metric},call list_metrics first")
    
    points = await store.fetch_metric_points(service, metric, minutes, step)
    values = [p["value"] for p in points]
    return {
        "service": service, "metric": metric, "minutes": minutes,
        "count": len(points),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": round(statistics.fmean(values), 3) if values else None,
        # 点数多时抽样返回，避免上下文爆炸
        "points": points if len(points) <= 60 else points[:: max(len(points) // 60, 1)],
        "downsampled": len(points) > 60,}

@mcp.tool
async def detect_anomaly(service: str, metric: str, minutes: int = 30) -> dict:
    """检测指标是否异常，返回 z-score 和异常窗口。

    基线取「对比窗口之前的 24 小时」，与对比窗口**不重叠**；
    |z| > 3 视为异常。

    Args:
        service: 服务名
        metric: 指标名
        minutes: 对比窗口（分钟）
    """
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"unknown metric: {metric}")

    now = datetime.now(timezone.utc)
    recent = await store.fetch_metric_points(service, metric, minutes, 60)
    # 基线窗口的右边界 = 对比窗口的左边界，两段绝不重叠。
    # 若用"最近 24 小时"当基线，故障期会被算进基线，标准差被撑大，
    # 越严重的故障 z 反而越小 —— 这是第一版的坑。
    baseline = await store.fetch_metric_points(
        service, metric, 24 * 60, 300, until=now - timedelta(minutes=minutes)
    )

    if not recent or len(baseline) < 5:
        return {"service": service, "metric": metric, "anomalous": False,
                "reason": "insufficient data"}
    
    rvals = [p["value"] for p in recent]
    bvals = [p["value"] for p in baseline]
    mean = statistics.fmean(bvals)
    stdev = statistics.stdev(bvals) or 1e-9
    current = rvals[-1]
    return {
        "service": service, "metric": metric,
        "anomalous": abs((current - mean) / stdev) > 3,
        "z_score": round((current - mean) / stdev, 2),
        "current": round(current, 3),
        "baseline_mean": round(mean, 3),
        "baseline_stdev": round(stdev, 3),
        "peak": round(max(rvals), 3),
        "direction": "up" if current > mean else "down",
        "window_minutes": minutes,
    }

@mcp.tool
async def compare_to_baseline(service: str, metric: str, hours: int = 24) -> dict:
    """把当前值与历史基线对比，返回变化百分比。用于判断"是否真的变差了"。"""
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"unknown metric: {metric}")

    now = datetime.now(timezone.utc)
    recent = await store.fetch_metric_points(service, metric, 15, 60)
    # 同理：历史基线只取"最近 15 分钟之前"的部分，不含当前窗口
    hist = await store.fetch_metric_points(
        service, metric, hours * 60, 300, until=now - timedelta(minutes=15)
    )
    if not recent or not hist:
        return {"service": service, "metric": metric, "insufficient_data": True}

    cur = statistics.fmean([p["value"] for p in recent])
    base = statistics.fmean([p["value"] for p in hist]) or 1e-9
    return {
        "service": service, "metric": metric,
        "current": round(cur, 3), "baseline": round(base, 3),
        "delta_pct": round((cur - base) / base * 100, 2),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8002)