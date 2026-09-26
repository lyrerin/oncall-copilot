"""app/agent/mcp_client.py —— MCP 工具加载与按名索引。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 1411 行起（§7 MCP Client）。
职责：连上 4 个 MCP server，把工具转成 LangChain 可用形式，
      分成只读工具（诊断阶段）与写工具（只在 execute 节点用）两组按名索引。
"""
