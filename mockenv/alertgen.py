"""mockenv/alertgen.py —— 定时灌假告警。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 1317 行起（§6.2 告警发生器）。
职责：按间隔朝 app/api/webhook.py 发 Alertmanager 格式的假告警，
      用来端到端演示"告警进 → 调查 → 审批 → 执行"整条链路。
"""
# mockenv/alertgen.py
"""定时向自家 webhook 灌假告警，模拟真实告警流。

用法：python -m mockenv.alertgen --interval 120
"""
import argparse
import asyncio
import random
import time
from datetime import datetime, timezone

import httpx

from mockenv.generate import SCENARIOS

API = "http://api:8000"


def build_alertmanager_payload(scenario: str) -> dict:
    """构造一个严格符合 Alertmanager webhook 格式的载荷。

    格式对齐真实 Alertmanager，后续换成真告警时不用改接收端。
    """
    spec = SCENARIOS[scenario]
    service = spec["service"]
    names = {
        "memory_leak": ("HighMemoryUsage", "critical"),
        "bad_release": ("HighErrorRate", "critical"),
        "db_deadlock": ("DBPoolExhausted", "warning"),
        "cpu_saturation": ("HighCPUUsage", "warning"),
        "cache_miss_storm": ("LowCacheHitRate", "warning"),
    }
    alertname, severity = names[scenario]
    now = datetime.now(timezone.utc).isoformat()

    return {
        "version": "4",
        "groupKey": f'{{}}:{{alertname="{alertname}", service="{service}"}}',
        "status": "firing",
        "receiver": "oncall-copilot",
        "groupLabels": {"alertname": alertname, "service": service},
        "commonLabels": {"alertname": alertname, "service": service, "severity": severity},
        "commonAnnotations": {
            "summary": f"{service} {alertname}",
            "description": f"scenario={scenario}; see runbook",
        },
        "externalURL": "http://alertmanager:9093",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": alertname, "service": service, "severity": severity,
                "instance": f"{service}-{random.randint(0, 5)}",
                "scenario": scenario,        # 只有 mock 才有，评估集用它做 ground truth
            },
            "annotations": {"summary": f"{service} {alertname} firing"},
            "startsAt": now,
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": f"http://prometheus:9090/graph?g0.expr={alertname}",
            "fingerprint": f"fp-{scenario}",
        }],
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            scenario = random.choice(sorted(SCENARIOS))
            payload = build_alertmanager_payload(scenario)
            try:
                r = await client.post(f"{API}/webhook/alertmanager", json=payload)
                print(f"[{time.strftime('%H:%M:%S')}] {scenario} -> {r.status_code} {r.text[:120]}")
            except Exception as e:
                print(f"send failed: {e}")
            if args.once:
                return
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())