# oncall-copilot · 完整项目计划

> **这是什么**：一个新项目（不是现有 `kbqa` 项目的延续）的完整设计与实施计划，含可落地的代码。
> **怎么用**：把整个 `oncall-copilot-plan` 目录移出本仓库，作为新仓库 `oncall-copilot` 的 `docs/` 目录，或独立成你自己的项目文档。
> **🏁 不知道从哪下手？直接打开 [00-开工指南.md](00-开工指南.md)。** 那是唯一一份按"天"写的文档，讲 Day 0~Day 4 具体敲什么命令。下面的索引按"层"组织，是查手册用的。

---

## 项目一句话

**智能值班 Copilot**：接收 Prometheus Alertmanager 告警 → 自动并行调查多系统证据（日志 / 指标 / 变更 / 历史故障）→ 产出带置信度的根因判断与处置方案 → 高危操作走**人在环审批**后经 MCP 执行 → 全程可观测、可评估、可断点续跑。

**简历叙事**（技术栈不变，但把"业务"降权、"LLM 应用工程"提权）：

> 基于 **MCP + LangGraph** 的故障诊断 Agent：多系统工具编排 + 人在环审批 + 全链路可观测

❌ 不要写成"运维告警分析平台" —— 听起来像内部工具，AI 应用开发的权重被业务盖住。

---

## 文档索引

| 文档 | 内容 | 什么时候看 |
|---|---|---|
| [**00-开工指南.md**](00-开工指南.md) | **前期流程：Day 0 装环境 → Day 1 建仓库 → Day 2 读文档 → Day 3 打探针 → Day 4 进 W1** | **项目刚开始，第一份看它** |
| [01-架构设计.md](01-架构设计.md) | 服务拓扑、MCP 工具清单、LangGraph 图设计、HITL 时序、参考项目 | 动手前通读 |
| [02-数据库设计.md](02-数据库设计.md) | 11 张表 DDL、SQLAlchemy 模型、状态机、pgvector、checkpointer 表 | W1 |
| [03-代码指导-基础层.md](03-代码指导-基础层.md) | compose / 配置 / Redis / 4 个 MCP Server / mock 数据 / MCP Client | W1-W3 |
| [04-代码指导-Agent层.md](04-代码指导-Agent层.md) | State + reducer、11 个节点、图装配、checkpointer、interrupt/resume | W4-W7 |
| [05-代码指导-API与UI层.md](05-代码指导-API与UI层.md) | webhook / ARQ worker / SSE / 审批接口 / Streamlit 审批台 | W8-W9 |
| [06-可观测与评估.md](06-可观测与评估.md) | Langfuse、token 埋点、指标接口、评估集、run_eval | W10-W11 |
| [**07-排期与验收.md**](07-排期与验收.md) | **12 周完整计划表** + 每周验收清单 + 砍单顺序 + 风险表 + 演示脚本 | 全程对照 |
| [08-面试问答映射.md](08-面试问答映射.md) | 25 个高频问题 → 答法 → 代码位置 → 加分补充 | W8 起每周练 |

---

## 技术栈

| 层 | 选型 | 为什么选它 |
|---|---|---|
| Agent 编排 | **LangGraph** + `langgraph-checkpoint-postgres` | 图可读可测；checkpointer 支撑断点续跑与 HITL |
| 工具协议 | **MCP**（FastMCP 写 Server + langchain-mcp-adapters 做 Client） | 工具属于不同系统/团队，协议统一 + 进程级权限隔离 |
| LLM | DeepSeek（OpenAI 兼容接口） | 成本敏感；接口兼容所以可替换；primary/cheap 双角色 |
| Web | FastAPI + Uvicorn + SSE | async 一致；SSE 推实时时间线 |
| 主库 | **PostgreSQL 16** + **pgvector** | 状态机多表关联 + 向量检索同事务 |
| 缓存/队列 | **Redis 7** + **ARQ** | 告警去重、分布式锁、async 任务队列 |
| ORM/迁移 | SQLAlchemy 2.0 (async) + asyncpg + **Alembic** | 迁移版本化可回滚 |
| 可观测 | **Langfuse**（自托管） | 自托管无外部依赖；LangChain 集成现成 |
| UI | Streamlit | 复用你已有的经验，成本低，演示效果够 |
| 部署 | **Docker Compose**（11 个服务） | 分层 `profiles`，按周按需起，**不要一次全开** |

**新增技术覆盖**（相对你已会的东西）：MCP 双端、LangGraph checkpointer/子图/Send 并行/interrupt HITL、Postgres+pgvector+Redis、ARQ、Langfuse、Docker Compose 多服务编排、评估体系。

---

## 12 周排期速览

| 周 | 主题 | 周末必须能演示 |
|---|---|---|
| W1 | 地基 | `alembic downgrade -1 && upgrade head` 来回跑通 |
| W2 ★ | MCP 服务层 | 手调工具看到错误模板聚类；幂等键调两次第二次 replay |
| W3 | 打通链路 | LLM 自己选工具查到日志并回答 |
| W4 | 并行调查 | 日志显示 4 个分支并行，耗时对比串行 |
| W5 | 根因推理 | 阈值调高后自动补调查，轮次上限自动停 |
| W6 ★ | 状态持久化 | **重启进程后从 checkpoint 继续** |
| W7 | 人在环 | 点批准 → 图继续跑完；驳回 → 转人工 |
| W8 ★ | 幂等与队列 | **完整闭环全通**（M2 里程碑，最低可交付线） |
| W9 | 完整应用 | 三个 UI 页面 + 审计流水可见 |
| W10 | 可观测 | Langfuse 看单次诊断开销分解 |
| W11 | 评估体系 | 跑评估出准确率；改坏 prompt 看它掉 |
| W12 | 收尾 | 8 分钟完整演示 + README + 简历条目 |

★ = 关键周。**M2（W8 末完整闭环）是最低可交付线**，W9-W12 都是加值。

详细到天的任务、每周验收清单、风险对策见 [07-排期与验收.md](07-排期与验收.md)。

---

## 四个杀手锏演示（必须准备）

| # | 演示 | 证明什么 |
|---|---|---|
| 1 | 审批卡片带完整上下文（根因 + 置信度 + 证据摘要 + 参数 + 回滚计划） | 人在环不是走过场 |
| 2 | 审计流水完整（agent 调查 → human 批准 → agent 执行 → agent 验证） | AI 操作系统必须有可追溯性 |
| 3 | **主动重复点审批 → 409 拒绝** | 幂等不是嘴上说的 |
| 4 | **`docker compose restart worker` 后再审批，照样执行成功** | checkpointer 的必要性 —— 状态在 Postgres 不在内存 |

**第 4 个是决胜局。** 绝大多数候选人的 Agent 项目跑完就结束，讲不出"状态在哪、重启会怎样"。

---

## 时间不够时的砍单顺序

从上往下砍，**砍到第 8 项项目依然成立**：

1. Streamlit 三页面 → 只留待审批页
2. 指标看板图表 → 保留接口
3. Langfuse → 自建 `llm_calls` 表
4. SSE 实时时间线 → 刷新按钮
5. ARQ → FastAPI BackgroundTasks
6. 评估集 → 只留 1 个案例
7. pgvector 检索 → alertname 精确匹配

### 绝对不能砍的四项

| 不可砍 | 为什么 |
|---|---|
| **MCP Server + Client 双端** | 项目定位。只写 client 就是"调了个库" |
| **checkpointer + interrupt** | 技术巅峰。没有它项目退化成一个脚本 |
| **幂等执行 + 审批强绑定** | 安全边界。砍了就变成"AI 能直接改生产环境" |
| **至少 1 个量化数字**（成本或延迟） | 没有数字就只是"做了个东西" |

---

## 参考项目（怎么用，别怎么用）

| 用途 | 仓库 | 只抄什么 |
|---|---|---|
| **领域设计参考** | [HolmesGPT/holmesgpt](https://github.com/HolmesGPT/holmesgpt)（CNCF Sandbox） | 调查动作怎么切分、评估怎么做。[中文深度拆解](https://cloud.tencent.com.cn/developer/article/2695252) |
| **MCP 接入** | [langchain-ai/langchain-mcp-adapters](https://github.com/langchain-ai/langchain-mcp-adapters) | MCP 工具 → LangChain Tool 的官方做法 |
| **MCP Server 编写** | [jlowin/fastmcp](https://github.com/jlowin/fastmcp)、[modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) | 装饰器写 server、streamable-http 传输 |
| **HITL 写法** | [langchain-ai/langchain-skills · langgraph-human-in-the-loop](https://github.com/langchain-ai/langchain-skills/blob/26b7f14e9657ca7c9caadc6eacc844240489305b/config/skills/langgraph-human-in-the-loop/SKILL.md) | `interrupt()` 与 checkpointer 的配合 |
| **工程分层** | [wassim249/fastapi-langgraph-agent-production-ready-template](https://github.com/wassim249/fastapi-langgraph-agent-production-ready-template) | FastAPI 包 LangGraph、目录分层、配置管理 |
| **可观测** | [langfuse/langfuse](https://github.com/langfuse/langfuse) | 自托管 compose + LangChain 集成 |
| **mock 数据** | [logpai/loghub](https://github.com/logpai/loghub) | **真实生产日志**数据集，比手编可信 |
| **生态速览** | [pavangudiwada/awesome-ai-sre](https://github.com/pavangudiwada/awesome-ai-sre)、[last9/awesome-sre-agents](https://github.com/last9/awesome-sre-agents) | AIOps agent 全景 |

> ⚠️ **HolmesGPT 是自研 agent 循环 + 自带 toolset 抽象，既不用 LangGraph 也不用 MCP，架构跟本项目是两条路。** 只抄它的领域模型，执行引擎自己搭。面试时"我为什么不用 HolmesGPT 的架构"本身就是加分项。

---

## 三条防烂尾铁律

1. **每周末项目必须能跑。** 宁可少做一个功能，不能让项目停在跑不起来的状态。
2. **每天 commit。** 半成品也提交，配 `WIP:` 前缀。
3. **踩坑当天记录。** 事后回忆的坑没有细节，而面试恰恰问细节。

---

## 关键文档之外的两件事

**面试官最想听的是判断和取舍，不是技术名词。** 每个回答都要有「我选了 X，因为 Y，代价是 Z」的结构。只罗列技术栈的候选人和能讲清 tradeoff 的候选人，差距就在这里。

**把失败也讲出来。** "评估第一版用字符串匹配，准确率永远是 0，我改成标签匹配才有意义" 比 "我的准确率 90%" 可信十倍。见 [08-面试问答映射.md](08-面试问答映射.md) 的 G4 题。
