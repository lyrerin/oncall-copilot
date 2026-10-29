# oncall-copilot

基于 **MCP + LangGraph** 的故障诊断 Agent：多系统工具编排 + 人在环审批 + 全链路可观测。

告警进 → 自动并行调查（日志/指标/变更/历史故障）→ 带置信度的根因判断 → 高危操作走人在环审批 → MCP 执行 → 可评估可续跑。

## 进度

| 周 | 内容 | 状态 |
|---|---|---|
| W1 | 地基：compose 起 postgres + redis、12 张业务表迁移、Dockerfile 依赖层、配置与仓储骨架 | ✅ |
| W2 | MCP 服务层：mock 三表 + 5 场景造数、4 个 MCP server（logs / metrics / cmdb / executor）、alertgen 告警发生器 | ✅ 代码已落地 |
| W3 | 链路打通：MCP Client 加载全部工具 + 最小两节点图 | 待开始 |

排期与验收标准见 [`docs/07-排期与验收.md`](docs/07-排期与验收.md)。

## 快速开始

前置：Docker（含 compose）、Python 3.12、[uv](https://docs.astral.sh/uv/)。

```bash
cp .env.example .env        # 至少把 LLM_API_KEY 换成真 key，其余本地可留默认

docker compose up -d        # 默认只起 postgres + redis（其余服务都挂了 profile）

# 建表 + 灌 mock 数据：.env 里 DSN 用的是服务名 postgres/redis，
# 裸跑本机命令会连不上，所以这两个都在容器里执行
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m mockenv.generate --all

docker compose --profile mcp up -d --build   # 起 4 个 MCP server：8001~8004
```

起完先探通工具层（有响应即可，路径默认 `/mcp`）：

```bash
curl http://localhost:8001/mcp    # mcp-logs
curl http://localhost:8002/mcp    # mcp-metrics
curl http://localhost:8003/mcp    # mcp-cmdb
curl http://localhost:8004/mcp    # mcp-executor
```

compose 分层：默认只有 postgres / redis；`--profile mcp` 起工具层；`--profile app` 起 api / worker / ui / alertgen；`--profile obs` 起 langfuse（单个吃 1GB+ 内存，W10 之前别开）。

本机跑代码（不建容器）：

```bash
uv python pin 3.12          # 机器默认 3.14 会被 >=3.12 放行，必须 pin
uv sync --extra dev
uv run python -c "import langgraph, fastapi, sqlalchemy, mcp, fastmcp; print('依赖 OK')"
```

## 目录

| 路径 | 作用 |
|---|---|
| `app/` | FastAPI + LangGraph 主体：`api/` 路由、`agent/` 图与节点、`db/` 模型与仓储、`services/`、`obs/` 追踪 |
| `mcp_servers/` | 4 个独立 MCP server 进程 + `store.py` 数据访问 |
| `mockenv/` | `generate.py` 造 5 个故障场景的日志/指标/变更，`alertgen.py` 定时灌 Alertmanager 格式的假告警 |
| `workers/` | arq 异步诊断 worker |
| `ui/` | Streamlit 审批控制台 |
| `eval/` | 场景回放评测 |
| `alembic/` | `0001` 业务表，`0002` 三张 mock 表 |
| `spikes/` | 技术验证脚本（LLM / MCP / LangGraph 各一根针） |

## 文档

设计文档在 `docs/`，从 [`docs/00-开工指南.md`](docs/00-开工指南.md) 开始看。
