"""app/db/repo.py —— 数据访问函数（唯一碰 session 的地方）。

状态：骨架，未实现。
实现位置：docs/02-数据库设计.md 第 656 行起（§ app/db/repo.py），
          docs/04-代码指导-Agent层.md 第 283 行起（只列 Agent 层用到的部分）。
约定：上层（节点 / API）不直接写 SQL，一律通过这里的函数。
"""
from sqlalchemy import delete
from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError
from datetime import datetime,timezone
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import (
    Alert,Approval,AuditLog,Evidence,Hypothesis,Incident,ActionPlan,Runbook,Execution
)


async def claim_execution(
    session: AsyncSession,*,plan_id: int,action: str,target: str,
    params: dict,idempotency_key: str):
    """尝试占用幂等键。

        返回 (execution, is_new)：
        is_new=True  → 本次是首次执行，调用方应该真的执行动作
        is_new=False → 已经执行过，直接返回历史结果，不重复执行
        """
    existing = await session.scalar(
        select(Execution).where(Execution.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing, False

    row = Execution(
        action_plan_id = plan_id,idempotency_key = idempotency_key,
        action = action,target = target,params = params,status = "running"
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Execution).where(Execution.idempotency_key == idempotency_key)
        )
        return existing, False
    return row, True

async def get_incident(s: AsyncSession,incident_id:int):
    return await s.get(Incident,incident_id)


async def set_incident_status(s:AsyncSession,incident_id: int,status:str,**fields):
    """统一的状态更新入口。

    用 **fields 允许顺带更新别的列（如 confidence / root_cause）。
    所有状态变更都走这一个函数 —— 好处是可以在这一处埋审计日志。
    """
    values = {'status': status,'updated_at': datetime.now(timezone.utc),**fields}
    if status == 'resolved' and 'resolved_at' not in values:
        values['resolved_at'] = datetime.now(timezone.utc)
    await s.execute(update(Incident).where(Incident.id == incident_id).values(**values))

async def add_evidence(s:AsyncSession, incident_id: int,**kw):
    row = Evidence(incident_id = incident_id,**kw)
    s.add(row)
    await s.flush()
    return row

async def save_hypotheses(s:AsyncSession, incident_id: int,round_no: int,hyps: list[dict]):
    """覆盖式保存某一轮的假设。

    先删同轮旧记录：因为调查回环时同一轮号会被重算，
    不删会出现 rank 唯一约束冲突。
    """
    await s.execute(
        delete(Hypothesis).where(
            Hypothesis.incident_id == incident_id,
            Hypothesis.round == round_no)
    )
    rows = [
        Hypothesis(incident_id = incident_id,round = round_no, rank = i + 1,**h)
        for i, h in enumerate(hyps)
    ]
    s.add_all(rows)
    await s.flush()
    return rows

async def list_active_runbooks(s: AsyncSession, service:str | None):
    stmt = select(Runbook).where(Runbook.is_active.is_(True))
    rows = (await s.execute(stmt)).scalars().all()
    return [r for r in rows if r.service is None or r.service == service]
