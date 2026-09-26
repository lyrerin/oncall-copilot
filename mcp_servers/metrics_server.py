"""mcp_servers/metrics_server.py —— mcp-metrics :8002。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 719 行起（§5.3 指标 MCP Server）。
职责：暴露查指标的工具。
关键点：指标名要走白名单校验（同文档第 736 行），防止 LLM 编造指标名。
"""
