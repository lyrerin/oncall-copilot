"""mcp_servers/store.py —— mock 生产环境数据访问（4 个 server 共用）。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 469 行起（§5.1 共用的 mock 数据层）。
职责：读 mockenv 造出来的日志/指标/变更数据，供 4 个 MCP server 查询。
"""
