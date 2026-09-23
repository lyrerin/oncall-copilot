# 05 · 代码指导 · API 与 UI 层

> 覆盖：FastAPI 应用装配 → 告警 webhook → ARQ 队列与 worker → 查询与 SSE → 审批接口（resume）→ 超时处理 → Streamlit 审批台。
> **这一层做完，项目从"能跑的脚本"变成"能演示的系统"。**

---

## 1. FastAPI 应用装配

```python
# app/main.py
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.graph import compile_graph, set_graph
from app.agent.mcp_client import init_mcp_tools
from app.api import approvals, incidents, webhook
from app.config import settings
from app.obs.tracing import init_langfuse

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting %s (env=%s)", settings.app_name, settings.env)

    init_langfuse()

    # MCP 工具：4 个 server 各握手一次，加载工具清单
    await init_mcp_tools()

    # checkpointer 常驻整个应用生命周期
    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.checkpointer_dsn)
        )
        await saver.setup()
        set_graph(compile_graph(saver))
        app.state.checkpointer = saver

        log.info("ready")
        yield

    log.info("shutdown")


app = FastAPI(
    title="oncall-copilot",
    version="0.1.0",
    description="AI 值班 Copilot：告警自动诊断 + 人在环审批",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router, tags=["webhook"])
app.include_router(incidents.router, prefix="/incidents", tags=["incidents"])
app.include_router(approvals.router, prefix="/incidents", tags=["approvals"])


@app.get("/healthz")
async def healthz() -> dict:
    """健康检查。liveness 用这个，readiness 应该额外探 MCP 连通性。"""
    from app.agent.mcp_client import _tools_by_server
    return {
        "ok": True,
        "mcp_servers": {k: len(v) for k, v in _tools_by_server.items()},
        "checkpointer": app.state.checkpointer is not None,
    }


@app.get("/readyz")
async def readyz() -> dict:
    """就绪探针：验证依赖真的可用，而不只是进程活着。"""
    from sqlalchemy import text
    from app.db.base import SessionLocal
    from app.services.dedup import get_redis

    checks: dict[str, bool] = {}
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:                             # noqa: BLE001
        checks["postgres"] = False
    try:
        await get_redis().ping()
        checks["redis"] = True
    except Exception:                             # noqa: BLE001
        checks["redis"] = False

    from app.agent.mcp_client import _tools_by_server
    checks["mcp"] = all(len(v) > 0 for v in _tools_by_server.values()) if _tools_by_server else False

    return {"ready": all(checks.values()), "checks": checks}
```

> **`/healthz` 和 `/readyz` 的区别要能讲**：liveness 只回答"进程要不要重启"，就绪探针回答"要不要给我导流量"。**MCP server 挂了不应该重启 API**（重启也没用），但应该暂时不导流量 —— 这就是 K8s 里 liveness/readiness 分离的意义。

---

## 2. 告警入口

```python
# app/api/webhook.py
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.db.base import session_scope
from app.db.models import Alert, Incident
from app.db import repo
from app.services.dedup import alert_fingerprint, try_claim_alert
from app.services.queue import enqueue_diagnosis

router = APIRouter()
log = logging.getLogger(__name__)


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook v4 载荷。

    只声明我们用到的字段，其余进 model_extra —— 这样 Alertmanager
    升级加字段不会让接口 422。
    """
    model_config = {"extra": "allow"}

    version: str = "4"
    groupKey: str | None = None
    status: str = "firing"
    receiver: str | None = None
    commonLabels: dict[str, Any] = Field(default_factory=dict)
    commonAnnotations: dict[str, Any] = Field(default_factory=dict)
    alerts: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/webhook/alertmanager", status_code=status.HTTP_202_ACCEPTED)
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    background: BackgroundTasks,
    x_webhook_token: str | None = Header(default=None),
) -> dict:
    """接收 Alertmanager 告警。

    设计原则：这个接口必须"快且绝不失败"。
    Alertmanager 有重试机制，我们处理太久会让它堆积。
    所以这里只做三件事：验签 → 落库 → 入队，然后立刻返回 202。
    真正的诊断在 worker 里异步跑。
    """
    # ---- 简单鉴权：真实环境应该用 HMAC 签名 ----
    if settings.webhook_token and x_webhook_token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="invalid webhook token")

    if payload.status == "resolved":
        # 告警恢复：关闭对应 incident，不启动诊断
        await _resolve_by_fingerprint(payload)
        return {"accepted": True, "action": "resolved"}

    if not payload.alerts:
        raise HTTPException(status_code=422, detail="no alerts in payload")

    first = payload.alerts[0]
    labels = first.get("labels", {})
    fingerprint = (
        payload.groupKey
        or first.get("fingerprint")
        or alert_fingerprint({"labels": labels})
    )

    # ---- 第一道去重：Redis SETNX，重复告警不进队列 ----
    if not await try_claim_alert(fingerprint):
        log.info("duplicate alert dropped: %s", fingerprint)
        return {"accepted": True, "action": "duplicate_ignored", "fingerprint": fingerprint}

    # ---- 落库 + 建 incident ----
    service = labels.get("service") or payload.commonLabels.get("service")
    alertname = labels.get("alertname") or payload.commonLabels.get("alertname", "UnknownAlert")
    severity = labels.get("severity") or payload.commonLabels.get("severity", "warning")

    async with session_scope() as s:
        incident = await _find_or_create_incident(s, service, alertname, severity, first)
        s.add(Alert(
            fingerprint=fingerprint,
            source="alertmanager",
            name=alertname,
            severity=severity,
            service=service,
            labels=labels,
            annotations=first.get("annotations", {}),
            raw=payload.model_dump(),
            starts_at=_parse_ts(first.get("startsAt")),
            status="firing",
            incident_id=incident.id,
        ))
        incident_id = incident.id

    # ---- 入队 ----
    thread_id = f"incident-{incident_id}"
    job_id = await enqueue_diagnosis(
        incident_id=incident_id, thread_id=thread_id, alert=payload.model_dump()
    )

    log.info("accepted alert %s -> incident=%s job=%s", alertname, incident_id, job_id)
    return {
        "accepted": True,
        "incident_id": incident_id,
        "thread_id": thread_id,
        "job_id": job_id,
        "fingerprint": fingerprint,
    }


async def _find_or_create_incident(s, service, alertname, severity, first_alert) -> Incident:
    """同一服务 15 分钟内的告警聚合到同一个 incident。

    真实系统里的告警关联要复杂得多（拓扑关联、时间窗、根因指纹），
    这里用"同服务 + 未关闭"作为简化规则，并且把它写成独立函数，
    方便以后替换成真正的关联算法。
    """
    existing = await s.scalar(
        select(Incident)
        .where(Incident.service == service)
        .where(Incident.status.notin_(["resolved", "escalated"]))
        .order_by(Incident.detected_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing

    incident = Incident(
        title=f"{service}: {alertname}",
        service=service,
        severity=severity,
        status="new",
        first_alert_at=_parse_ts(first_alert.get("startsAt")),
    )
    s.add(incident)
    await s.flush()
    return incident


async def _resolve_by_fingerprint(payload: AlertmanagerWebhook) -> None:
    """告警恢复时关闭 incident。"""
    first = payload.alerts[0] if payload.alerts else {}
    fp = payload.groupKey or first.get("fingerprint")
    if not fp:
        return
    async with session_scope() as s:
        alert = await s.scalar(
            select(Alert).where(Alert.fingerprint == fp).order_by(Alert.id.desc()).limit(1)
        )
        if alert is None:
            return
        alert.status = "resolved"
        alert.ends_at = datetime.now(timezone.utc)
        if alert.incident_id:
            incident = await repo.get_incident(s, alert.incident_id)
            # 只关闭还在调查中的；已完成的不动
            if incident and incident.status in ("new", "investigating", "awaiting_approval"):
                await repo.set_incident_status(s, incident.id, "resolved",
                                               summary="alert auto-resolved by Alertmanager")


def _parse_ts(value: str | None) -> datetime:
    if not value or str(value).startswith("0001"):
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
```

**四个设计点**：

| 点 | 说明 |
|---|---|
| **202 + BackgroundTasks / 队列** | 接口只做"收下来"，不诊断。Alertmanager 的重试机制决定了接收端必须幂等且极快。 |
| **`extra: allow`** | 上游加字段不该让接口 422。这是"对上游宽容"的 API 设计原则。 |
| **`status == "resolved"` 分支** | 告警恢复是正常信号，不是错误。必须处理，否则 incident 永远不关闭，看板上挂一堆假故障。 |
| **去重在 entrypoint 而不是在 Agent 里** | 重复告警根本不该进队列 —— 省一次 worker 调度。图和 ingest 节点的去重是**第二道兜底**。 |

---

## 3. ARQ 队列与 worker

### 3.1 入队封装

```python
# app/services/queue.py
import logging

from arq import create_pool
from arq.connections import RedisSettings

from app.config import settings

log = logging.getLogger(__name__)

_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(str(settings.redis_dsn)))
    return _pool


async def enqueue_diagnosis(*, incident_id: int, thread_id: str, alert: dict) -> str | None:
    """入队诊断任务。

    _job_id 显式指定成 diagnosis:{incident_id}：
      - 同一个 incident 重复入队会被 ARQ 去重（默认 1 小时内同名 job 不重复）
      - 这构成了第三道去重防线
    """
    pool = await get_pool()
    job = await pool.enqueue_job(
        "run_diagnosis_job",
        incident_id=incident_id,
        thread_id=thread_id,
        alert=alert,
        _job_id=f"diagnosis:{incident_id}",
        _queue_name="diagnosis",
    )
    log.info("enqueued diagnosis job incident=%s", incident_id)
    return job.job_id if job else None


async def enqueue_resume(*, incident_id: int, thread_id: str, decision: dict) -> str | None:
    """入队恢复任务（审批后）。

    ⚠️ job_id 必须加 approval 序号，否则会被上面的 diagnosis:{id} 去重掉！
    这是很隐蔽的 bug —— 表现形式是"审批通过了但什么都不发生"。
    """
    pool = await get_pool()
    job = await pool.enqueue_job(
        "resume_diagnosis_job",
        incident_id=incident_id,
        thread_id=thread_id,
        decision=decision,
        _job_id=f"resume:{incident_id}:{decision.get('approval_id', 0)}",
        _queue_name="diagnosis",
    )
    log.info("enqueued resume job incident=%s decision=%s",
             incident_id, decision.get("decision"))
    return job.job_id if job else None
```

> **那个 `_job_id` 的坑一定要写进 README**。`diagnosis:{incident_id}` 和 `resume:{incident_id}` 如果用了同样的 id 格式，ARQ 会把 resume 任务当重复任务丢掉。**现象是"审批通过后图不动"，排查半天才发现是队列去重。** 这种"我踩过的坑"是面试最好的素材。

### 3.2 Worker

```python
# workers/diagnosis_worker.py
"""ARQ worker。启动：arq workers.diagnosis_worker.WorkerSettings"""
import logging
from contextlib import AsyncExitStack

from arq.connections import RedisSettings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.graph import compile_graph, set_graph
from app.agent.mcp_client import init_mcp_tools
from app.agent.runner import resume_diagnosis, run_diagnosis
from app.config import settings
from app.db.base import session_scope
from app.db import repo
from app.obs.tracing import init_langfuse

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("worker")

_exit_stack: AsyncExitStack | None = None


async def startup(ctx: dict) -> None:
    """worker 启动：和 API 一样要初始化 MCP + checkpointer。

    注意：图和 API 是两份独立编译的实例（不同进程），
    但 checkpointer 指向同一个库 —— 所以 API 能让 worker 的图 resume。
    这就是"进程分离 + 状态共享"。
    """
    global _exit_stack
    init_langfuse()
    await init_mcp_tools()

    _exit_stack = AsyncExitStack()
    saver = await _exit_stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(settings.checkpointer_dsn)
    )
    await saver.setup()
    set_graph(compile_graph(saver))
    ctx["checkpointer"] = saver
    log.info("worker ready")


async def shutdown(ctx: dict) -> None:
    global _exit_stack
    if _exit_stack:
        await _exit_stack.aclose()
    log.info("worker shutdown")


async def run_diagnosis_job(
    ctx: dict, *, incident_id: int, thread_id: str, alert: dict
) -> dict:
    """诊断任务。可能正常跑完，也可能停在 interrupt 处。"""
    log.info("diagnosis start incident=%s", incident_id)
    try:
        result = await run_diagnosis(incident_id, thread_id, alert)
    except Exception as e:                        # noqa: BLE001
        log.exception("diagnosis failed incident=%s", incident_id)
        async with session_scope() as s:
            await repo.set_incident_status(s, incident_id, "failed")
            await repo.write_audit(
                s, incident_id=incident_id, actor="system", actor_type="system",
                action="diagnosis_error", detail={"error": f"{type(e).__name__}: {e}"},
            )
        raise

    if result["interrupted"]:
        log.info("incident=%s paused at approval: plan_id=%s",
                 incident_id, (result["interrupt_payload"] or {}).get("plan_id"))

    return {
        "incident_id": incident_id,
        "status": result["status"],
        "interrupted": result["interrupted"],
    }


async def resume_diagnosis_job(
    ctx: dict, *, incident_id: int, thread_id: str, decision: dict
) -> dict:
    """审批后继续执行。"""
    log.info("resume incident=%s decision=%s", incident_id, decision.get("decision"))
    result = await resume_diagnosis(thread_id, decision, incident_id)
    return {
        "incident_id": incident_id,
        "status": result["status"],
        "recovered": (result.get("verification") or {}).get("recovered"),
    }


async def check_approval_timeouts(ctx: dict) -> None:
    """定时任务：把超时未审批的方案标记为 timeout 并转人工。

    为什么需要这个：值班人可能在睡觉，没人点审批，incident 就永远挂着。
    生产系统必须有超时兜底 —— 这是"人在环"的另一半。
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.db.models import ActionPlan, Approval

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.approval_timeout_minutes
    )
    async with session_scope() as s:
        rows = (await s.execute(
            select(Approval, ActionPlan)
            .join(ActionPlan, ActionPlan.id == Approval.action_plan_id)
            .where(Approval.decision.is_(None))
            .where(Approval.requested_at < cutoff)
        )).all()

        for approval, plan in rows:
            approval.decision = "timeout"
            approval.decided_at = datetime.now(timezone.utc)
            approval.approver = "system"
            approval.comment = f"no response within {settings.approval_timeout_minutes} min"
            plan.status = "rejected"
            await repo.set_incident_status(s, plan.incident_id, "escalated",
                                           summary="approval timeout")
            await repo.write_audit(
                s, incident_id=plan.incident_id, actor="system", actor_type="system",
                action="approval_timeout", target_type="action_plan",
                target_id=str(plan.id),
            )
            # 入队 resume，让图走到 escalate 分支并收尾
            from app.services.queue import enqueue_resume
            await enqueue_resume(
                incident_id=plan.incident_id, thread_id=approval.thread_id,
                decision={"decision": "timeout", "approver": "system",
                          "approval_id": approval.id},
            )
    if rows:
        log.info("marked %d approvals as timeout", len(rows))


class WorkerSettings:
    functions = [run_diagnosis_job, resume_diagnosis_job]
    cron_jobs = [
        # 每 5 分钟扫一次超时审批
        __import__("arq.cron", fromlist=["cron"]).cron(
            check_approval_timeouts, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(str(settings.redis_dsn))
    queue_name = "diagnosis"
    max_jobs = 5              # 并发上限：同时跑 5 个诊断
    job_timeout = 900         # 单个诊断最多 15 分钟
    keep_result = 3600
```

**`max_jobs=5` 是一个有意识的容量决策**，面试可讲：

> "我把 worker 并发设成 5。这不是拍脑袋 —— 单个诊断平均调 4~6 次 LLM、8~12 次 MCP 工具，端到端 20~40 秒。并发 5 意味着峰值 5 个诊断同时跑，Postgres 连接池 10 也够。如果设成 50，Postgres 连接和 LLM 的 rate limit 会先炸。"

**`check_approval_timeouts` 是"人在环"的另一半**：有中断就必须有超时。**只做 interrupt 不做 timeout 是半成品** —— 这是面试里区分"做过 Demo"和"想过生产"的细节。

---

## 4. 查询接口与 SSE

```python
# app/api/incidents.py
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent.runner import get_graph_state
from app.db.base import get_session
from app.db.models import (
    ActionPlan, Alert, Approval, Evidence, Hypothesis, Incident,
)
from app.db import repo

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("")
async def list_incidents(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    s: AsyncSession = Depends(get_session),
) -> dict:
    """故障列表。审批台的主列表页用它。"""
    stmt = select(Incident).order_by(Incident.detected_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Incident.status == status_filter)
    rows = (await s.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "items": [
            {
                "id": i.id, "title": i.title, "service": i.service,
                "severity": i.severity, "status": i.status,
                "confidence": i.confidence, "root_cause": i.root_cause,
                "detected_at": i.detected_at.isoformat(),
                "mttr_seconds": i.mttr_seconds,
                "total_tokens": i.total_tokens,
                "total_cost_usd": float(i.total_cost_usd),
            }
            for i in rows
        ],
    }


@router.get("/{incident_id}")
async def get_incident(
    incident_id: int, s: AsyncSession = Depends(get_session)
) -> dict:
    """详情页：告警 + 证据 + 假设 + 方案 + 审批 + 执行，一次全给。

    为什么一次全给而不是分页：审批人要在一屏里看完全部上下文
    才能判断该不该批。多次请求会让审批体验变差。
    """
    incident = await repo.get_incident(s, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")

    alerts = (await s.execute(
        select(Alert).where(Alert.incident_id == incident_id).order_by(Alert.id)
    )).scalars().all()
    evidence = (await s.execute(
        select(Evidence).where(Evidence.incident_id == incident_id)
        .order_by(Evidence.round, Evidence.id)
    )).scalars().all()
    hyps = (await s.execute(
        select(Hypothesis).where(Hypothesis.incident_id == incident_id)
        .order_by(Hypothesis.round.desc(), Hypothesis.rank)
    )).scalars().all()
    plans = (await s.execute(
        select(ActionPlan).where(ActionPlan.incident_id == incident_id)
        .order_by(ActionPlan.id.desc())
    )).scalars().all()

    approvals = []
    for p in plans:
        a = await repo.get_approval(s, p.id)
        approvals.append({
            "plan_id": p.id, "decision": a.decision if a else None,
            "approver": a.approver if a else None, "comment": a.comment if a else None,
            "requested_at": a.requested_at.isoformat() if a else None,
            "decided_at": a.decided_at.isoformat() if a and a.decided_at else None,
        })

    return {
        "incident": {
            "id": incident.id, "title": incident.title, "service": incident.service,
            "severity": incident.severity, "status": incident.status,
            "root_cause": incident.root_cause, "confidence": incident.confidence,
            "summary": incident.summary, "diagnosis_rounds": incident.diagnosis_rounds,
            "first_alert_at": incident.first_alert_at.isoformat(),
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
            "mttr_seconds": incident.mttr_seconds,
            "total_tokens": incident.total_tokens,
            "total_cost_usd": float(incident.total_cost_usd),
        },
        "alerts": [
            {"id": a.id, "name": a.name, "severity": a.severity,
             "labels": a.labels, "starts_at": a.starts_at.isoformat()}
            for a in alerts
        ],
        "evidence": [
            {"id": e.id, "round": e.round, "kind": e.kind, "tool_name": e.tool_name,
             "summary": e.summary, "ok": e.ok, "error": e.error,
             "duration_ms": e.duration_ms, "request": e.request,
             "collected_at": e.collected_at.isoformat()}
            for e in evidence
        ],
        "hypotheses": [
            {"round": h.round, "rank": h.rank, "statement": h.statement,
             "confidence": h.confidence, "rationale": h.rationale,
             "adopted": h.adopted}
            for h in hyps
        ],
        "plans": [
            {"id": p.id, "action": p.action, "target": p.target, "params": p.params,
             "risk_level": p.risk_level, "status": p.status, "reason": p.reason,
             "created_at": p.created_at.isoformat()}
            for p in plans
        ],
        "approvals": approvals,
    }


@router.get("/{incident_id}/approval-payload")
async def get_approval_payload(
    incident_id: int, s: AsyncSession = Depends(get_session)
) -> dict:
    """取 interrupt 载荷，给审批台渲染待批卡片。

    数据来源是 LangGraph 的图状态（而不是业务表）—— 因为
    interrupt 载荷是"模型希望审批人看到的东西"，是图的产物。
    """
    incident = await repo.get_incident(s, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    if incident.status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"incident status is '{incident.status}', not awaiting_approval",
        )

    state = await get_graph_state(f"incident-{incident_id}")
    return {
        "incident_id": incident_id,
        "thread_id": f"incident-{incident_id}",
        "graph_next": state["next"],
        "values": {
            "status": state["values"].get("status"),
            "confidence": state["values"].get("confidence"),
            "hypotheses": state["values"].get("hypotheses", []),
            "plan": state["values"].get("plan"),
        },
    }


@router.get("/{incident_id}/stream")
async def stream_incident(incident_id: int) -> EventSourceResponse:
    """SSE 实时时间线。

    前端订阅这个端点，就能看到"Agent 正在查日志…查到 3 条证据…
    置信度 0.72…生成处置方案"的实时推进过程。
    演示时这个界面是最有冲击力的部分。
    """
    async def event_generator():
        last_seen: dict[str, int] = {"evidence": 0, "hypotheses": 0, "plans": 0}
        for _ in range(300):                    # 最多推 300 次（约 10 分钟）
            snapshot = await _collect_timeline(incident_id)
            for kind in ("evidence", "hypotheses", "plans"):
                items = snapshot[kind]
                if len(items) > last_seen[kind]:
                    new_items = items[last_seen[kind]:]
                    last_seen[kind] = len(items)
                    yield {
                        "event": kind,
                        "data": json.dumps(
                            {"kind": kind, "items": new_items}, ensure_ascii=False
                        ),
                    }
            # 状态变化也要推
            yield {
                "event": "status",
                "data": json.dumps({"status": snapshot["status"],
                                    "confidence": snapshot["confidence"],
                                    "round": snapshot["round"]}, ensure_ascii=False),
            }
            if snapshot["status"] in ("resolved", "escalated", "failed"):
                yield {"event": "done", "data": json.dumps({"status": snapshot["status"]})}
                return
            await asyncio.sleep(2)

    return EventSourceResponse(event_generator())


async def _collect_timeline(incident_id: int) -> dict:
    """每次轮询都开新 session —— 长连接不能一直持有数据库连接。"""
    from app.db.base import SessionLocal
    async with SessionLocal() as s:
        incident = await repo.get_incident(s, incident_id)
        evidence = (await s.execute(
            select(Evidence).where(Evidence.incident_id == incident_id).order_by(Evidence.id)
        )).scalars().all()
        hyps = (await s.execute(
            select(Hypothesis).where(Hypothesis.incident_id == incident_id)
            .order_by(Hypothesis.round, Hypothesis.rank)
        )).scalars().all()
        plans = (await s.execute(
            select(ActionPlan).where(ActionPlan.incident_id == incident_id).order_by(ActionPlan.id)
        )).scalars().all()

    return {
        "status": incident.status if incident else "unknown",
        "confidence": incident.confidence if incident else None,
        "round": incident.diagnosis_rounds if incident else 0,
        "evidence": [
            {"round": e.round, "tool_name": e.tool_name, "summary": e.summary,
             "ok": e.ok, "duration_ms": e.duration_ms}
            for e in evidence
        ],
        "hypotheses": [
            {"round": h.round, "rank": h.rank, "statement": h.statement,
             "confidence": h.confidence, "adopted": h.adopted}
            for h in hyps
        ],
        "plans": [
            {"id": p.id, "action": p.action, "risk_level": p.risk_level, "status": p.status}
            for p in plans
        ],
    }
```

> **SSE 里"每次轮询开新 session"是刻意的**：一个 SSE 连接可能挂 10 分钟，如果全程持有数据库连接，5 个审批台同时打开就能把连接池（10 个）耗尽。**长连接不能持有短生命周期资源** —— 这是后端基本功，面试能加分。

---

## 5. 审批接口（最关键的业务接口）

```python
# app/api/approvals.py
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import ActionPlan, Approval, Incident
from app.db import repo
from app.services.queue import enqueue_resume

router = APIRouter()
log = logging.getLogger(__name__)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    approver: str = Field(min_length=1, description="审批人标识")
    comment: str | None = None


@router.get("/pending")
async def list_pending(s: AsyncSession = Depends(get_session)) -> dict:
    """待审批列表。审批台首页。"""
    rows = (await s.execute(
        select(Approval, ActionPlan, Incident)
        .join(ActionPlan, ActionPlan.id == Approval.action_plan_id)
        .join(Incident, Incident.id == ActionPlan.incident_id)
        .where(Approval.decision.is_(None))
        .order_by(Approval.requested_at)
    )).all()

    now = datetime.now(timezone.utc)
    return {
        "count": len(rows),
        "items": [
            {
                "approval_id": a.id,
                "plan_id": p.id,
                "incident_id": i.id,
                "thread_id": a.thread_id,
                "title": i.title,
                "service": i.service,
                "severity": i.severity,
                "action": p.action,
                "target": p.target,
                "params": p.params,
                "risk_level": p.risk_level,
                "reason": p.reason,
                "confidence": i.confidence,
                "root_cause": i.root_cause,
                "requested_at": a.requested_at.isoformat(),
                "waiting_seconds": int((now - a.requested_at).total_seconds()),
                "context_snapshot": a.context_snapshot,
            }
            for a, p, i in rows
        ],
    }


@router.post("/{incident_id}/approve")
async def approve(
    incident_id: int,
    body: ApprovalDecision,
    s: AsyncSession = Depends(get_session),
) -> dict:
    """审批通过或驳回。批准后唤醒图继续执行。

    这个接口是「人在环」的落地入口，必须做到：
      1. 幂等：重复提交同一个人审批不报错也不重复执行
      2. 状态校验：只接受 awaiting_approval 状态的 incident
      3. 审计：谁在什么时候批了什么，全部留痕
    """
    incident = await repo.get_incident(s, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")

    # ---- 状态校验：防止重复审批 ----
    if incident.status == "executing":
        raise HTTPException(status_code=409, detail="already executing, approve ignored")
    if incident.status not in ("awaiting_approval",):
        raise HTTPException(
            status_code=409,
            detail=f"incident is '{incident.status}', only 'awaiting_approval' can be decided",
        )

    plan = await s.scalar(
        select(ActionPlan)
        .where(ActionPlan.incident_id == incident_id)
        .where(ActionPlan.status == "awaiting_approval")
        .order_by(ActionPlan.id.desc())
        .limit(1)
    )
    if plan is None:
        raise HTTPException(status_code=409, detail="no pending action plan")

    approval = await repo.decide_approval(
        s, plan.id, decision=body.decision,
        approver=body.approver, comment=body.comment,
    )
    if approval is None:
        raise HTTPException(status_code=500, detail="approval record missing")

    plan.status = "approved" if body.decision == "approved" else "rejected"

    await repo.write_audit(
        s, incident_id=incident_id, actor=body.approver, actor_type="human",
        action=body.decision, target_type="action_plan", target_id=str(plan.id),
        detail={"comment": body.comment, "risk_level": plan.risk_level,
                "action": plan.action},
    )

    decision_payload = {
        "decision": body.decision,
        "approver": body.approver,
        "comment": body.comment,
        "approval_id": approval.id,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    # 会话提交后再入队 —— 否则 worker 可能读不到刚写的审批记录
    await s.commit()

    job_id = await enqueue_resume(
        incident_id=incident_id, thread_id=approval.thread_id,
        decision=decision_payload,
    )

    log.info("incident=%s plan=%s %s by %s -> job=%s",
             incident_id, plan.id, body.decision, body.approver, job_id)

    return {
        "ok": True,
        "incident_id": incident_id,
        "plan_id": plan.id,
        "decision": body.decision,
        "approver": body.approver,
        "job_id": job_id,
        "next": "executing" if body.decision == "approved" else "escalated",
    }


@router.get("/{incident_id}/audit")
async def get_audit(
    incident_id: int, s: AsyncSession = Depends(get_session)
) -> dict:
    """审计流水。面试演示"可追溯"时打开这个页面。"""
    from app.db.models import AuditLog
    rows = (await s.execute(
        select(AuditLog).where(AuditLog.incident_id == incident_id)
        .order_by(AuditLog.created_at)
    )).scalars().all()
    return {
        "incident_id": incident_id,
        "entries": [
            {"at": r.created_at.isoformat(), "actor": r.actor,
             "actor_type": r.actor_type, "action": r.action,
             "target": f"{r.target_type}:{r.target_id}" if r.target_type else None,
             "detail": r.detail}
            for r in rows
        ],
    }
```

**`approve` 接口的五个细节，每个都是面试点**：

| 细节 | 为什么 |
|---|---|
| **`await s.commit()` 在入队之前** | worker 是另一个进程，读的是已提交的数据。如果先入队后提交，worker 可能读到旧的 `approvals` 记录（decided_at 还是 NULL），导致图以为还没审批。**这是个真实的竞态，会表现为"偶发审批不生效"。** |
| **状态校验拒绝非 `awaiting_approval`** | 防止重复点击导致两次 resume。 |
| **`plan.status` 和 `approval.decision` 都写** | 两个视角各自完整。查询"哪些方案被驳回了"不用 join 审批表。 |
| **`approval_id` 塞进 decision 载荷** | executor 侧要求 HIGH 风险动作带 `approval_id`。这个 ID 从 resume 载荷一路传到 MCP server，**形成审批→执行的证据链**。 |
| **`write_audit` 记录 risk_level 和 action** | 审计日志不能只记"谁批了"，要记"批了什么风险级别的什么动作"。事后追责要看这个。 |

---

## 6. Embedding 服务

```python
# app/services/embedding.py
"""本地 embedding + 历史故障检索。

用本地模型而不是在线 API：诊断链路上多一个外部依赖就多一个故障点，
而且 embedding 调用量大（每次诊断 1~2 次）。
"""
import logging
from functools import lru_cache

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import IncidentKnowledge

log = logging.getLogger(__name__)


@lru_cache
def _model():
    """懒加载模型，进程内单例。

    sentence-transformers 加载模型要几秒，绝不能每次调用都加载。
    lru_cache 在这里等同于"进程级单例"。
    """
    from sentence_transformers import SentenceTransformer
    log.info("loading embedding model %s", settings.embedding_model)
    return SentenceTransformer(settings.embedding_model)


async def embed_text(text: str) -> list[float]:
    """同步模型在异步环境里要放到线程池跑，否则阻塞事件循环。"""
    import asyncio
    loop = asyncio.get_running_loop()
    vec = await loop.run_in_executor(
        None,
        lambda: _model().encode(text, normalize_embeddings=True).tolist(),
    )
    if len(vec) != settings.embedding_dim:
        raise ValueError(
            f"embedding dim mismatch: got {len(vec)}, "
            f"expected {settings.embedding_dim}. "
            f"Check EMBEDDING_MODEL vs vector({settings.embedding_dim}) in the migration."
        )
    return vec


async def find_similar_incidents_text(
    query: str, *, limit: int = 3, min_score: float = 0.7
) -> list[tuple[str, str, float]]:
    """返回 [(title, content, similarity), ...]"""
    emb = await embed_text(query)
    distance = IncidentKnowledge.embedding.cosine_distance(emb).label("distance")
    stmt = (
        select(IncidentKnowledge, distance)
        .where(IncidentKnowledge.embedding.is_not(None))
        .where(IncidentKnowledge.kind == "incident")
        .order_by(distance)
        .limit(limit)
    )
    async with SessionLocal() as s:
        rows = (await s.execute(stmt)).all()
    out = []
    for kb, dist in rows:
        score = 1.0 - float(dist)
        if score >= min_score:
            out.append((kb.title, kb.content, score))
    return out
```

> **`run_in_executor` 这一步不能省**。`SentenceTransformer.encode` 是 CPU 密集的同步调用，直接在 async 函数里调会**阻塞整个事件循环** —— 表现为"诊断期间所有接口都卡住"。这个坑在本项目里特别容易踩，因为 embedding 在 `synthesize` 和 `report` 两个节点都会调。

---

## 7. Streamlit 审批台

```python
# ui/approval_console.py
"""值班审批台。

三个页面：
  1. 待审批 —— 值班人的主战场
  2. 故障时间线 —— 看 Agent 的推理过程（演示用）
  3. 指标看板 —— 成本 / 准确率 / MTTR

启动：streamlit run ui/approval_console.py
"""
import time

import httpx
import pandas as pd
import streamlit as st

API = "http://api:8000"

st.set_page_config(page_title="oncall-copilot 审批台", layout="wide")

RISK_COLOR = {
    "LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴",
}
STATUS_LABEL = {
    "new": "新建", "investigating": "调查中", "awaiting_approval": "待审批",
    "executing": "执行中", "verifying": "验证中", "resolved": "已恢复",
    "escalated": "已转人工", "failed": "失败",
}


@st.cache_data(ttl=2)
def fetch(path: str) -> dict:
    try:
        r = httpx.get(f"{API}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:                        # noqa: BLE001
        return {"_error": str(e)}


def post(path: str, body: dict) -> dict:
    try:
        r = httpx.post(f"{API}{path}", json=body, timeout=30)
        return {"_status": r.status_code, **r.json()}
    except Exception as e:                        # noqa: BLE001
        return {"_error": str(e)}


# ============================ 页面 1：待审批 ============================
def page_pending() -> None:
    st.header("⏳ 待审批处置方案")
    st.caption("值班人在这里做决定。高风险动作必须人工确认。")

    data = fetch("/incidents/pending")
    if "_error" in data:
        st.error(f"API 不可达：{data['_error']}")
        return
    if not data["items"]:
        st.success("当前没有待审批方案 🎉")
        return

    for item in data["items"]:
        waited = item["waiting_seconds"]
        risk = item["risk_level"]
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])

            with c1:
                st.subheader(f"{RISK_COLOR.get(risk, '⚪')} {item['title']}")
                st.markdown(
                    f"**动作**：`{item['action']}` → `{item['target']}`  \n"
                    f"**风险等级**：{risk} ｜ **置信度**：{item['confidence']:.2f} "
                    f"｜ **已等待**：{waited // 60}分{waited % 60}秒"
                )
                st.markdown(f"**根因判断**：{item['root_cause'] or '（无）'}")
                st.info(f"**为什么这么做**：{item['reason'] or '（无说明）'}")

                with st.expander("📋 Agent 采集的证据"):
                    snap = item.get("context_snapshot") or {}
                    for e in snap.get("evidence_summary", []):
                        st.markdown(f"- {e}")

                with st.expander("🔧 将要执行的参数（JSON）"):
                    st.json(item["params"])

            with c2:
                st.markdown("### 审批")
                approver = st.text_input(
                    "审批人", value="alice", key=f"ap_{item['approval_id']}"
                )
                comment = st.text_area(
                    "备注", key=f"cm_{item['approval_id']}", height=80,
                    placeholder="驳回时请说明原因，会写进审计日志",
                )
                if st.button("✅ 批准并执行", key=f"ok_{item['approval_id']}",
                             type="primary", use_container_width=True):
                    res = post(f"/incidents/{item['incident_id']}/approve",
                               {"decision": "approved", "approver": approver,
                                "comment": comment})
                    if res.get("ok"):
                        st.success(f"已批准，任务已入队（job={res.get('job_id')}）")
                        st.cache_data.clear()
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"失败：{res}")

                if st.button("❌ 驳回并转人工", key=f"no_{item['approval_id']}",
                             use_container_width=True):
                    res = post(f"/incidents/{item['incident_id']}/approve",
                               {"decision": "rejected", "approver": approver,
                                "comment": comment or "驳回"})
                    if res.get("ok"):
                        st.warning("已驳回，转人工处理")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"失败：{res}")


# ========================= 页面 2：故障时间线 =========================
def page_timeline() -> None:
    st.header("🔍 故障时间线")
    st.caption("看 Agent 一步一步怎么推出来的 —— 演示和复盘都用这个页面。")

    data = fetch("/incidents?limit=100")
    if "_error" in data:
        st.error(f"API 不可达：{data['_error']}")
        return

    options = {f"#{i['id']} {i['title']} [{STATUS_LABEL.get(i['status'], i['status'])}]": i["id"]
               for i in data["items"]}
    if not options:
        st.info("还没有故障记录。用 `python -m mockenv.alertgen --once` 灌一条。")
        return

    chosen = st.selectbox("选择故障事件", list(options))
    incident_id = options[chosen]
    detail = fetch(f"/incidents/{incident_id}")
    inc = detail["incident"]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("状态", STATUS_LABEL.get(inc["status"], inc["status"]))
    m2.metric("置信度", f"{inc['confidence']:.2f}" if inc["confidence"] else "—")
    m3.metric("调查轮次", inc["diagnosis_rounds"])
    m4.metric("Token 消耗", inc["total_tokens"])
    m5.metric("成本 (USD)", f"${inc['total_cost_usd']:.4f}")

    st.divider()
    st.subheader("根因判断")
    st.success(inc["root_cause"] or "（未得出结论）")

    # ---- 按轮次展示证据和假设 ----
    rounds = sorted({e["round"] for e in detail["evidence"]} | {1})
    for r in rounds:
        with st.expander(f"第 {r} 轮调查", expanded=(r == max(rounds))):
            st.markdown("**采集的证据**")
            evs = [e for e in detail["evidence"] if e["round"] == r]
            if evs:
                st.dataframe(pd.DataFrame([
                    {"工具": e["tool_name"], "类型": e["kind"],
                     "耗时(ms)": e["duration_ms"],
                     "结果": "✅" if e["ok"] else f"❌ {e['error']}",
                     "摘要": e["summary"]}
                    for e in evs
                ]), use_container_width=True, hide_index=True)
            else:
                st.caption("本轮无证据")

            st.markdown("**根因假设**")
            hyps = [h for h in detail["hypotheses"] if h["round"] == r]
            for h in hyps:
                icon = "🥇" if h["rank"] == 1 else f"#{h['rank']}"
                st.markdown(f"{icon} **{h['statement']}** （置信度 {h['confidence']:.2f}）")
                st.caption(h["rationale"] or "")

    st.divider()
    st.subheader("处置与审计")
    st.dataframe(pd.DataFrame(detail["plans"]), use_container_width=True, hide_index=True)

    audit = fetch(f"/incidents/{incident_id}/audit")
    if audit.get("entries"):
        st.dataframe(pd.DataFrame([
            {"时间": a["at"], "操作者": a["actor"], "类型": a["actor_type"],
             "动作": a["action"], "目标": a["target"]}
            for a in audit["entries"]
        ]), use_container_width=True, hide_index=True)


# ========================== 页面 3：指标看板 ==========================
def page_metrics() -> None:
    st.header("📊 运营指标")
    st.caption("这一页是给面试官看的 —— 证明你量化了效果，而不只是做了个 Demo。")

    data = fetch("/incidents?limit=200")
    if "_error" in data:
        st.error(f"API 不可达：{data['_error']}")
        return
    items = data["items"]
    if not items:
        st.info("暂无数据")
        return

    df = pd.DataFrame(items)
    total = len(df)
    resolved = (df["status"] == "resolved").sum()
    escalated = (df["status"] == "escalated").sum()
    awaiting = (df["status"] == "awaiting_approval").sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总故障数", total)
    c2.metric("自动恢复率", f"{resolved / total:.1%}")
    c3.metric("转人工率", f"{escalated / total:.1%}")
    c4.metric("待审批", awaiting)

    c5, c6, c7 = st.columns(3)
    c5.metric("总 Token", int(df["total_tokens"].sum()))
    c6.metric("总成本", f"${df['total_cost_usd'].sum():.4f}")
    avg_cost = df["total_cost_usd"].mean()
    c7.metric("单次平均成本", f"${avg_cost:.4f}")

    mttr = df["mttr_seconds"].dropna()
    if len(mttr):
        st.metric("平均 MTTR", f"{mttr.mean() / 60:.1f} 分钟")

    st.divider()
    st.subheader("各服务故障分布")
    st.bar_chart(df.groupby("service").size())

    st.subheader("置信度分布")
    conf = df["confidence"].dropna()
    if len(conf):
        st.bar_chart(pd.cut(conf, bins=[0, .3, .5, .6, .7, .85, 1.0]).value_counts().sort_index())

    st.subheader("明细")
    st.dataframe(df, use_container_width=True, hide_index=True)


# ============================== 主入口 ==============================
def main() -> None:
    st.sidebar.title("🚨 oncall-copilot")
    st.sidebar.caption("AI 值班 Copilot · 人在环审批")
    page = st.sidebar.radio("导航", ["待审批", "故障时间线", "运营指标"])
    st.sidebar.divider()
    st.sidebar.markdown(
        "**架构**\n\n"
        "Alertmanager → FastAPI → Redis/ARQ → LangGraph → MCP ×4 → Postgres"
    )
    if st.sidebar.button("🔄 刷新"):
        st.cache_data.clear()
        st.rerun()

    if page == "待审批":
        page_pending()
    elif page == "故障时间线":
        page_timeline()
    else:
        page_metrics()


if __name__ == "__main__":
    main()
```

**为什么这一页值得花一周做**（面试防御用）：

> 面试官问"你这个是给谁用的"，能直接打开审批台说"值班人打开这个页面，看到待审批卡片，上面有根因、置信度、证据摘要、将要执行的参数和风险等级，点批准后 3 秒内开始执行，全过程写进审计" —— **这是"应用"和"脚本"的分界线**。

---

## 8. 端到端演示脚本

写进 README，面试时照着跑：

```bash
# ============ 0. 启动 ============
docker compose up -d
docker compose exec postgres psql -U oncall -c "CREATE DATABASE langfuse;"   # 首次
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m mockenv.generate --all

# ============ 1. 灌一条告警，触发全流程 ============
docker compose run --rm api python -m mockenv.alertgen --once
# 输出：memory_leak -> 202 {"incident_id": 3, "thread_id": "incident-3", ...}

# ============ 2. 看 Agent 跑完调查并停在审批 ============
sleep 25
curl -s localhost:8000/incidents/3 | jq '{
  status: .incident.status,
  root_cause: .incident.root_cause,
  confidence: .incident.confidence,
  evidence: [.evidence[].tool_name],
  plan: .plans[0].action
}'
# 期望：status=awaiting_approval, confidence≈0.8,
#       evidence=[list_changes, top_error_patterns, detect_anomaly, ...],
#       plan=restart_deployment

# ============ 3. 打开审批台批准 ============
# 浏览器：http://localhost:8501 → 待审批 → 看到卡片 → 点"批准并执行"

# ============ 4. 验证执行与恢复 ============
sleep 15
curl -s localhost:8000/incidents/3 | jq '{
  status: .incident.status, mttr: .incident.mttr_seconds,
  tokens: .incident.total_tokens, cost: .incident.total_cost_usd,
  verification: .incident.summary
}'
# 期望：status=resolved, mttr 有值, tokens/cost 非零

# ============ 5. 看审计流水（证明可追溯）============
curl -s localhost:8000/incidents/3/audit | jq '.entries[] | {actor, action, at}'
# 期望：agent investigate → human approved → agent execute → agent verify

# ============ 6. 验证幂等（重复 resume 不重复执行）============
curl -s -X POST localhost:8000/incidents/3/approve \
  -H 'content-type: application/json' \
  -d '{"decision":"approved","approver":"alice"}'
# 期望：409 already executing / only awaiting_approval can be decided

# ============ 7. 打开 Langfuse 看 trace ============
# 浏览器：http://localhost:3000
# 能看到每个节点的耗时、每个 LLM 调用的 token 和成本
```

**第 6 步是演示的杀手锏**：主动做一次"重复审批"，让面试官看到系统拒绝它。**证明幂等不是嘴上说的**。

另外准备一个"重启进程后 resume"的演示（这是技术巅峰）：

```bash
# 图停在审批后：
docker compose restart worker          # 杀掉 worker，内存里的图状态全丢
# 然后审批：
curl -X POST localhost:8000/incidents/4/approve -d '{"decision":"approved","approver":"alice"}' -H 'content-type: application/json'
# 结果：worker 重新拉起，从 Postgres 的 checkpoint 恢复，继续执行成功
```

> 面试话术：*"我把 worker 停掉再审批，执行照样成功 —— 因为图状态在 Postgres 里，不在内存里。这就是为什么必须用 checkpointer，而不是把 Agent 跑在一个 HTTP 请求里。"*

---

## 9. 本层验收清单

- [ ] `docker compose up -d` 后 `curl localhost:8000/readyz` 返回 `{"ready": true}`
- [ ] `curl -X POST localhost:8000/webhook/alertmanager -d @sample.json` 返回 202 和 `thread_id`
- [ ] **重复发同一个 webhook，第二次返回 `duplicate_ignored`**（验证 Redis 去重）
- [ ] 发 `status: resolved` 的 webhook，incident 状态变 `resolved`
- [ ] `/incidents/pending` 能列出待审批卡片，`waiting_seconds` 在增长
- [ ] 审批台三个页面都能打开，待审批页点批准后 3 秒内 incident 进入 `executing`
- [ ] **审批通过后再调一次 approve，返回 409**（验证状态机校验）
- [ ] `/incidents/{id}/stream` 用 `curl -N` 能看到 SSE 事件流
- [ ] 把 `approval_timeout_minutes` 改成 1，等 5 分钟看 cron 是否把审批标成 timeout 并转人工
- [ ] `docker compose restart worker` 后审批仍能成功执行（checkpointer 终极验收）
- [ ] `/healthz` 和 `/readyz` 在停掉一个 MCP server 后行为不同（readyz=false，healthz 仍 true）

**第 10 条要专门准备成演示**，因为它同时证明了：进程分离、状态共享、checkpointer 的必要性。三个面试考点一次打穿。

---

## 下一步

系统能跑了，开始量化 → `06-可观测与评估.md`
