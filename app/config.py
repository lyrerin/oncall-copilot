# app/config.py
from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- 基础 ---
    app_name: str = "oncall-copilot"
    env: str = "dev"
    log_level: str = "INFO"

    # --- 数据库 ---
    pg_dsn_raw: str = Field(
        default="postgresql+asyncpg://oncall:oncall@postgres:5432/oncall",
        alias="PG_DSN",
    )
    # checkpointer 用 psycopg3，DSN 前缀不同，由 pg_dsn_raw 推导
    @property
    def pg_dsn(self) -> str:
        return self.pg_dsn_raw

    @property
    def checkpointer_dsn(self) -> str:
        # postgresql+asyncpg:// → postgresql://
        return self.pg_dsn_raw.replace("postgresql+asyncpg://", "postgresql://")

    redis_dsn: RedisDsn = Field(default="redis://redis:6379/0", alias="REDIS_DSN")

    # --- LLM ---
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")
    llm_model_cheap: str = Field(default="deepseek-chat", alias="LLM_MODEL_CHEAP")
    llm_temperature: float = 0.1          # 诊断要稳定，不要创造性

    # --- Embedding（历史故障检索） ---
    embedding_model: str = Field(default="BAAI/bge-m3", alias="EMBEDDING_MODEL")
    embedding_dim: int = 1024             # 必须和迁移里的 vector(1024) 一致

    # --- MCP ---
    mcp_logs_url: str = "http://mcp-logs:8001/mcp"
    mcp_metrics_url: str = "http://mcp-metrics:8002/mcp"
    mcp_cmdb_url: str = "http://mcp-cmdb:8003/mcp"
    mcp_executor_url: str = "http://mcp-executor:8004/mcp"
    mcp_timeout_s: float = 15.0

    # --- Agent 策略 ---
    confidence_threshold: float = 0.6     # 低于它 → 回环补调查
    max_investigation_rounds: int = 3     # 回环上限，成本闸门
    max_evidence_per_round: int = 6       # 单轮并行分支上限
    approval_timeout_minutes: int = 30    # 超时视为 rejected

    # --- 幂等与去重 ---
    alert_dedup_ttl_s: int = 600
    action_lock_ttl_s: int = 300

    # --- 可观测 ---
    langfuse_enabled: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://langfuse:3000"

    # --- 成本（用于报表，按你的模型单价改） ---
    price_prompt_per_1k: float = 0.00014
    price_completion_per_1k: float = 0.00028


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
