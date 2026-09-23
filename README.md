
# oncall-copilot

基于 **MCP + LangGraph** 的故障诊断 Agent：多系统工具编排 + 人在环审批 + 全链路可观测。

告警进 → 自动并行调查（日志/指标/变更/历史故障）→ 带置信度的根因判断 → 高危操作走人在环审批 → MCP 执行 → 可评估可续跑。

## 文档

设计文档在 `docs/`，从 `docs/00-开工指南.md` 开始看。

## 开发

（W1 结束时补：`docker compose up -d`）
