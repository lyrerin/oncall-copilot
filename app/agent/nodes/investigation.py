"""app/agent/nodes/investigation.py —— plan / dispatch / collect 三个节点。

状态：骨架，未实现。
实现位置：docs/04-代码指导-Agent层.md 第 658 行起（§ app/agent/nodes/investigation.py）。
职责：
  plan_investigation —— LLM 决定本轮查哪些证据（返回 EvidenceRequest[]）；
  dispatch_evidence  —— 把 N 个请求变成 N 个 Send 并行分支；
  collect_evidence   —— 各分支独立调 MCP 工具、写 evidence 表。
关键点：只清 evidence_requests，不清 evidence（回环时证据要累积）。
"""
