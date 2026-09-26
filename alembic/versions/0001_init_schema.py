"""init schema —— 12 张表 + 2 个扩展

Revision ID: 0001
Revises:
Create Date: 2026-09-25 00:14:38.464156

DDL 来源：docs/02-数据库设计.md §4「建表 DDL」，逐条照抄，没有走 --autogenerate。

和文档 §4 的两处差别（照抄会失败）：
  1. 建表顺序：文档里 alerts 排在 incidents 前面，但 alerts.incident_id 是指向
     incidents(id) 的外键，所以这里把 incidents 提到最前。
  2. 一条 DDL 一个 op.execute()：SQLAlchemy 的 asyncpg 方言对所有语句都走
     prepared statement，一次塞多条会报
     "cannot insert multiple commands into a prepared statement"。

创建顺序（downgrade 严格逆序）：
  incidents → alerts → evidence → hypotheses → runbooks → action_plans
  → approvals → executions → audit_logs → incident_knowledge
  → eval_cases → eval_runs

注意：SQL 顶格写在字符串里（不缩进），方便和 docs/02 直接对照复制。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建扩展 → 建表 → 建索引。"""

    # ============ 扩展 ============
    # 必须排在建表之前：incident_knowledge.embedding 是 vector(1024)，
    # 扩展没建就报 type "vector" does not exist
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ============ 1. incidents ============
    # （文档里是第 2 张，这里提到最前，因为 alerts 的外键指向它）
    op.execute(
        """
CREATE TABLE incidents (
    id                  BIGSERIAL PRIMARY KEY,
    title               TEXT        NOT NULL,
    service             TEXT,
    severity            TEXT        NOT NULL DEFAULT 'warning',
    status              TEXT        NOT NULL DEFAULT 'new',
    first_alert_at      TIMESTAMPTZ NOT NULL,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    mttr_seconds        INTEGER,
    root_cause          TEXT,
    confidence          REAL,
    summary             TEXT,
    diagnosis_rounds    SMALLINT    NOT NULL DEFAULT 0,
    total_tokens        INTEGER     NOT NULL DEFAULT 0,
    total_cost_usd      NUMERIC(10,6) NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_incidents_status CHECK (status IN (
        'new','investigating','awaiting_approval','executing',
        'verifying','resolved','escalated','failed'
    )),
    CONSTRAINT ck_incidents_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
)
"""
    )
    op.execute("CREATE INDEX idx_incidents_status   ON incidents (status)")
    op.execute("CREATE INDEX idx_incidents_service  ON incidents (service, detected_at DESC)")
    op.execute(
        """
CREATE INDEX idx_incidents_open     ON incidents (detected_at DESC)
    WHERE status NOT IN ('resolved','escalated')
"""
    )

    # ============ 2. alerts ============
    op.execute(
        """
CREATE TABLE alerts (
    id              BIGSERIAL PRIMARY KEY,
    fingerprint     TEXT        NOT NULL,
    source          TEXT        NOT NULL,
    name            TEXT        NOT NULL,
    severity        TEXT        NOT NULL,
    service         TEXT,
    labels          JSONB       NOT NULL DEFAULT '{}',
    annotations     JSONB       NOT NULL DEFAULT '{}',
    raw             JSONB       NOT NULL,
    starts_at       TIMESTAMPTZ NOT NULL,
    ends_at         TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'firing',
    incident_id     BIGINT      REFERENCES incidents(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_alerts_fingerprint_firing
    ON alerts (fingerprint) WHERE status = 'firing'
"""
    )
    op.execute("CREATE INDEX idx_alerts_incident  ON alerts (incident_id)")
    op.execute("CREATE INDEX idx_alerts_service_t ON alerts (service, starts_at DESC)")
    op.execute("CREATE INDEX idx_alerts_labels    ON alerts USING GIN (labels jsonb_path_ops)")

    # ============ 3. evidence ============
    op.execute(
        """
CREATE TABLE evidence (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     BIGINT      NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    round           SMALLINT    NOT NULL DEFAULT 1,
    kind            TEXT        NOT NULL,
    tool_name       TEXT        NOT NULL,
    request         JSONB       NOT NULL DEFAULT '{}',
    payload         JSONB       NOT NULL,
    summary         TEXT,
    duration_ms     INTEGER,
    ok              BOOLEAN     NOT NULL DEFAULT true,
    error           TEXT,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
    )
    op.execute("CREATE INDEX idx_evidence_incident ON evidence (incident_id, round, kind)")

    # ============ 4. hypotheses ============
    op.execute(
        """
CREATE TABLE hypotheses (
    id                      BIGSERIAL PRIMARY KEY,
    incident_id             BIGINT   NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    round                   SMALLINT NOT NULL DEFAULT 1,
    rank                    SMALLINT NOT NULL,
    statement               TEXT     NOT NULL,
    confidence              REAL     NOT NULL,
    rationale               TEXT,
    supporting_evidence_ids JSONB    NOT NULL DEFAULT '[]',
    adopted                 BOOLEAN  NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hypotheses_rank UNIQUE (incident_id, round, rank)
)
"""
    )
    op.execute("CREATE INDEX idx_hypotheses_incident ON hypotheses (incident_id, round)")

    # ============ 5. runbooks ============
    op.execute(
        """
CREATE TABLE runbooks (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT        NOT NULL UNIQUE,
    title       TEXT        NOT NULL,
    service     TEXT,
    match_rule  JSONB       NOT NULL DEFAULT '{}',
    action      TEXT        NOT NULL,
    params_tpl  JSONB       NOT NULL DEFAULT '{}',
    risk_level  TEXT        NOT NULL,
    requires_approval BOOLEAN NOT NULL DEFAULT true,
    steps       JSONB       NOT NULL DEFAULT '[]',
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_runbooks_risk CHECK (risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL'))
)
"""
    )
    op.execute("CREATE INDEX idx_runbooks_match ON runbooks (service, is_active)")

    # ============ 6. action_plans ============
    op.execute(
        """
CREATE TABLE action_plans (
    id            BIGSERIAL PRIMARY KEY,
    incident_id   BIGINT      NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    runbook_id    BIGINT      REFERENCES runbooks(id),
    action        TEXT        NOT NULL,
    target        TEXT        NOT NULL,
    params        JSONB       NOT NULL DEFAULT '{}',
    risk_level    TEXT        NOT NULL,
    reason        TEXT,
    status        TEXT        NOT NULL DEFAULT 'proposed',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_plans_status CHECK (status IN (
        'proposed','awaiting_approval','approved','rejected','executing','succeeded','failed'
    ))
)
"""
    )
    op.execute("CREATE INDEX idx_plans_incident ON action_plans (incident_id, created_at DESC)")
    op.execute(
        """
CREATE INDEX idx_plans_pending  ON action_plans (status) WHERE status = 'awaiting_approval'
"""
    )

    # ============ 7. approvals ============
    op.execute(
        """
CREATE TABLE approvals (
    id             BIGSERIAL PRIMARY KEY,
    action_plan_id BIGINT      NOT NULL REFERENCES action_plans(id) ON DELETE CASCADE,
    thread_id      TEXT        NOT NULL,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ,
    decision       TEXT,
    approver       TEXT,
    comment        TEXT,
    context_snapshot JSONB     NOT NULL DEFAULT '{}',
    CONSTRAINT ck_approvals_decision CHECK (
        decision IS NULL OR decision IN ('approved','rejected','timeout')
    )
)
"""
    )
    op.execute(
        """
CREATE INDEX idx_approvals_pending ON approvals (requested_at DESC) WHERE decision IS NULL
"""
    )
    op.execute("CREATE UNIQUE INDEX uq_approvals_plan ON approvals (action_plan_id)")

    # ============ 8. executions ============
    op.execute(
        """
CREATE TABLE executions (
    id                BIGSERIAL PRIMARY KEY,
    action_plan_id    BIGINT      NOT NULL REFERENCES action_plans(id) ON DELETE CASCADE,
    idempotency_key   TEXT        NOT NULL UNIQUE,
    action            TEXT        NOT NULL,
    target            TEXT        NOT NULL,
    params            JSONB       NOT NULL DEFAULT '{}',
    status            TEXT        NOT NULL DEFAULT 'running',
    result            JSONB,
    error             TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    duration_ms       INTEGER,
    CONSTRAINT ck_exec_status CHECK (status IN ('running','succeeded','failed','skipped'))
)
"""
    )
    op.execute("CREATE INDEX idx_exec_plan ON executions (action_plan_id)")

    # ============ 9. audit_logs ============
    op.execute(
        """
CREATE TABLE audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    incident_id  BIGINT      REFERENCES incidents(id) ON DELETE SET NULL,
    actor        TEXT        NOT NULL,
    actor_type   TEXT        NOT NULL,
    action       TEXT        NOT NULL,
    target_type  TEXT,
    target_id    TEXT,
    detail       JSONB       NOT NULL DEFAULT '{}',
    ip           INET,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
    )
    op.execute("CREATE INDEX idx_audit_incident ON audit_logs (incident_id, created_at DESC)")
    op.execute("CREATE INDEX idx_audit_actor    ON audit_logs (actor, created_at DESC)")

    # ============ 10. incident_knowledge ============
    op.execute(
        """
CREATE TABLE incident_knowledge (
    id           BIGSERIAL PRIMARY KEY,
    incident_id  BIGINT      REFERENCES incidents(id) ON DELETE SET NULL,
    kind         TEXT        NOT NULL DEFAULT 'incident',
    title        TEXT        NOT NULL,
    content      TEXT        NOT NULL,
    embedding    vector(1024),
    metadata     JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
    )
    op.execute(
        """
CREATE INDEX idx_knowledge_embedding ON incident_knowledge
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
"""
    )
    op.execute("CREATE INDEX idx_knowledge_kind ON incident_knowledge (kind)")

    # ============ 11. eval_cases ============
    op.execute(
        """
CREATE TABLE eval_cases (
    id                   BIGSERIAL PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE,
    alert_payload        JSONB NOT NULL,
    expected_service     TEXT,
    expected_root_cause  TEXT NOT NULL,
    expected_tags        JSONB NOT NULL DEFAULT '[]',
    expected_action      TEXT,
    difficulty           TEXT NOT NULL DEFAULT 'normal',
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
    )

    # ============ 12. eval_runs ============
    op.execute(
        """
CREATE TABLE eval_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    git_sha         TEXT,
    model           TEXT,
    prompt_version  TEXT,
    total           INTEGER NOT NULL DEFAULT 0,
    service_correct INTEGER NOT NULL DEFAULT 0,
    root_cause_correct INTEGER NOT NULL DEFAULT 0,
    action_correct  INTEGER NOT NULL DEFAULT 0,
    avg_latency_ms  INTEGER,
    avg_prompt_tokens     INTEGER,
    avg_completion_tokens INTEGER,
    total_cost_usd  NUMERIC(10,6),
    detail          JSONB NOT NULL DEFAULT '[]',
    CONSTRAINT uq_eval_runs_sha UNIQUE (git_sha, model, prompt_version)
)
"""
    )
    op.execute("CREATE INDEX idx_eval_runs_time ON eval_runs (started_at DESC)")


def downgrade() -> None:
    """逆序删表 → 删扩展。"""

    # 表自己带索引，DROP TABLE 会一起带走，不需要单独 DROP INDEX
    op.execute("DROP TABLE IF EXISTS eval_runs")
    op.execute("DROP TABLE IF EXISTS eval_cases")
    op.execute("DROP TABLE IF EXISTS incident_knowledge")
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP TABLE IF EXISTS executions")
    op.execute("DROP TABLE IF EXISTS approvals")
    op.execute("DROP TABLE IF EXISTS action_plans")
    op.execute("DROP TABLE IF EXISTS runbooks")
    op.execute("DROP TABLE IF EXISTS hypotheses")
    op.execute("DROP TABLE IF EXISTS evidence")
    op.execute("DROP TABLE IF EXISTS alerts")
    op.execute("DROP TABLE IF EXISTS incidents")

    # 必须最后删：表还在用 vector 时删扩展会失败
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
