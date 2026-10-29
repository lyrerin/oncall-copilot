"""mcp_servers/executor_server.py —— mcp-executor :8004（最关键的一个）。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 922 行起（§5.5 执行器 MCP Server）。
职责：暴露执行动作的工具。全部带 dry_run 参数。
三条硬约束（写进 server，不靠 prompt）：
  1. 动作必须在 ACTION_CATALOG 白名单里，否则直接抛错；
  2. apply_action 先查 idempotency_key，已执行则原样返回首次结果；
  3. CRITICAL 级动作要求 approval_id 非空，否则拒绝。
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastmcp import FastMCP
from sqlalchemy import select

from app.db import repo
from app.db.base import SessionLocal
from app.db.models import Execution

log = logging.getLogger("mcp.executor")

mcp = FastMCP(
  name = 'executor',
  instructions = (
    "执行运维动作。所有动作都必须先 dry_run_action 预演。"
    "apply_action 会真正改变生产环境，只在获得人工审批后调用。"
  ),
)
# 动作白名单：不在表里的动作一律拒绝
ACTION_CATALOG: dict[str, dict] = {
    "restart_deployment": {
        "risk": "MEDIUM", "requires_approval": True,
        "description": "滚动重启服务的所有副本",
        "params_schema": {"service": "str", "grace_period": "int=30"},
    },
    "scale_deployment": {
        "risk": "MEDIUM", "requires_approval": True,
        "description": "调整服务副本数",
        "params_schema": {"service": "str", "replicas": "int"},
    },
    "rollback_deployment": {
        "risk": "HIGH", "requires_approval": True,
        "description": "回滚到上一个发布版本",
        "params_schema": {"service": "str", "to_revision": "str"},
    },
    "clear_cache": {
        "risk": "LOW", "requires_approval": False,
        "description": "清理缓存命名空间（低风险，可自动执行）",
        "params_schema": {"namespace": "str"},
    },
}

_FAKE_RESULTS: dict[str, dict] = {}

@mcp.tool
async def get_action_catalog() -> dict:
    """列出所有允许执行的动作及其风险等级。生成处置方案前必须先调这个。"""
    return {"actions": [{"action": k, **v} for k, v in ACTION_CATALOG.items()]}

@mcp.tool
async def dry_run_action(action: str, target: str, params: dict | None = None) -> dict:
    """预演一个动作，返回将要发生什么，但不产生任何实际影响。

    Args:
        action: 动作名，必须在 get_action_catalog 列出的清单里
        target: 目标服务名
        params: 动作参数
    """
    spec = ACTION_CATALOG.get(action)
    if not spec:
        raise ValueError(
            f"action '{action}' is not in the allow-list. "
            f"allowed: {sorted(ACTION_CATALOG)}"
        )
    return {
        "dry_run": True,
        "action": action,
        "target": target,
        "params": params or {},
        "risk": spec["risk"],
        "requires_approval": spec["requires_approval"],
        "effect": spec["description"],
        "warning": (
            f"This will affect {target} in production. "
            f"Risk level {spec['risk']}."
        ),
    }

@mcp.tool
async def apply_action(
    action: str,
    target: str,
    params: dict | None = None,
    idempotency_key: str | None = None,
    approval_id: int | None = None,
    plan_id: int | None = None,
) -> dict:
    """真正执行运维动作。危险操作，只在审批通过后调用。

    Args:
        action: 动作名，必须在白名单内
        target: 目标服务名
        params: 动作参数
        idempotency_key: 幂等键，必填。同键重复调用不会重复执行
        approval_id: 审批记录 ID。HIGH/CRITICAL 风险动作必填
        plan_id: 本次动作所属的 action_plan id。executions.action_plan_id
                 是 NOT NULL，所以"首次执行"必须给（重复调用时不再校验）
    """
    params = params or {}

    # ---- 约束 1：白名单 ----
    spec = ACTION_CATALOG.get(action)
    if not spec:
        raise ValueError(f"action '{action}' is not in the allow-list")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")

    # ---- 约束 3：高危动作必须有审批号 ----
    if spec["risk"] in ("HIGH", "CRITICAL") and approval_id is None:
        raise ValueError(
            f"action '{action}' has risk {spec['risk']} and requires a valid approval_id"
        )

    # ---- 约束 2：幂等占用。先查键，没有就插一行 running；插入靠唯一索引兜底 ----
    # 用 repo.claim_execution 而不是在 server 里手写 SELECT/INSERT：
    # 两个 worker 同时进来时，第二个人撞唯一索引 → 回滚后重新查 → 拿到同一行。
    if plan_id is None:
        raise ValueError("plan_id is required: executions 必须挂在 action_plan 上")
    async with SessionLocal() as s:
        row, is_new = await repo.claim_execution(
            s,
            plan_id=plan_id,
            action=action,
            target=target,
            params=params,
            idempotency_key=idempotency_key,
        )
        await s.commit()

        if row is None:
            raise ValueError(f"plan_id {plan_id} does not exist")
        if not is_new:
            if row.status == "succeeded":
                log.info("idempotent hit: key=%s reuse previous result", idempotency_key)
                return {
                    "idempotent_replay": True,
                    "action": action, "target": target,
                    "status": row.status,
                    "result": row.result,
                    "executed_at": row.finished_at.isoformat() if row.finished_at else None,
                }
            if row.status == "running":
                # 同 key 正在执行中（另一个人/另一个 worker 抢到了），本次不重复执行
                return {
                    "idempotent_replay": False,
                    "in_flight": True,
                    "action": action, "target": target,
                    "status": row.status,
                    "message": "same idempotency_key is executing elsewhere",
                }
            # failed / skipped：允许重试，落到下面真的执行一次

    # ---- 真的执行（mock：sleep 模拟耗时）----
    started = datetime.now(timezone.utc)
    await asyncio.sleep(1.5)
    result = {
        "action": action,
        "target": target,
        "params": params,
        "revision_before": "v42",
        "revision_after": "v42" if action != "rollback_deployment" else params.get("to_revision", "v41"),
        "affected_replicas": 6,
        "dry_run": False,
    }
    finished = datetime.now(timezone.utc)

    # 回写执行记录，让幂等短路在下次生效
    async with SessionLocal() as s:
        row = await s.scalar(
            select(Execution).where(Execution.idempotency_key == idempotency_key)
        )
        if row is not None:
            row.status = "succeeded"
            row.result = result
            row.finished_at = finished
            row.duration_ms = int((finished - started).total_seconds() * 1000)
            await s.commit()

    _FAKE_RESULTS[idempotency_key] = result
    return {
        "idempotent_replay": False,
        "status": "succeeded",
        "action": action, "target": target,
        "result": result,
        "duration_ms": int((finished - started).total_seconds() * 1000),
    }

@mcp.tool
async def get_action_status(idempotency_key: str) -> dict:
    """查询某个动作的执行状态。"""
    async with SessionLocal() as s:
        row = await s.scalar(
            select(Execution).where(Execution.idempotency_key == idempotency_key)
        )
    if row is None:
        return {"found": False, "idempotency_key": idempotency_key}
    return {
        "found": True, "idempotency_key": idempotency_key,
        "action": row.action, "target": row.target, "status": row.status,
        "result": row.result, "error": row.error,
        "duration_ms": row.duration_ms,
    }


@mcp.tool
async def rollback(idempotency_key: str) -> dict:
    """回滚之前执行过的一个动作。用于执行后验证失败的情况。"""
    status = await get_action_status(idempotency_key)
    if not status.get("found"):
        return {"ok": False, "reason": "execution not found"}
    await asyncio.sleep(1.0)
    return {
        "ok": True, "idempotency_key": idempotency_key,
        "message": f"rolled back {status['action']} on {status['target']}",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8004)