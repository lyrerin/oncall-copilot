"""mcp_servers/executor_server.py —— mcp-executor :8004（最关键的一个）。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 922 行起（§5.5 执行器 MCP Server）。
职责：暴露执行动作的工具。全部带 dry_run 参数。
三条硬约束（写进 server，不靠 prompt）：
  1. 动作必须在 ACTION_CATALOG 白名单里，否则直接抛错；
  2. apply_action 先查 idempotency_key，已执行则原样返回首次结果；
  3. CRITICAL 级动作要求 approval_id 非空，否则拒绝。
"""
