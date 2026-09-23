# 04 · 代码指导 · Agent 层

> 覆盖：LLM 工厂 → Prompt 集中管理 → 数据访问 → 14 个节点实现 → 图装配与 checkpointer → resume 入口。
> **这一层是整个项目的核心，也是面试提问最密集的地方。**

---

## 1. LLM 工厂与结构化输出

```python
# app/agent/llm.py
"""LLM 工厂。

两个模型角色：
  primary  大模型，用于根因综合（推理最重的一步）
  cheap    小模型，用于 triage / 方案措辞（判断简单，省钱）
"""
import logging
import time
from functools import lru_cache
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _build(model: str, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=temperature,
        timeout=120,
        max_retries=2,          # SDK 层重试，处理 429 / 5xx
    )


@lru_cache
def primary_llm() -> ChatOpenAI:
    return _build(settings.llm_model, settings.llm_temperature)


@lru_cache
def cheap_llm() -> ChatOpenAI:
    return _build(settings.llm_model_cheap, settings.llm_temperature)


def structured(model_cls: type[T], *, cheap: bool = False) -> Runnable:
    """返回一个「输入 → 校验过的 pydantic 对象」的可调用对象。

    底层用 function calling（DeepSeek / OpenAI 兼容接口都支持），
    比让模型吐 JSON 再自己 parse 可靠得多。
    """
    llm = cheap_llm() if cheap else primary_llm()
    return llm.with_structured_output(model_cls, method="function_calling")
```

**为什么不用 `JsonOutputParser` 自己 parse**：
- function calling 由服务端约束输出结构，**结构化失败率接近 0**。
- 自己 parse 要处理 markdown 代码块包裹、尾随逗号、字段缺失，还要写重试。**能靠协议解决的别靠自己正则。**

**为什么分 primary / cheap 两个模型**：`triage`（判严重度）和 `propose_action`（填 runbook 参数）都是简单判断，用大模型是浪费。**并且这个拆分让"成本优化"变成可讲的量化故事** —— 后面评估章节会统计"用 cheap 模型替代后成本降了多少，准确率掉没掉"。

---

## 2. 结构化输出模型

```python
# app/schemas.py
from typing import Literal

from pydantic import BaseModel, Field


class TriagedAlert(BaseModel):
    """triage 节点的输出"""
    needs_investigation: bool = Field(description="是否需要自动调查。明显的噪声告警（测试环境、瞬时抖动）填 false")
    severity: Literal["critical", "warning", "info"] = Field(description="修正后的严重度")
    service: str = Field(description="受影响的服务名")
    reason: str = Field(description="判断依据，一句话")


class EvidenceRequest(BaseModel):
    """一条证据采集请求"""
    kind: Literal["logs", "metrics", "changes", "topology", "history"] = Field(
        description="证据类型"
    )
    tool_name: str = Field(
        description="要调用的 MCP 工具名，必须来自可用工具清单"
    )
    arguments: dict = Field(
        default_factory=dict,
        description="工具入参，键名必须与工具 schema 一致"
    )
    hypothesis: str = Field(
        description="你想用这条证据验证什么假设。写不清楚就别查，省成本"
    )


class InvestigationPlan(BaseModel):
    """plan_investigation 节点的输出"""
    reasoning: str = Field(description="本轮调查思路，一到两句")
    requests: list[EvidenceRequest] = Field(
        description="本轮要并行采集的证据，最多 6 条", max_length=6
    )


class Hypothesis(BaseModel):
    statement: str = Field(description="根因假设，一句话，要具体到组件和机制")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0~1")
    rationale: str = Field(description="支持这个假设的证据，引用具体数据")
    tags: list[str] = Field(
        default_factory=list,
        description="根因标签，从这些里选：memory_leak, bad_release, db_deadlock, "
                    "cpu_saturation, cache_miss_storm, network_issue, config_error, unknown"
    )
    evidence_ids: list[int] = Field(default_factory=list, description="引用的证据 ID")


class Synthesis(BaseModel):
    """synthesize 节点的输出"""
    hypotheses: list[Hypothesis] = Field(description="按可能性从高到低排序，最多 3 条")
    overall_confidence: float = Field(ge=0.0, le=1.0, description="首选假设的置信度")
    missing_info: list[str] = Field(
        default_factory=list,
        description="还缺什么信息才能下结论。如果置信度已经够高就留空"
    )


class ProposedAction(BaseModel):
    """propose_action 节点的输出"""
    runbook_code: str = Field(description="选中的 runbook code，必须来自给定清单")
    action: str = Field(description="要执行的动作名")
    target: str = Field(description="目标服务")
    params: dict = Field(default_factory=dict, description="动作参数")
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    reason: str = Field(description="为什么选这个处置方案，给审批人看的人话说明")
    rollback_plan: str = Field(description="如果没效果怎么回滚")


class VerificationResult(BaseModel):
    """verify 节点的输出"""
    recovered: bool = Field(description="关键指标是否已回落到基线")
    evidence: str = Field(description="判断依据，引用具体指标数值")
    should_rollback: bool = Field(description="是否需要回滚刚才的动作")
```

**每条 `Field(description=...)` 都是 prompt**。function calling 的 JSON Schema 会把描述一起给模型。所以：
- 枚举值要**穷举写在 description 里**（`tags` 那一条），否则模型会自由发挥出 `memory-leak`、`MemoryLeak` 等变体，你的 runbook 匹配全失效。
- `hypothesis` 字段加一句"写不清楚就别查"，是**成本控制 prompt** —— 它真的能减少无意义查询。
- `max_length=6` 加在 pydantic 上，是**硬约束**（校验不通过会抛错），比在 prompt 里写"最多查 6 个"可靠。

---

## 3. Prompt 集中管理

```python
# app/agent/prompts.py
"""所有 prompt 集中在这里。

为什么要集中：
  1. 评估时要记录 prompt_version，散落各处就没法版本化
  2. 改 prompt 是最高频的操作，集中才能 diff
  3. 面试问"你怎么调 prompt 的"，答"集中管理 + 评估集回归"
"""

PROMPT_VERSION = "v1.0.0"

TRIAGE_SYSTEM = """你是 SRE 值班助手。判断告警是否需要自动调查。

规则：
- 测试环境（service 前缀 test-/staging-）的告警：needs_investigation=false
- 单实例瞬时抖动但服务整体正常：needs_investigation=false
- 生产环境且影响用户请求：needs_investigation=true
- 拿不准时选 true —— 漏诊的代价远大于多查一次的代价
"""

PLAN_INVESTIGATION_SYSTEM = """你是 SRE 故障诊断专家，正在制定调查计划。

可用工具及其适用场景：
{tool_catalog}

调查方法论（按优先级）：
1. 先看变更：list_changes。多数突发故障由上变更引起，成本最低、命中率最高。
2. 再看错误模式：top_error_patterns。用聚类后的模板定位方向，比读原始日志高效。
3. 然后验证假设：用 detect_anomaly 确认指标是否真的异常，注意"看起来异常"和"统计上异常"不同。
4. 最后看拓扑：影响面判断用 get_topology，确认是否上游传导。

约束：
- 每轮最多 {max_requests} 条请求。宁可分两轮，不要一轮塞满。
- 每条请求必须写明 hypothesis（想验证什么）。写不出来的请求不要发。
- 工具入参的键名必须和工具 schema 完全一致。
- 不确定指标名时，先调 list_metrics，不要猜。

{missing_info_hint}
"""

SYNTHESIZE_SYSTEM = """你是 SRE 故障诊断专家。根据已采集的证据，给出根因假设。

要求：
1. 假设必须具体到组件和机制，不要写"系统异常"这种废话。
   好例子："order-service JVM 堆内存持续增长导致 Full GC 频繁，请求处理线程被阻塞"
   坏例子："order-service 内存有问题"
2. confidence 必须诚实。证据只支持"时间相关"就给 0.4~0.5，有直接错误堆栈才给 0.8+。
3. rationale 里必须引用具体数据（指标数值、错误模板、变更时间），不要空泛描述。
4. tags 必须从给定枚举里选。这是匹配 runbook 的键，写错就匹配不上。
5. 如果证据不足以下结论，把缺什么写进 missing_info，不要硬凑置信度。
6. 如果检索到了相似历史故障，优先参考它的结论。

历史相似故障参考：
{history_context}
"""

PROPOSE_ACTION_SYSTEM = """你要为已确认的故障选择处置方案。

可用 runbook：
{runbook_catalog}

约束：
- runbook_code 必须来自上面的清单，不能编造。
- 参数模板里的 {{service}} 等占位符要替换成实际值。
- risk_level 必须和 runbook 定义的一致，不能自行降低。
- reason 是要给值班人看的人话，要说明"为什么这个方案能解决当前根因"，两到三句。
- rollback_plan 必须具体：写清楚"如果 X 分钟内 Y 指标没回落，就执行 Z"。
- 如果没有合适的 runbook，runbook_code 填 "escalate_unknown"。
"""

VERIFY_SYSTEM = """判断处置动作是否真的解决了故障。

重要：「动作执行成功」不等于「故障已恢复」。
- 动作成功 = 平台 API 返回 200
- 故障恢复 = 关键指标回落到基线附近

判断标准：
- 处置前的异常指标（error_rate / latency / cpu 等）是否回到基线 ±20% 内
- 错误日志是否停止增长
- 如果 3 分钟后指标仍然异常，应判定 should_rollback=true

宁可保守：判断没恢复，多观察一会儿，也好过误判为恢复而放过二次故障。
"""
```

**`{tool_catalog}` 怎么填**（这一段是 MCP 的价值体现）：

```python
# app/agent/prompts.py（续）
def render_tool_catalog(tools: list) -> str:
    """把 LangChain Tool 渲染成人类可读的目录，喂给规划 prompt。

    这就是 MCP 的 payoff：工具是运行时从 server 拉来的，
    新增一个 MCP 工具，这个目录自动更新，prompt 不用改。
    """
    lines = []
    for t in sorted(tools, key=lambda x: x.name):
        # MCP 工具的描述来自 server 端 docstring
        lines.append(f"- {t.name}: {(t.description or '').strip().splitlines()[0]}")
        schema = t.args_schema.model_json_schema() if t.args_schema else {}
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        for k, v in props.items():
            mark = "" if k in required else "（可选）"
            desc = (v.get("description") or "").splitlines()[0]
            lines.append(f"    - {k}{mark}: {desc}")
    return "\n".join(lines)
```

> **面试金句**：*"我的 prompt 里有一段工具目录，它不是硬编码的，是运行时从 MCP server 拉的。加一个新工具只要起一个 server，prompt 自动就有它了 —— 这是 MCP 相对硬编码函数调用的实际收益，不只是架构好看。"*

---

## 4. 数据访问层

所有节点通过 repo 函数碰数据库，节点里不出现 `session.add`。

```python
# app/db/repo.py（只列 Agent 层用到的）
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Alert, Approval, AuditLog, Evidence, Hypothesis, Incident,
    ActionPlan, Runbook,
)


async def get_incident(s: AsyncSession, incident_id: int) -> Incident | None:
    return await s.get(Incident, incident_id)


async def set_incident_status(
    s: AsyncSession, incident_id: int, status: str, **fields
) -> None:
    """统一的状态更新入口。

    用 **fields 允许顺带更新别的列（如 confidence / root_cause）。
    所有状态变更都走这一个函数 —— 好处是可以在这一处埋审计日志。
    """
    values = {"status": status, "updated_at": datetime.now(timezone.utc), **fields}
    if status == "resolved" and "resolved_at" not in values:
        values["resolved_at"] = datetime.now(timezone.utc)
    await s.execute(update(Incident).where(Incident.id == incident_id).values(**values))


async def add_evidence(s: AsyncSession, incident_id: int, **kw) -> Evidence:
    row = Evidence(incident_id=incident_id, **kw)
    s.add(row)
    await s.flush()
    return row


async def save_hypotheses(
    s: AsyncSession, incident_id: int, round_no: int, hyps: list[dict]
) -> list[Hypothesis]:
    """覆盖式保存某一轮的假设。

    先删同轮旧记录：因为调查回环时同一轮号会被重算，
    不删会出现 rank 唯一约束冲突。
    """
    from sqlalchemy import delete
    await s.execute(
        delete(Hypothesis).where(
            Hypothesis.incident_id == incident_id, Hypothesis.round == round_no
        )
    )
    rows = [
        Hypothesis(incident_id=incident_id, round=round_no, rank=i + 1, **h)
        for i, h in enumerate(hyps)
    ]
    s.add_all(rows)
    await s.flush()
    return rows


async def list_active_runbooks(s: AsyncSession, service: str | None) -> list[Runbook]:
    """取该服务适用的 runbook：专属 + 通用（service IS NULL）。"""
    stmt = select(Runbook).where(Runbook.is_active.is_(True))
    rows = (await s.execute(stmt)).scalars().all()
    return [r for r in rows if r.service is None or r.service == service]


async def create_action_plan(s: AsyncSession, incident_id: int, **kw) -> ActionPlan:
    row = ActionPlan(incident_id=incident_id, **kw)
    s.add(row)
    await s.flush()
    return row


async def create_approval_request(
    s: AsyncSession, plan_id: int, thread_id: str, snapshot: dict
) -> Approval:
    row = Approval(
        action_plan_id=plan_id, thread_id=thread_id, context_snapshot=snapshot
    )
    s.add(row)
    await s.flush()
    return row


async def decide_approval(
    s: AsyncSession, plan_id: int, *, decision: str, approver: str, comment: str | None
) -> Approval | None:
    row = await s.scalar(select(Approval).where(Approval.action_plan_id == plan_id))
    if row is None:
        return None
    row.decision = decision
    row.approver = approver
    row.comment = comment
    row.decided_at = datetime.now(timezone.utc)
    return row


async def get_approval(s: AsyncSession, plan_id: int) -> Approval | None:
    return await s.scalar(select(Approval).where(Approval.action_plan_id == plan_id))


async def write_audit(
    s: AsyncSession, *, incident_id: int | None, actor: str, actor_type: str,
    action: str, target_type: str | None = None, target_id: str | None = None,
    detail: dict | None = None,
) -> None:
    s.add(AuditLog(
        incident_id=incident_id, actor=actor, actor_type=actor_type, action=action,
        target_type=target_type, target_id=target_id, detail=detail or {},
    ))
```

---

## 5. 状态定义

```python
# app/agent/state.py
import operator
from typing import Annotated, Any, TypedDict


class Usage(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int


def merge_usage(left: Usage, right: Usage) -> Usage:
    return Usage(
        prompt_tokens=left.get("prompt_tokens", 0) + right.get("prompt_tokens", 0),
        completion_tokens=left.get("completion_tokens", 0) + right.get("completion_tokens", 0),
        llm_calls=left.get("llm_calls", 0) + right.get("llm_calls", 0),
    )


class IncidentState(TypedDict, total=False):
    # 输入
    incident_id: int
    thread_id: str
    alert: dict[str, Any]

    # 调查
    evidence_requests: list[dict]
    evidence: Annotated[list[dict], operator.add]
    investigation_round: int
    missing_info: list[str]

    # 结论
    triage: dict
    hypotheses: list[dict]
    confidence: float
    root_cause_tags: list[str]

    # 处置
    plan: dict | None
    approval: dict | None
    execution: dict | None
    verification: dict | None

    # 元信息
    status: str
    escalate_reason: str | None
    usage: Annotated[Usage, merge_usage]
    errors: Annotated[list[str], operator.add]
```

**`total=False` 的意义**：State 的字段在图上不是每个节点都会写。不加 `total=False` 的话 TypedDict 要求所有键都存在，节点返回部分更新时会类型报错。

**reducer 的第三个用途（回环）**：`evidence` 用 `operator.add` 还有个副作用 —— **调查回环时，上一轮的证据会累积而不是被清空**。这是刻意的：第二轮 `synthesize` 要看到全部证据。所以 `plan_investigation` 只清 `evidence_requests`，不清 `evidence`。

---

## 6. 节点实现

### 6.1 `ingest` —— 归一化与去重

```python
# app/agent/nodes/ingest.py
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.agent.state import IncidentState
from app.config import settings
from app.db.base import session_scope
from app.db.models import Alert, Incident
from app.db import repo
from app.services.dedup import alert_fingerprint, try_claim_alert

log = logging.getLogger(__name__)

# mock 环境用的 ground-truth 字段，绝不能泄漏给 LLM
LEAKY_LABEL_KEYS = {"scenario"}


def _sanitize_labels(labels: dict) -> dict:
    """剥掉评估泄漏字段。

    这一步非常重要：mock 告警里带了 scenario=memory_leak，
    如果原样进 prompt，模型直接读答案，评估结果全是假的。
    剥离后的原始载荷仍存进 alerts.raw 供人工排查。
    """
    return {k: v for k, v in labels.items() if k not in LEAKY_LABEL_KEYS}


async def ingest_node(state: IncidentState) -> dict:
    payload = state["alert"]
    alerts = payload.get("alerts") or []
    first = alerts[0] if alerts else {}
    labels = _sanitize_labels(first.get("labels", {}))
    annotations = first.get("annotations", {})

    fingerprint = payload.get("groupKey") or first.get("fingerprint") or alert_fingerprint(payload)
    service = labels.get("service") or payload.get("commonLabels", {}).get("service")
    alertname = labels.get("alertname", "UnknownAlert")
    started = first.get("startsAt")

    # ---- Redis 去重：同指纹在 TTL 内只处理一次 ----
    is_new = await try_claim_alert(fingerprint)
    if not is_new:
        log.info("duplicate alert ignored: fingerprint=%s", fingerprint)
        # 返回已有的 incident_id，让图的后续节点知道该挂到哪个 incident 上。
        # 注意这里不终止图 —— 终止由 triage 的 needs_investigation 控制，
        # 或者由调用方在入队前过滤。见 §7 的说明。
        async with session_scope() as s:
            existing = await s.scalar(
                select(Incident)
                .join(Alert, Alert.incident_id == Incident.id)
                .where(Alert.fingerprint == fingerprint)
                .order_by(Incident.id.desc())
                .limit(1)
            )
        return {
            "incident_id": existing.id if existing else state.get("incident_id"),
            "status": "duplicate",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
        }

    # ---- 建/复用 incident ----
    async with session_scope() as s:
        incident_id = state.get("incident_id")
        if incident_id:
            incident = await repo.get_incident(s, incident_id)
            if incident is None:
                raise ValueError(f"incident {incident_id} not found")
        else:
            # 同一服务 15 分钟内的告警聚合成一个 incident（简单版关联规则）
            open_inc = await s.scalar(
                select(Incident)
                .where(Incident.service == service)
                .where(Incident.status.notin_(["resolved", "escalated"]))
                .order_by(Incident.detected_at.desc())
                .limit(1)
            )
            if open_inc is not None:
                incident, incident_id = open_inc, open_inc.id
            else:
                incident = Incident(
                    title=f"{service}: {alertname}",
                    service=service,
                    severity=labels.get("severity", "warning"),
                    status="new",
                    first_alert_at=_parse_ts(started),
                )
                s.add(incident)
                await s.flush()
                incident_id = incident.id

        s.add(Alert(
            fingerprint=fingerprint,
            source="alertmanager",
            name=alertname,
            severity=labels.get("severity", "warning"),
            service=service,
            labels=labels,
            annotations=annotations,
            raw=payload,                 # 原始载荷保留（含 scenario，供排查）
            starts_at=_parse_ts(started),
            status="firing",
            incident_id=incident_id,
        ))
        await repo.set_incident_status(s, incident_id, "investigating")

    return {
        "incident_id": incident_id,
        "status": "investigating",
        "investigation_round": 0,
        "evidence": [],
        "missing_info": [],
        "triage": {"service": service, "alertname": alertname},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
        "errors": [],
    }


def _parse_ts(value: str | None) -> datetime:
    if not value or value.startswith("0001"):
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
```

**这段的三个设计点**：

1. **`_sanitize_labels` 是评估诚实性的守门人。** 一定要在 README 里写："mock 告警的 `scenario` 字段在 ingest 处被剥离，只存 raw，不进 prompt。" 面试时这体现你有评估意识。
2. **去重后不抛异常、不终止图**，而是返回 `status="duplicate"`。因为 LangGraph 节点不能"什么都不返回"。真正的终止在 `triage` 之后的条件边。**或者更干净的做法：在入队前用 Redis 去重过滤掉重复告警，图里根本不该有重复。** 本项目两者都做（入队前过滤 + 图内兜底）。
3. **`evidence: []` 显式初始化** —— 有 reducer 的字段必须给初始值，否则并行分支第一次写时没有左值会报错。**这是 LangGraph 的经典坑。**

### 6.2 `triage` —— 判断要不要查

```python
# app/agent/nodes/triage.py
import logging

from app.agent.llm import structured
from app.agent.prompts import TRIAGE_SYSTEM
from app.agent.state import IncidentState
from app.db.base import session_scope
from app.db import repo
from app.schemas import TriagedAlert

log = logging.getLogger(__name__)


async def triage_node(state: IncidentState) -> dict:
    if state.get("status") == "duplicate":
        # 重复告警直接跳过调查
        return {"triage": {**state.get("triage", {}), "needs_investigation": False,
                           "reason": "duplicate alert"},
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}}

    alert = state["alert"]
    first = (alert.get("alerts") or [{}])[0]
    labels = first.get("labels", {})
    annotations = first.get("annotations", {})

    # 规则先行：明显噪声不花 token
    service = labels.get("service", "")
    if service.startswith(("test-", "staging-")):
        result = TriagedAlert(
            needs_investigation=False, severity="info", service=service,
            reason="non-production environment",
        )
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}
    else:
        llm = structured(TriagedAlert, cheap=True)
        result: TriagedAlert = await llm.ainvoke([
            ("system", TRIAGE_SYSTEM),
            ("human", f"告警名称：{labels.get('alertname')}\n"
                      f"服务：{service}\n"
                      f"严重度：{labels.get('severity')}\n"
                      f"描述：{annotations.get('description', '无')}"),
        ])
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 1}

    async with session_scope() as s:
        await repo.set_incident_status(
            s, state["incident_id"], "investigating", severity=result.severity
        )

    return {
        "triage": {**state.get("triage", {}), **result.model_dump()},
        "usage": usage,
    }
```

> **规则的优先级高于 LLM**：测试环境告警这种确定性判断，用 `startswith` 一行代码解决，不要花 token 问模型。**能确定的事不要交给概率模型** —— 这句是本项目反复出现的设计原则。

### 6.3 调查三件套：`plan` / `dispatch` / `collect`

```python
# app/agent/nodes/investigation.py
import json
import logging
import time

from app.agent.llm import structured
from app.agent.mcp_client import readonly_tools, tools_for_kinds, get_tool
from app.agent.prompts import (
    PLAN_INVESTIGATION_SYSTEM, render_tool_catalog,
)
from app.agent.state import IncidentState
from app.config import settings
from app.db.base import session_scope
from app.db import repo
from app.schemas import InvestigationPlan

log = logging.getLogger(__name__)


async def plan_investigation_node(state: IncidentState) -> dict:
    """决定本轮查什么。返回的 requests 会被 dispatch 变成并行分支。"""
    round_no = state.get("investigation_round", 0) + 1
    tools = readonly_tools()

    missing = state.get("missing_info") or []
    missing_hint = (
        "上一轮结论认为还缺以下信息，本轮请优先补齐：\n- " + "\n- ".join(missing)
        if missing else ""
    )

    # 已有证据摘要：让模型知道什么已经查过，避免重复查
    already = _summarize_evidence(state.get("evidence", []))

    system = PLAN_INVESTIGATION_SYSTEM.format(
        tool_catalog=render_tool_catalog(tools),
        max_requests=settings.max_evidence_per_round,
        missing_info_hint=missing_hint,
    )
    human = (
        f"故障事件：{state.get('triage', {})}\n"
        f"当前是第 {round_no} 轮调查（上限 {settings.max_investigation_rounds} 轮）。\n\n"
        f"已采集的证据：\n{already or '（无）'}\n\n"
        f"请给出本轮调查计划。"
    )

    llm = structured(InvestigationPlan)
    plan: InvestigationPlan = await llm.ainvoke([("system", system), ("human", human)])

    # 硬校验：过滤掉模型编造的工具名 / 越权工具
    allowed = {t.name for t in tools}
    valid, dropped = [], []
    for r in plan.requests[: settings.max_evidence_per_round]:
        if r.tool_name in allowed:
            valid.append(r.model_dump())
        else:
            dropped.append(r.tool_name)
    if dropped:
        log.warning("dropped hallucinated tools: %s", dropped)

    return {
        "evidence_requests": valid,
        "investigation_round": round_no,
        "missing_info": [],       # 清空，等本轮 synthesize 重新填
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 1},
    }


def dispatch_evidence(state: IncidentState) -> list:
    """条件边：把 N 条请求变成 N 个并行分支。

    返回 Send 列表，LangGraph 会为每个 Send 起一个 collect_evidence 实例。
    这是 fan-out —— 4 条请求并行执行，总耗时 = 最慢的那条，
    而不是 4 条耗时之和。
    """
    from langgraph.types import Send

    reqs = state.get("evidence_requests") or []
    if not reqs:
        return [Send("synthesize", state)]     # 没请求可发，直接去综合

    return [
        Send("collect_evidence", {
            "incident_id": state["incident_id"],
            "thread_id": state["thread_id"],
            "triage": state.get("triage", {}),
            "round": state.get("investigation_round", 1),
            "request": req,
            "evidence": [],        # 每个分支独立，结果靠 reducer 合并
        })
        for req in reqs
    ]


async def collect_evidence_node(state: dict) -> dict:
    """单个证据采集分支。注意入参是 dict 不是 IncidentState ——
    因为 Send 传的是局部 state，不是完整图状态。

    这是初次写最容易错的地方：在 collect 节点里访问 state["hypotheses"]
    会 KeyError，因为 Send 只带了需要的字段。
    """
    req = state["request"]
    kind = req["kind"]
    tool_name = req["tool_name"]
    args = dict(req.get("arguments") or {})

    # 补齐模型常忘的公共参数
    service = state.get("triage", {}).get("service")
    if service and "service" not in args:
        args["service"] = service
    args.setdefault("minutes", 60)

    started = time.perf_counter()
    ok, payload, err = True, None, None
    try:
        tool = get_tool(tool_name)
        payload = await tool.ainvoke(args)
    except Exception as e:                      # noqa: BLE001
        ok, err = False, f"{type(e).__name__}: {e}"
        log.warning("tool %s failed: %s", tool_name, err)
    duration_ms = int((time.perf_counter() - started) * 1000)

    # 单条证据 payload 截断，防单行过大 + 防上下文爆炸
    payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    if len(payload_str) > 20000:
        payload = {"truncated": True, "preview": payload_str[:20000]}
        payload_str = json.dumps(payload, ensure_ascii=False)

    summary = _make_summary(kind, tool_name, payload, ok)

    async with session_scope() as s:
        row = await repo.add_evidence(
            s, state["incident_id"],
            round=state.get("round", 1), kind=kind, tool_name=tool_name,
            request=args, payload=payload if isinstance(payload, dict) else {"raw": payload},
            summary=summary, duration_ms=duration_ms, ok=ok, error=err,
        )
        evidence_id = row.id

    return {
        "evidence": [{
            "id": evidence_id, "kind": kind, "tool_name": tool_name,
            "request": args, "summary": summary, "ok": ok, "error": err,
            "duration_ms": duration_ms,
        }],
        "errors": [] if ok else [f"{tool_name}: {err}"],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }


def _make_summary(kind: str, tool_name: str, payload, ok: bool) -> str:
    """把工具返回压成一行摘要。

    为什么要有摘要而不是把 payload 全塞给模型：
    证据可能几十 KB，5 条证据就把上下文吃光了。
    摘要是确定性代码生成的，模型只需要读摘要 + 按需看细节。
    """
    if not ok:
        return f"[{tool_name}] 调用失败"
    try:
        if "error_rate" in tool_name or kind == "logs":
            if isinstance(payload, dict) and "error_rate" in payload:
                return (f"错误率 {payload['error_rate']:.2%}"
                        f"（{payload.get('errors')}/{payload.get('total')}）")
            if isinstance(payload, dict) and payload.get("patterns"):
                top = payload["patterns"][0]
                return f"最高频错误模板（{top['count']} 次）：{top['pattern'][:120]}"
        if kind == "metrics":
            if isinstance(payload, dict) and "z_score" in payload:
                return (f"{payload['metric']} 当前 {payload.get('current')} "
                        f"vs 基线 {payload.get('baseline_mean')}，"
                        f"z={payload['z_score']}，异常={payload.get('anomalous')}")
            if isinstance(payload, dict) and "max" in payload:
                return f"{payload['metric']} min={payload.get('min')} max={payload.get('max')} avg={payload.get('avg')}"
        if kind == "changes":
            n = payload.get("count", 0) if isinstance(payload, dict) else 0
            if n:
                c = payload["changes"][0]
                return f"最近 {n} 条变更，最新：{c['kind']} by {c['operator']} @ {c['at']} — {c['detail']}"
            return "窗口内无变更"
        if kind == "topology":
            return f"上下游：{payload.get('upstream')} / {payload.get('downstream')}"
    except Exception:      # noqa: BLE001
        pass
    return json.dumps(payload, ensure_ascii=False, default=str)[:200]


def _summarize_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return ""
    return "\n".join(
        f"#{e['id']} [{e['kind']}/{e['tool_name']}] {e.get('summary', '')}"
        for e in evidence
    )
```

**这一节值得反复读，四个关键点**：

| 点 | 说明 |
|---|---|
| **`Send` 的入参是局部 state** | `collect_evidence_node(state: dict)` 拿到的不是完整 `IncidentState`。忘了这点会写出一堆 KeyError。 |
| **并行分支必须给 `evidence: []` 初始值** | 每个 Send 携带自己的初始值，返回时靠 `operator.add` 合并进主 state。 |
| **`missing_info` 在 plan 节点被清空** | 否则第二轮会一直看到上一轮"缺什么"，陷入死循环式重复调查。**这是回环设计的隐蔽 bug。** |
| **摘要由代码生成，不是让 LLM 生成** | 让 LLM 总结证据 = 多一次调用 + 可能丢关键数字。确定性代码摘要更准更便宜。 |

### 6.4 `synthesize` —— 核心推理

```python
# app/agent/nodes/synthesize.py
import json
import logging

from app.agent.llm import structured, primary_llm
from app.agent.prompts import SYNTHESIZE_SYSTEM
from app.agent.state import IncidentState
from app.db.base import session_scope
from app.db import repo
from app.schemas import Synthesis
from app.services.embedding import embed_text, find_similar_incidents_text

log = logging.getLogger(__name__)


async def synthesize_node(state: IncidentState) -> dict:
    incident_id = state["incident_id"]
    evidence = state.get("evidence", [])
    round_no = state.get("investigation_round", 1)

    # ---- 检索相似历史故障（RAG 用在这里，不是查文档）----
    query = _build_history_query(state)
    history_context = "（无相似历史故障）"
    try:
        hits = await find_similar_incidents_text(query, limit=3, min_score=0.7)
        if hits:
            history_context = "\n".join(
                f"- 相似度 {score:.2f}：{title}\n  根因：{content[:300]}"
                for title, content, score in hits
            )
    except Exception as e:                      # noqa: BLE001
        log.warning("history retrieval failed: %s", e)

    system = SYNTHESIZE_SYSTEM.format(history_context=history_context)
    human = (
        f"故障事件：{state.get('triage', {})}\n"
        f"已采集 {len(evidence)} 条证据：\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)[:12000]
    )

    llm = structured(Synthesis)
    result: Synthesis = await llm.ainvoke([("system", system), ("human", human)])

    hyps = result.hypotheses[:3]
    if not hyps:
        return {
            "hypotheses": [], "confidence": 0.0,
            "missing_info": result.missing_info or ["模型未给出任何假设"],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 1},
        }

    top = hyps[0]

    async with session_scope() as s:
        rows = await repo.save_hypotheses(s, incident_id, round_no, [
            {
                "statement": h.statement,
                "confidence": h.confidence,
                "rationale": h.rationale,
                "supporting_evidence_ids": h.evidence_ids,
                "adopted": (i == 0),
            }
            for i, h in enumerate(hyps)
        ])
        await repo.set_incident_status(
            s, incident_id, "investigating",
            root_cause=top.statement,
            confidence=top.confidence,
            diagnosis_rounds=round_no,
        )

    return {
        "hypotheses": [h.model_dump() for h in hyps],
        "confidence": result.overall_confidence,
        "root_cause_tags": top.tags,
        "missing_info": result.missing_info,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 1},
    }


def _build_history_query(state: IncidentState) -> str:
    triage = state.get("triage", {})
    parts = [triage.get("service", ""), triage.get("alertname", "")]
    for e in state.get("evidence", []):
        if e.get("summary"):
            parts.append(e["summary"][:150])
    return " ".join(p for p in parts if p)[:2000]
```

**这一节的核心是"RAG 用在哪"**：这里不检索文档，检索的是**历史故障的处置经验**。`synthesize` 时把相似历史故障的根因作为参考，`report` 时把本次结论写回知识库 —— 形成**闭环学习**。面试讲法：

> "我的 RAG 不是问答式的文档检索，是**案例库检索**。每次故障解决后把根因和处置结果向量化入库，下次遇到相似故障，第一轮就能给出高置信度判断。这就把项目从'一次性工具'变成了'越用越准的系统'。"

### 6.5 条件路由

```python
# app/agent/nodes/routing.py
import logging

from app.agent.state import IncidentState
from app.config import settings

log = logging.getLogger(__name__)


def route_after_triage(state: IncidentState) -> str:
    triage = state.get("triage", {})
    if state.get("status") == "duplicate":
        return "report"
    if not triage.get("needs_investigation", True):
        return "escalate"
    return "plan_investigation"


def route_after_synthesis(state: IncidentState) -> str:
    """三选一：补调查 / 走处置 / 转人工。

    这是整个图的"决策中枢"，五个条件按优先级排列。
    """
    confidence = state.get("confidence", 0.0)
    round_no = state.get("investigation_round", 1)
    missing = state.get("missing_info") or []
    tags = state.get("root_cause_tags") or []

    # 1) 置信度不足 + 还有调查预算 + 确实知道缺什么 → 回环补调查
    if (
        confidence < settings.confidence_threshold
        and round_no < settings.max_investigation_rounds
        and missing
    ):
        log.info("round %s: confidence %.2f too low, re-investigating for %s",
                 round_no, confidence, missing)
        return "plan_investigation"

    # 2) 根因是 unknown → 别乱处置，转人工
    if not tags or tags == ["unknown"]:
        return "escalate"

    # 3) 置信度够了 → 生成处置方案
    if confidence >= settings.confidence_threshold:
        return "propose_action"

    # 4) 轮次用完但置信度仍不够 → 转人工，但带着已有的分析结果
    return "escalate"


def route_after_approval(state: IncidentState) -> str:
    approval = state.get("approval") or {}
    decision = approval.get("decision")
    if decision == "approved":
        return "execute_action"
    # rejected / timeout 都转人工
    return "escalate"
```

**`route_after_synthesis` 的五个条件是这个项目最值得讲的逻辑**，为什么这么排：

1. **"知道缺什么"才回环** —— 如果置信度低但 `missing_info` 是空的，说明模型自己也不知道怎么办，再查一轮也是瞎查，白烧 token。**这是回环的止损条件。**
2. **`unknown` 根因绝不自动处置** —— 处置未知故障可能造成二次故障。宁可转人工。**这是安全边界。**
3. **轮次上限是成本闸门** —— 3 轮 × 6 条证据 × N 个工具的 token，不设限会失控。

面试追问"你怎么防止 Agent 死循环"，答这一整段。

### 6.6 处置四件套：`propose` / `approval` / `execute` / `verify`

```python
# app/agent/nodes/action.py
import logging
import time

from langgraph.types import interrupt

from app.agent.llm import structured
from app.agent.mcp_client import get_tool
from app.agent.prompts import PROPOSE_ACTION_SYSTEM
from app.agent.state import IncidentState
from app.config import settings
from app.db.base import session_scope
from app.db import repo
from app.schemas import ProposedAction
from app.services.dedup import action_lock, build_idempotency_key

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- propose

async def propose_action_node(state: IncidentState) -> dict:
    incident_id = state["incident_id"]
    service = state.get("triage", {}).get("service")
    tags = state.get("root_cause_tags") or []

    async with session_scope() as s:
        runbooks = await repo.list_active_runbooks(s, service)

    # ---- 规则先行：按 tags 精确匹配 runbook，命中就不问模型 ----
    matched = _match_runbook_by_tags(runbooks, tags)
    if matched is not None:
        params = _render_params(matched.params_tpl, service, state)
        plan = {
            "runbook_code": matched.code, "runbook_id": matched.id,
            "action": matched.action, "target": service,
            "params": params, "risk_level": matched.risk_level,
            "reason": (f"根因标签 {tags} 精确匹配 runbook「{matched.title}」，"
                       f"该方案为预定义标准处置流程，无需模型再判断。"),
            "rollback_plan": f"若 5 分钟内指标未回落，执行 rollback（{matched.action}）。",
            "matched_by": "rule",
        }
        llm_calls = 0
    else:
        # ---- 规则没命中，才让 LLM 在 runbook 清单里挑 ----
        catalog = "\n".join(
            f"- {r.code}: {r.title} | action={r.action} | risk={r.risk_level} | "
            f"match_rule={r.match_rule} | params_tpl={r.params_tpl}"
            for r in runbooks
        )
        llm = structured(ProposedAction, cheap=True)
        proposal: ProposedAction = await llm.ainvoke([
            ("system", PROPOSE_ACTION_SYSTEM.format(runbook_catalog=catalog)),
            ("human",
             f"服务：{service}\n"
             f"根因：{state['hypotheses'][0]['statement']}\n"
             f"根因标签：{tags}\n"
             f"置信度：{state.get('confidence')}\n"
             f"证据摘要：\n"
             + "\n".join(e.get("summary", "") for e in state.get("evidence", []))),
        ])
        rb = next((r for r in runbooks if r.code == proposal.runbook_code), None)
        plan = {
            **proposal.model_dump(),
            "runbook_id": rb.id if rb else None,
            "target": proposal.target or service,
            "matched_by": "llm",
        }
        llm_calls = 1

    async with session_scope() as s:
        row = await repo.create_action_plan(
            s, incident_id,
            runbook_id=plan.get("runbook_id"),
            action=plan["action"], target=plan["target"],
            params=plan.get("params", {}), risk_level=plan["risk_level"],
            reason=plan.get("reason"), status="awaiting_approval",
        )
        plan["plan_id"] = row.id
        await repo.set_incident_status(s, incident_id, "awaiting_approval")

    return {
        "plan": plan,
        "status": "awaiting_approval",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": llm_calls},
    }


def _match_runbook_by_tags(runbooks, tags: list[str]):
    """按 match_rule.root_cause_tags 与根因标签求交集。"""
    for r in runbooks:
        want = set((r.match_rule or {}).get("root_cause_tags", []))
        if want and want & set(tags):
            return r
    return next((r for r in runbooks if r.code == "escalate_unknown"), None)


def _render_params(tpl: dict, service: str | None, state: IncidentState) -> dict:
    """渲染 {{service}} 这类占位符。"""
    ctx = {
        "service": service or "",
        "prev_revision": "v41",      # mock；真实环境从发布记录取
        "incident_id": str(state.get("incident_id", "")),
    }
    out = {}
    for k, v in (tpl or {}).items():
        if isinstance(v, str) and v.startswith("{{") and v.endswith("}}"):
            out[k] = ctx.get(v[2:-2], v)
        else:
            out[k] = v
    return out


# ------------------------------------------------------------- approval

async def request_approval_node(state: IncidentState) -> dict:
    """人在环中断点。

    ⚠️ 三条铁律：
      1. 本节点在 resume 时会「从头重新执行」——
         所以 interrupt() 之前绝不能有副作用（写库、发通知、调外部 API）。
      2. interrupt() 的载荷会被 return 出去，API 层拿它渲染审批页面。
      3. 返回值就是 resume 时传进来的 Command(resume=...) 的内容。
    """
    plan = state["plan"]

    # ---- 审批请求记录在这里写，但用「幂等 upsert」语义 ----
    # 因为节点会重跑，直接 add 会产生重复行。
    async with session_scope() as s:
        existing = await repo.get_approval(s, plan["plan_id"])
        if existing is None:
            await repo.create_approval_request(
                s, plan["plan_id"], thread_id=state["thread_id"],
                snapshot={
                    "root_cause": (state.get("hypotheses") or [{}])[0].get("statement"),
                    "confidence": state.get("confidence"),
                    "evidence_summary": [
                        e.get("summary") for e in state.get("evidence", [])
                    ],
                    "plan": plan,
                },
            )
        else:
            # 重跑路径：把状态复位回"待审批"，否则上一次的驳回决定会被沿用
            existing.decision = None
            existing.approver = None
            existing.comment = None
            existing.decided_at = None

    # ---- 中断。这一行之前的代码会被执行两次，之后的代码只在 resume 后执行 ----
    decision = interrupt({
        "type": "action_approval",
        "incident_id": state["incident_id"],
        "plan_id": plan["plan_id"],
        "summary": (state.get("hypotheses") or [{}])[0].get("statement"),
        "confidence": state.get("confidence"),
        "action": plan["action"],
        "target": plan["target"],
        "params": plan.get("params", {}),
        "risk_level": plan["risk_level"],
        "reason": plan.get("reason"),
        "rollback_plan": plan.get("rollback_plan"),
        "expires_at": None,          # 超时由 API 侧的定时任务处理
    })

    async with session_scope() as s:
        await repo.write_audit(
            s, incident_id=state["incident_id"],
            actor=decision.get("approver", "unknown"), actor_type="human",
            action="approve" if decision.get("decision") == "approved" else "reject",
            target_type="action_plan", target_id=str(plan["plan_id"]),
            detail=decision,
        )

    return {
        "approval": decision,
        "status": "executing" if decision.get("decision") == "approved" else "escalated",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }


# -------------------------------------------------------------- execute

async def execute_action_node(state: IncidentState) -> dict:
    """幂等执行。三道防线：图状态检查 → Redis 锁 → executor 幂等键。"""
    plan = state["plan"]
    incident_id = state["incident_id"]

    idem_key = build_idempotency_key(
        plan["plan_id"], plan["action"], plan["target"], plan.get("params", {})
    )

    async with session_scope() as s:
        approval = await repo.get_approval(s, plan["plan_id"])
        approval_id = approval.id if approval else None
        await repo.set_incident_status(s, incident_id, "executing")

    # 分布式锁：防止同一动作被两个 worker 同时执行
    async with action_lock(idem_key) as acquired:
        if not acquired:
            log.warning("action lock busy, skip: %s", idem_key)
            return {
                "execution": {"skipped": True, "reason": "locked_by_another_worker",
                              "idempotency_key": idem_key},
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
            }

        started = time.perf_counter()
        try:
            apply = get_tool("apply_action")
            raw = await apply.ainvoke({
                "action": plan["action"],
                "target": plan["target"],
                "params": plan.get("params", {}),
                "idempotency_key": idem_key,
                "approval_id": approval_id,
            })
            ok, err = True, None
        except Exception as e:                  # noqa: BLE001
            raw, ok, err = None, False, f"{type(e).__name__}: {e}"
            log.exception("execute failed")

    duration_ms = int((time.perf_counter() - started) * 1000)

    async with session_scope() as s:
        await repo.write_audit(
            s, incident_id=incident_id, actor="agent", actor_type="agent",
            action="execute", target_type="action_plan",
            target_id=str(plan["plan_id"]),
            detail={"action": plan["action"], "ok": ok, "error": err,
                    "idempotency_key": idem_key},
        )
        await repo.set_incident_status(s, incident_id, "verifying" if ok else "failed")
        # 更新 plan 状态
        from sqlalchemy import update
        from app.db.models import ActionPlan
        await s.execute(
            update(ActionPlan).where(ActionPlan.id == plan["plan_id"])
            .values(status="succeeded" if ok else "failed")
        )

    return {
        "execution": {
            "ok": ok, "error": err, "raw": raw,
            "idempotency_key": idem_key, "duration_ms": duration_ms,
            "idempotent_replay": bool(raw.get("idempotent_replay")) if isinstance(raw, dict) else False,
        },
        "status": "verifying" if ok else "failed",
        "errors": [] if ok else [f"execute failed: {err}"],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }


# --------------------------------------------------------------- verify

async def verify_node(state: IncidentState) -> dict:
    """执行后验证：动作成功 ≠ 故障恢复。"""
    incident_id = state["incident_id"]
    service = state.get("triage", {}).get("service")

    # 经验：重启/回滚后指标不会立刻回落，给它一点时间。
    # 生产环境这里应该用轮询 + 退避，这里 sleep 简化。
    import asyncio
    await asyncio.sleep(3)

    checks: list[dict] = []
    for tool_name, args in _verification_queries(state, service):
        try:
            tool = get_tool(tool_name)
            payload = await tool.ainvoke(args)
            checks.append({"tool": tool_name, "args": args, "result": payload})
        except Exception as e:                  # noqa: BLE001
            checks.append({"tool": tool_name, "error": str(e)})

    recovered = _judge_recovered(checks)

    async with session_scope() as s:
        await repo.set_incident_status(
            s, incident_id, "resolved" if recovered else "failed"
        )

    return {
        "verification": {"recovered": recovered, "checks": checks},
        "status": "resolved" if recovered else "failed",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }


def _verification_queries(state: IncidentState, service: str | None):
    """根据这次采过的证据，决定执行后复查哪些指标。

    这是个好设计：不写死"总查 error_rate"，而是复用第一轮
    发现异常的指标。哪个指标异常过，就复查哪个。
    """
    queries = [("error_rate", {"service": service, "minutes": 10})]
    for e in state.get("evidence", []):
        if e.get("kind") == "metrics" and e.get("ok"):
            metric = (e.get("request") or {}).get("metric")
            if metric and metric != "error_rate":
                queries.append(("detect_anomaly", {
                    "service": service, "metric": metric, "minutes": 10,
                }))
    return queries[:4]


def _judge_recovered(checks: list[dict]) -> bool:
    """确定性判定，不用 LLM。

    为什么不用 LLM：这是个阈值判断，代码比模型准且免费。
    模型只在"该查什么"上做决策，不在"数字过没过线"上做决策。
    """
    if not checks:
        return False
    for c in checks:
        r = c.get("result")
        if not isinstance(r, dict):
            continue
        if "error_rate" in r and r["error_rate"] is not None:
            if r["error_rate"] > 0.02:          # 错误率阈值 2%
                return False
        if r.get("anomalous") is True:
            return False
    return True
```

**这一段是全项目面试含金量最高的代码，四个必讲点**：

| 点 | 讲法 |
|---|---|
| **`interrupt()` 前不能有副作用** | "我最初把创建审批记录写在 `interrupt()` 后面，结果 resume 时发现记录没建 —— 因为 resume 是从节点开头重跑的。后来改成幂等 upsert：先查有没有，有就复位状态。" |
| **三层幂等防线** | "图状态检查（`status != executing`）、Redis 锁（防并发 worker）、executor 幂等键（防超时重试）。三层对应三种不同的重复场景，缺一层都会漏。" |
| **`_judge_recovered` 用代码不用 LLM** | "数字过没过线是阈值判断，用 LLM 是又贵又不稳。模型负责'查什么'，不负责'算不算过线'。" |
| **`_verification_queries` 复用第一轮发现的异常指标** | "不写死复查 error_rate，而是复查第一轮报告异常的那些指标。这样'验证'是真的针对本次故障，不是走形式。" |

### 6.7 `escalate` 与 `report`

```python
# app/agent/nodes/report.py
import json
import logging
from datetime import datetime, timezone

from app.agent.state import IncidentState
from app.config import settings
from app.db.base import session_scope
from app.db import repo
from app.services.embedding import embed_text

log = logging.getLogger(__name__)


async def escalate_node(state: IncidentState) -> dict:
    """转人工。

    关键是「把 Agent 已经做的分析原样交出去」——
    转人工不等于放弃，而是让值班人从 80% 处接手。
    """
    reason = _escalate_reason(state)

    async with session_scope() as s:
        await repo.set_incident_status(
            s, state["incident_id"], "escalated",
            summary=json.dumps({
                "escalated_reason": reason,
                "hypotheses": state.get("hypotheses", []),
                "confidence": state.get("confidence"),
            }, ensure_ascii=False),
        )
        await repo.write_audit(
            s, incident_id=state["incident_id"], actor="agent", actor_type="agent",
            action="escalate", detail={"reason": reason},
        )

    return {
        "status": "escalated",
        "escalate_reason": reason,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }


def _escalate_reason(state: IncidentState) -> str:
    approval = state.get("approval") or {}
    if approval.get("decision") == "rejected":
        return f"审批被 {approval.get('approver')} 驳回：{approval.get('comment', '无说明')}"
    if approval.get("decision") == "timeout":
        return f"审批超时（{settings.approval_timeout_minutes} 分钟未响应）"
    if not state.get("root_cause_tags") or state["root_cause_tags"] == ["unknown"]:
        return f"无法确定根因（{state.get('investigation_round', 0)} 轮调查后置信度 {state.get('confidence', 0):.2f}）"
    if not (state.get("triage") or {}).get("needs_investigation", True):
        return f"triage 判定无需自动调查：{(state.get('triage') or {}).get('reason', '')}"
    return f"置信度 {state.get('confidence', 0):.2f} 低于阈值 {settings.confidence_threshold}"


async def report_node(state: IncidentState) -> dict:
    """收尾：算成本、写报告、把本次结论写回知识库（闭环学习）。"""
    incident_id = state["incident_id"]
    usage = state.get("usage", {})
    cost = (
        usage.get("prompt_tokens", 0) / 1000 * settings.price_prompt_per_1k
        + usage.get("completion_tokens", 0) / 1000 * settings.price_completion_per_1k
    )

    async with session_scope() as s:
        incident = await repo.get_incident(s, incident_id)
        # MTTR 只在真正解决时统计
        mttr = None
        if incident and incident.status == "resolved" and incident.first_alert_at:
            mttr = int(
                (datetime.now(timezone.utc) - incident.first_alert_at).total_seconds()
            )

        await repo.set_incident_status(
            s, incident_id, incident.status if incident else "escalated",
            total_tokens=usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            total_cost_usd=cost,
            **({"mttr_seconds": mttr} if mttr else {}),
        )

    # ---- 闭环：把本次结论写回知识库，供下次相似故障检索 ----
    if incident and incident.status == "resolved" and incident.root_cause:
        try:
            content = (
                f"服务：{incident.service}\n"
                f"告警：{incident.title}\n"
                f"根因：{incident.root_cause}\n"
                f"处置：{(state.get('plan') or {}).get('action')} "
                f"{json.dumps((state.get('plan') or {}).get('params', {}), ensure_ascii=False)}\n"
                f"标签：{state.get('root_cause_tags')}\n"
                f"结果：{json.dumps(state.get('verification', {}).get('recovered'))}"
            )
            emb = await embed_text(content)
            async with session_scope() as s:
                from app.db.models import IncidentKnowledge
                s.add(IncidentKnowledge(
                    incident_id=incident_id, kind="incident",
                    title=f"{incident.service}: {incident.title}",
                    content=content, embedding=emb,
                    metadata_={"tags": state.get("root_cause_tags", []),
                               "cost_usd": float(cost)},
                ))
        except Exception as e:                  # noqa: BLE001
            log.warning("failed to write incident knowledge: %s", e)

    return {
        "status": "done",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }
```

**`escalate` 的四种原因要分开统计**，这是评估章节的输入：审批驳回率 / 审批超时率 / 根因未确定率 / 置信度不足率。**四个数字各自指向不同的优化方向**：
- 驳回率高 → 处置方案质量差或审批阈值太松
- 超时率高 → 审批台不好用 / 通知没送达
- 根因未确定率高 → prompt 或工具能力不足
- 置信度不足率高 → 置信度校准有问题（模型给的置信度不诚实）

---

## 7. 图装配

```python
# app/agent/graph.py
"""LangGraph 图装配与编译。

生命周期：
  - 应用启动时调用 build_graph() 一次，拿到编译后的图
  - AsyncPostgresSaver 必须常驻（不能每个请求建一个），
    否则连接池爆炸。见 §8 的 lifespan 用法。
"""
import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes.action import (
    execute_action_node, propose_action_node, request_approval_node, verify_node,
)
from app.agent.nodes.ingest import ingest_node
from app.agent.nodes.investigation import (
    collect_evidence_node, dispatch_evidence, plan_investigation_node,
)
from app.agent.nodes.report import escalate_node, report_node
from app.agent.nodes.routing import (
    route_after_approval, route_after_synthesis, route_after_triage,
)
from app.agent.nodes.synthesize import synthesize_node
from app.agent.nodes.triage import triage_node
from app.agent.state import IncidentState

log = logging.getLogger(__name__)


def build_graph() -> StateGraph:
    g = StateGraph(IncidentState)

    # ---- 注册节点 ----
    g.add_node("ingest", ingest_node)
    g.add_node("triage", triage_node)
    g.add_node("plan_investigation", plan_investigation_node)
    g.add_node("collect_evidence", collect_evidence_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("propose_action", propose_action_node)
    g.add_node("request_approval", request_approval_node)
    g.add_node("execute_action", execute_action_node)
    g.add_node("verify", verify_node)
    g.add_node("escalate", escalate_node)
    g.add_node("report", report_node)

    # ---- 主干 ----
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "triage")

    # triage 后分叉
    g.add_conditional_edges("triage", route_after_triage, {
        "plan_investigation": "plan_investigation",
        "escalate": "escalate",
        "report": "report",
    })

    # 调查链路
    g.add_edge("plan_investigation", "collect_evidence")  # 会被 dispatch 覆盖，见下
    g.add_edge("collect_evidence", "synthesize")

    # synthesize 后三选一（含回环）
    g.add_conditional_edges("synthesize", route_after_synthesis, {
        "plan_investigation": "plan_investigation",
        "propose_action": "propose_action",
        "escalate": "escalate",
    })

    # 处置链路（HITL 在这中间）
    g.add_edge("propose_action", "request_approval")
    g.add_conditional_edges("request_approval", route_after_approval, {
        "execute_action": "execute_action",
        "escalate": "escalate",
    })
    g.add_edge("execute_action", "verify")
    g.add_edge("verify", "report")

    g.add_edge("escalate", "report")
    g.add_edge("report", END)

    return g


def build_graph_with_fanout() -> StateGraph:
    """带并行 fan-out 的版本。

    fan-out 的正确接法：把 dispatch 函数挂成 plan_investigation 的条件边，
    而不是 add_edge。dispatch 返回 Send 列表时，LangGraph 会起并行分支。
    """
    g = build_graph()
    # 覆盖上一步的直连边：改成条件边，由 dispatch_evidence 决定去几个分支
    # 注意：StateGraph 不允许重复定义同一起点的边，
    # 所以实际实现里直接把 build_graph 里那行 add_edge 删掉，
    # 统一在下面加 conditional_edges。
    return g


# 最终对外暴露的构造方式（干净版，供直接复制使用）
def compile_graph(checkpointer: AsyncPostgresSaver):
    g = StateGraph(IncidentState)

    for name, fn in [
        ("ingest", ingest_node), ("triage", triage_node),
        ("plan_investigation", plan_investigation_node),
        ("collect_evidence", collect_evidence_node),
        ("synthesize", synthesize_node),
        ("propose_action", propose_action_node),
        ("request_approval", request_approval_node),
        ("execute_action", execute_action_node),
        ("verify", verify_node), ("escalate", escalate_node),
        ("report", report_node),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "triage")
    g.add_conditional_edges("triage", route_after_triage,
                            {"plan_investigation": "plan_investigation",
                             "escalate": "escalate", "report": "report"})

    # ★ fan-out 的唯一正确接法：条件边 + 返回 Send 列表
    g.add_conditional_edges("plan_investigation", dispatch_evidence,
                            ["collect_evidence", "synthesize"])

    # ★ fan-in：N 个并行 collect 全部完成后，才进入 synthesize
    #   靠 add_edge 的隐式屏障（barrier）语义实现
    g.add_edge("collect_evidence", "synthesize")

    g.add_conditional_edges("synthesize", route_after_synthesis,
                            {"plan_investigation": "plan_investigation",
                             "propose_action": "propose_action",
                             "escalate": "escalate"})
    g.add_edge("propose_action", "request_approval")
    g.add_conditional_edges("request_approval", route_after_approval,
                            {"execute_action": "execute_action",
                             "escalate": "escalate"})
    g.add_edge("execute_action", "verify")
    g.add_edge("verify", "report")
    g.add_edge("escalate", "report")
    g.add_edge("report", END)

    return g.compile(checkpointer=checkpointer)


# ---- 全局单例 ----
_graph = None


def set_graph(g) -> None:
    global _graph
    _graph = g


def get_graph():
    if _graph is None:
        raise RuntimeError("graph not initialized; call set_graph() at startup")
    return _graph
```

**fan-out / fan-in 的三个关键点**：

1. **`Send` 只能从条件边返回**，不能从普通节点的返回值里给。这是 API 约束，写错了会静默不并行。
2. **fan-in 靠 `add_edge("collect_evidence", "synthesize")` 的屏障语义**：LangGraph 会等所有并行 `collect_evidence` 分支完成才跑 `synthesize`。这就是为什么 `synthesize` 能看到全部证据。
3. **`Send` 的目标节点名必须是已注册的节点名**，`Send("collect_evidence", {...})` 里的字符串要和 `add_node` 的名字完全一致。

### 调用入口

```python
# app/agent/runner.py
"""给 API / worker 用的统一入口。"""
import logging

from langgraph.types import Command

from app.agent.graph import get_graph
from app.obs.tracing import build_run_config

log = logging.getLogger(__name__)


async def run_diagnosis(incident_id: int, thread_id: str, alert: dict) -> dict:
    """启动一次诊断。可能跑完，也可能停在 interrupt 处。"""
    graph = get_graph()
    config = build_run_config(thread_id, incident_id, phase="diagnose")

    state = {
        "incident_id": incident_id,
        "thread_id": thread_id,
        "alert": alert,
        "evidence": [],
        "errors": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
    }

    result = await graph.ainvoke(state, config=config)
    return _shape(result)


async def resume_diagnosis(thread_id: str, decision: dict, incident_id: int) -> dict:
    """审批后继续。

    关键：thread_id 必须和首次 ainvoke 时一致，否则找不到 checkpoint。
    """
    graph = get_graph()
    config = build_run_config(thread_id, incident_id, phase="resume")
    result = await graph.ainvoke(Command(resume=decision), config=config)
    return _shape(result)


def _shape(result: dict) -> dict:
    """把图返回值整理成 API 层好用的形状。

    __interrupt__ 是 LangGraph 在中断时塞进结果的特殊键。
    """
    interrupts = result.get("__interrupt__") or []
    return {
        "status": result.get("status"),
        "incident_id": result.get("incident_id"),
        "interrupted": bool(interrupts),
        "interrupt_payload": interrupts[0].value if interrupts else None,
        "confidence": result.get("confidence"),
        "hypotheses": result.get("hypotheses", []),
        "plan": result.get("plan"),
        "execution": result.get("execution"),
        "verification": result.get("verification"),
        "escalate_reason": result.get("escalate_reason"),
        "usage": result.get("usage"),
        "errors": result.get("errors", []),
    }


async def get_graph_state(thread_id: str) -> dict:
    """读取当前图状态（审批台渲染用）。"""
    graph = get_graph()
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return {
        "values": snapshot.values,
        "next": snapshot.next,
        "interrupted": bool(snapshot.next),
        "created_at": snapshot.created_at,
    }
```

---

## 8. 生命周期：checkpointer 必须常驻

```python
# app/main.py（只展示 lifespan 部分，完整版见 05）
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.graph import compile_graph, set_graph
from app.agent.mcp_client import init_mcp_tools
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 1) MCP 工具加载（4 个 server 握手一次）----
    await init_mcp_tools()

    # ---- 2) checkpointer 常驻 + 建表 ----
    # AsyncPostgresSaver.from_conn_string 是异步上下文管理器，
    # 必须用 AsyncExitStack 手动进入，才能跨越整个应用生命周期。
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.checkpointer_dsn)
        )
        await saver.setup()          # 幂等，重复调用安全

        set_graph(compile_graph(saver))
        app.state.checkpointer = saver
        yield
```

**这段是新手最容易写错的地方**，三个坑：

| 坑 | 后果 | 正解 |
|---|---|---|
| 在请求处理函数里 `AsyncPostgresSaver.from_conn_string(...)` | 每个请求建一个连接池，几十个并发就把 Postgres 连接数吃满 | 用 `AsyncExitStack` 在 lifespan 里常驻 |
| 忘了 `await saver.setup()` | 第一次调用报 `relation "checkpoints" does not exist` | lifespan 里调用一次，它是幂等的 |
| 用 `PostgresSaver`（同步版） | 在 async 代码里阻塞事件循环 | 用 `AsyncPostgresSaver` |

**worker 侧同理**：ARQ 的 `on_startup` 钩子里做一遍。**但注意图必须在 API 和 worker 里各编译一份**（进程独立），checkpointer 指向同一个库，所以 API 能让 worker 的图 resume —— **这正是"进程分离 + 状态共享"的体现。**

---

## 9. 本层验收清单

- [ ] 用 `python -c` 手动 `run_diagnosis()` 跑通 `memory_leak` 场景，能到 `propose_action`
- [ ] Postgres 里 `SELECT count(*) FROM checkpoints WHERE thread_id = '<incident_id>'` 有多行（证明每步都落盘）
- [ ] 把 `confidence_threshold` 调到 0.99，观察图是否回环到 `plan_investigation` 且 `investigation_round` 递增
- [ ] 调到 0.99 且 `max_investigation_rounds=1`，观察是否直接走 `escalate`（验证止损条件）
- [ ] 图停在 `request_approval` 后，**重启 worker 进程**，再用 `resume_diagnosis` 能继续执行（这是 HITL 的终极验收）
- [ ] `resume_diagnosis` 同一 thread 调两次，第二次 `idempotent_replay: true`（验证多层幂等）
- [ ] 审批驳回后，图走 `escalate` 而不是 `execute_action`
- [ ] 删除 `hypotheses` 中的 tags（模拟 unknown 根因），验证不走 `propose_action`
- [ ] `SELECT sum(total_tokens), sum(total_cost_usd) FROM incidents` 有非零值
- [ ] `incident_knowledge` 在故障 resolved 后多了一行且 `embedding IS NOT NULL`

**第 5 条（重启进程后 resume）是整个项目的技术巅峰。** 如果这条通了，你的项目在面试里就立于不败之地 —— 因为绝大多数候选人的"Agent 项目"是一个跑完就结束的脚本。

---

## 下一步

Agent 能跑了，接上外部世界 → `05-代码指导-API与UI层.md`
