# mockenv/generate.py
"""生成假的日志 / 指标 / 变更数据，写入 mock_logs / mock_metrics / mock_changes。

用法：
    python -m mockenv.generate --scenario memory_leak        # 生成单个场景
    python -m mockenv.generate --all                         # 生成 5 个故障场景
    python -m mockenv.generate --all --minutes 30            # 缩短窗口

（service 跟着场景走，不用传：memory_leak 自动就是 order-service）

注意：数据窗口锚定在"运行那一刻"（now - minutes）。演示或跑评估前要重跑一次，
      否则隔几小时后 fetch_logs 的 ts >= now - 30min 会查不到数据。
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.base import SessionLocal

# 每个场景 = 一种"故障指纹"，评估集和它一一对应
SCENARIOS: dict[str, dict] = {
    "memory_leak": {
        "service": "order-service",
        "metrics": {"memory_usage": (45, 95), "restart_count": (0, 2), "p99_latency_ms": (120, 1800)},
        "log_templates": [
            "java.lang.OutOfMemoryError: Java heap space",
            "GC overhead limit exceeded, pause {n}ms",
            "connection pool exhausted, waiting for {n}ms",
        ],
        "change": {"kind": "deploy", "detail": "release v42: add order cache layer"},
        "root_cause_tags": ["memory_leak"],
    },
    "bad_release": {
        "service": "api-gateway",
        "metrics": {"error_rate": (0.002, 0.15), "p99_latency_ms": (80, 900)},
        "log_templates": [
            "upstream connect error or disconnect/reset before headers",
            "503 Service Unavailable from order-service",
            "panic: nil pointer dereference at router.go:{n}",
        ],
        "change": {"kind": "deploy", "detail": "release v87: refactor routing middleware"},
        "root_cause_tags": ["bad_release"],
    },
    "db_deadlock": {
        "service": "order-db",
        "metrics": {"db_pool_used": (10, 100), "p99_latency_ms": (20, 3000)},
        "log_templates": [
            "deadlock detected, transaction {n} rolled back",
            "lock wait timeout exceeded; try restarting transaction",
            "too many connections, max {n} reached",
        ],
        "change": {"kind": "config", "detail": "max_connections 200 -> 100"},
        "root_cause_tags": ["db_deadlock"],
    },
    "cpu_saturation": {
        "service": "user-service",
        "metrics": {"cpu_usage": (30, 98), "p99_latency_ms": (60, 1200)},
        "log_templates": [
            "request queue full, dropping {n} requests",
            "slow handler /api/user/profile took {n}ms",
        ],
        "change": {"kind": "scale", "detail": "replicas 6 -> 4 (cost optimization)"},
        "root_cause_tags": ["cpu_saturation"],
    },
    "cache_miss_storm": {
        "service": "redis-cache",
        "metrics": {"cache_hit_rate": (0.95, 0.20), "connections": (100, 900)},
        "log_templates": [
            "cache MISS for key user:profile:{n}, falling back to db",
            "evicted {n} keys due to maxmemory policy",
        ],
        "change": {"kind": "config", "detail": "maxmemory 8gb -> 2gb"},
        "root_cause_tags": ["cache_miss_storm"],
    },
}


async def seed_scenario(name: str, *, minutes: int = 60) -> dict:
    spec = SCENARIOS[name]
    service = spec["service"]
    now = datetime.now(timezone.utc)

    # 用场景名当随机种子：同一场景每次生成完全相同的数据。
    # 不能用 hash(name)：字符串 hash 按进程随机化，同一个场景每次跑结果都不同 ——
    # 评估时"准确率变化"就分不清是 prompt 的功劳还是数据波动。
    # random.Random(str) 内部走 sha512，跨进程稳定。
    rng = random.Random(name)

    logs, metrics = [], []
    for i in range(minutes):
        ts = now - timedelta(minutes=minutes - i)
        # 故障在窗口的 60% 处开始 —— 保证"之前正常、之后异常"的形态
        broken = i > minutes * 0.6

        # 正常期也有 0~2 条 ERROR/分钟（基线噪声），故障期 20~60 条/分钟。
        # 所以"第一条 ERROR 出现在变更之前"是正常的，判断异常要用密度而不是首条。
        n_err = rng.randint(20, 60) if broken else rng.randint(0, 2)
        for _ in range(n_err):
            tpl = rng.choice(spec["log_templates"])
            logs.append({
                "ts": ts + timedelta(seconds=rng.randint(0, 59)),
                "level": "ERROR", "service": service,
                "message": tpl.replace("{n}", str(rng.randint(100, 9999))),
                "trace_id": f"tr-{rng.randint(10**11, 10**12 - 1):x}",
            })
        for _ in range(rng.randint(5, 15)):
            logs.append({
                "ts": ts + timedelta(seconds=rng.randint(0, 59)),
                "level": "INFO", "service": service,
                "message": f"handled request path=/api/{rng.choice(['order', 'user', 'cart'])} status=200",
                "trace_id": None,
            })

        for metric, (lo, hi) in spec["metrics"].items():
            ratio = (i / minutes) if broken else rng.uniform(0, 0.15)
            metrics.append({
                "ts": ts, "service": service, "metric": metric,
                "value": round(lo + (hi - lo) * ratio + rng.uniform(-lo * 0.05, lo * 0.05), 3),
            })

    change_at = now - timedelta(minutes=int(minutes * 0.55))
    changes = [{
        "service": service, "kind": spec["change"]["kind"],
        "operator": rng.choice(["alice", "bob", "carol"]),
        "at": change_at, "detail": spec["change"]["detail"],
        "commit": f"{rng.randint(0x1000000, 0xFFFFFFF):x}",
    }]

    async with SessionLocal() as s:
        await s.execute(text("DELETE FROM mock_logs WHERE service = :svc"), {"svc": service})
        await s.execute(text("DELETE FROM mock_metrics WHERE service = :svc"), {"svc": service})
        await s.execute(text("DELETE FROM mock_changes WHERE service = :svc"), {"svc": service})
        if logs:
            await s.execute(
                text("INSERT INTO mock_logs (ts, level, service, message, trace_id) "
                     "VALUES (:ts, :level, :service, :message, :trace_id)"),
                logs,
            )
        if metrics:
            await s.execute(
                text("INSERT INTO mock_metrics (ts, service, metric, value) "
                     "VALUES (:ts, :service, :metric, :value)"),
                metrics,
            )
        await s.execute(
            text('INSERT INTO mock_changes (service, kind, operator, at, detail, "commit") '
                 "VALUES (:service, :kind, :operator, :at, :detail, :commit)"),
            changes,
        )
        await s.commit()

    return {
        "scenario": name, "service": service,
        "logs": len(logs), "metrics": len(metrics),
        "root_cause_tags": spec["root_cause_tags"],
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--minutes", type=int, default=60)
    args = ap.parse_args()

    names = sorted(SCENARIOS) if args.all else [args.scenario]
    for n in names:
        print(await seed_scenario(n, minutes=args.minutes))


if __name__ == "__main__":
    asyncio.run(main())
