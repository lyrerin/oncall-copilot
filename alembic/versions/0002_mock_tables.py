from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建 3 张 mock 表 + 索引。"""

    # ============ 1. mock_logs ============
    op.execute(
        """
CREATE TABLE mock_logs (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    level       TEXT        NOT NULL DEFAULT 'INFO',
    service     TEXT        NOT NULL,
    message     TEXT,
    trace_id    TEXT
)
"""
    )
    # fetch_logs：WHERE service + ts >= + ORDER BY ts DESC
    op.execute("CREATE INDEX idx_mock_logs_service_t ON mock_logs (service, ts DESC)")
    # get_trace：WHERE trace_id = :tid
    op.execute("CREATE INDEX idx_mock_logs_trace     ON mock_logs (trace_id)")

    # ============ 2. mock_metrics ============
    op.execute(
        """
CREATE TABLE mock_metrics (
    id          BIGSERIAL        PRIMARY KEY,
    ts          TIMESTAMPTZ      NOT NULL,
    metric      TEXT             NOT NULL,
    service     TEXT             NOT NULL,
    value       DOUBLE PRECISION NOT NULL
)
"""
    )
    # fetch_metric_points：WHERE service + metric + ts >= + ORDER BY ts
    # 三列等值 / 范围都在这条上，一条索引顶两条，不要再建 (service, ts) 或单列 (metric)
    op.execute(
        "CREATE INDEX idx_mock_metrics_service_metric_t "
        "ON mock_metrics (service, metric, ts)"
    )

    # ============ 3. mock_changes ============
    op.execute(
        """
CREATE TABLE mock_changes (
    id          BIGSERIAL   PRIMARY KEY,
    service     TEXT        NOT NULL,
    kind        TEXT        NOT NULL DEFAULT 'change',
    operator    TEXT        NOT NULL,
    at          TIMESTAMPTZ NOT NULL,
    detail      TEXT,
    "commit"    TEXT        NOT NULL
)
"""
    )
    # fetch_recent_changes：WHERE service + at >= + ORDER BY at DESC
    # kind 只出现在 SELECT 里、从不做过滤条件，建索引只会白担写入成本
    op.execute("CREATE INDEX idx_mock_changes_service_t ON mock_changes (service, at DESC)")


def downgrade() -> None:
    """逆序删表 → 无扩展要删（pg_trgm / vector 都归 0001）。"""

    # 表自己带索引，DROP TABLE 会一起带走，不需要单独 DROP INDEX
    op.execute("DROP TABLE IF EXISTS mock_changes")
    op.execute("DROP TABLE IF EXISTS mock_metrics")
    op.execute("DROP TABLE IF EXISTS mock_logs")
